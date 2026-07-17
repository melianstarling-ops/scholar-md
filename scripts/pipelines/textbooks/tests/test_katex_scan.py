import scripts.pipelines.textbooks.katex_scan as ks
from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks.paths import resolve_layout

import json
import os
from pathlib import Path
import shutil
import subprocess


def test_scan_katex_returns_none_when_node_missing(monkeypatch):
    monkeypatch.setattr(ks.shutil, "which", lambda name: None)
    assert ks.scan_katex("book.md", "out.json") is None


def test_scan_katex_invokes_node_with_mjs_and_md_and_out(monkeypatch, tmp_path):
    captured = {}
    monkeypatch.setattr(ks.shutil, "which", lambda name: "C:/node/node.exe")

    out_file = tmp_path / "render_errors.json"

    class FakeProc:
        returncode = 0
        stdout = ""

    def fake_run(argv, **kwargs):
        captured["argv"] = argv
        Path(argv[argv.index("--out") + 1]).write_text('{"errors": []}', encoding="utf-8")
        return FakeProc()

    monkeypatch.setattr(ks.subprocess, "run", fake_run)
    result = ks.scan_katex("book.md", str(out_file))

    assert captured["argv"][0] == "C:/node/node.exe"
    assert any(a.endswith("scan_katex_errors.mjs") for a in captured["argv"])
    assert "book.md" in captured["argv"]
    node_out = Path(captured["argv"][captured["argv"].index("--out") + 1])
    assert node_out.parent == out_file.parent
    assert node_out.name.startswith(".katex_scan_")
    assert out_file.exists()
    assert result == {"errors": []}


def test_scan_katex_does_not_read_stale_output_after_node_failure(monkeypatch, tmp_path):
    monkeypatch.setattr(ks.shutil, "which", lambda name: "C:/node/node.exe")
    out_file = tmp_path / "render_errors.json"
    out_file.write_text('{"errors": [{"stale": true}]}', encoding="utf-8")

    class FakeProc:
        returncode = 1
        stdout = ""
        stderr = "boom"

    def fake_run(argv, **kwargs):
        return FakeProc()

    monkeypatch.setattr(ks.subprocess, "run", fake_run)

    assert ks.scan_katex("book.md", str(out_file)) is None
    assert json.loads(out_file.read_text(encoding="utf-8")) == {"errors": [{"stale": True}]}


def test_scan_katex_work_pages_emits_page_and_block_markers(monkeypatch, tmp_path):
    layout = resolve_layout("book", str(tmp_path / "out"))
    cp.save_manifest(layout.work_dir, cp.new_manifest(
        "book.pdf", {"page_count": 1, "size_bytes": 123}, 150, "A"))
    os.makedirs(layout.work_dir, exist_ok=True)
    with open(cp.page_res_path(layout.work_dir, 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": [
            {"block_label": "display_formula", "block_id": 7, "block_order": 1,
             "block_bbox": [0, 0, 10, 10],
             "block_content": r"$$ x^{\prime}^{2} $$"},
            {"block_label": "formula_number", "block_id": 8, "block_order": 2,
             "block_bbox": [20, 0, 30, 10], "block_content": "(2.45a)"},
        ]}, f)

    out_file = tmp_path / "render_errors.json"
    captured = {}
    monkeypatch.setattr(ks.shutil, "which", lambda name: "C:/node/node.exe")

    class FakeProc:
        returncode = 0
        stdout = ""

    def fake_run(argv, **kwargs):
        md_arg = argv[argv.index("--md") + 1]
        captured["md"] = open(md_arg, encoding="utf-8").read()
        Path(argv[argv.index("--out") + 1]).write_text(json.dumps({"errors": [
            {"page": 1, "block_ids": [7, 8], "formula_number": "2.45a",
             "latex_head": "x", "mode": "display"}
        ]}), encoding="utf-8")
        return FakeProc()

    monkeypatch.setattr(ks.subprocess, "run", fake_run)

    result = ks.scan_katex_work_pages(layout, str(out_file))

    assert "<!-- page: 1 block_ids: 7,8 -->" in captured["md"]
    assert r"\tag{2.45a}" in captured["md"]
    assert result["errors"][0]["page"] == 1
    assert result["errors"][0]["block_ids"] == [7, 8]


def test_work_pages_scan_applies_accepted_corrections(tmp_path):
    from scripts.pipelines.textbooks.vision_repair import content_fingerprint

    layout = resolve_layout("book", str(tmp_path / "out"))
    cp.save_manifest(layout.work_dir, cp.new_manifest(
        "book.pdf", {"page_count": 1, "size_bytes": 123}, 150, "A"))
    original = r"$$ \begin{array}{r}{bad_original $$"
    corrected = r"$$ x+y $$"
    with open(cp.page_res_path(layout.work_dir, 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": [
            {"block_label": "display_formula", "block_id": 7, "block_order": 1,
             "block_bbox": [0, 0, 10, 10], "block_content": original},
        ]}, f)
    Path(layout.corrections_path).write_text(json.dumps({
        "stem": "book",
        "corrections": [{
            "page": 1,
            "block_id": 7,
            "corrected_latex": corrected,
            "content_fingerprint": content_fingerprint(original),
            "status": "accepted",
        }],
    }), encoding="utf-8")

    scan_md = ks._work_pages_scan_md(layout)

    assert "x+y" in scan_md
    assert "bad_original" not in scan_md
    assert "<!-- page: 1 block_ids: 7 -->" in scan_md


