import json
import os
import fitz
import pytest
from scripts.pipelines.textbooks import convert as cv
from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks.paths import resolve_layout


def _make_scan_pdf(tmp_path, n_pages):
    """无文本层 PDF(空白页) → triage 判 A。"""
    doc = fitz.open()
    for _ in range(n_pages):
        doc.new_page()
    p = tmp_path / "scan.pdf"
    doc.save(str(p))
    return str(p)


def _make_text_pdf(tmp_path, n_pages):
    """干净文本层 PDF → triage 判 B。"""
    doc = fitz.open()
    for _ in range(n_pages):
        page = doc.new_page()
        page.insert_text((72, 72), "clean born-digital text " * 20)
    p = tmp_path / "text.pdf"
    doc.save(str(p))
    return str(p)


def _stub_engine(monkeypatch, behavior):
    """behavior: page(1-indexed)->blocks 或抛异常的可调用。桩掉 predict_page,
    并模拟 engine 对非空结果落 res.json 的副作用(空结果不落,复刻真 engine)。"""
    def fake_predict(png_path, work_dir):
        stem = os.path.splitext(os.path.basename(png_path))[0]  # page_0002
        page = int(stem.split("_")[1])
        blocks = behavior(page)                                 # 可能抛异常
        if blocks:                                              # 复刻 engine:非空才落盘
            os.makedirs(work_dir, exist_ok=True)
            with open(os.path.join(work_dir, f"{stem}_res.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"parsing_res_list": blocks}, f)
        return blocks
    monkeypatch.setattr(cv, "predict_page", fake_predict)


def _one_text_block(page):
    return [{"block_order": 0, "block_label": "text",
             "block_content": f"page {page} content"}]


def _layout(out, stem="scan"):
    return resolve_layout(stem, str(out))


def test_convert_full_run_A(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 3)
    _stub_engine(monkeypatch, _one_text_block)
    res = cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    assert res["route"] == "A"
    assert os.path.exists(res["md_path"])
    md = open(res["md_path"], encoding="utf-8").read()
    assert "page 1 content" in md and "page 3 content" in md
    assert res["failed_pages"] == []


def test_convert_force_ocr_processes_clean_text_pdf(tmp_path, monkeypatch):
    pdf = _make_text_pdf(tmp_path, 1)
    _stub_engine(monkeypatch, _one_text_block)

    res = cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100, force_ocr=True)

    assert res["route"] == "F"
    assert os.path.exists(res["md_path"])


def test_scheduled_rest_sleeps_after_active_window():
    times = iter((0.0, 21600.0, 24000.0))
    sleeps = []
    scheduler = cv.ScheduledRest(21600, 2400, clock=lambda: next(times),
                                 sleeper=sleeps.append)

    assert scheduler.rest_if_due() is True
    assert sleeps == [2400]


def test_convert_checks_scheduled_rest_after_each_page(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 2)
    _stub_engine(monkeypatch, _one_text_block)
    checks = []

    class FakeScheduledRest:
        def __init__(self, work_seconds, rest_seconds):
            assert (work_seconds, rest_seconds) == (21600, 2400)

        def rest_if_due(self):
            checks.append(True)
            return False

    monkeypatch.setattr(cv, "ScheduledRest", FakeScheduledRest)

    cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)

    assert checks == [True, True]


def test_convert_disk_bounded(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 4)
    seen_png_counts = []
    layout = _layout(tmp_path / "out")

    def behavior(page):
        # predict 时快照 _work 里 png 数量,应 ≤1
        seen_png_counts.append(len([f for f in os.listdir(layout.work_dir) if f.endswith(".png")]))
        return _one_text_block(page)
    _stub_engine(monkeypatch, behavior)
    cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    assert max(seen_png_counts) <= 1
    # 结束后无残留 png
    assert [f for f in os.listdir(layout.work_dir) if f.endswith(".png")] == []


def test_convert_resume_skips_done(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 3)
    predicted = []
    def behavior(page):
        predicted.append(page)
        return _one_text_block(page)
    _stub_engine(monkeypatch, behavior)
    # 预置第 1、2 页检查点 + 匹配 manifest
    layout = _layout(tmp_path / "out")
    os.makedirs(layout.work_dir, exist_ok=True)
    for pg in (1, 2):
        with open(cp.page_res_path(layout.work_dir, pg), "w", encoding="utf-8") as f:
            json.dump({"parsing_res_list": _one_text_block(pg)}, f)
    m = cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "A")
    cp.save_manifest(layout.work_dir, m)
    cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    assert predicted == [3]                       # 只跑缺失页


def test_convert_bad_page_isolated(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 3)
    def behavior(page):
        if page == 2:
            raise RuntimeError("boom on page 2")
        return _one_text_block(page)
    _stub_engine(monkeypatch, behavior)
    res = cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    md = open(res["md_path"], encoding="utf-8").read()
    assert "page 1 content" in md and "page 3 content" in md
    assert [f["page"] for f in res["failed_pages"]] == [2]
    assert res["failed_pages"][0]["kind"] == "page-exception"


def test_convert_empty_page_checkpointed(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 2)
    calls = []
    def behavior(page):
        calls.append(page)
        return [] if page == 1 else _one_text_block(page)  # 第1页空白
    _stub_engine(monkeypatch, behavior)
    out = str(tmp_path / "out")
    cv.convert_pdf(pdf, out, dpi=100)
    layout = _layout(out)
    assert cp.is_page_done(layout.work_dir, 1) is True            # 空白页也落了检查点
    # 再跑一次:空白页不应被重跑
    calls.clear()
    cv.convert_pdf(pdf, out, dpi=100)
    assert calls == []


