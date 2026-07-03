import json
import os
import fitz
import pytest
from scripts.pipelines.textbooks import convert as cv
from scripts.pipelines.textbooks import checkpoint as cp


def _make_scan_pdf(tmp_path, n_pages):
    """无文本层 PDF(空白页) → triage 判 A。"""
    doc = fitz.open()
    for _ in range(n_pages):
        doc.new_page()
    p = tmp_path / "scan.pdf"
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


def test_convert_full_run_A(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 3)
    _stub_engine(monkeypatch, _one_text_block)
    res = cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    assert res["route"] == "A"
    assert os.path.exists(res["md_path"])
    md = open(res["md_path"], encoding="utf-8").read()
    assert "page 1 content" in md and "page 3 content" in md
    assert res["failed_pages"] == []


def test_convert_disk_bounded(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 4)
    seen_png_counts = []

    def behavior(page):
        # predict 时快照 _work 里 png 数量,应 ≤1
        work = os.path.join(str(tmp_path / "out"), "scan", "_work")
        seen_png_counts.append(len([f for f in os.listdir(work) if f.endswith(".png")]))
        return _one_text_block(page)
    _stub_engine(monkeypatch, behavior)
    cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    assert max(seen_png_counts) <= 1
    # 结束后无残留 png
    work = os.path.join(str(tmp_path / "out"), "scan", "_work")
    assert [f for f in os.listdir(work) if f.endswith(".png")] == []


def test_convert_resume_skips_done(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 3)
    predicted = []
    def behavior(page):
        predicted.append(page)
        return _one_text_block(page)
    _stub_engine(monkeypatch, behavior)
    # 预置第 1、2 页检查点 + 匹配 manifest
    work = os.path.join(str(tmp_path / "out"), "scan", "_work")
    os.makedirs(work, exist_ok=True)
    for pg in (1, 2):
        with open(cp.page_res_path(work, pg), "w", encoding="utf-8") as f:
            json.dump({"parsing_res_list": _one_text_block(pg)}, f)
    m = cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "A")
    cp.save_manifest(work, m)
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
    work = os.path.join(out, "scan", "_work")
    assert cp.is_page_done(work, 1) is True            # 空白页也落了检查点
    # 再跑一次:空白页不应被重跑
    calls.clear()
    cv.convert_pdf(pdf, out, dpi=100)
    assert calls == []


def test_convert_fingerprint_mismatch_wipes(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 2)
    _stub_engine(monkeypatch, _one_text_block)
    out = str(tmp_path / "out")
    work = os.path.join(out, "scan", "_work")
    os.makedirs(work, exist_ok=True)
    # 预置一个 DPI 不同的旧 manifest + 一页旧检查点
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": [{"block_order": 0, "block_label": "text",
                                         "block_content": "STALE 150dpi"}]}, f)
    cp.save_manifest(work, cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 150, "A"))
    cv.convert_pdf(pdf, out, dpi=100)                   # 请求 100 ≠ 记录 150
    md = open(os.path.join(out, "scan", "scan.md"), encoding="utf-8").read()
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
    work = os.path.join(out, "scan", "_work")
    os.makedirs(work, exist_ok=True)
    # 模拟:第 2 页已硬崩进程 MAX-1 次,残留 in_progress(该页无 res.json);
    # 本次 startup resolve_poison 再记一次 → 达阈值判毒页
    m = cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "A")
    m["in_progress"] = 2
    m["attempts_by_page"] = {"2": cp.MAX_HARD_ATTEMPTS - 1}
    cp.save_manifest(work, m)
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
    m = cp.load_manifest(os.path.join(out, "scan", "_work"))
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
    work = os.path.join(out, "scan", "_work")
    os.makedirs(work, exist_ok=True)
    m = cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "A")
    m["in_progress"] = 2
    m["attempts_by_page"] = {"2": cp.MAX_HARD_ATTEMPTS - 1}   # 差一次到阈值
    cp.save_manifest(work, m)
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
    selfcheck_path = os.path.join(str(tmp_path / "out"), "scan", "scan_selfcheck.json")
    assert os.path.exists(selfcheck_path)
    with open(selfcheck_path, encoding="utf-8") as f:
        on_disk = json.load(f)
    assert on_disk == res["selfcheck"]


