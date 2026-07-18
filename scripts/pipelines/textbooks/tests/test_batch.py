import json
import os
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


def test_job_argv_passes_born_digital_mode_to_convert_subprocess(tmp_path):
    argv = bp._job_argv(tmp_path / "A.pdf", tmp_path / "out", None, 150,
                        no_selfcheck_json=False, born_digital_mode="hybrid")
    assert "--born-digital-mode" in argv
    assert argv[argv.index("--born-digital-mode") + 1] == "hybrid"


def test_job_argv_defaults_born_digital_mode_to_hybrid(tmp_path):
    argv = bp._job_argv(tmp_path / "A.pdf", tmp_path / "out", None, 150,
                        no_selfcheck_json=False)
    assert argv[argv.index("--born-digital-mode") + 1] == "hybrid"


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


# ---------------------------------------------------------------------------
# Task 10:_read_summary 综合 source audit 状态(计划 §7.2/Task 10 checklist)。
# selfcheck.json 的 "source_audit" 紧凑字段由 Task 10 的 convert.py/selfcheck.py
# 写入;这里直接手写该字段模拟已落盘的 selfcheck.json,不依赖真跑一遍 convert_pdf。
# ---------------------------------------------------------------------------

def _write_selfcheck_with_audit(layout, source_audit: dict | None) -> None:
    Path(layout.doc_work_dir).mkdir(parents=True, exist_ok=True)
    payload = {"in_md": 1, "total": 1}
    if source_audit is not None:
        payload["source_audit"] = source_audit
    with open(layout.selfcheck_path, "w", encoding="utf-8") as f:
        json.dump(payload, f)


def test_read_summary_audit_suspect_overrides_ok_status(tmp_path):
    # 有产物(无 failed_pages)但 audit 判 SUSPECT → 文档状态仍须是 SUSPECT,不能计入 OK。
    pdf = _make_pdf(tmp_path, 3, name="book")
    out_root = tmp_path / "out"
    layout = resolve_layout(pdf.stem, str(out_root))
    work = Path(layout.work_dir)
    for i in (1, 2, 3):
        _mark_page_done(work, i)
    cp.save_manifest(str(work),
                     cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "B"))
    _write_selfcheck_with_audit(layout, {
        "status": "SUSPECT", "suspect_pages": [2],
        "adoption": {"adopted": 1, "fallback_ocr": 1},
        "issue_counts": {"prose_mismatch": 1},
        "report": "book_source_audit.json",
    })

    summary = bp._read_summary(out_root, None, pdf)

    assert summary["status"] == "SUSPECT"


def test_read_summary_grades_severe_and_mild_issue_counts(tmp_path):
    pdf = _make_pdf(tmp_path, 10, name="book")
    out_root = tmp_path / "out"
    layout = resolve_layout(pdf.stem, str(out_root))
    work = Path(layout.work_dir)
    for i in range(1, 11):
        _mark_page_done(work, i)
    cp.save_manifest(str(work),
                     cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "B"))
    _write_selfcheck_with_audit(layout, {
        "status": "SUSPECT", "suspect_pages": [2, 5, 7],
        "adoption": {"adopted": 5, "fallback_ocr": 5},
        "issue_counts": {"sign_flip": 2, "adoption_error": 1,
                         "prose_mismatch": 3, "missing_prose": 1},
        "report": "book_source_audit.json",
    })

    grade = bp._read_summary(out_root, None, pdf)["source_audit_grade"]

    assert grade["suspect_page_count"] == 3
    assert grade["pages"] == 10
    assert grade["suspect_page_rate"] == 0.3
    assert grade["severe_issue_count"] == 3      # sign_flip(2) + adoption_error(1)
    assert grade["mild_issue_count"] == 4        # prose_mismatch(3) + missing_prose(1)


