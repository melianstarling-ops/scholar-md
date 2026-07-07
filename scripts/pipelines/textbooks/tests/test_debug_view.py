import json
import os
import re
import shutil
import subprocess

from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks import debug_view as dv
from scripts.pipelines.textbooks.paths import resolve_layout
from scripts.pipelines.textbooks.vision_repair import content_fingerprint


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
