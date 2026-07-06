import scripts.pipelines.textbooks.katex_scan as ks


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
        out_file.write_text('{"errors": []}', encoding="utf-8")
        return FakeProc()

    monkeypatch.setattr(ks.subprocess, "run", fake_run)
    result = ks.scan_katex("book.md", str(out_file))

    assert captured["argv"][0] == "C:/node/node.exe"
    assert any(a.endswith("scan_katex_errors.mjs") for a in captured["argv"])
    assert "book.md" in captured["argv"]
    assert str(out_file) in captured["argv"]
    assert result == {"errors": []}