def test_read_summary_distinguishes_mild_only_from_severe_document(tmp_path):
    # mild-only 文档 severe_issue_count==0,severe 文档 >0——batch 输出据此可区分,
    # 单页 mild issue 不得与重度问题同级显示。
    out_root = tmp_path / "out"

    pdf_mild = _make_pdf(tmp_path, 5, name="mild")
    layout_mild = resolve_layout(pdf_mild.stem, str(out_root))
    work_mild = Path(layout_mild.work_dir)
    for i in range(1, 6):
        _mark_page_done(work_mild, i)
    cp.save_manifest(str(work_mild), cp.new_manifest(
        str(pdf_mild), cp.pdf_fingerprint(str(pdf_mild)), 150, "B"))
    _write_selfcheck_with_audit(layout_mild, {
        "status": "SUSPECT", "suspect_pages": [1],
        "adoption": {"adopted": 3, "fallback_ocr": 2},
        "issue_counts": {"missing_prose": 1},
        "report": "mild_source_audit.json",
    })

    pdf_severe = _make_pdf(tmp_path, 5, name="severe")
    layout_severe = resolve_layout(pdf_severe.stem, str(out_root))
    work_severe = Path(layout_severe.work_dir)
    for i in range(1, 6):
        _mark_page_done(work_severe, i)
    cp.save_manifest(str(work_severe), cp.new_manifest(
        str(pdf_severe), cp.pdf_fingerprint(str(pdf_severe)), 150, "B"))
    _write_selfcheck_with_audit(layout_severe, {
        "status": "SUSPECT", "suspect_pages": [1],
        "adoption": {"adopted": 3, "fallback_ocr": 2},
        "issue_counts": {"decimal_shift": 1},
        "report": "severe_source_audit.json",
    })

    grade_mild = bp._read_summary(out_root, None, pdf_mild)["source_audit_grade"]
    grade_severe = bp._read_summary(out_root, None, pdf_severe)["source_audit_grade"]

    assert grade_mild["severe_issue_count"] == 0 and grade_mild["mild_issue_count"] == 1
    assert grade_severe["severe_issue_count"] == 1 and grade_severe["mild_issue_count"] == 0


def test_read_summary_still_reports_deferred_b_after_grading_added(tmp_path):
    # deferred B(defer 模式 marker)仍被 batch 正确报告(旧行为不受分级改动影响)。
    pdf = _make_pdf(tmp_path, 2, name="born")
    out_root = tmp_path / "out"
    marker_dir = out_root / "_deferred_born_digital"
    marker_dir.mkdir(parents=True)
    (marker_dir / "born.txt").write_text(str(pdf) + "\n", encoding="utf-8")

    summary = bp._read_summary(out_root, None, pdf)

    assert summary["status"] == "B"
    assert summary["route"] == "B"
    assert summary["selfcheck"] is None
    assert summary["source_audit_grade"] is None


def test_run_not_applicable_audit_keeps_a_route_output_unchanged(tmp_path, monkeypatch, capsys):
    # A 路(NOT_APPLICABLE)不影响 A 路既有 batch 行为(零回归):打印摘要行须与
    # 改动前逐字节一致,不得因为 selfcheck 里多了个 source_audit 字段就多出后缀。
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
        _write_selfcheck_with_audit(layout, {
            "status": "NOT_APPLICABLE", "suspect_pages": [],
            "adoption": {"adopted": 0, "fallback_ocr": 0},
            "issue_counts": {}, "report": "A_source_audit.json",
        })
        return 0

    monkeypatch.setattr(bp, "scan_katex_work_pages", lambda layout, out_path: {"errors": []})

    rc, results = bp.run([str(d)], out=str(out_root), dpi=150, runner=fake_runner)

    assert rc == 0
    assert results[0]["status"] == "OK"
    out = capsys.readouterr().out
    assert "  [OK] A — route=A failed_pages=0 coverage=1/1\n" in out
    assert "audit=" not in out


