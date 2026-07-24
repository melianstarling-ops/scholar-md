from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

from scripts.pipelines.textbooks.quality_repair import engine
from scripts.pipelines.textbooks.quality_repair import cli as cli_module
from scripts.pipelines.textbooks.quality_repair.cli import build_parser, main
from scripts.pipelines.textbooks.quality_repair.models import Finding, Severity
from scripts.pipelines.textbooks.document_lock import DocumentLock


def _cli_document(tmp_path, text="alpha\n"):
    deliverables = tmp_path / "out"
    work_root = tmp_path / "work"
    doc = deliverables / "Demo"
    doc.mkdir(parents=True)
    (doc / "Demo.md").write_text(text, encoding="utf-8")
    work = work_root / "Demo" / "_work"
    work.mkdir(parents=True)
    (work / "manifest.json").write_text(json.dumps({
        "fingerprint": {"page_count": 1}, "failed_pages": []}), encoding="utf-8")
    (work / "page_0001_res.json").write_text(json.dumps({
        "parsing_res_list": [{"block_order": 1, "block_content": "alpha"}]}),
        encoding="utf-8")
    return deliverables, work_root, doc / "Demo.md"


def _apply_result(context, *, finding, applied):
    findings = () if finding is None else (finding,)
    return SimpleNamespace(
        proposal_run=SimpleNamespace(
            summary=SimpleNamespace(
                finding_count=1, report_dir=str(context.run_dir)),
            patch_plan=SimpleNamespace(conflicts=()),
            findings=findings,
            event_batch=SimpleNamespace(events=()),
        ),
        transaction=SimpleNamespace(
            applied=applied, rolled_back=False,
            reason="" if applied else "empty patch plan"),
        after_findings=findings,
    )


def test_cli_conflicting_document_lock_returns_one_without_artifacts(
        tmp_path, capsys):
    deliverables, work_root, md = _cli_document(tmp_path)
    doc_work = work_root / "Demo"
    before = hashlib.sha256(md.read_bytes()).hexdigest()

    with DocumentLock(doc_work, run_id="convert-running"):
        rc = main([
            "--stem", "Demo",
            "--deliverables-root", str(deliverables),
            "--work-root", str(work_root),
            "--mode", "apply",
            "--run-id", "must-not-start",
        ])

    captured = capsys.readouterr()
    assert rc == 1
    assert "document locked" in captured.err
    assert "convert-running" in captured.err
    assert hashlib.sha256(md.read_bytes()).hexdigest() == before
    assert not (doc_work / "_quality_repair").exists()


def test_cli_audit_uses_explicit_paths_and_never_requires_agent(tmp_path):
    deliverables = tmp_path / "out"
    work_root = tmp_path / "work"
    doc = deliverables / "Demo"
    doc.mkdir(parents=True)
    (doc / "Demo.md").write_text("alpha\n", encoding="utf-8")
    work = work_root / "Demo" / "_work"
    work.mkdir(parents=True)
    (work / "manifest.json").write_text(json.dumps({
        "fingerprint": {"page_count": 1}, "failed_pages": []}), encoding="utf-8")
    (work / "page_0001_res.json").write_text(json.dumps({
        "parsing_res_list": [{"block_order": 1, "block_content": "alpha"}]}), encoding="utf-8")

    rc = main(["--stem", "Demo", "--deliverables-root", str(deliverables),
               "--work-root", str(work_root), "--mode", "audit",
               "--run-id", "fixed", "--agent-workers", "8",
               "--max-rounds", "5"])

    assert rc == 0
    assert (work_root / "Demo" / "_quality_repair" / "fixed" / "summary.json").is_file()
    assert not (work_root / "Demo" / "_quality_repair"
                / "fixed" / "round-01").exists()


def test_cli_accepts_repeated_explicit_agents_in_order():
    args = build_parser().parse_args([
        "--stem", "Demo", "--deliverables-root", "out", "--mode", "propose",
        "--agent", "codex:gpt-5.6-sol:high",
        "--agent", "gemini:Gemini-3.1-Pro:high",
    ])
    assert args.agent == ["codex:gpt-5.6-sol:high", "gemini:Gemini-3.1-Pro:high"]


def test_cli_agent_workers_and_max_rounds_defaults_and_overrides():
    defaults = build_parser().parse_args([
        "--stem", "Demo", "--deliverables-root", "out",
    ])
    explicit = build_parser().parse_args([
        "--stem", "Demo", "--deliverables-root", "out",
        "--agent-workers", "7", "--max-rounds", "3",
        "--max-agent-items", "11",
    ])

    assert defaults.agent_workers == 4
    assert defaults.max_rounds == 1
    assert explicit.agent_workers == 7
    assert explicit.max_rounds == 3
    assert explicit.max_agent_items == 11


