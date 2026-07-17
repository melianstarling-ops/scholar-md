import json
import os
import re
import shutil
import subprocess

from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks import debug_view as dv
from scripts.pipelines.textbooks.paths import resolve_layout
from scripts.pipelines.textbooks.vision_repair import content_fingerprint

APP_JS = os.path.join(dv.ASSETS, "app.js")


def _layout(tmp_path, stem="book"):
    return resolve_layout(stem, str(tmp_path / "out"), str(tmp_path / "work_root"))


def test_build_payloads_applies_corrections(tmp_path):
    layout = _layout(tmp_path)
    work = layout.work_dir
    os.makedirs(work, exist_ok=True)
    original = "$$ bad $$"
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        json.dump({"width": 100, "height": 100, "parsing_res_list": [
            {"block_label": "display_formula", "block_id": 5, "block_order": 0,
             "block_bbox": [0, 0, 10, 10], "block_content": original}]}, f)
    cp.save_manifest(work, cp.new_manifest("x.pdf", {"page_count": 1, "size_bytes": 0},
                                           150, "A"))
    corrections = {"stem": "book", "corrections": [
        {"page": 1, "block_id": 5, "corrected_latex": "$$ good $$",
         "content_fingerprint": content_fingerprint(original), "status": "accepted"}]}
    with open(layout.corrections_path, "w", encoding="utf-8") as f:
        json.dump(corrections, f)

    stem, pages = dv.build_payloads(layout, pdf_path=None, dpi=150, img_dpi=150,
                                    embed_images=False, img_cache={})

    assert "good" in pages[0]["md"]
    assert "bad" not in pages[0]["md"]


def test_build_payloads_does_not_apply_pending_correction(tmp_path):
    layout = _layout(tmp_path)
    work = layout.work_dir
    os.makedirs(work, exist_ok=True)
    original = "$$ bad $$"
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        json.dump({"width": 100, "height": 100, "parsing_res_list": [
            {"block_label": "display_formula", "block_id": 5, "block_order": 0,
             "block_bbox": [0, 0, 10, 10], "block_content": original}]}, f)
    cp.save_manifest(work, cp.new_manifest("x.pdf", {"page_count": 1, "size_bytes": 0},
                                           150, "A"))
    corrections = {"stem": "book", "corrections": [
        {"page": 1, "block_id": 5, "corrected_latex": "$$ good $$",
         "content_fingerprint": content_fingerprint(original), "status": "pending"}]}
    with open(layout.corrections_path, "w", encoding="utf-8") as f:
        json.dump(corrections, f)

    stem, pages = dv.build_payloads(layout, pdf_path=None, dpi=150, img_dpi=150,
                                    embed_images=False, img_cache={})

    assert "bad" in pages[0]["md"]                  # 人工确认门:待审不生效
    assert "good" not in pages[0]["md"]


def test_build_payloads_unaffected_when_no_corrections_file(tmp_path):
    layout = _layout(tmp_path)
    work = layout.work_dir
    os.makedirs(work, exist_ok=True)
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        json.dump({"width": 100, "height": 100, "parsing_res_list": [
            {"block_label": "display_formula", "block_id": 5, "block_order": 0,
             "block_bbox": [0, 0, 10, 10], "block_content": "$$ untouched $$"}]}, f)
    cp.save_manifest(work, cp.new_manifest("x.pdf", {"page_count": 1, "size_bytes": 0},
                                           150, "A"))

    stem, pages = dv.build_payloads(layout, pdf_path=None, dpi=150, img_dpi=150,
                                    embed_images=False, img_cache={})

    assert "untouched" in pages[0]["md"]


def test_build_payloads_attaches_pending_correction_preview(tmp_path):
    layout = _layout(tmp_path)
    work = layout.work_dir
    os.makedirs(work, exist_ok=True)
    original = "$$ bad $$"
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        json.dump({"width": 100, "height": 100, "parsing_res_list": [
            {"block_label": "display_formula", "block_id": 5, "block_order": 0,
             "block_bbox": [0, 0, 10, 10], "block_content": original}]}, f)
    cp.save_manifest(work, cp.new_manifest("x.pdf", {"page_count": 1, "size_bytes": 0},
                                           150, "A"))
    corrections = {"stem": "book", "corrections": [
        {"page": 1, "block_id": 5, "corrected_latex": "$$ good $$", "confidence": "high",
         "content_fingerprint": content_fingerprint(original), "status": "pending"}]}
    with open(layout.corrections_path, "w", encoding="utf-8") as f:
        json.dump(corrections, f)

    stem, pages = dv.build_payloads(layout, pdf_path=None, dpi=150, img_dpi=150,
                                    embed_images=False, img_cache={})

    b = next(b for b in pages[0]["blocks"] if b["block_id"] == 5)
    assert b["correction"]["status"] == "pending"
    assert b["correction"]["corrected_latex"] == "$$ good $$"