# ---------------------------------------------------------------------------
# Review fix(Important 1):selfcheck.json 缺失(--no-selfcheck-json)或其
# source_audit 字段缺失/结构损坏时,_read_summary 必须直接兜底读磁盘上的
# <stem>_source_audit.json(convert 主链独立管理,Task 9 保证总会写),不能因为
# selfcheck 没落盘/字段坏了就把"其实是 SUSPECT"漏报成 OK。
# ---------------------------------------------------------------------------

def _write_real_audit_report(layout, pdf, dpi, status, suspect_pages=None, issue_counts=None):
    fp = cp.pdf_fingerprint(str(pdf))
    report = {
        "schema_version": bp.AUDIT_SCHEMA_VERSION,
        "stem": layout.stem, "route": "B", "born_digital_mode": "hybrid",
        "pdf_fingerprint": {"size_bytes": fp["size_bytes"], "page_count": fp["page_count"]},
        "ocr_fingerprint": {"dpi": dpi, "page_count": fp["page_count"]},
        "threshold_profile": "route_b_v1_uncalibrated",
        "adoption_source": "recorded",
        "summary": {
            "status": status, "pages": fp["page_count"], "scorable_pages": fp["page_count"],
            "suspect_pages": suspect_pages or [],
            "adoption": {"prose_blocks": 0, "adopted": 0, "fallback_ocr": 0, "fallback_reasons": {}},
            "issue_counts": issue_counts or {},
        },
        "pages": [],
    }
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    with open(layout.source_audit_path, "w", encoding="utf-8") as f:
        json.dump(report, f)


def test_read_summary_falls_back_to_disk_audit_when_selfcheck_json_absent(tmp_path):
    # 模拟 --no-selfcheck-json:selfcheck.json 从不落盘,但 audit 报告仍由 convert
    # 主链独立写盘——batch 必须直接读磁盘兜底,不能漏报 SUSPECT。
    pdf = _make_pdf(tmp_path, 2, name="book")
    out_root = tmp_path / "out"
    layout = resolve_layout(pdf.stem, str(out_root))
    work = Path(layout.work_dir)
    _mark_page_done(work, 1)
    _mark_page_done(work, 2)
    cp.save_manifest(str(work),
                     cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "B"))
    _write_real_audit_report(layout, pdf, 150, "SUSPECT", suspect_pages=[2],
                             issue_counts={"prose_mismatch": 1})
    assert not os.path.exists(layout.selfcheck_path)      # 确认真的没有 selfcheck.json

    summary = bp._read_summary(out_root, None, pdf, dpi=150)

    assert summary["status"] == "SUSPECT"
    assert summary["source_audit_grade"]["suspect_page_count"] == 1
    assert summary["source_audit_grade"]["mild_issue_count"] == 1


def test_run_no_selfcheck_json_still_reports_audit_suspect(tmp_path, monkeypatch):
    # 端到端:--no-selfcheck-json 批跑 + audit 落盘 SUSPECT → batch 状态仍是 SUSPECT。
    d = tmp_path / "src"
    d.mkdir()
    pdf = _make_pdf(d, 2, name="book")
    out_root = tmp_path / "out"

    def fake_runner(argv):
        assert "--no-selfcheck-json" in argv
        layout = resolve_layout("book", str(out_root))
        work = Path(layout.work_dir)
        Path(layout.doc_deliverable_dir).mkdir(parents=True, exist_ok=True)
        Path(layout.md_path).write_text("# book\n", encoding="utf-8")
        _mark_page_done(work, 1)
        _mark_page_done(work, 2)
        cp.save_manifest(str(work),
                         cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "B"))
        _write_real_audit_report(layout, pdf, 150, "SUSPECT", suspect_pages=[2],
                                 issue_counts={"prose_mismatch": 1})
        # 真实 convert.py 在 --no-selfcheck-json 下不写 selfcheck.json,这里刻意不写。
        return 0

    monkeypatch.setattr(bp, "scan_katex_work_pages", lambda layout, out_path: {"errors": []})

    rc, results = bp.run([str(d)], out=str(out_root), dpi=150, runner=fake_runner,
                        no_selfcheck_json=True)

    assert results[0]["status"] == "SUSPECT"