def test_convert_fingerprint_mismatch_wipes(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 2)
    _stub_engine(monkeypatch, _one_text_block)
    out = str(tmp_path / "out")
    layout = _layout(out)
    os.makedirs(layout.work_dir, exist_ok=True)
    # 预置一个 DPI 不同的旧 manifest + 一页旧检查点
    with open(cp.page_res_path(layout.work_dir, 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": [{"block_order": 0, "block_label": "text",
                                         "block_content": "STALE 150dpi"}]}, f)
    cp.save_manifest(layout.work_dir, cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 150, "A"))
    cv.convert_pdf(pdf, out, dpi=100)                   # 请求 100 ≠ 记录 150
    md = open(layout.md_path, encoding="utf-8").read()
    assert "STALE" not in md                            # 旧检查点被清空重跑


def test_convert_raster_failure_isolated(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 3)
    _stub_engine(monkeypatch, _one_text_block)
    orig = cv.pdf_page_to_png
    def flaky(pdf_path, page, out_dir, dpi=150):
        if page == 2:
            raise RuntimeError("raster boom p2")
        return orig(pdf_path, page, out_dir, dpi=dpi)
    monkeypatch.setattr(cv, "pdf_page_to_png", flaky)
    res = cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    md = open(res["md_path"], encoding="utf-8").read()
    assert "page 1 content" in md and "page 3 content" in md      # 其它页照常完成
    assert [f["page"] for f in res["failed_pages"]] == [2]


def test_convert_stale_failure_cleared_on_resume(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 2)
    state = {"fail_p1": True}
    def behavior(page):
        if page == 1 and state["fail_p1"]:
            raise RuntimeError("transient p1")
        return _one_text_block(page)
    _stub_engine(monkeypatch, behavior)
    out = str(tmp_path / "out")
    res1 = cv.convert_pdf(pdf, out, dpi=100)
    assert [f["page"] for f in res1["failed_pages"]] == [1]     # 第1趟失败
    state["fail_p1"] = False
    res2 = cv.convert_pdf(pdf, out, dpi=100)                     # 续跑,第1页成功
    assert res2["failed_pages"] == []                           # 陈旧失败被清


def test_convert_route_B_registers(tmp_path, monkeypatch):
    # 有优质文本层 → triage 判 B,登记不转
    doc = fitz.open()
    for _ in range(3):
        pg = doc.new_page()
        pg.insert_text((72, 72), "the quick brown fox jumps over the lazy dog " * 8)
    pdf = tmp_path / "born.pdf"
    doc.save(str(pdf))
    res = cv.convert_pdf(str(pdf), str(tmp_path / "out"))
    assert res["route"] == "B"
    assert res["md_path"] is None


def test_convert_poison_page_skipped_on_startup(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 3)
    _stub_engine(monkeypatch, _one_text_block)
    out = str(tmp_path / "out")
    layout = _layout(out)
    os.makedirs(layout.work_dir, exist_ok=True)
    # 模拟:第 2 页已硬崩进程 MAX-1 次,残留 in_progress(该页无 res.json);
    # 本次 startup resolve_poison 再记一次 → 达阈值判毒页
    m = cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "A")
    m["in_progress"] = 2
    m["attempts_by_page"] = {"2": cp.MAX_HARD_ATTEMPTS - 1}
    cp.save_manifest(layout.work_dir, m)
    res = cv.convert_pdf(pdf, out, dpi=100)
    # 第 2 页被判毒页跳过,进 failed_pages(process-killed),1、3 正常
    kinds = {f["page"]: f["kind"] for f in res["failed_pages"]}
    assert kinds.get(2) == "process-killed"
    md = open(res["md_path"], encoding="utf-8").read()
    assert "page 1 content" in md and "page 3 content" in md


def test_convert_in_progress_cleared_after_success(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 2)
    _stub_engine(monkeypatch, _one_text_block)
    out = str(tmp_path / "out")
    cv.convert_pdf(pdf, out, dpi=100)
    layout = _layout(out)
    m = cp.load_manifest(layout.work_dir)
    assert m["in_progress"] is None      # 正常跑完不残留 in_progress


def test_convert_failed_pages_deduped_across_runs(tmp_path, monkeypatch):
    # 同页跨多次运行反复失败(page-exception)不应在 failed_pages 累积重复条目
    pdf = _make_scan_pdf(tmp_path, 2)
    def behavior(page):
        if page == 1:
            raise RuntimeError("always fails p1")
        return _one_text_block(page)
    _stub_engine(monkeypatch, behavior)
    out = str(tmp_path / "out")
    cv.convert_pdf(pdf, out, dpi=100)                 # run1: p1 失败
    res = cv.convert_pdf(pdf, out, dpi=100)           # run2: p1 再次失败
    pages = [f["page"] for f in res["failed_pages"]]
    assert pages.count(1) == 1                        # 只留一条,不累积


def test_convert_poison_not_reset_by_earlier_failing_page(tmp_path, monkeypatch):
    # 更靠前的持续失败页不重置毒页计数;毒页之后的页仍被转换
    pdf = _make_scan_pdf(tmp_path, 3)
    def behavior(page):
        if page == 1:
            raise RuntimeError("p1 always fails")
        return _one_text_block(page)
    _stub_engine(monkeypatch, behavior)
    out = str(tmp_path / "out")
    layout = _layout(out)
    os.makedirs(layout.work_dir, exist_ok=True)
    m = cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "A")
    m["in_progress"] = 2
    m["attempts_by_page"] = {"2": cp.MAX_HARD_ATTEMPTS - 1}   # 差一次到阈值
    cp.save_manifest(layout.work_dir, m)
    res = cv.convert_pdf(pdf, out, dpi=100)                   # startup: page2 达阈值→毒页跳过
    kinds = {f["page"]: f["kind"] for f in res["failed_pages"]}
    assert kinds.get(2) == "process-killed"
    assert kinds.get(1) == "page-exception"
    md = open(res["md_path"], encoding="utf-8").read()
    assert "page 3 content" in md                            # 毒页之后仍转换