import json as _json


def test_handle_post_corrections_updates_status(tmp_path):
    layout = _layout(tmp_path)
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    with open(layout.corrections_path, "w", encoding="utf-8") as f:
        _json.dump({"stem": "book", "corrections": [
            {"page": 1, "block_id": 5, "status": "pending"}]}, f)

    status, body = dv.handle_post(layout, "/corrections",
                                  _json.dumps({"page": 1, "block_id": 5, "status": "accepted"}))

    assert status == 200
    with open(layout.corrections_path, encoding="utf-8") as f:
        data = _json.load(f)
    assert data["corrections"][0]["status"] == "accepted"


def test_handle_post_corrections_404_when_no_match(tmp_path):
    layout = _layout(tmp_path)
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    with open(layout.corrections_path, "w", encoding="utf-8") as f:
        _json.dump({"stem": "book", "corrections": []}, f)

    status, body = dv.handle_post(layout, "/corrections",
                                  _json.dumps({"page": 1, "block_id": 5, "status": "accepted"}))
    assert status == 404


def test_handle_post_other_path_writes_annotations_file(tmp_path):
    layout = _layout(tmp_path)
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    status, body = dv.handle_post(layout, "/annotations", '{"a":1}')
    assert status == 200
    assert open(os.path.join(layout.doc_work_dir, "book_annotations.json"),
                encoding="utf-8").read() == '{"a":1}'


import base64 as _b64


def test_build_payloads_attaches_crop_photo_to_correction(tmp_path):
    layout = _layout(tmp_path)
    work = layout.work_dir
    os.makedirs(work, exist_ok=True)
    original = "$$ bad $$"
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        json.dump({"width": 100, "height": 100, "parsing_res_list": [
            {"block_label": "display_formula", "block_id": 5, "block_order": 0,
             "block_bbox": [0, 0, 10, 10], "block_content": original}]}, f)
    cp.save_manifest(work, cp.new_manifest("x.pdf", {"page_count": 1, "size_bytes": 0},
                                           150, "A"))
    corrections = {"stem": "book", "corrections": [
        {"page": 1, "block_id": 5, "corrected_latex": "$$ good $$",
         "content_fingerprint": content_fingerprint(original), "status": "pending"}]}
    with open(layout.corrections_path, "w", encoding="utf-8") as f:
        json.dump(corrections, f)
    crops_dir = os.path.join(layout.repair_dir, "crops")
    os.makedirs(crops_dir, exist_ok=True)
    png_bytes = b"\x89PNG\r\n\x1a\nfake-png-bytes"
    with open(os.path.join(crops_dir, "page_0001_block_5.png"), "wb") as f:
        f.write(png_bytes)

    stem, pages = dv.build_payloads(layout, pdf_path=None, dpi=150, img_dpi=150,
                                    embed_images=False, img_cache={})

    b = next(b for b in pages[0]["blocks"] if b["block_id"] == 5)
    assert b["correction"]["crop_b64"] == _b64.b64encode(png_bytes).decode()


def test_build_payloads_correction_has_no_crop_b64_when_file_missing(tmp_path):
    layout = _layout(tmp_path)
    work = layout.work_dir
    os.makedirs(work, exist_ok=True)
    original = "$$ bad $$"
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        json.dump({"width": 100, "height": 100, "parsing_res_list": [
            {"block_label": "display_formula", "block_id": 5, "block_order": 0,
             "block_bbox": [0, 0, 10, 10], "block_content": original}]}, f)
    cp.save_manifest(work, cp.new_manifest("x.pdf", {"page_count": 1, "size_bytes": 0},
                                           150, "A"))
    corrections = {"stem": "book", "corrections": [
        {"page": 1, "block_id": 5, "corrected_latex": "$$ good $$",
         "content_fingerprint": content_fingerprint(original), "status": "pending"}]}
    with open(layout.corrections_path, "w", encoding="utf-8") as f:
        json.dump(corrections, f)

    stem, pages = dv.build_payloads(layout, pdf_path=None, dpi=150, img_dpi=150,
                                    embed_images=False, img_cache={})

    b = next(b for b in pages[0]["blocks"] if b["block_id"] == 5)
    assert b["correction"].get("crop_b64", "") == ""