def test_convert_no_selfcheck_json_when_disabled(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 2)
    _stub_engine(monkeypatch, _one_text_block)
    cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100, write_selfcheck=False)
    selfcheck_path = os.path.join(str(tmp_path / "out"), "scan", "scan_selfcheck.json")
    assert not os.path.exists(selfcheck_path)


def test_convert_cli_no_selfcheck_json_forwards_flag(monkeypatch):
    captured = {}
    def fake_convert_pdf(pdf_path, out_dir, dpi=150, write_selfcheck=True):
        captured["write_selfcheck"] = write_selfcheck
        return {"route": "A", "md_path": "x.md",
                "selfcheck": {"total": 0, "in_md": 0, "missing": []}, "failed_pages": []}
    monkeypatch.setattr(cv, "convert_pdf", fake_convert_pdf)
    monkeypatch.setattr("sys.argv", ["convert.py", "--src", "x.pdf", "--no-selfcheck-json"])
    cv.main()
    assert captured["write_selfcheck"] is False


def _one_image_block(page):
    return [{"block_order": None, "block_label": "image", "block_id": 1,
             "block_content": "", "block_bbox": [5, 5, 15, 15]}]


def test_convert_crops_images_before_png_deleted(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 1)
    _stub_engine(monkeypatch, _one_image_block)
    out = str(tmp_path / "out")
    res = cv.convert_pdf(pdf, out, dpi=100)
    assets_dir = os.path.join(out, "scan", "scan.assets")
    assert os.path.exists(os.path.join(assets_dir, "page_0001_block_1.png"))
    md = open(res["md_path"], encoding="utf-8").read()
    assert "scan.assets/page_0001_block_1.png" in md


def test_convert_clears_assets_on_fingerprint_mismatch(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 1)
    _stub_engine(monkeypatch, _one_image_block)
    out = str(tmp_path / "out")
    cv.convert_pdf(pdf, out, dpi=100)
    assets_dir = os.path.join(out, "scan", "scan.assets")
    assert os.path.exists(os.path.join(assets_dir, "page_0001_block_1.png"))
    # 换 DPI 触发指纹失配 → 全新跑,旧资产应被清空(重新裁出的文件名相同,
    # 用一个哨兵文件验证目录整体被清过,而不仅是被覆盖)
    sentinel = os.path.join(assets_dir, "STALE_SENTINEL.png")
    open(sentinel, "w").close()
    cv.convert_pdf(pdf, out, dpi=120)
    assert not os.path.exists(sentinel)


def test_convert_backfills_assets_for_pre_existing_checkpoint(tmp_path, monkeypatch):
    # 模拟"图片功能上线前跑完的检查点":res.json 里有 image 块,但 assets 目录不存在
    pdf = _make_scan_pdf(tmp_path, 1)
    _stub_engine(monkeypatch, _one_image_block)
    out = str(tmp_path / "out")
    work = os.path.join(out, "scan", "_work")
    os.makedirs(work, exist_ok=True)
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": _one_image_block(1)}, f)
    cp.save_manifest(work, cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "A"))
    res = cv.convert_pdf(pdf, out, dpi=100)          # 该页已"完成",不会重新进 OCR 循环
    assets_dir = os.path.join(out, "scan", "scan.assets")
    assert os.path.exists(os.path.join(assets_dir, "page_0001_block_1.png"))  # 补裁生效
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
    work = os.path.join(out, "scan", "_work")
    os.makedirs(work, exist_ok=True)
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": _one_image_block(1)}, f)
    cp.save_manifest(work, cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "A"))

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
    assets_dir = os.path.join(out, "scan", "scan.assets")
    assert not os.path.exists(os.path.join(assets_dir, "page_0001_block_1.png"))
    assert res["selfcheck"]["missing_assets"] == []
    # 显式断言目录本身不存在或为空,防止 scoping 回归时测试仍"意外"通过
    assert not os.path.isdir(assets_dir) or os.listdir(assets_dir) == []


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