def test_convert_writes_selfcheck_json_by_default(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 2)
    _stub_engine(monkeypatch, _one_text_block)
    res = cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    layout = _layout(tmp_path / "out")
    assert os.path.exists(layout.selfcheck_path)
    with open(layout.selfcheck_path, encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk == res["selfcheck"]


def test_convert_no_selfcheck_json_when_disabled(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 2)
    _stub_engine(monkeypatch, _one_text_block)
    cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100, write_selfcheck=False)
    layout = _layout(tmp_path / "out")
    assert not os.path.exists(layout.selfcheck_path)


def test_convert_cli_no_selfcheck_json_forwards_flag(monkeypatch):
    captured = {}
    def fake_convert_pdf(pdf_path, deliverables_dir, work_dir=None, dpi=150,
                         write_selfcheck=True, **_kwargs):
        captured["write_selfcheck"] = write_selfcheck
        captured["work_dir"] = work_dir
        return {"route": "A", "md_path": "x.md",
                "selfcheck": {"total": 0, "in_md": 0, "missing": []}, "failed_pages": []}
    monkeypatch.setattr(cv, "convert_pdf", fake_convert_pdf)
    monkeypatch.setattr("sys.argv", ["convert.py", "--src", "x.pdf", "--no-selfcheck-json"])
    cv.main()
    assert captured["write_selfcheck"] is False


def test_convert_cli_forwards_force_ocr_and_rest_schedule(monkeypatch):
    captured = {}

    def fake_convert_pdf(*_args, **kwargs):
        captured.update(kwargs)
        return {"route": "F", "md_path": "x.md",
                "selfcheck": {"total": 0, "in_md": 0, "missing": []},
                "failed_pages": []}

    monkeypatch.setattr(cv, "convert_pdf", fake_convert_pdf)
    monkeypatch.setattr("sys.argv", [
        "convert.py", "--src", "x.pdf", "--force-ocr",
        "--work-hours", "6", "--rest-minutes", "40",
    ])

    cv.main()

    assert captured["force_ocr"] is True
    assert captured["work_seconds"] == 21600
    assert captured["rest_seconds"] == 2400


def test_convert_cli_forwards_born_digital_mode(monkeypatch):
    captured = {}

    def fake_convert_pdf(*_args, **kwargs):
        captured.update(kwargs)
        return {"route": "B", "md_path": None, "selfcheck": None, "failed_pages": []}

    monkeypatch.setattr(cv, "convert_pdf", fake_convert_pdf)
    monkeypatch.setattr("sys.argv", [
        "convert.py", "--src", "x.pdf", "--born-digital-mode", "hybrid",
    ])

    cv.main()

    assert captured["born_digital_mode"] == "hybrid"


def test_convert_cli_born_digital_mode_defaults_to_defer(monkeypatch):
    captured = {}

    def fake_convert_pdf(*_args, **kwargs):
        captured.update(kwargs)
        return {"route": "A", "md_path": "x.md",
                "selfcheck": {"total": 0, "in_md": 0, "missing": []}, "failed_pages": []}

    monkeypatch.setattr(cv, "convert_pdf", fake_convert_pdf)
    monkeypatch.setattr("sys.argv", ["convert.py", "--src", "x.pdf"])

    cv.main()

    assert captured["born_digital_mode"] == "defer"


def test_convert_cli_rejects_invalid_born_digital_mode(monkeypatch, capsys):
    monkeypatch.setattr("sys.argv", [
        "convert.py", "--src", "x.pdf", "--born-digital-mode", "bogus",
    ])
    with pytest.raises(SystemExit) as exc:
        cv.main()
    assert exc.value.code != 0


def _one_image_block(page):
    return [{"block_order": None, "block_label": "image", "block_id": 1,
             "block_content": "", "block_bbox": [5, 5, 15, 15]}]


def test_convert_crops_images_before_png_deleted(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 1)
    _stub_engine(monkeypatch, _one_image_block)
    out = str(tmp_path / "out")
    res = cv.convert_pdf(pdf, out, dpi=100)
    layout = _layout(out)
    assert os.path.exists(os.path.join(layout.assets_dir, "page_0001_block_1.png"))
    md = open(res["md_path"], encoding="utf-8").read()
    assert "scan.assets/page_0001_block_1.png" in md


def test_convert_clears_assets_on_fingerprint_mismatch(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 1)
    _stub_engine(monkeypatch, _one_image_block)
    out = str(tmp_path / "out")
    cv.convert_pdf(pdf, out, dpi=100)
    layout = _layout(out)
    assert os.path.exists(os.path.join(layout.assets_dir, "page_0001_block_1.png"))
    # 换 DPI 触发指纹失配 → 全新跑,旧资产应被清空(重新裁出的文件名相同,
    # 用一个哨兵文件验证目录整体被清过,而不仅是被覆盖)
    sentinel = os.path.join(layout.assets_dir, "STALE_SENTINEL.png")
    open(sentinel, "w").close()
    cv.convert_pdf(pdf, out, dpi=120)
    assert not os.path.exists(sentinel)