def test_build_payloads_attaches_formula_candidates(tmp_path):
    layout = _layout(tmp_path)
    work = layout.work_dir
    os.makedirs(work, exist_ok=True)
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        json.dump({"width": 100, "height": 100, "parsing_res_list": [
            {"block_label": "display_formula", "block_id": 5, "block_order": 0,
             "block_bbox": [0, 0, 10, 10], "block_content": "$$ suspect $$"}]}, f)
    cp.save_manifest(work, cp.new_manifest("x.pdf", {"page_count": 1, "size_bytes": 0},
                                           150, "A"))
    os.makedirs(layout.repair_dir, exist_ok=True)
    with open(layout.formula_candidates_path, "w", encoding="utf-8") as f:
        f.write(json.dumps({
            "candidate_id": "p0001-b0005",
            "page": 1,
            "block_id": 5,
            "reasons": ["katex_warning:unicodeTextInMathMode"],
            "estimate_basis": "bbox_proxy",
        }) + "\n")

    stem, pages = dv.build_payloads(layout, pdf_path=None, dpi=150, img_dpi=150,
                                    embed_images=False, img_cache={})

    assert pages[0]["candidates"][0]["candidate_id"] == "p0001-b0005"
    block = next(b for b in pages[0]["blocks"] if b["block_id"] == 5)
    assert block["candidate"]["reasons"] == ["katex_warning:unicodeTextInMathMode"]


def test_handle_post_reassemble_runs_when_dirty(tmp_path):
    layout = _layout(tmp_path)
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    calls = []
    state = {"dirty": True}
    status, body = dv.handle_post(
        layout, "/reassemble", "",
        state=state, reassemble_fn=lambda: calls.append(1))
    assert status == 200
    assert calls == [1]                 # dirty → 跑
    assert state["dirty"] is False      # 跑完清脏


def test_handle_post_reassemble_skips_when_clean(tmp_path):
    layout = _layout(tmp_path)
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    calls = []
    state = {"dirty": False}
    status, body = dv.handle_post(
        layout, "/reassemble", "",
        state=state, reassemble_fn=lambda: calls.append(1))
    assert status == 200
    assert calls == []                  # 无脏 → 秒回不跑


def test_handle_post_corrections_sets_dirty(tmp_path):
    layout = _layout(tmp_path)
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    with open(layout.corrections_path, "w", encoding="utf-8") as f:
        _json.dump({"stem": "book", "corrections": [
            {"page": 1, "block_id": 5, "status": "pending"}]}, f)
    state = {"dirty": False}
    status, body = dv.handle_post(
        layout, "/corrections",
        _json.dumps({"page": 1, "block_id": 5, "status": "accepted"}),
        state=state)
    assert status == 200
    assert state["dirty"] is True       # 采纳成功 → 置脏


def test_safe_reassemble_swallows_exception(tmp_path):
    def boom(*a, **k):
        raise RuntimeError("assemble boom")
    layout = _layout(tmp_path)
    out = dv._safe_reassemble(layout, pdf_path=None, dpi=100, reassemble_fn=boom)
    assert out is None                  # 异常被吞,返回 None、不抛


def test_safe_reassemble_returns_path_on_success(tmp_path):
    def ok(layout, pdf_path, dpi):
        return "MD_PATH"
    layout = _layout(tmp_path)
    out = dv._safe_reassemble(layout, pdf_path=None, dpi=100, reassemble_fn=ok)
    assert out == "MD_PATH"


def test_cli_reassemble_calls_reassemble(tmp_path, monkeypatch):
    layout = resolve_layout("scan", str(tmp_path / "out"), str(tmp_path / "work_root"))
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    captured = {}
    def fake_reassemble(lay, pdf_path, dpi):
        captured["layout"] = lay
        return layout.md_path
    monkeypatch.setattr(dv, "reassemble_md", fake_reassemble)
    monkeypatch.setattr(dv.cp, "load_manifest",
                        lambda w: {"dpi": 100, "pdf_path": None})
    monkeypatch.setattr("sys.argv",
                        ["debug_view.py", "--out", layout.deliverables_root,
                         "--work-dir", layout.work_root, "--stem", layout.stem,
                         "--reassemble"])
    dv.main()
    assert captured["layout"] == layout


