from __future__ import annotations

import json

from ..models import DetectorContext, Finding, Severity
from ..formula_state import unresolved_formula_candidates
from ._shared import read_json


def _read_jsonl(path) -> list[dict]:
    out: list[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return out
    for line in lines:
        try:
            item = json.loads(line)
        except ValueError:
            continue
        if isinstance(item, dict):
            out.append(item)
    return out


def detect_formulas(context: DetectorContext) -> list[Finding]:
    report = read_json(context.selfcheck_path)
    findings: list[Finding] = []
    for command in report.get("katex_incompat") or []:
        findings.append(Finding.create(
            capability="formulas", kind="katex_incompatible_command",
            severity=Severity.P1, message="最终 Markdown 含 KaTeX 不兼容命令",
            evidence={"command": command},
        ))
    for suspicion in report.get("formula_suspicions") or []:
        if not isinstance(suspicion, dict):
            continue
        findings.append(Finding.create(
            capability="formulas", kind="formula_suspicion",
            severity=Severity.P2, message="既有公式启发式报告疑似识别问题",
            evidence=dict(suspicion),
        ))
    render = read_json(context.doc_work_dir / f"{context.stem}_render_errors.json")
    errors = [item for item in (render.get("errors") or []) if isinstance(item, dict)]
    if errors:
        findings.append(Finding.create(
            capability="formulas", kind="katex_render_errors",
            severity=Severity.P1, message="既有 KaTeX 扫描仍有渲染错误",
            evidence={"count": len(errors), "samples": errors[:10]},
        ))
    candidates = _read_jsonl(
        context.doc_work_dir / f"{context.stem}_repair" / "formula_candidates.jsonl")
    pending_candidates, _states = unresolved_formula_candidates(context, candidates)
    if pending_candidates:
        findings.append(Finding.create(
            capability="formulas", kind="formula_candidates_pending",
            severity=Severity.P2, message="既有公式候选漏斗仍有待核项目",
            evidence={"count": len(pending_candidates),
                      "terminal_count": len(candidates) - len(pending_candidates),
                      "samples": pending_candidates[:10]},
        ))
    return findings