def test_convert_backfills_assets_for_pre_existing_checkpoint(tmp_path, monkeypatch):
    # 模拟"图片功能上线前跑完的检查点":res.json 里有 image 块,但 assets 目录不存在
    pdf = _make_scan_pdf(tmp_path, 1)
    _stub_engine(monkeypatch, _one_image_block)
    out = str(tmp_path / "out")
    layout = _layout(out)
    os.makedirs(layout.work_dir, exist_ok=True)
    with open(cp.page_res_path(layout.work_dir, 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": _one_image_block(1)}, f)
    cp.save_manifest(layout.work_dir, cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "A"))
    res = cv.convert_pdf(pdf, out, dpi=100)          # 该页已"完成",不会重新进 OCR 循环
    assert os.path.exists(os.path.join(layout.assets_dir, "page_0001_block_1.png"))  # 补裁生效
    assert res["selfcheck"]["missing_assets"] == []


def test_convert_missing_assets_reported_when_backfill_impossible(tmp_path, monkeypatch):
    # 补裁时重新栅格化该页失败(如源文件损坏/权限问题等极端场景)→ 补裁失败,
    # 但不应崩溃,应如实反映在 missing_assets 里。
    # 源 PDF 保持完好(triage 正常通过),page 1 预置为"已完成"检查点,
    # 使其不进入本次 OCR 循环(裁图钩子不会为它触发)、转而在 assemble() 的
    # 补裁分支尝试重新栅格化 —— 这里桩掉 pdf_page_to_png 让该次调用失败。
    pdf = _make_scan_pdf(tmp_path, 1)
    _stub_engine(monkeypatch, _one_image_block)
    out = str(tmp_path / "out")
    layout = _layout(out)
    os.makedirs(layout.work_dir, exist_ok=True)
    with open(cp.page_res_path(layout.work_dir, 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": _one_image_block(1)}, f)
    cp.save_manifest(layout.work_dir, cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "A"))

    def flaky(pdf_path, page, out_dir, dpi=150):
        raise RuntimeError("backfill raster boom")
    monkeypatch.setattr(cv, "pdf_page_to_png", flaky)

    res = cv.convert_pdf(pdf, out, dpi=100)
    assert res["selfcheck"]["missing_assets"] == ["page_0001_block_1.png"]


def _one_ordered_image_block(page):
    # block_order 不为 None 的 image 块:超出 reconstruct.py 的可视块处理范围
    # (只有 block_order is None 的可视块才会渲染成 md 图片链接),不应被裁图/计入
    # expected assets——这是 Finding 1 的回归防护:防止"裁了但没链"式静默丢失。
    return [{"block_order": 1, "block_label": "image", "block_id": 1,
             "block_content": "", "block_bbox": [5, 5, 15, 15]}]


def test_convert_ordered_image_block_not_cropped_or_missing(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 1)
    _stub_engine(monkeypatch, _one_ordered_image_block)
    out = str(tmp_path / "out")
    res = cv.convert_pdf(pdf, out, dpi=100)
    layout = _layout(out)
    assert not os.path.exists(os.path.join(layout.assets_dir, "page_0001_block_1.png"))
    assert res["selfcheck"]["missing_assets"] == []
    # 显式断言目录本身不存在或为空,防止 scoping 回归时测试仍"意外"通过
    assert not os.path.isdir(layout.assets_dir) or os.listdir(layout.assets_dir) == []


def test_convert_selfcheck_has_four_new_fields(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 2)
    _stub_engine(monkeypatch, _one_text_block)
    res = cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    for key in ("unhandled_labels", "visual_warnings", "column_layout_suspected", "missing_assets"):
        assert key in res["selfcheck"]
    assert res["selfcheck"]["unhandled_labels"] == {}
    assert res["selfcheck"]["visual_warnings"] == []
    assert res["selfcheck"]["column_layout_suspected"] == []
    assert res["selfcheck"]["missing_assets"] == []


def _one_display_formula_block(page):
    return [{"block_order": 0, "block_label": "display_formula", "block_id": 5,
             "block_content": "$$ bad $$"}]


def test_convert_applies_corrections_json(tmp_path, monkeypatch):
    from scripts.pipelines.textbooks.vision_repair import content_fingerprint
    pdf = _make_scan_pdf(tmp_path, 1)
    _stub_engine(monkeypatch, _one_display_formula_block)
    out = str(tmp_path / "out")
    layout = _layout(out)
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    corrections_payload = {"stem": "scan", "corrections": [
        {"page": 1, "block_id": 5, "corrected_latex": "$$ good $$",
         "content_fingerprint": content_fingerprint("$$ bad $$"), "status": "accepted"}]}
    with open(layout.corrections_path, "w", encoding="utf-8") as f:
        json.dump(corrections_payload, f)

    res = cv.convert_pdf(pdf, out, dpi=100)

    md = open(res["md_path"], encoding="utf-8").read()
    assert "good" in md
    assert "bad" not in md


def test_convert_skips_correction_on_fingerprint_mismatch(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 1)
    _stub_engine(monkeypatch, _one_display_formula_block)
    out = str(tmp_path / "out")
    layout = _layout(out)
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    corrections_payload = {"stem": "scan", "corrections": [
        {"page": 1, "block_id": 5, "corrected_latex": "$$ good $$",
         "content_fingerprint": "stale-hash-does-not-match", "status": "accepted"}]}
    with open(layout.corrections_path, "w", encoding="utf-8") as f:
        json.dump(corrections_payload, f)

    res = cv.convert_pdf(pdf, out, dpi=100)

    md = open(res["md_path"], encoding="utf-8").read()
    assert "bad" in md
    assert "good" not in md


def test_convert_does_not_apply_pending_correction(tmp_path, monkeypatch):
    from scripts.pipelines.textbooks.vision_repair import content_fingerprint
    pdf = _make_scan_pdf(tmp_path, 1)
    _stub_engine(monkeypatch, _one_display_formula_block)
    out = str(tmp_path / "out")
    layout = _layout(out)
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    corrections_payload = {"stem": "scan", "corrections": [
        {"page": 1, "block_id": 5, "corrected_latex": "$$ good $$",
         "content_fingerprint": content_fingerprint("$$ bad $$"), "status": "pending"}]}
    with open(layout.corrections_path, "w", encoding="utf-8") as f:
        json.dump(corrections_payload, f)

    res = cv.convert_pdf(pdf, out, dpi=100)

    md = open(res["md_path"], encoding="utf-8").read()
    assert "bad" in md                             # 人工确认门:待审不生效
    assert "good" not in md


def test_reassemble_md_applies_accepted(tmp_path):
    from scripts.pipelines.textbooks.vision_repair import content_fingerprint
    layout = _layout(tmp_path)
    os.makedirs(layout.work_dir, exist_ok=True)
    original = "$$ bad $$"
    with open(cp.page_res_path(layout.work_dir, 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": [
            {"block_order": 0, "block_label": "display_formula", "block_id": 5,
             "block_content": original}]}, f)
    cp.save_manifest(layout.work_dir, cp.new_manifest(
        "x.pdf", {"page_count": 1, "size_bytes": 0}, 100, "A"))
    corrections = {"stem": "scan", "corrections": [
        {"page": 1, "block_id": 5, "corrected_latex": "$$ good $$",
         "content_fingerprint": content_fingerprint(original), "status": "accepted"}]}
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    with open(layout.corrections_path, "w", encoding="utf-8") as f:
        json.dump(corrections, f)

    md_path = cv.reassemble_md(layout, pdf_path=None, dpi=100)

    assert md_path == layout.md_path
    md = open(md_path, encoding="utf-8").read()
    assert "good" in md and "bad" not in md


def test_reassemble_md_does_not_apply_pending(tmp_path):
    from scripts.pipelines.textbooks.vision_repair import content_fingerprint
    layout = _layout(tmp_path)
    os.makedirs(layout.work_dir, exist_ok=True)
    original = "$$ bad $$"
    with open(cp.page_res_path(layout.work_dir, 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": [
            {"block_order": 0, "block_label": "display_formula", "block_id": 5,
             "block_content": original}]}, f)
    cp.save_manifest(layout.work_dir, cp.new_manifest(
        "x.pdf", {"page_count": 1, "size_bytes": 0}, 100, "A"))
    corrections = {"stem": "scan", "corrections": [
        {"page": 1, "block_id": 5, "corrected_latex": "$$ good $$",
         "content_fingerprint": content_fingerprint(original), "status": "pending"}]}
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    with open(layout.corrections_path, "w", encoding="utf-8") as f:
        json.dump(corrections, f)

    md_path = cv.reassemble_md(layout, pdf_path=None, dpi=100)

    md = open(md_path, encoding="utf-8").read()
    assert "bad" in md and "good" not in md