def test_cli_static_debug_html_writes_to_process_root_not_deliverables(tmp_path, monkeypatch):
    layout = resolve_layout("scan", str(tmp_path / "out"), str(tmp_path / "work_root"))

    monkeypatch.setattr(dv, "_resolve_pdf", lambda lay, src: (None, 150))
    monkeypatch.setattr(dv, "build_payloads",
                        lambda lay, pdf_path, dpi, img_dpi, embed_images, img_cache:
                        (lay.stem, [{"image_b64": None, "render_errors": []}]))
    monkeypatch.setattr(dv, "render_html", lambda stem, pages, serve: "<html></html>")
    monkeypatch.setattr("sys.argv",
                        ["debug_view.py", "--out", layout.deliverables_root,
                         "--work-dir", layout.work_root, "--stem", layout.stem])

    dv.main()

    assert os.path.exists(layout.debug_html_path)
    assert layout.debug_html_path.startswith(layout.doc_work_dir)
    assert not os.path.exists(os.path.join(layout.doc_deliverable_dir,
                                           f"{layout.stem}_debug.html"))


def test_debug_asset_renders_table_html_without_enabling_global_html():
    app_js = open(os.path.join(dv.ASSETS, "app.js"), encoding="utf-8").read()

    assert "window.markdownit({ html: false" in app_js
    assert "function renderSafeTableHtml" in app_js
    assert "function renderMarkdownFragment" in app_js
    assert "renderMarkdownFragment(f.md || \"\")" in app_js


def test_debug_asset_normalizes_inline_math_delimiter_padding():
    app_js = open(os.path.join(dv.ASSETS, "app.js"), encoding="utf-8").read()

    assert "function normalizeInlineMathPadding" in app_js
    assert "const trimmed = body.trim()" in app_js
    assert "mdit.render(normalizeInlineMathPadding(md || \"\"))" in app_js


def test_debug_asset_surfaces_formula_candidates():
    app_js = open(os.path.join(dv.ASSETS, "app.js"), encoding="utf-8").read()
    app_css = open(os.path.join(dv.ASSETS, "app.css"), encoding="utf-8").read()

    assert "nCand" in app_js
    assert "候选复核" in app_js
    assert "candidateBids" in app_js
    assert "OCR 正确" in app_js
    assert "needs_repair" in app_js
    assert "candidate_reviews" in app_js
    assert ".mdblk.candidate" in app_css
    assert ".box.candidate" in app_css
    assert ".candcard" in app_css