def test_read_summary_falls_back_when_selfcheck_source_audit_field_corrupt(tmp_path):
    # selfcheck.json 存在,但其 source_audit 字段结构损坏(非 dict/缺关键键)——
    # 兜底读磁盘上的真实 audit 报告(SUSPECT),覆盖"corrupt 降级为 OK"这个边角。
    pdf = _make_pdf(tmp_path, 2, name="book")
    out_root = tmp_path / "out"
    layout = resolve_layout(pdf.stem, str(out_root))
    work = Path(layout.work_dir)
    _mark_page_done(work, 1)
    _mark_page_done(work, 2)
    cp.save_manifest(str(work),
                     cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "B"))
    _write_real_audit_report(layout, pdf, 150, "SUSPECT", suspect_pages=[1],
                             issue_counts={"sign_flip": 1})
    Path(layout.doc_work_dir).mkdir(parents=True, exist_ok=True)
    with open(layout.selfcheck_path, "w", encoding="utf-8") as f:
        json.dump({"in_md": 1, "total": 1, "source_audit": "CORRUPTED_NOT_A_DICT"}, f)

    summary = bp._read_summary(out_root, None, pdf, dpi=150)

    assert summary["status"] == "SUSPECT"
    assert summary["source_audit_grade"]["severe_issue_count"] == 1


# ---------------------------------------------------------------------------
# Review 顺手改进:rollup 汇总行单列 UNSCORABLE 计数,不再与"审计通过"共享 OK。
# ---------------------------------------------------------------------------

def test_run_rollup_line_lists_unscorable_separately(tmp_path, monkeypatch, capsys):
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
        # 不写 selfcheck.json、不写 audit 报告 → 兜底判 UNSCORABLE(audit_report_missing),
        # 文档状态仍是 OK(failed_pages 为空),但 rollup 不应把它计进"审计通过"的 OK 桶。
        return 0

    monkeypatch.setattr(bp, "scan_katex_work_pages", lambda layout, out_path: {"errors": []})

    rc, results = bp.run([str(d)], out=str(out_root), dpi=150, runner=fake_runner)

    assert results[0]["status"] == "OK"
    out = capsys.readouterr().out
    assert "批处理完成: 0 OK/B / 0 SUSPECT / 1 UNSCORABLE / 0 GIVEUP / 0 SKIP" in out


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


def test_main_forwards_born_digital_mode(monkeypatch):
    captured = {}

    def fake_run(src_paths, **kwargs):
        captured["kwargs"] = kwargs
        return 0, []

    monkeypatch.setattr(bp, "run", fake_run)
    monkeypatch.setattr("sys.argv", [
        "batch.py", "--src", "src", "--born-digital-mode", "hybrid",
    ])

    rc = bp.main()

    assert rc == 0
    assert captured["kwargs"]["born_digital_mode"] == "hybrid"


def test_main_born_digital_mode_defaults_to_hybrid(monkeypatch):
    captured = {}

    def fake_run(src_paths, **kwargs):
        captured["kwargs"] = kwargs
        return 0, []

    monkeypatch.setattr(bp, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["batch.py", "--src", "src"])

    rc = bp.main()

    assert rc == 0
    assert captured["kwargs"]["born_digital_mode"] == "hybrid"


def test_main_rejects_invalid_born_digital_mode(monkeypatch):
    monkeypatch.setattr("sys.argv", [
        "batch.py", "--src", "src", "--born-digital-mode", "bogus",
    ])
    with pytest.raises(SystemExit) as exc:
        bp.main()
    assert exc.value.code != 0