def test_reassemble_md_idempotent(tmp_path):
    layout = _layout(tmp_path)
    os.makedirs(layout.work_dir, exist_ok=True)
    with open(cp.page_res_path(layout.work_dir, 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": [
            {"block_order": 0, "block_label": "text",
             "block_content": "hello page 1"}]}, f)
    cp.save_manifest(layout.work_dir, cp.new_manifest(
        "x.pdf", {"page_count": 1, "size_bytes": 0}, 100, "A"))

    p1 = cv.reassemble_md(layout, pdf_path=None, dpi=100)
    first = open(p1, encoding="utf-8").read()
    p2 = cv.reassemble_md(layout, pdf_path=None, dpi=100)
    second = open(p2, encoding="utf-8").read()

    assert first == second


def test_reassemble_md_returns_none_when_no_manifest(tmp_path):
    layout = _layout(tmp_path)
    os.makedirs(layout.doc_deliverable_dir, exist_ok=True)   # 无 _work / 无 manifest
    assert cv.reassemble_md(layout, pdf_path=None, dpi=100) is None
    assert not os.path.exists(layout.md_path)


# ===========================================================================
# Task 9:路线 B 编排(defer/ocr/hybrid)、采信、审计落盘、断点恢复、marker 清理
# ---------------------------------------------------------------------------
# 引擎全部 stub,零 GPU。采信测试用"源正确/OCR 一字之差(brown vs browm)"的
# 构造,采信生效时 md 含 brown、不含 browm;回退时反之——干净可判。
# ===========================================================================

_SRC_SENTENCE = "the quick brown fox jumps over the lazy dog"


def _prose_text(n=4):
    return "\n".join([_SRC_SENTENCE] * n) + "\n"


def _ocr_prose_text(n=4):
    return "\n".join([_SRC_SENTENCE.replace("brown", "browm")] * n) + "\n"


def _make_prose_pdf(tmp_path, n_pages):
    """干净文本层 PDF,词落在页内文本框(triage 判 B;采信几何可标定)。"""
    doc = fitz.open()
    for _ in range(n_pages):
        page = doc.new_page()
        page.insert_textbox(fitz.Rect(60, 60, 550, 750), _prose_text(), fontsize=11)
    p = tmp_path / "born.pdf"
    doc.save(str(p))
    return str(p)


def _adopt_ocr_block(page):
    # 整页覆盖的正文块:width/height 与 block_bbox 同为 1000,归一化后覆盖全页,
    # 全部源 words 归属本块;block_content 是 OCR 的"一字之差"文本。
    return [{"block_order": 0, "block_label": "text", "block_id": 0,
             "block_content": _ocr_prose_text(), "block_bbox": [0, 0, 1000, 1000]}]


def _adopt_res_payload(page):
    return {"parsing_res_list": _adopt_ocr_block(page), "width": 1000, "height": 1000}


def _stub_engine_adopt(monkeypatch, calls=None):
    def fake_predict(png_path, work_dir):
        stem = os.path.splitext(os.path.basename(png_path))[0]
        page = int(stem.split("_")[1])
        if calls is not None:
            calls.append(page)
        blocks = _adopt_ocr_block(page)
        os.makedirs(work_dir, exist_ok=True)
        with open(os.path.join(work_dir, f"{stem}_res.json"), "w", encoding="utf-8") as f:
            json.dump(_adopt_res_payload(page), f)
        return blocks
    monkeypatch.setattr(cv, "predict_page", fake_predict)


def _write_adopt_checkpoint(work_dir, page):
    os.makedirs(work_dir, exist_ok=True)
    with open(cp.page_res_path(work_dir, page), "w", encoding="utf-8") as f:
        json.dump(_adopt_res_payload(page), f)