def test_debug_asset_inline_math_padding_does_not_swallow_prose_between_formulas(tmp_path):
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available")
    app_js = open(os.path.join(dv.ASSETS, "app.js"), encoding="utf-8").read()
    match = re.search(
        r"function normalizeInlineMathPadding\(md\) \{\n(?P<body>.*?)\n  \}",
        app_js,
        re.S,
    )
    assert match
    probe = tmp_path / "probe.mjs"
    expected = (
        r"then (12.76) represent $n$ uncoupled sets of two-conductor lines, "
        r"each with incident field excitation through elements of the vectors "
        r"$\mathbf{V}_{\mathrm{Fm}}(z,t)$ and $\mathbf{I}_{\mathrm{Fm}}(z,t)$. Once"
    )
    probe.write_text(
        "function normalizeInlineMathPadding(md) {\n"
        f"{match.group('body')}\n"
        "}\n"
        f"const md = String.raw`{expected}`;\n"
        "console.log(normalizeInlineMathPadding(md));\n",
        encoding="utf-8",
    )

    proc = subprocess.run([node, str(probe)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=10)

    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == expected
    assert "$n$uncoupled" not in proc.stdout
    assert "vectors$\\mathbf{V}" not in proc.stdout
    assert "$and$" not in proc.stdout


def test_correction_preview_carries_agent_provenance():
    from scripts.pipelines.textbooks.debug_payload import _correction_preview
    from scripts.pipelines.textbooks.vision_repair import content_fingerprint

    corr = {"page": 1, "block_id": 1, "engine_latex": "r_{nf} + 1",
            "corrected_latex": "$$ r_{hf} + 1 $$", "status": "pending",
            "content_fingerprint": content_fingerprint("r_{nf} + 1"),
            "provider": "kimi", "model": "kimi-coding", "effort": "thinking",
            "confidence": 0.72, "attempt": 1, "verdict": "correct",
            "cross_checked_by": "gemini", "note": "下标是 h"}
    preview = _correction_preview({"block_id": 1, "block_content": "r_{nf} + 1"},
                                  {1: corr})
    assert preview is not None
    assert preview["provider"] == "kimi"
    assert preview["cross_checked_by"] == "gemini"


def test_app_js_renders_agent_provenance():
    with open(APP_JS, encoding="utf-8") as f:
        src = f.read()
    for token in ("provider", "cross_checked_by", "模型来源"):
        assert token in src, token


def test_build_payloads_audit_missing_file_is_none(tmp_path):
    # 报告文件不存在(未跑 source_audit / 独立重跑前)→ 页级 audit 明确为 None,不崩。
    layout = _layout(tmp_path)
    work = layout.work_dir
    os.makedirs(work, exist_ok=True)
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        json.dump({"width": 100, "height": 100, "parsing_res_list": []}, f)
    cp.save_manifest(work, cp.new_manifest("x.pdf", {"page_count": 1, "size_bytes": 0},
                                           150, "A"))

    stem, pages = dv.build_payloads(layout, pdf_path=None, dpi=150, img_dpi=150,
                                    embed_images=False, img_cache={})

    assert pages[0]["audit"] is None


def test_build_payloads_audit_corrupted_json_is_none_not_raise(tmp_path):
    # 报告文件存在但损坏(非法 JSON)→ 优雅缺席,不抛异常。
    layout = _layout(tmp_path)
    work = layout.work_dir
    os.makedirs(work, exist_ok=True)
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        json.dump({"width": 100, "height": 100, "parsing_res_list": []}, f)
    cp.save_manifest(work, cp.new_manifest("x.pdf", {"page_count": 1, "size_bytes": 0},
                                           150, "A"))
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    with open(layout.source_audit_path, "w", encoding="utf-8") as f:
        f.write("{not valid json")

    stem, pages = dv.build_payloads(layout, pdf_path=None, dpi=150, img_dpi=150,
                                    embed_images=False, img_cache={})

    assert pages[0]["audit"] is None


def test_build_payloads_attaches_page_audit_from_report(tmp_path):
    # 报告齐全时,build_payloads 按页号把 source_audit 报告接进对应 payload。
    layout = _layout(tmp_path)
    work = layout.work_dir
    os.makedirs(work, exist_ok=True)
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        json.dump({"width": 100, "height": 100, "parsing_res_list": [
            {"block_label": "text", "block_id": 5, "block_order": 0,
             "block_bbox": [0, 0, 10, 10], "block_content": "hi"}]}, f)
    cp.save_manifest(work, cp.new_manifest("x.pdf", {"page_count": 1, "size_bytes": 0},
                                           150, "A"))
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    report = {"schema_version": 2, "pages": [
        {"page": 1, "status": "OK", "issues": [],
         "blocks": [{"block_id": 5, "label": "text", "content_source": "source_text",
                     "reasons": [], "block_ned": 0.01}],
         "prose_audit": {"status": "OK",
                         "block_metrics": {"5": {"content_source": "source_text",
                                                  "block_ned": 0.01}}},
         "table_audit": []},
    ]}
    with open(layout.source_audit_path, "w", encoding="utf-8") as f:
        json.dump(report, f)

    stem, pages = dv.build_payloads(layout, pdf_path=None, dpi=150, img_dpi=150,
                                    embed_images=False, img_cache={})

    assert pages[0]["audit"]["status"] == "OK"
    b = next(b for b in pages[0]["blocks"] if b["block_id"] == 5)
    assert b["provenance"]["content_source"] == "source_text"


def test_app_js_problem_pages_filter_includes_audit_suspect_and_adoption_disagreement(tmp_path):
    # problem-pages 过滤器必须纳入:status SUSPECT、status UNSCORABLE(裁决:
    # "审计判不了"的页恰恰是操作者必须人工看的页,排除在外会被静默跳过)、以及
    # 含 adoption_disagreement 原因码的块(即便页级 status 是 OK)。纯 OK 页
    # 且无 adoption_disagreement、以及无 audit 数据的页,都不应被纳入。
    app_js = open(APP_JS, encoding="utf-8").read()
    match = re.search(r"function pageAuditSuspect\(p\) \{\n(?P<body>.*?)\n  \}", app_js, re.S)
    assert match, "pageAuditSuspect() 未在 app.js 中找到"
    assert "pageAuditSuspect(p)" in app_js  # 定义了必须真的接进 computeProblems

    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available")
    probe = tmp_path / "probe_audit_suspect.mjs"
    probe.write_text(
        "function pageAuditSuspect(p) {\n"
        f"{match.group('body')}\n"
        "}\n"
        "const cases = [\n"
        "  { audit: { status: 'SUSPECT', blocks: [] } },\n"
        "  { audit: { status: 'UNSCORABLE', blocks: [] } },\n"
        "  { audit: { status: 'OK', blocks: [] } },\n"
        "  { audit: { status: 'OK', blocks: [{ block_id: 1, reasons: ['adoption_disagreement'] }] } },\n"
        "  {},\n"
        "];\n"
        "console.log(JSON.stringify(cases.map(pageAuditSuspect)));\n",
        encoding="utf-8",
    )
    proc = subprocess.run([node, str(probe)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=10)
    assert proc.returncode == 0, proc.stderr
    assert json.loads(proc.stdout.strip()) == [True, True, False, True, False]


def test_app_js_escapes_audit_issue_detail_via_esc(tmp_path):
    # audit issue 的 detail(可能含用户/OCR 产出的任意文本)必须经既有 esc() 转义
    # 才能拼进 innerHTML——不得开新的未转义拼接路径。
    app_js = open(APP_JS, encoding="utf-8").read()
    assert re.search(r"esc\(\s*iss\.detail", app_js), \
        "audit issue.detail 必须经 esc() 转义后才能拼进 innerHTML"

    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available")
    esc_line = next((l for l in app_js.splitlines() if l.strip().startswith("const esc =")), None)
    assert esc_line, "esc() 转义函数未找到"
    probe = tmp_path / "probe_esc.mjs"
    probe.write_text(
        esc_line.strip() + "\n"
        "const detail = '<script>alert(1)</script>';\n"
        "console.log(esc(detail));\n",
        encoding="utf-8",
    )
    proc = subprocess.run([node, str(probe)], capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=10)
    assert proc.returncode == 0, proc.stderr
    out = proc.stdout.strip()
    assert "<script>" not in out
    assert "&lt;script&gt;" in out


def test_render_html_defines_provenance_badges_and_distinct_unscorable_style():
    # HTML 输出(app.css/app.js 内嵌)须含 provenance 徽标 class,且 UNSCORABLE
    # 独立样式类不得复用 KaTeX 硬报错的"错误红"(var(--bad))。
    pages = [{
        "page": 1, "width": 100, "height": 100, "image_b64": None,
        "blocks": [], "md": "", "frags": [],
        "signals": {"column_suspected": False, "unhandled_labels": [], "visual_warnings": []},
        "render_errors": [], "suspicions": [], "candidates": [],
        "audit": None,
    }]
    html = dv.render_html("stem", pages, serve=False)

    assert "prov-badge" in html
    assert "prov-source" in html
    assert "prov-ocr" in html
    assert ".katex-error" in html  # 既有渲染报错类仍在,基线不受影响

    m = re.search(r"\.badge\.audit-unscorable\{([^}]*)\}", html)
    assert m, "app.css 缺 .badge.audit-unscorable 独立样式类"
    assert "var(--bad)" not in m.group(1)


def test_app_js_renders_limited_missing_and_added_samples_per_block():
    # missing_samples/added_samples(真实来自 prose_audit.block_metrics,commit
    # 22d53eb,payload 侧已按块归属并截断)必须真的渲染出来,不能只在 payload
    # 里携带却在 UI 上哑掉;渲染入口必须挂在块级 provenance 徽标(renderProvBadge)
    # 上,而不是凭空发明的页级字段。
    app_js = open(APP_JS, encoding="utf-8").read()
    assert "function renderSampleList" in app_js
    assert "p.missing_samples" in app_js
    assert "p.added_samples" in app_js
    assert "missing_samples_truncated" in app_js
    assert "added_samples_truncated" in app_js
    # 必须真的从 renderProvBadge 调用,不能定义了却没接进渲染路径
    assert re.search(r"renderSampleList\([^)]*missing_samples", app_js)
    assert re.search(r"renderSampleList\([^)]*added_samples", app_js)