def test_cli_propose_writes_patch_plan_without_changing_markdown(tmp_path):
    deliverables = tmp_path / "out"
    work_root = tmp_path / "work"
    doc = deliverables / "Demo"
    doc.mkdir(parents=True)
    md = doc / "Demo.md"
    md.write_text("$ x + 1 $\n", encoding="utf-8")
    before = hashlib.sha256(md.read_bytes()).hexdigest()
    work = work_root / "Demo" / "_work"
    work.mkdir(parents=True)
    (work / "manifest.json").write_text(json.dumps({
        "fingerprint": {"page_count": 1}, "failed_pages": []}), encoding="utf-8")
    (work / "page_0001_res.json").write_text(json.dumps({
        "parsing_res_list": [{"block_order": 1, "block_content": "x + 1"}]}),
        encoding="utf-8")

    rc = main(["--stem", "Demo", "--deliverables-root", str(deliverables),
               "--work-root", str(work_root), "--mode", "propose",
               "--run-id", "proposal"])

    assert rc == 2
    run = work_root / "Demo" / "_quality_repair" / "proposal"
    assert (run / "patch_plan.json").is_file()
    assert hashlib.sha256(md.read_bytes()).hexdigest() == before


def test_cli_propose_forwards_workers_but_ignores_max_rounds(
        tmp_path, monkeypatch):
    deliverables, work_root, _md = _cli_document(tmp_path)
    calls = []

    def fake_propose(context, **kwargs):
        calls.append((context.run_dir.name, kwargs["agent_workers"]))
        return SimpleNamespace(
            summary=SimpleNamespace(
                stem="Demo", status="OK", finding_count=0,
                report_dir=str(context.run_dir)),
            patch_plan=SimpleNamespace(conflicts=()),
        )

    monkeypatch.setattr(cli_module, "propose_document", fake_propose)
    rc = main([
        "--stem", "Demo", "--deliverables-root", str(deliverables),
        "--work-root", str(work_root), "--mode", "propose",
        "--run-id", "proposal-once", "--agent-workers", "6",
        "--max-rounds", "9",
    ])

    assert rc == 0
    assert calls == [("proposal-once", 6)]


def test_cli_apply_reaudits_for_two_rounds_and_forwards_workers(
        tmp_path, monkeypatch):
    deliverables, work_root, md = _cli_document(tmp_path)
    finding = Finding.create(
        capability="test", kind="remaining", severity=Severity.P1,
        message="one more round", page=1)
    calls = []

    def fake_apply(context, **kwargs):
        calls.append((context.run_dir.name, kwargs["agent_workers"],
                      kwargs["max_agent_items"]))
        md.write_text(
            md.read_text(encoding="utf-8") + f"round-{len(calls)}\n",
            encoding="utf-8")
        return _apply_result(
            context, finding=finding if len(calls) == 1 else None, applied=1)

    monkeypatch.setattr(engine, "apply_document", fake_apply)
    rc = main([
        "--stem", "Demo", "--deliverables-root", str(deliverables),
        "--work-root", str(work_root), "--mode", "apply",
        "--run-id", "multi", "--agent-workers", "3",
        "--max-rounds", "4", "--max-agent-items", "9",
    ])

    assert rc == 0
    assert calls == [("round-01", 3, 9), ("round-02", 3, 9)]


def test_cli_apply_stops_after_one_no_progress_round(tmp_path, monkeypatch):
    deliverables, work_root, _md = _cli_document(tmp_path)
    finding = Finding.create(
        capability="test", kind="remaining", severity=Severity.P1,
        message="cannot safely repair", page=1)
    calls = []

    def fake_apply(context, **_kwargs):
        calls.append(context.run_dir.name)
        return _apply_result(context, finding=finding, applied=0)

    monkeypatch.setattr(engine, "apply_document", fake_apply)
    rc = main([
        "--stem", "Demo", "--deliverables-root", str(deliverables),
        "--work-root", str(work_root), "--mode", "apply",
        "--run-id", "stalled", "--max-rounds", "5",
    ])

    assert rc == 2
    assert calls == ["round-01"]


def test_cli_audit_returns_two_when_findings_remain(tmp_path):
    deliverables, work_root, _md = _cli_document(tmp_path, "$ x + 1 $\n")

    rc = main([
        "--stem", "Demo", "--deliverables-root", str(deliverables),
        "--work-root", str(work_root), "--mode", "audit",
        "--run-id", "audit-findings",
    ])

    assert rc == 2


def test_cli_internal_failure_returns_one(tmp_path, monkeypatch):
    deliverables, work_root, _md = _cli_document(tmp_path)

    def fail(*_args, **_kwargs):
        raise RuntimeError("simulated internal failure")

    monkeypatch.setattr(cli_module, "auto_apply", fail)
    rc = main([
        "--stem", "Demo", "--deliverables-root", str(deliverables),
        "--work-root", str(work_root), "--mode", "apply",
        "--run-id", "internal-failure",
    ])

    assert rc == 1
