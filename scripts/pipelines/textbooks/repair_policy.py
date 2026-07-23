"""Shared CLI policy for the textbooks automatic repair pipeline.

This module is intentionally orchestration-free.  ``convert``, ``batch`` and
``watchdog`` can share one parser contract and one compatibility resolver
without importing each other or the repair engines.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
from enum import IntEnum
from typing import Iterable


REPAIR_MODES = ("auto", "off")
FORMULA_REPAIR_MODES = ("deterministic", "agents", "agents-apply", "off")
QUALITY_REPAIR_MODES = ("off", "audit", "propose", "apply")

DEFAULT_REPAIR_MODE = "auto"
DEFAULT_REPAIR_WORKERS = 4
DEFAULT_REPAIR_MAX_ROUNDS = 2

SEVERE_SOURCE_AUDIT_CODES = frozenset({
    "adoption_error", "audit_error",
    "numeric_mismatch", "numeric_missing",
    "sign_flip", "decimal_shift", "exponent_change",
    "severe_prose_degradation",
})
INTEGRITY_SOURCE_AUDIT_CODES = frozenset({
    "adoption_error", "audit_error",
})


class CompletionStatus(IntEnum):
    """Process exit contract shared by convert, watchdog and batch."""

    OK = 0
    FAILED = 1
    SUSPECT = 2


_STATUS_PRIORITY = {
    CompletionStatus.OK: 0,
    CompletionStatus.SUSPECT: 1,
    CompletionStatus.FAILED: 2,
}


def combine_completion_statuses(
        statuses: Iterable[CompletionStatus | int]) -> CompletionStatus:
    """Return the batch aggregate using FAILED > SUSPECT > OK precedence."""
    resolved = [CompletionStatus(value) for value in statuses]
    return max(resolved, key=_STATUS_PRIORITY.__getitem__,
               default=CompletionStatus.OK)


def quality_final_is_conclusive(result: dict | None) -> bool:
    """Whether quality apply proves the current final Markdown is closed."""
    data = result or {}
    stop_reason = str(data.get("stop_reason") or "")
    return (
        data.get("mode") in {"apply", "auto"}
        and data.get("status") == "OK"
        and int(data.get("findings") or 0) == 0
        and not data.get("rolled_back")
        and int(data.get("conflicts") or 0) == 0
        and not data.get("unresolved")
        and not data.get("unresolved_events")
        and int(data.get("unresolved_count") or 0) == 0
        and int(data.get("unresolved_event_count") or 0) == 0
        and stop_reason in {"", "resolved"}
        and str(data.get("reason") or "") in {"", "empty patch plan"}
    )


def source_audit_blocks_completion(
        source_audit: dict | None, quality_result: dict | None) -> bool:
    """Keep source-grounded failures closed only by source-grounded evidence.

    The generic quality pass validates the current Markdown, but does not
    compare numeric content with the source PDF.  Its clean result therefore
    cannot clear a severe source-audit finding.  ``quality_result`` remains in
    the signature for the shared caller contract and a future source-audit
    specific closure record.
    """
    if not source_audit:
        return False
    summary = source_audit.get("summary") or source_audit
    status = summary.get("status")
    issue_counts = summary.get("issue_counts") or {}
    if status == "UNSCORABLE":
        return True
    if any(int(issue_counts.get(code) or 0) > 0
           for code in INTEGRITY_SOURCE_AUDIT_CODES):
        return True
    severe = any(int(issue_counts.get(code) or 0) > 0
                 for code in SEVERE_SOURCE_AUDIT_CODES)
    return bool(status == "SUSPECT" and severe)


@dataclass(frozen=True)
class AgentSpec:
    """One explicitly selected external Agent."""

    provider: str
    model: str
    effort: str

    @classmethod
    def parse(cls, value: str) -> "AgentSpec":
        parts = value.split(":")
        if len(parts) != 3 or not all(part.strip() for part in parts):
            raise ValueError("agent must be provider:model:effort")
        return cls(*(part.strip() for part in parts))

    def to_cli(self) -> str:
        return f"{self.provider}:{self.model}:{self.effort}"


@dataclass(frozen=True)
class RepairPolicy:
    """Resolved high-level and legacy-compatible repair configuration.

    ``formula_mode``/``quality_mode`` are ``auto`` unless an explicit legacy
    stage flag overrides that stage.  An explicit legacy ``--quality-agent``
    overrides the unified Agent chain for quality only.  The formula stage
    keeps the unified chain because no legacy formula-model flag existed.
    """

    mode: str
    formula_mode: str
    quality_mode: str
    formula_agents: tuple[AgentSpec, ...]
    quality_agents: tuple[AgentSpec, ...]
    workers: int
    max_rounds: int
    legacy_formula_explicit: bool
    legacy_quality_explicit: bool
    legacy_quality_agents_explicit: bool
    use_legacy_formula_chain: bool

    @property
    def runtime_formula_repair(self) -> str:
        """Map high-level auto to the existing formula-repair runtime mode."""
        if self.formula_mode != "auto":
            return self.formula_mode
        return "agents-apply" if self.formula_agents else "deterministic"

    @property
    def runtime_quality_repair(self) -> str:
        """Map high-level auto to the existing quality-repair runtime mode."""
        return "apply" if self.quality_mode == "auto" else self.quality_mode

    @property
    def runtime_quality_agents(self) -> tuple[str, ...]:
        """Return the existing quality runtime's string-shaped Agent chain."""
        return tuple(spec.to_cli() for spec in self.quality_agents)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError) as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be greater than zero")
    return parsed