def _inject_thresholds(monkeypatch):
    from scripts.pipelines.textbooks.prose_adoption import AdoptionThresholds
    from scripts.pipelines.textbooks.source_audit import AuditThresholds
    monkeypatch.setattr(cv, "ROUTE_B_ADOPTION_THRESHOLDS", AdoptionThresholds(
        adoption_min_char_ratio=0.5, adoption_max_char_ratio=2.0, adoption_max_ned=0.3))
    monkeypatch.setattr(cv, "ROUTE_B_AUDIT_THRESHOLDS", AuditThresholds(
        minimum_reliable_chars=1, maximum_bad_char_ratio=0.5, maximum_block_ned=0.5,
        minimum_char_recall=0.5, minimum_token_recall=0.5, minimum_numeric_token_recall=0.5,
        maximum_addition_ratio=0.5, maximum_repetition_score=0.9,
        minimum_single_column_sequence_ratio=0.3))


def test_route_b_defer_keeps_legacy_behavior(tmp_path, monkeypatch):
    pdf = _make_prose_pdf(tmp_path, 2)
    calls = []
    _stub_engine_adopt(monkeypatch, calls)
    out = str(tmp_path / "out")
    res = cv.convert_pdf(pdf, out, dpi=100, born_digital_mode="defer")
    assert res["route"] == "B"
    assert res["md_path"] is None                       # 登记不转(逐字不变)
    assert calls == []                                  # 未跑 OCR
    marker = os.path.join(out, "_deferred_born_digital", "born.txt")
    assert os.path.exists(marker)                       # 登记标记已建
    layout = resolve_layout("born", out)
    assert not os.path.exists(layout.source_audit_path)  # defer 不产审计


def test_route_b_ocr_mode_never_adopts(tmp_path, monkeypatch):
    _inject_thresholds(monkeypatch)
    pdf = _make_prose_pdf(tmp_path, 2)
    _stub_engine_adopt(monkeypatch)
    out = str(tmp_path / "out")
    res = cv.convert_pdf(pdf, out, dpi=100, born_digital_mode="ocr")
    assert res["route"] == "B"
    assert res["born_digital_mode"] == "ocr"
    md = open(res["md_path"], encoding="utf-8").read()
    assert "browm" in md and "brown" not in md          # 从不把源文本写进 md
    report = res["source_audit"]
    assert report is not None
    assert report["born_digital_mode"] == "ocr"
    assert report["adoption_source"] == "dry_run"       # 只审计推演,绝不 apply


def test_route_b_hybrid_adopts_healthy_prose_blocks(tmp_path, monkeypatch):
    _inject_thresholds(monkeypatch)
    pdf = _make_prose_pdf(tmp_path, 2)
    _stub_engine_adopt(monkeypatch)
    out = str(tmp_path / "out")
    res = cv.convert_pdf(pdf, out, dpi=100, born_digital_mode="hybrid")
    assert res["route"] == "B"
    assert res["born_digital_mode"] == "hybrid"
    assert res["adoption_error"] is False
    md = open(res["md_path"], encoding="utf-8").read()
    assert "brown" in md and "browm" not in md          # 采信了源文本层
    report = res["source_audit"]
    assert report["born_digital_mode"] == "hybrid"
    assert report["adoption_source"] == "recorded"
    assert report["summary"]["adoption"]["adopted"] >= 1


def test_route_b_hybrid_falls_back_whole_book_on_adoption_error(tmp_path, monkeypatch):
    _inject_thresholds(monkeypatch)
    pdf = _make_prose_pdf(tmp_path, 2)
    _stub_engine_adopt(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("adoption boom")
    monkeypatch.setattr(cv, "adopt_prose_blocks", boom)

    out = str(tmp_path / "out")
    res = cv.convert_pdf(pdf, out, dpi=100, born_digital_mode="hybrid")  # 绝不抛出
    md = open(res["md_path"], encoding="utf-8").read()
    assert "browm" in md and "brown" not in md          # 整本回退等价 OCR 内容
    assert res["adoption_error"] is True
    report = res["source_audit"]
    assert report["summary"]["status"] == "SUSPECT"
    assert any(iss["code"] == "adoption_error" for iss in report.get("issues", []))
    layout = resolve_layout("born", out)
    assert cp.is_page_done(layout.work_dir, 1)          # 检查点未被删
    assert cp.is_page_done(layout.work_dir, 2)


def test_audit_failure_keeps_checkpoints_and_returns_suspect(tmp_path, monkeypatch):
    # hybrid 中纯审计步骤(audit_document)异常:采信已成功 → 保留已采信 md、
    # 记 audit_error、SUSPECT、检查点完好;不回退内容、adoption_error=False。
    _inject_thresholds(monkeypatch)
    pdf = _make_prose_pdf(tmp_path, 2)
    _stub_engine_adopt(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("audit boom")
    monkeypatch.setattr(cv, "audit_document", boom)

    out = str(tmp_path / "out")
    res = cv.convert_pdf(pdf, out, dpi=100, born_digital_mode="hybrid")
    assert res["adoption_error"] is False               # 采信本身成功
    report = res["source_audit"]
    assert report["summary"]["status"] == "SUSPECT"
    assert any(iss["code"] == "audit_error" for iss in report.get("issues", []))
    layout = resolve_layout("born", out)
    assert cp.is_page_done(layout.work_dir, 1)          # 审计崩溃不删检查点
    assert cp.is_page_done(layout.work_dir, 2)
    md = open(res["md_path"], encoding="utf-8").read()
    assert "brown" in md and "browm" not in md          # 保留已采信 md,不回退


def test_resume_rebuilds_missing_audit_without_reocr(tmp_path, monkeypatch):
    _inject_thresholds(monkeypatch)
    pdf = _make_prose_pdf(tmp_path, 2)
    calls = []
    _stub_engine_adopt(monkeypatch, calls)
    out = str(tmp_path / "out")
    layout = resolve_layout("born", out)
    for pg in (1, 2):
        _write_adopt_checkpoint(layout.work_dir, pg)
    cp.save_manifest(layout.work_dir, cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "B"))
    assert not os.path.exists(layout.source_audit_path)

    res = cv.convert_pdf(pdf, out, dpi=100, born_digital_mode="ocr")

    assert calls == []                                  # 引擎 stub 未被调用
    assert os.path.exists(layout.source_audit_path)     # 审计缺失 → 重建
    assert res["source_audit"] is not None


def test_resume_reruns_adoption_and_reconstruct_when_stale(tmp_path, monkeypatch):
    _inject_thresholds(monkeypatch)
    pdf = _make_prose_pdf(tmp_path, 2)
    calls = []
    _stub_engine_adopt(monkeypatch, calls)
    out = str(tmp_path / "out")
    layout = resolve_layout("born", out)
    for pg in (1, 2):
        _write_adopt_checkpoint(layout.work_dir, pg)
    cp.save_manifest(layout.work_dir, cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "B"))
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    stale = {"schema_version": cv.AUDIT_SCHEMA_VERSION, "stem": "born",
             "pdf_fingerprint": {"size_bytes": 1, "page_count": 99},
             "ocr_fingerprint": {"dpi": 100, "page_count": 99},
             "summary": {"status": "OK"}}
    with open(layout.source_audit_path, "w", encoding="utf-8") as f:
        json.dump(stale, f)

    res = cv.convert_pdf(pdf, out, dpi=100, born_digital_mode="hybrid")

    assert calls == []                                  # 不重跑 OCR、不加载引擎
    md = open(res["md_path"], encoding="utf-8").read()
    assert "brown" in md                                # 采信+重组照跑
    report = res["source_audit"]
    assert report["pdf_fingerprint"]["page_count"] == 2  # 审计按真实指纹重算
    assert report["adoption_source"] == "recorded"