# ---------------------------------------------------------------------------
# Task B(2026-07-17 所有者批准):--formula-repair 三入口透传之一——batch 比较
# 特殊:batch 收尾本就已有自己一套 katex_scan+katex_triage 自动化(见上方
# test_run_invokes_katex_scan_by_default 等),早于本任务、默认打开。batch 的
# --formula-repair 默认取 "off"(与 convert.py/watchdog.py 默认 "deterministic"
# 刻意不同,取舍见 batch.py 顶部 FORMULA_REPAIR_MODES 注释与 task-B-report):
#   - 默认 "off":不透传给每本书的 convert.py 子进程,batch 沿用自己已有的收尾
#     自动化,零行为变化。
#   - 显式选 deterministic/agents:透传给子进程(该本书的 convert.py 会自己跑
#     katex_scan+triage+candidates/agents),此时 batch 自己的收尾 katex 步骤
#     必须让路(不管 --no-katex-scan 传的是什么),避免同一本书的 katex_scan/
#     katex_triage 被跑两遍。
# ---------------------------------------------------------------------------

def test_job_argv_passes_formula_repair_to_convert_subprocess(tmp_path):
    argv = bp._job_argv(tmp_path / "A.pdf", tmp_path / "out", None, 150,
                        no_selfcheck_json=False, formula_repair="agents")
    assert "--formula-repair" in argv
    assert argv[argv.index("--formula-repair") + 1] == "agents"


def test_job_argv_passes_agents_apply_to_convert_subprocess(tmp_path):
    argv = bp._job_argv(tmp_path / "A.pdf", tmp_path / "out", None, 150,
                        no_selfcheck_json=False, formula_repair="agents-apply")
    assert argv[argv.index("--formula-repair") + 1] == "agents-apply"


def test_job_argv_defaults_formula_repair_to_off(tmp_path):
    argv = bp._job_argv(tmp_path / "A.pdf", tmp_path / "out", None, 150,
                        no_selfcheck_json=False)
    assert argv[argv.index("--formula-repair") + 1] == "off"


def test_run_rejects_invalid_formula_repair(tmp_path):
    with pytest.raises(ValueError, match="formula_repair"):
        bp.run([str(tmp_path)], out=str(tmp_path / "out"), formula_repair="bogus")


def _fake_runner_writes_book(out_root, pdf):
    def fake_runner(argv):
        layout = resolve_layout(pdf.stem, str(out_root))
        work = Path(layout.work_dir)
        Path(layout.doc_deliverable_dir).mkdir(parents=True, exist_ok=True)
        Path(layout.md_path).write_text(f"# {pdf.stem}\n", encoding="utf-8")
        _mark_page_done(work, 1)
        cp.save_manifest(str(work),
                         cp.new_manifest(str(pdf), cp.pdf_fingerprint(str(pdf)), 150, "A"))
        return 0
    return fake_runner


def test_run_default_formula_repair_off_forwards_off_and_keeps_own_katex_tail(
        tmp_path, monkeypatch):
    # 默认(formula_repair="off")行为零变化:argv 里显式带 --formula-repair off,
    # 且 batch 自己的 katex_scan 收尾照常跑(既有回归,重新断言一次防漂移)。
    d = tmp_path / "src"
    d.mkdir()
    pdf = _make_pdf(d, 1, name="A")
    out_root = tmp_path / "out"
    fake_runner = _fake_runner_writes_book(out_root, pdf)
    scan_calls = []
    monkeypatch.setattr(bp, "scan_katex_work_pages",
                        lambda layout, out_path: (scan_calls.append(layout.stem)
                                                  or {"errors": []}))

    rc, results = bp.run([str(d)], out=str(out_root), dpi=150, runner=fake_runner)

    assert rc == 0
    assert scan_calls == ["A"]


