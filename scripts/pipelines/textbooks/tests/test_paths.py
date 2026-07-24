import os
from scripts.pipelines.textbooks.paths import DocLayout, resolve_layout


def test_work_root_defaults_to_work_root_subdir_of_deliverables():
    lay = resolve_layout("Book", "/out")
    assert lay.work_root == os.path.join("/out", "_work_root")


def test_explicit_work_root_overrides_default():
    lay = resolve_layout("Book", "/out", "/scratch")
    assert lay.work_root == "/scratch"


def test_deliverable_side_paths():
    lay = resolve_layout("Book", "/out", "/scratch")
    assert lay.doc_deliverable_dir == os.path.join("/out", "Book")
    assert lay.md_path == os.path.join("/out", "Book", "Book.md")
    assert lay.assets_dir == os.path.join("/out", "Book", "Book.assets")


def test_process_side_paths():
    lay = resolve_layout("Book", "/out", "/scratch")
    assert lay.doc_work_dir == os.path.join("/scratch", "Book")
    assert lay.work_dir == os.path.join("/scratch", "Book", "_work")
    assert lay.repair_dir == os.path.join("/scratch", "Book", "Book_repair")
    assert lay.worklist_path == os.path.join(
        "/scratch", "Book", "Book_repair", "worklist.json")
    assert lay.render_errors_path == os.path.join(
        "/scratch", "Book", "Book_render_errors.json")
    assert lay.corrections_path == os.path.join(
        "/scratch", "Book", "Book_corrections.json")
    assert lay.selfcheck_path == os.path.join(
        "/scratch", "Book", "Book_selfcheck.json")
    assert lay.debug_html_path == os.path.join(
        "/scratch", "Book", "Book_debug.html")
    assert lay.formula_candidates_path == os.path.join(
        "/scratch", "Book", "Book_repair", "formula_candidates.jsonl")
    assert lay.formula_candidates_summary_path == os.path.join(
        "/scratch", "Book", "Book_repair", "formula_candidates_summary.json")
    assert lay.quality_repair_dir == os.path.join(
        "/scratch", "Book", "_quality_repair")


def test_debug_html_is_process_side_not_deliverable():
    lay = resolve_layout("Book", "/out", "/scratch")
    assert lay.debug_html_path.startswith(os.path.join("/scratch", "Book"))
    assert "/out" not in lay.debug_html_path.replace("\\", "/")


def test_source_audit_path_is_process_side_json():
    lay = resolve_layout("Book", "/out", "/scratch")
    assert lay.source_audit_path == os.path.join(
        "/scratch", "Book", "Book_source_audit.json")
    assert "/out" not in lay.source_audit_path.replace("\\", "/")
