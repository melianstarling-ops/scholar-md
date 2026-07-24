"""Independent quality-repair CLI. Initial production path is audit-only."""
from __future__ import annotations

import argparse
import sys
import uuid
from datetime import datetime
from pathlib import Path

from scripts.pipelines.textbooks.document_lock import (
    DocumentLock,
    DocumentLockedError,
)
from scripts.pipelines.textbooks.paths import resolve_layout

from .agents import AgentSpec
from .detectors.assets import detect_assets
from .detectors.final_delimiters import detect_final_delimiters
from .detectors.formulas import detect_formulas
from .detectors.novel_discovery import detect_novel_signals
from .detectors.page_completeness import detect_page_completeness
from .detectors.unordered_blocks import detect_unordered_blocks
from .engine import (
    DEFAULT_MAX_AGENT_ITEMS,
    audit_document,
    auto_apply,
    propose_document,
)
from .gates import build_default_gates
from .models import DetectorContext
from .registry import Capability, Registry


_CAPABILITIES = {
    "assets": detect_assets,
    "final_delimiters": detect_final_delimiters,
    "formulas": detect_formulas,
    "novel_discovery": detect_novel_signals,
    "page_completeness": detect_page_completeness,
    "unordered_blocks": detect_unordered_blocks,
}


def default_registry(*, discovery: str = "signals",
                     only: list[str] | None = None) -> Registry:
    names = sorted(_CAPABILITIES)
    if discovery == "off":
        names.remove("novel_discovery")
    if only:
        selected = set(only)
        names = [name for name in names if name in selected]
    return Registry([
        Capability(name, _CAPABILITIES[name]) for name in names
    ])


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="textbooks 转换后质量审计/修复")
    parser.add_argument("--stem", required=True)
    parser.add_argument("--deliverables-root", required=True)
    parser.add_argument("--work-root", default=None)
    parser.add_argument("--mode", choices=("audit", "propose", "apply"), default="audit")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--discovery", choices=("off", "signals"), default="signals")
    parser.add_argument("--only", action="append", choices=tuple(sorted(_CAPABILITIES)))
    parser.add_argument("--agent", action="append", default=[],
                        help="显式 provider:model:effort；可重复，顺序即 fallback")
    parser.add_argument("--agent-timeout", type=int, default=300)
    parser.add_argument("--max-agent-items", type=int, default=DEFAULT_MAX_AGENT_ITEMS)
    parser.add_argument("--agent-workers", type=int, default=4)
    parser.add_argument("--max-rounds", type=int, default=1)
    parser.add_argument("--learn", choices=("off", "package"), default="off")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.run_id is None:
        args.run_id = (
            datetime.now().strftime("%Y%m%dT%H%M%S")
            + f"-{uuid.uuid4().hex[:8]}"
        )
    layout = resolve_layout(args.stem, args.deliverables_root, args.work_root)
    try:
        with DocumentLock(
            layout.doc_work_dir,
            run_id=args.run_id,
            metadata={
                "operation": "quality_repair",
                "mode": args.mode,
            },
        ):
            return _run(args)
    except DocumentLockedError as exc:
        print(f"[quality_repair] document locked: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # noqa: BLE001 CLI boundary: internal failure is exit 1
        print(
            f"[quality_repair] internal error: {type(exc).__name__}: {exc}",
            file=sys.stderr,
        )
        return 1


def _run(args: argparse.Namespace) -> int:
    layout = resolve_layout(args.stem, args.deliverables_root, args.work_root)
    run_id = args.run_id or (
        datetime.now().strftime("%Y%m%dT%H%M%S")
        + f"-{uuid.uuid4().hex[:8]}"
    )
    run_dir = Path(layout.quality_repair_dir) / run_id
    context = DetectorContext.from_paths(
        stem=args.stem, md_path=layout.md_path,
        work_dir=layout.work_dir, run_dir=run_dir,
    )
    registry = default_registry(discovery=args.discovery, only=args.only)
    specs = [AgentSpec.parse(value) for value in args.agent]
    if (args.agent_timeout <= 0 or args.max_agent_items <= 0
            or args.agent_workers <= 0 or args.max_rounds <= 0):
        raise ValueError(
            "agent-timeout, max-agent-items, agent-workers and max-rounds "
            "must be positive")
    if args.mode == "audit":
        summary = audit_document(context, registry=registry)
        stem = summary.stem
        status = summary.status
        finding_count = summary.finding_count
        report_dir = summary.report_dir
        applied = 0
        blocked = summary.finding_count > 0 or summary.status != "OK"
    elif args.mode == "propose":
        result = propose_document(
            context, registry=registry, agent_specs=specs,
            agent_timeout=args.agent_timeout, learn=args.learn,
            max_agent_items=args.max_agent_items,
            agent_workers=args.agent_workers)
        summary = result.summary
        stem = summary.stem
        status = summary.status
        finding_count = summary.finding_count
        report_dir = summary.report_dir
        applied = 0
        blocked = bool(
            summary.finding_count > 0
            or summary.status != "OK"
            or result.patch_plan.conflicts
        )
    else:
        result = auto_apply(
            context,
            registry=registry,
            agent_specs=specs,
            agent_timeout=args.agent_timeout,
            learn=args.learn,
            max_agent_items=args.max_agent_items,
            agent_workers=args.agent_workers,
            max_rounds=args.max_rounds,
            gate_factory=lambda round_context: build_default_gates(
                round_context.md_path, round_context.run_dir),
        )
        stem = args.stem
        status = result["status"]
        finding_count = result["findings"]
        report_dir = result["report_dir"]
        applied = result["applied"]
        blocked = bool(
            status != "OK"
            or result["rolled_back"]
            or result["conflicts"]
            or (result["reason"]
                and result["reason"] != "empty patch plan")
        )
    print(f"[quality_repair] {stem}: {status} mode={args.mode} "
          f"findings={finding_count} applied={applied} report={report_dir}")
    return 2 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