def test_work_pages_scan_accepts_external_corrections_dir(tmp_path):
    from scripts.pipelines.textbooks.vision_repair import content_fingerprint

    layout = resolve_layout("book", str(tmp_path / "out"), str(tmp_path / "legacy-work"))
    cp.save_manifest(layout.work_dir, cp.new_manifest(
        "book.pdf", {"page_count": 1, "size_bytes": 123}, 150, "A"))
    original = r"$$ bad $$"
    with open(cp.page_res_path(layout.work_dir, 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": [{
            "block_label": "display_formula", "block_id": 1, "block_order": 1,
            "block_bbox": [0, 0, 10, 10], "block_content": original,
        }]}, f)
    corrections_dir = tmp_path / "current" / "book"
    corrections_dir.mkdir(parents=True)
    (corrections_dir / "book_corrections.json").write_text(json.dumps({
        "stem": "book",
        "corrections": [{
            "page": 1, "block_id": 1, "corrected_latex": r"$$ good $$",
            "content_fingerprint": content_fingerprint(original), "status": "accepted",
        }],
    }), encoding="utf-8")

    scan_md = ks._work_pages_scan_md(layout, corrections_dir=str(corrections_dir))

    assert "good" in scan_md
    assert "bad" not in scan_md


def test_js_scanner_keeps_tagged_display_math_out_of_inline_scan(tmp_path):
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available")

    scanner = os.path.abspath(ks._MJS)
    md = "\n".join([
        "<!-- page: 361 block_ids: 1 -->",
        "A malformed literal $5 should not consume the next display delimiter.",
        "<!-- page: 361 block_ids: 7,8 -->",
        r"$$ x = y \tag{8.1a} $$",
        "![figure](images/p361.png)",
        "<!-- page: 362 block_ids: 9 -->",
        "A normal inline formula $a+b$ remains inline.",
    ])
    probe = tmp_path / "probe_scan_katex.mjs"
    probe.write_text(
        "\n".join([
            "import { pathToFileURL } from 'node:url';",
            f"const scanner = {json.dumps(scanner)};",
            f"const md = {json.dumps(md)};",
            "const mod = await import(pathToFileURL(scanner).href);",
            "console.log(JSON.stringify({ formulas: mod.extractMath(md), result: mod.scan(md) }));",
        ]),
        encoding="utf-8",
    )

    proc = subprocess.run([node, str(probe)], capture_output=True, text=True,
                          encoding="utf-8", check=True)
    payload = json.loads(proc.stdout)
    formulas = payload["formulas"]

    tagged = [f for f in formulas if f["formula_number"] == "8.1a"]
    assert len(tagged) == 1
    assert tagged[0]["mode"] == "display"
    assert tagged[0]["page"] == 361
    assert tagged[0]["block_ids"] == [7, 8]
    assert [f["mode"] for f in formulas] == ["display", "inline"]
    assert all("figure" not in f["latex"] for f in formulas)
    assert not any("tag works only in display equations" in e["error"]
                   for e in payload["result"]["errors"])


def test_js_scanner_skips_dollars_inside_code_fence(tmp_path):
    # ``` 围栏代码里的 $(如 BASIC 字符串变量 A$=INKEY$)不是数学,不该报红(假阳性);
    # 围栏之外的真公式仍要被扫到。
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available")

    scanner = os.path.abspath(ks._MJS)
    md = "\n".join([
        "```",
        'Spcbr$=INKEY$',
        'IF Spcbr$=CHR$(32) THEN GOTO Loop:',
        "E2#=0",
        "```",
        "",
        "真公式在围栏外 $a_b_c$ 应被扫到。",
    ])
    probe = tmp_path / "probe_fence.mjs"
    probe.write_text(
        "\n".join([
            "import { pathToFileURL } from 'node:url';",
            f"const scanner = {json.dumps(scanner)};",
            f"const md = {json.dumps(md)};",
            "const mod = await import(pathToFileURL(scanner).href);",
            "console.log(JSON.stringify({ formulas: mod.extractMath(md), result: mod.scan(md) }));",
        ]),
        encoding="utf-8",
    )
    proc = subprocess.run([node, str(probe)], capture_output=True, text=True,
                          encoding="utf-8", check=True)
    payload = json.loads(proc.stdout)
    formulas = payload["formulas"]
    # 围栏内的 $ 不产生任何公式;只认出围栏外那一个
    assert [f["latex"] for f in formulas] == ["a_b_c"]
    # 围栏内代码没有被误判成公式报红
    assert not any("INKEY" in e.get("latex_head", "") for e in payload["result"]["errors"])


def test_js_scanner_stops_inline_span_at_crlf_paragraph_break(tmp_path):
    # md 是 \r\n 换行:两个价格里的字面 $($5.00 / $25.00)分属不同段落,不该跨 \r\n\r\n
    # 段落界配成一个 $…$ 数学(否则把 ___ 之类当公式报红,纯假阳性)。
    node = shutil.which("node")
    if not node:
        import pytest
        pytest.skip("node not available")

    scanner = os.path.abspath(ks._MJS)
    md = "The kit costs $5.00 today.\r\n\r\nYes, on disk ___ ($25.00) please."
    probe = tmp_path / "probe_crlf.mjs"
    probe.write_text(
        "\n".join([
            "import { pathToFileURL } from 'node:url';",
            f"const scanner = {json.dumps(scanner)};",
            f"const md = {json.dumps(md)};",
            "const mod = await import(pathToFileURL(scanner).href);",
            "console.log(JSON.stringify({ formulas: mod.extractMath(md), result: mod.scan(md) }));",
        ]),
        encoding="utf-8",
    )
    proc = subprocess.run([node, str(probe)], capture_output=True, text=True,
                          encoding="utf-8", check=True)
    payload = json.loads(proc.stdout)
    # 单个未配对的 $ 各自开不成 span(段落界处终止)→ 无公式、无报红
    assert payload["formulas"] == []
    assert payload["result"]["errors"] == []