def test_stale_audit_fingerprint_recomputed(tmp_path, monkeypatch):
    _inject_thresholds(monkeypatch)
    pdf = _make_prose_pdf(tmp_path, 2)
    _stub_engine_adopt(monkeypatch)
    out = str(tmp_path / "out")
    res1 = cv.convert_pdf(pdf, out, dpi=100, born_digital_mode="ocr")
    assert res1["source_audit"]["pdf_fingerprint"]["page_count"] == 2
    layout = resolve_layout("born", out)
    with open(layout.source_audit_path, encoding="utf-8") as f:
        rep = json.load(f)
    rep["pdf_fingerprint"]["page_count"] = 999          # 污染指纹 → 过期
    with open(layout.source_audit_path, "w", encoding="utf-8") as f:
        json.dump(rep, f)

    res2 = cv.convert_pdf(pdf, out, dpi=100, born_digital_mode="ocr")

    assert res2["source_audit"]["pdf_fingerprint"]["page_count"] == 2  # 已重算


def test_deferred_marker_removed_only_after_success(tmp_path, monkeypatch):
    _inject_thresholds(monkeypatch)
    pdf = _make_prose_pdf(tmp_path, 2)
    out = str(tmp_path / "out")
    marker = os.path.join(out, "_deferred_born_digital", "born.txt")

    cv.convert_pdf(pdf, out, dpi=100, born_digital_mode="defer")     # 1) 登记
    assert os.path.exists(marker)

    def all_fail(png_path, work_dir):                                # 2) 全页崩(giveup)
        raise RuntimeError("engine down")
    monkeypatch.setattr(cv, "predict_page", all_fail)
    cv.convert_pdf(pdf, out, dpi=100, born_digital_mode="ocr")
    assert os.path.exists(marker)                                    # giveup 不算成功

    _stub_engine_adopt(monkeypatch)                                  # 3) 成功转换
    cv.convert_pdf(pdf, out, dpi=100, born_digital_mode="ocr")
    assert not os.path.exists(marker)                                # 成功后删除


def test_route_c_does_not_treat_bad_source_as_ground_truth(tmp_path, monkeypatch):
    # 真实 C 路(低质文本层)PDF 无法经 insert_text 构造——PyMuPDF 把插入的
    # PUA/U+FFFD round-trip 回读成合法间隔号 ·(见 test_triage 的实验记录),故与
    # triage 的 C 路单测一致,这里直接注入 triage→"C" 只验 convert 的 C 路行为。
    pdf = _make_prose_pdf(tmp_path, 2)                   # 源层含 brown/quick 等正文
    monkeypatch.setattr(cv, "triage", lambda _p: "C")
    _stub_engine(monkeypatch, _one_text_block)          # OCR 出干净正文
    out = str(tmp_path / "out")
    res = cv.convert_pdf(pdf, out, dpi=100, born_digital_mode="hybrid")
    assert res["route"] == "C"                          # C 路不受 hybrid 影响
    md = open(res["md_path"], encoding="utf-8").read()
    assert "page 1 content" in md                       # md 用 OCR
    assert "brown" not in md and "quick" not in md      # 坏源层没被当真相写进 md
    report = res["source_audit"]
    assert report is not None
    assert report["pages"][0].get("source_health")      # 保存了页级 source health
    assert report["summary"]["adoption"]["adopted"] == 0  # 一块都没采信