def test_run_formula_repair_deterministic_forwards_and_skips_own_katex_tail(
        tmp_path, monkeypatch):
    # 不双跑:formula_repair="deterministic" 转发给子进程(该本书 convert.py
    # 自己会跑 katex_scan+triage),batch 自己的收尾 katex_scan 不得再跑一遍——
    # 即便 katex_scan_enabled 仍是默认 True。
    d = tmp_path / "src"
    d.mkdir()
    pdf = _make_pdf(d, 1, name="A")
    out_root = tmp_path / "out"
    captured_argv = {}

    def fake_runner(argv):
        captured_argv["argv"] = argv
        return _fake_runner_writes_book(out_root, pdf)(argv)

    scan_calls = []
    monkeypatch.setattr(bp, "scan_katex_work_pages",
                        lambda layout, out_path: scan_calls.append(layout.stem))

    rc, results = bp.run([str(d)], out=str(out_root), dpi=150, runner=fake_runner,
                         formula_repair="deterministic")

    assert rc == 0
    argv = captured_argv["argv"]
    assert argv[argv.index("--formula-repair") + 1] == "deterministic"
    assert scan_calls == []            # batch 自己的收尾 katex 步骤已让路,未被调用


def test_run_formula_repair_agents_also_skips_own_katex_tail(tmp_path, monkeypatch):
    d = tmp_path / "src"
    d.mkdir()
    pdf = _make_pdf(d, 1, name="A")
    out_root = tmp_path / "out"
    fake_runner = _fake_runner_writes_book(out_root, pdf)
    scan_calls = []
    monkeypatch.setattr(bp, "scan_katex_work_pages",
                        lambda layout, out_path: scan_calls.append(layout.stem))

    rc, results = bp.run([str(d)], out=str(out_root), dpi=150, runner=fake_runner,
                         formula_repair="agents", katex_scan_enabled=True)

    assert rc == 0
    assert scan_calls == []


def test_run_formula_repair_agents_apply_forwards_and_skips_own_katex_tail(
        tmp_path, monkeypatch):
    d = tmp_path / "src"
    d.mkdir()
    pdf = _make_pdf(d, 1, name="A")
    out_root = tmp_path / "out"
    captured = {}

    def fake_runner(argv):
        captured["argv"] = argv
        return _fake_runner_writes_book(out_root, pdf)(argv)

    scan_calls = []
    monkeypatch.setattr(bp, "scan_katex_work_pages",
                        lambda layout, out_path: scan_calls.append(layout.stem))

    rc, results = bp.run([str(d)], out=str(out_root), dpi=150, runner=fake_runner,
                         formula_repair="agents-apply", katex_scan_enabled=True)

    assert rc == 0
    argv = captured["argv"]
    assert argv[argv.index("--formula-repair") + 1] == "agents-apply"
    assert scan_calls == []


def test_main_forwards_formula_repair(monkeypatch):
    captured = {}

    def fake_run(src_paths, **kwargs):
        captured["kwargs"] = kwargs
        return 0, []

    monkeypatch.setattr(bp, "run", fake_run)
    monkeypatch.setattr("sys.argv", [
        "batch.py", "--src", "src", "--formula-repair", "deterministic",
    ])

    rc = bp.main()

    assert rc == 0
    assert captured["kwargs"]["formula_repair"] == "deterministic"


def test_main_forwards_agents_apply(monkeypatch):
    captured = {}

    def fake_run(src_paths, **kwargs):
        captured["kwargs"] = kwargs
        return 0, []

    monkeypatch.setattr(bp, "run", fake_run)
    monkeypatch.setattr("sys.argv", [
        "batch.py", "--src", "src", "--formula-repair", "agents-apply",
    ])

    rc = bp.main()

    assert rc == 0
    assert captured["kwargs"]["formula_repair"] == "agents-apply"


def test_main_formula_repair_defaults_to_off(monkeypatch):
    captured = {}

    def fake_run(src_paths, **kwargs):
        captured["kwargs"] = kwargs
        return 0, []

    monkeypatch.setattr(bp, "run", fake_run)
    monkeypatch.setattr("sys.argv", ["batch.py", "--src", "src"])

    rc = bp.main()

    assert rc == 0
    assert captured["kwargs"]["formula_repair"] == "off"


def test_main_rejects_invalid_formula_repair(monkeypatch):
    monkeypatch.setattr("sys.argv", [
        "batch.py", "--src", "src", "--formula-repair", "bogus",
    ])
    with pytest.raises(SystemExit) as exc:
        bp.main()
    assert exc.value.code != 0
