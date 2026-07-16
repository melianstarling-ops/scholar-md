import json
from pathlib import Path

import fitz
import pytest

from scripts.pipelines.textbooks import batch as bp
from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks.paths import resolve_layout


def _make_pdf(tmp_path, n_pages, name="book"):
    doc = fitz.open()
    for _ in range(n_pages):
        doc.new_page()
    p = tmp_path / f"{name}.pdf"
    doc.save(str(p))
    return p


def _mark_page_done(work: Path, page: int, content: str = "x") -> None:
    work.mkdir(parents=True, exist_ok=True)
    with open(cp.page_res_path(str(work), page), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": [
            {"block_order": 0, "block_label": "text", "block_content": content}]}, f)


def test_discover_dir_and_file_mixed_dedup(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    a = d / "A.pdf"
    a.write_bytes(b"%PDF-1.4")
    b = d / "B.pdf"
    b.write_bytes(b"%PDF-1.4")
    result = bp.discover([str(d), str(a)])   # 目录+文件混用,a 只应出现一次
    assert result == [a, b]


def test_discover_skips_non_pdf(tmp_path, capsys):
    d = tmp_path / "src"
    d.mkdir()
    (d / "notes.txt").write_text("x", encoding="utf-8")
    result = bp.discover([str(d / "notes.txt")])
    assert result == []
    assert "跳过" in capsys.readouterr().err


def test_discover_cross_dir_stem_collision_raises(tmp_path):
    d1 = tmp_path / "s1"
    d1.mkdir()
    d2 = tmp_path / "s2"
    d2.mkdir()
    (d1 / "A.pdf").write_bytes(b"%PDF-1.4")
    (d2 / "A.pdf").write_bytes(b"%PDF-1.4")
    with pytest.raises(ValueError, match="跨目录同名"):
        bp.discover([str(d1), str(d2)])


def test_already_done_false_when_no_manifest(tmp_path):
    pdf = _make_pdf(tmp_path, 2)
    assert bp._already_done(tmp_path / "out", None, pdf, 150) is False


def test_already_done_true_when_all_pages_done(tmp_path):
    pdf = _make_pdf(tmp_path, 2)
    out_root = tmp_path / "out"
    layout = resolve_layout(pdf.stem, str(out_root))
    work = Path(layout.work_dir)
    _mark_page_done(work, 1)
    _mark_page_done(work, 2)
    m = cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A")
    cp.save_manifest(str(work), m)
    assert bp._already_done(out_root, None, pdf, 150) is True


def test_already_done_false_on_dpi_mismatch(tmp_path):
    pdf = _make_pdf(tmp_path, 1)
    out_root = tmp_path / "out"
    layout = resolve_layout(pdf.stem, str(out_root))
    work = Path(layout.work_dir)
    _mark_page_done(work, 1)
    m = cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A")
    cp.save_manifest(str(work), m)
    assert bp._already_done(out_root, None, pdf, 200) is False   # 请求 DPI 200 ≠ 记录 150


def test_already_done_false_on_source_replaced(tmp_path):
    pdf = _make_pdf(tmp_path, 1)
    out_root = tmp_path / "out"
    layout = resolve_layout(pdf.stem, str(out_root))
    work = Path(layout.work_dir)
    _mark_page_done(work, 1)
    m = cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A")
    cp.save_manifest(str(work), m)
    # 同名文件被替换成不同页数(指纹变了)
    doc = fitz.open()
    doc.new_page(); doc.new_page(); doc.new_page()
    doc.save(str(pdf))
    assert bp._already_done(out_root, None, pdf, 150) is False


def test_already_done_true_when_only_poisoned_page_remains(tmp_path):
    pdf = _make_pdf(tmp_path, 2)
    out_root = tmp_path / "out"
    layout = resolve_layout(pdf.stem, str(out_root))
    work = Path(layout.work_dir)
    _mark_page_done(work, 1)
    m = cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A")
    cp.record_failure(m, 2, "process killed repeatedly", "process-killed")
    cp.save_manifest(str(work), m)
    assert bp._already_done(out_root, None, pdf, 150) is True     # 毒页不算"未完成"


def test_already_done_false_when_page_exception_pending(tmp_path):
    pdf = _make_pdf(tmp_path, 2)
    out_root = tmp_path / "out"
    layout = resolve_layout(pdf.stem, str(out_root))
    work = Path(layout.work_dir)
    _mark_page_done(work, 1)
    m = cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A")
    cp.record_failure(m, 2, "transient", "page-exception")
    cp.save_manifest(str(work), m)
    assert bp._already_done(out_root, None, pdf, 150) is False    # 瞬时失败页仍算未完成,允许重试


def test_run_calls_watchdog_once_per_book(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    (d / "A.pdf").write_bytes(b"%PDF-1.4")
    (d / "B.pdf").write_bytes(b"%PDF-1.4")
    calls = []
    def fake_runner(argv):
        calls.append(argv)
        return 0
    rc, results = bp.run([str(d)], out=str(tmp_path / "out"), runner=fake_runner)
    assert rc == 0
    assert len(calls) == 2
    assert [r["stem"] for r in results] == ["A", "B"]


def test_run_reports_giveup_and_nonzero_rc(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    (d / "A.pdf").write_bytes(b"%PDF-1.4")
    def fake_runner(argv):
        return 1   # 永远崩
    rc, results = bp.run([str(d)], out=str(tmp_path / "out"), max_restarts=2, runner=fake_runner)
    assert rc == 1
    assert results[0]["status"] == "GIVEUP"


def test_run_resume_skips_done_book_without_spawning(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    pdf = d / "A.pdf"
    doc = fitz.open()
    doc.new_page()
    doc.save(str(pdf))
    out_root = tmp_path / "out"
    layout = resolve_layout("A", str(out_root))
    work = Path(layout.work_dir)
    _mark_page_done(work, 1)
    cp.save_manifest(str(work),
                     cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), cp.DEFAULT_DPI, "A"))
    calls = []
    def fake_runner(argv):
        calls.append(argv)
        return 0
    rc, results = bp.run([str(d)], out=str(out_root), resume=True, runner=fake_runner)
    assert calls == []
    assert results[0]["status"] == "SKIP"


def test_run_limit_truncates_before_resume(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    for name in ("A", "B", "C"):
        (d / f"{name}.pdf").write_bytes(b"%PDF-1.4")
    calls = []
    def fake_runner(argv):
        calls.append(argv)
        return 0
    rc, results = bp.run([str(d)], out=str(tmp_path / "out"), limit=1, runner=fake_runner)
    assert len(results) == 1
    assert results[0]["stem"] == "A"


def test_main_list_flag_prints_pdfs_and_returns_zero(tmp_path, monkeypatch, capsys):
    d = tmp_path / "src"
    d.mkdir()
    (d / "A.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr("sys.argv", ["batch.py", "--src", str(d), "--list"])
    rc = bp.main()
    assert rc == 0
    assert "A.pdf" in capsys.readouterr().out


def test_main_returns_nonzero_on_stem_collision(tmp_path, monkeypatch):
    d1 = tmp_path / "s1"
    d1.mkdir()
    d2 = tmp_path / "s2"
    d2.mkdir()
    (d1 / "A.pdf").write_bytes(b"%PDF-1.4")
    (d2 / "A.pdf").write_bytes(b"%PDF-1.4")
    monkeypatch.setattr("sys.argv", ["batch.py", "--src", str(d1), str(d2)])
    rc = bp.main()
    assert rc == 1


def test_discover_cross_dir_stem_collision_case_insensitive_raises(tmp_path):
    d1 = tmp_path / "s1"
    d1.mkdir()
    d2 = tmp_path / "s2"
    d2.mkdir()
    (d1 / "Book.pdf").write_bytes(b"%PDF-1.4")
    (d2 / "book.pdf").write_bytes(b"%PDF-1.4")
    with pytest.raises(ValueError, match="跨目录同名"):
        bp.discover([str(d1), str(d2)])


def test_run_resume_survives_already_done_exception(tmp_path, monkeypatch):
    d = tmp_path / "src"
    d.mkdir()
    (d / "A.pdf").write_bytes(b"%PDF-1.4")
    (d / "B.pdf").write_bytes(b"%PDF-1.4")

    def raising_already_done(out_root, work_root, pdf_path, dpi):
        raise RuntimeError("simulated corrupt PDF")

    monkeypatch.setattr(bp, "_already_done", raising_already_done)
    calls = []
    def fake_runner(argv):
        calls.append(argv)
        return 0
    rc, results = bp.run([str(d)], out=str(tmp_path / "out"), resume=True, runner=fake_runner)
    assert rc == 0
    assert len(calls) == 2   # both books still processed, not aborted


def test_run_limit_zero_processes_nothing(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    (d / "A.pdf").write_bytes(b"%PDF-1.4")
    (d / "B.pdf").write_bytes(b"%PDF-1.4")
    calls = []
    def fake_runner(argv):
        calls.append(argv)
        return 0
    rc, results = bp.run([str(d)], out=str(tmp_path / "out"), limit=0, runner=fake_runner)
    assert results == []
    assert calls == []


def test_run_suspect_book_keeps_rc_zero(tmp_path):
    d = tmp_path / "src"
    d.mkdir()
    pdf = _make_pdf(d, 2, name="A")
    out_root = tmp_path / "out"
    layout = resolve_layout("A", str(out_root))
    work = Path(layout.work_dir)
    _mark_page_done(work, 1)
    m = cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), cp.DEFAULT_DPI, "A")
    cp.record_failure(m, 2, "transient", "page-exception")
    cp.save_manifest(str(work), m)

    def fake_runner(argv):
        return 0
    rc, results = bp.run([str(d)], out=str(out_root), runner=fake_runner)
    assert results[0]["status"] == "SUSPECT"
    assert rc == 0


def test_already_done_uses_explicit_work_root(tmp_path):
    pdf = _make_pdf(tmp_path, 1)
    out_root = tmp_path / "out"
    work_root = tmp_path / "scratch"
    layout = resolve_layout(pdf.stem, str(out_root), str(work_root))
    work = Path(layout.work_dir)
    _mark_page_done(work, 1)
    m = cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A")
    cp.save_manifest(str(work), m)
    assert bp._already_done(out_root, work_root, pdf, 150) is True


def test_read_summary_reads_process_side_selfcheck_and_manifest(tmp_path):
    pdf = _make_pdf(tmp_path, 1)
    out_root = tmp_path / "out"
    layout = resolve_layout(pdf.stem, str(out_root))
    work = Path(layout.work_dir)
    _mark_page_done(work, 1)
    cp.save_manifest(str(work),
                     cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A"))
    Path(layout.doc_work_dir).mkdir(parents=True, exist_ok=True)
    with open(layout.selfcheck_path, "w", encoding="utf-8") as f:
        json.dump({"in_md": 1, "total": 1}, f)

    summary = bp._read_summary(out_root, None, pdf)

    assert summary["status"] == "OK"
    assert summary["route"] == "A"
    assert summary["selfcheck"] == {"in_md": 1, "total": 1}


def test_job_argv_passes_work_dir_to_convert_subprocess(tmp_path):
    pdf = tmp_path / "A.pdf"
    out_root = tmp_path / "out"
    work_root = tmp_path / "scratch"

    argv = bp._job_argv(pdf, out_root, work_root, 150, no_selfcheck_json=False)

    assert "--work-dir" in argv
    assert argv[argv.index("--work-dir") + 1] == str(work_root)


def test_job_argv_passes_allow_sleep_to_convert_subprocess(tmp_path):
    pdf = tmp_path / "A.pdf"
    out_root = tmp_path / "out"
    argv = bp._job_argv(pdf, out_root, None, 150, no_selfcheck_json=False,
                        allow_sleep=True)
    assert "--allow-sleep" in argv


def test_job_argv_passes_force_ocr_and_rest_schedule(tmp_path):
    argv = bp._job_argv(tmp_path / "A.pdf", tmp_path / "out", None, 150,
                        no_selfcheck_json=False, force_ocr=True,
                        work_hours=6, rest_minutes=40)

    assert "--force-ocr" in argv
    assert argv[argv.index("--work-hours") + 1] == "6"
    assert argv[argv.index("--rest-minutes") + 1] == "40"


def test_run_invokes_katex_scan_by_default(tmp_path, monkeypatch):
    d = tmp_path / "src"
    d.mkdir()
    pdf = _make_pdf(d, 1, name="A")
    out_root = tmp_path / "out"
    called = []

    def fake_runner(argv):
        layout = resolve_layout("A", str(out_root))
        work = Path(layout.work_dir)
        Path(layout.doc_deliverable_dir).mkdir(parents=True, exist_ok=True)
        Path(layout.md_path).write_text("# A\n", encoding="utf-8")
        _mark_page_done(work, 1)
        cp.save_manifest(str(work),
                         cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A"))
        return 0

    def fake_scan(layout, out_path):
        called.append((layout.stem, out_path))
        return {"errors": []}

    monkeypatch.setattr(bp, "scan_katex_work_pages", fake_scan)

    rc, results = bp.run([str(d)], out=str(out_root), dpi=150, runner=fake_runner)

    layout = resolve_layout("A", str(out_root))
    assert rc == 0
    assert results[0]["status"] == "OK"
    assert called == [("A", layout.render_errors_path)]


def test_run_does_not_invoke_katex_scan_when_disabled(tmp_path, monkeypatch):
    d = tmp_path / "src"
    d.mkdir()
    pdf = _make_pdf(d, 1, name="A")
    out_root = tmp_path / "out"
    called = []

    def fake_runner(argv):
        layout = resolve_layout("A", str(out_root))
        work = Path(layout.work_dir)
        Path(layout.doc_deliverable_dir).mkdir(parents=True, exist_ok=True)
        Path(layout.md_path).write_text("# A\n", encoding="utf-8")
        _mark_page_done(work, 1)
        cp.save_manifest(str(work),
                         cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A"))
        return 0

    monkeypatch.setattr(bp, "scan_katex_work_pages",
                        lambda layout, out_path: called.append((layout.stem, out_path)))

    rc, results = bp.run([str(d)], out=str(out_root), dpi=150, runner=fake_runner,
                         katex_scan_enabled=False)

    assert rc == 0
    assert results[0]["status"] == "OK"
    assert called == []


def test_run_keeps_book_ok_when_katex_scan_node_missing(tmp_path, monkeypatch, capsys):
    d = tmp_path / "src"
    d.mkdir()
    pdf = _make_pdf(d, 1, name="A")
    out_root = tmp_path / "out"

    def fake_runner(argv):
        layout = resolve_layout("A", str(out_root))
        work = Path(layout.work_dir)
        Path(layout.doc_deliverable_dir).mkdir(parents=True, exist_ok=True)
        Path(layout.md_path).write_text("# A\n", encoding="utf-8")
        _mark_page_done(work, 1)
        cp.save_manifest(str(work),
                         cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A"))
        return 0

    monkeypatch.setattr(bp, "scan_katex_work_pages", lambda layout, out_path: None)

    rc, results = bp.run([str(d)], out=str(out_root), dpi=150, runner=fake_runner)

    assert rc == 0
    assert results[0]["status"] == "OK"
    assert "[katex] node 缺失,跳过 A" in capsys.readouterr().out


def test_main_no_katex_scan_threads_disabled_to_run(monkeypatch):
    captured = {}

    def fake_run(src_paths, **kwargs):
        captured["src_paths"] = src_paths
        captured["kwargs"] = kwargs
        return 0, []

    monkeypatch.setattr(bp, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["batch.py", "--src", "src", "--no-katex-scan"])

    rc = bp.main()

    assert rc == 0
    assert captured["kwargs"]["katex_scan_enabled"] is False


def test_main_forwards_force_ocr_and_rest_schedule(monkeypatch):
    captured = {}

    def fake_run(src_paths, **kwargs):
        captured["src_paths"] = src_paths
        captured["kwargs"] = kwargs
        return 0, []

    monkeypatch.setattr(bp, "run", fake_run)
    monkeypatch.setattr("sys.argv", [
        "batch.py", "--src", "src", "--force-ocr",
        "--work-hours", "6", "--rest-minutes", "40",
    ])

    rc = bp.main()

    assert rc == 0
    assert captured["kwargs"]["force_ocr"] is True
    assert captured["kwargs"]["work_hours"] == 6
    assert captured["kwargs"]["rest_minutes"] == 40