def test_route_b_hybrid_resume_byte_identical(tmp_path, monkeypatch):
    _inject_thresholds(monkeypatch)
    pdf = _make_prose_pdf(tmp_path, 3)
    _stub_engine_adopt(monkeypatch)
    out1 = str(tmp_path / "out1")                        # 一次跑完
    r1 = cv.convert_pdf(pdf, out1, dpi=100, born_digital_mode="hybrid")
    md1 = open(r1["md_path"], encoding="utf-8").read()

    out2 = str(tmp_path / "out2")                        # 跑一半再 resume
    layout2 = resolve_layout("born", out2)
    _write_adopt_checkpoint(layout2.work_dir, 1)         # 仅第 1 页检查点
    cp.save_manifest(layout2.work_dir, cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "B"))
    r2 = cv.convert_pdf(pdf, out2, dpi=100, born_digital_mode="hybrid")
    md2 = open(r2["md_path"], encoding="utf-8").read()

    assert md1 == md2                                    # 逐字节一致
    assert "brown" in md2                                # 采信生效


def test_audit_report_route_field_reflects_true_route_c_and_f(tmp_path, monkeypatch):
    # Important 1:audit_document 内部硬编码 route="B";convert 落盘前覆写为真实路由。
    _inject_thresholds(monkeypatch)
    _stub_engine(monkeypatch, _one_text_block)
    # F 路(force_ocr 把 detected B 降级为 F)——真实 triage
    pdf_f = _make_prose_pdf(tmp_path, 1)
    out_f = str(tmp_path / "outf")
    res_f = cv.convert_pdf(pdf_f, out_f, dpi=100, force_ocr=True)
    assert res_f["route"] == "F"
    rep_f = json.load(open(resolve_layout("born", out_f).source_audit_path, encoding="utf-8"))
    assert rep_f["route"] == "F"                         # 落盘报告 route 真实
    assert res_f["source_audit"]["route"] == "F"
    # C 路(注入 triage→C)
    monkeypatch.setattr(cv, "triage", lambda _p: "C")
    pdf_c = _make_prose_pdf(tmp_path, 1)
    out_c = str(tmp_path / "outc")
    res_c = cv.convert_pdf(pdf_c, out_c, dpi=100)
    assert res_c["route"] == "C"
    rep_c = json.load(open(resolve_layout("born", out_c).source_audit_path, encoding="utf-8"))
    assert rep_c["route"] == "C"                         # 落盘报告 route 真实
    assert res_c["source_audit"]["route"] == "C"


def test_ocr_audit_failure_writes_suspect_and_removes_marker(tmp_path, monkeypatch):
    # Important 2:任何路的纯审计异常 → SUSPECT(audit_error)落盘 + 文档计为完成 +
    # marker 正常删除(绝不因审计崩溃留 marker 造成每轮批处理活锁重跑)+ 检查点完好。
    _inject_thresholds(monkeypatch)
    pdf = _make_prose_pdf(tmp_path, 2)
    out = str(tmp_path / "out")
    marker = os.path.join(out, "_deferred_born_digital", "born.txt")
    cv.convert_pdf(pdf, out, dpi=100, born_digital_mode="defer")     # 建 marker
    assert os.path.exists(marker)

    _stub_engine_adopt(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("audit boom")
    monkeypatch.setattr(cv, "audit_document", boom)

    res = cv.convert_pdf(pdf, out, dpi=100, born_digital_mode="ocr")
    report = res["source_audit"]
    assert report is not None and report["summary"]["status"] == "SUSPECT"
    assert any(iss["code"] == "audit_error" for iss in report.get("issues", []))
    assert not os.path.exists(marker)                    # 审计崩溃仍算完成 → marker 删
    layout = resolve_layout("born", out)
    assert cp.is_page_done(layout.work_dir, 1) and cp.is_page_done(layout.work_dir, 2)


def test_error_report_not_reused_as_fresh(tmp_path, monkeypatch):
    # Minor 3:指纹一致但含 adoption_error/audit_error 的 SUSPECT 报告不得当 fresh 复用,
    # 下次 resume 必须重算(否则采信/审计已能成功仍顶着陈旧错误报告)。
    _inject_thresholds(monkeypatch)
    pdf = _make_prose_pdf(tmp_path, 2)
    _stub_engine_adopt(monkeypatch)
    out = str(tmp_path / "out")
    layout = resolve_layout("born", out)
    for pg in (1, 2):
        _write_adopt_checkpoint(layout.work_dir, pg)
    cp.save_manifest(layout.work_dir, cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "B"))
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    fp = cp.pdf_fingerprint(pdf)
    stale_err = {
        "schema_version": cv.AUDIT_SCHEMA_VERSION, "stem": "born", "route": "B",
        "pdf_fingerprint": {"size_bytes": fp["size_bytes"], "page_count": fp["page_count"]},
        "ocr_fingerprint": {"dpi": 100, "page_count": fp["page_count"]},
        "summary": {"status": "SUSPECT", "issue_counts": {"audit_error": 1}},
        "issues": [{"code": "audit_error", "block_id": None, "detail": "上轮审计崩溃残留"}],
    }
    with open(layout.source_audit_path, "w", encoding="utf-8") as f:
        json.dump(stale_err, f)

    res = cv.convert_pdf(pdf, out, dpi=100, born_digital_mode="ocr")

    report = res["source_audit"]
    assert report["summary"]["issue_counts"].get("audit_error") is None  # 已重算,非复用
    assert report["adoption_source"] == "dry_run"


def test_audit_write_failure_does_not_escape(tmp_path, monkeypatch):
    # Minor 4:错误/正常路径的 write_audit_report 写盘失败(OSError)记日志返回 None,
    # 绝不逃逸破坏批处理隔离;md 照常产出。
    _inject_thresholds(monkeypatch)
    pdf = _make_prose_pdf(tmp_path, 2)
    _stub_engine_adopt(monkeypatch)

    def boom(*_a, **_k):
        raise OSError("disk full")
    monkeypatch.setattr(cv, "write_audit_report", boom)

    out = str(tmp_path / "out")
    res = cv.convert_pdf(pdf, out, dpi=100, born_digital_mode="ocr")   # 不抛
    assert res["source_audit"] is None                   # 写盘失败 → None
    assert os.path.exists(res["md_path"])                 # md 照常产出