def _agent_value(value: str) -> str:
    try:
        return AgentSpec.parse(value).to_cli()
    except ValueError as exc:
        raise argparse.ArgumentTypeError(str(exc)) from exc


def add_repair_policy_arguments(
        parser: argparse.ArgumentParser, *, include_legacy: bool = True) -> None:
    """Add the shared automatic-repair arguments to one entry-point parser.

    Legacy arguments default to ``None`` rather than their historical runtime
    defaults.  This preserves whether the user explicitly supplied a legacy
    override; :func:`resolve_repair_policy` supplies the new defaults.
    """
    parser.add_argument(
        "--repair", choices=REPAIR_MODES, default=DEFAULT_REPAIR_MODE,
        help="自动修复闭环:auto(默认)/off")
    parser.add_argument(
        "--repair-agent", action="append", default=[], type=_agent_value,
        metavar="PROVIDER:MODEL:EFFORT",
        help="显式 Agent；可重复，顺序即单项 fallback 顺序")
    parser.add_argument(
        "--repair-workers", type=_positive_int, default=DEFAULT_REPAIR_WORKERS,
        metavar="N", help=f"并行 Agent 任务数(默认 {DEFAULT_REPAIR_WORKERS})")
    parser.add_argument(
        "--repair-max-rounds", type=_positive_int,
        default=DEFAULT_REPAIR_MAX_ROUNDS, metavar="N",
        help=f"自动修复最大轮数(默认 {DEFAULT_REPAIR_MAX_ROUNDS})")
    if not include_legacy:
        return
    parser.add_argument(
        "--formula-repair", choices=FORMULA_REPAIR_MODES, default=None,
        help="兼容旧参数；显式值仅覆盖 auto 的公式阶段")
    parser.add_argument(
        "--quality-repair", choices=QUALITY_REPAIR_MODES, default=None,
        help="兼容旧参数；显式值仅覆盖 auto 的通用质量阶段")
    parser.add_argument(
        "--quality-agent", action="append", default=None, type=_agent_value,
        metavar="PROVIDER:MODEL:EFFORT",
        help="兼容旧参数；显式链仅覆盖通用质量阶段")


def resolve_repair_policy(
        *, repair: str = DEFAULT_REPAIR_MODE,
        repair_agents: Iterable[str | AgentSpec] = (),
        workers: int = DEFAULT_REPAIR_WORKERS,
        max_rounds: int = DEFAULT_REPAIR_MAX_ROUNDS,
        formula_repair: str | None = None,
        quality_repair: str | None = None,
        quality_agents: Iterable[str | AgentSpec] | None = None) -> RepairPolicy:
    """Resolve new defaults and explicit legacy stage overrides."""
    if repair not in REPAIR_MODES:
        raise ValueError(f"repair must be one of {REPAIR_MODES}, got {repair!r}")
    if formula_repair is not None and formula_repair not in FORMULA_REPAIR_MODES:
        raise ValueError(
            f"formula_repair must be one of {FORMULA_REPAIR_MODES}, "
            f"got {formula_repair!r}")
    if quality_repair is not None and quality_repair not in QUALITY_REPAIR_MODES:
        raise ValueError(
            f"quality_repair must be one of {QUALITY_REPAIR_MODES}, "
            f"got {quality_repair!r}")
    if not isinstance(workers, int) or isinstance(workers, bool) or workers <= 0:
        raise ValueError("workers must be a positive integer")
    if (not isinstance(max_rounds, int) or isinstance(max_rounds, bool)
            or max_rounds <= 0):
        raise ValueError("max_rounds must be a positive integer")

    def parse_many(values: Iterable[str | AgentSpec]) -> tuple[AgentSpec, ...]:
        return tuple(value if isinstance(value, AgentSpec) else AgentSpec.parse(value)
                     for value in values)

    unified_agents = parse_many(repair_agents)
    quality_agents_explicit = quality_agents is not None
    resolved_quality_agents = (
        parse_many(quality_agents or ()) if quality_agents_explicit
        else unified_agents
    )
    stage_default = "auto" if repair == "auto" else "off"
    formula_mode = formula_repair if formula_repair is not None else stage_default
    quality_mode = quality_repair if quality_repair is not None else stage_default
    use_legacy_formula_chain = (
        formula_repair in {"agents", "agents-apply"} and not unified_agents
    )
    return RepairPolicy(
        mode=repair,
        formula_mode=formula_mode,
        quality_mode=quality_mode,
        formula_agents=unified_agents,
        quality_agents=resolved_quality_agents,
        workers=workers,
        max_rounds=max_rounds,
        legacy_formula_explicit=formula_repair is not None,
        legacy_quality_explicit=quality_repair is not None,
        legacy_quality_agents_explicit=quality_agents_explicit,
        use_legacy_formula_chain=use_legacy_formula_chain,
    )


def repair_policy_from_namespace(namespace: argparse.Namespace) -> RepairPolicy:
    """Resolve a namespace produced by :func:`add_repair_policy_arguments`."""
    return resolve_repair_policy(
        repair=namespace.repair,
        repair_agents=namespace.repair_agent,
        workers=namespace.repair_workers,
        max_rounds=namespace.repair_max_rounds,
        formula_repair=getattr(namespace, "formula_repair", None),
        quality_repair=getattr(namespace, "quality_repair", None),
        quality_agents=getattr(namespace, "quality_agent", None),
    )
