from __future__ import annotations

import argparse

import pytest

from scripts.pipelines.textbooks.repair_policy import (
    AgentSpec,
    CompletionStatus,
    DEFAULT_REPAIR_MAX_ROUNDS,
    DEFAULT_REPAIR_WORKERS,
    add_repair_policy_arguments,
    combine_completion_statuses,
    quality_final_is_conclusive,
    repair_policy_from_namespace,
    resolve_repair_policy,
    source_audit_blocks_completion,
)


def _parse(*argv: str):
    parser = argparse.ArgumentParser()
    add_repair_policy_arguments(parser)
    args = parser.parse_args(list(argv))
    return args, repair_policy_from_namespace(args)


def test_shared_parser_defaults_to_auto_without_implicit_agent():
    args, policy = _parse()

    assert args.repair == "auto"
    assert policy.mode == "auto"
    assert policy.formula_mode == "auto"
    assert policy.quality_mode == "auto"
    assert policy.formula_agents == ()
    assert policy.quality_agents == ()
    assert policy.workers == DEFAULT_REPAIR_WORKERS == 4
    assert policy.max_rounds == DEFAULT_REPAIR_MAX_ROUNDS == 2
    assert policy.use_legacy_formula_chain is False
    assert policy.runtime_formula_repair == "deterministic"
    assert policy.runtime_quality_repair == "apply"
    assert policy.runtime_quality_agents == ()


def test_repeated_unified_agents_keep_fallback_order_for_both_stages():
    _args, policy = _parse(
        "--repair-agent", "codex:gpt-5.6-sol:high",
        "--repair-agent", "gemini:gemini-pro:medium",
        "--repair-workers", "7",
        "--repair-max-rounds", "3",
    )

    expected = (
        AgentSpec("codex", "gpt-5.6-sol", "high"),
        AgentSpec("gemini", "gemini-pro", "medium"),
    )
    assert policy.formula_agents == expected
    assert policy.quality_agents == expected
    assert policy.workers == 7
    assert policy.max_rounds == 3
    assert policy.runtime_formula_repair == "agents-apply"
    assert policy.runtime_quality_repair == "apply"
    assert policy.runtime_quality_agents == (
        "codex:gpt-5.6-sol:high",
        "gemini:gemini-pro:medium",
    )


@pytest.mark.parametrize("flag", ["--repair-workers", "--repair-max-rounds"])
@pytest.mark.parametrize("value", ["0", "-1", "not-an-int"])
def test_parser_rejects_invalid_positive_integer(flag, value):
    parser = argparse.ArgumentParser()
    add_repair_policy_arguments(parser)

    with pytest.raises(SystemExit):
        parser.parse_args([flag, value])


def test_parser_rejects_malformed_agent_spec():
    parser = argparse.ArgumentParser()
    add_repair_policy_arguments(parser)

    with pytest.raises(SystemExit):
        parser.parse_args(["--repair-agent", "codex:gpt-5.6-sol"])


def test_legacy_formula_flag_overrides_formula_stage_only():
    _args, policy = _parse("--formula-repair", "deterministic")

    assert policy.formula_mode == "deterministic"
    assert policy.quality_mode == "auto"
    assert policy.legacy_formula_explicit is True
    assert policy.legacy_quality_explicit is False
    assert policy.runtime_formula_repair == "deterministic"
    assert policy.runtime_quality_repair == "apply"


def test_legacy_quality_flag_overrides_quality_stage_only():
    _args, policy = _parse("--quality-repair", "audit")

    assert policy.formula_mode == "auto"
    assert policy.quality_mode == "audit"
    assert policy.legacy_formula_explicit is False
    assert policy.legacy_quality_explicit is True
    assert policy.runtime_formula_repair == "deterministic"
    assert policy.runtime_quality_repair == "audit"


def test_explicit_legacy_quality_agents_override_quality_chain_only():
    _args, policy = _parse(
        "--repair-agent", "codex:gpt-5.6-sol:high",
        "--quality-agent", "claude:claude-sonnet-4-6:medium",
    )

    assert policy.formula_agents == (
        AgentSpec("codex", "gpt-5.6-sol", "high"),)
    assert policy.quality_agents == (
        AgentSpec("claude", "claude-sonnet-4-6", "medium"),)
    assert policy.legacy_quality_agents_explicit is True
    assert policy.runtime_formula_repair == "agents-apply"
    assert policy.runtime_quality_agents == (
        "claude:claude-sonnet-4-6:medium",)


def test_repair_off_can_be_overridden_by_one_explicit_legacy_stage():
    _args, policy = _parse(
        "--repair", "off",
        "--quality-repair", "apply",
    )

    assert policy.formula_mode == "off"
    assert policy.quality_mode == "apply"
    assert policy.runtime_formula_repair == "off"
    assert policy.runtime_quality_repair == "apply"


@pytest.mark.parametrize("legacy_mode", ["agents", "agents-apply"])
def test_explicit_legacy_formula_agents_mode_preserves_frozen_chain_compatibility(
        legacy_mode):
    _args, policy = _parse("--formula-repair", legacy_mode)

    assert policy.use_legacy_formula_chain is True
    assert policy.formula_agents == ()


def test_explicit_unified_agent_disables_legacy_implicit_formula_chain():
    _args, policy = _parse(
        "--formula-repair", "agents-apply",
        "--repair-agent", "codex:gpt-5.6-sol:high",
    )

    assert policy.use_legacy_formula_chain is False
    assert policy.formula_agents == (
        AgentSpec("codex", "gpt-5.6-sol", "high"),)


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"repair": "bogus"}, "repair"),
        ({"workers": 0}, "workers"),
        ({"max_rounds": 0}, "max_rounds"),
        ({"formula_repair": "bogus"}, "formula_repair"),
        ({"quality_repair": "bogus"}, "quality_repair"),
    ],
)
def test_programmatic_resolution_validates_inputs(kwargs, message):
    with pytest.raises(ValueError, match=message):
        resolve_repair_policy(**kwargs)


def test_completion_status_values_and_batch_precedence_are_frozen():
    assert CompletionStatus.OK.value == 0
    assert CompletionStatus.SUSPECT.value == 2
    assert CompletionStatus.FAILED.value == 1
    assert combine_completion_statuses([]) is CompletionStatus.OK
    assert combine_completion_statuses(
        [CompletionStatus.OK, CompletionStatus.SUSPECT]) is CompletionStatus.SUSPECT
    assert combine_completion_statuses(
        [CompletionStatus.SUSPECT, CompletionStatus.FAILED]) is CompletionStatus.FAILED


def test_mild_source_audit_signal_does_not_block_current_final():
    audit = {"summary": {
        "status": "SUSPECT", "issue_counts": {"prose_mismatch": 3},
    }}

    assert source_audit_blocks_completion(audit, None) is False


@pytest.mark.parametrize(
    "source_code",
    ["numeric_mismatch", "sign_flip", "decimal_shift", "exponent_change"],
)
@pytest.mark.parametrize("quality_mode", ["auto", "apply"])
def test_severe_source_audit_cannot_be_cleared_by_generic_quality_final(
        quality_mode, source_code):
    audit = {"summary": {
        "status": "SUSPECT", "issue_counts": {source_code: 1},
    }}

    assert source_audit_blocks_completion(audit, None) is True
    assert source_audit_blocks_completion(audit, {
        "mode": quality_mode, "status": "OK", "findings": 0,
        "conflicts": 0, "rolled_back": False, "reason": "",
    }) is True


def test_severe_prose_degradation_is_a_blocking_source_audit_signal():
    audit = {"summary": {
        "status": "SUSPECT",
        "issue_counts": {"severe_prose_degradation": 1},
    }}

    assert source_audit_blocks_completion(audit, None) is True


@pytest.mark.parametrize("audit", [
    {"summary": {"status": "UNSCORABLE", "issue_counts": {}}},
    {"summary": {"status": "SUSPECT", "issue_counts": {"adoption_error": 1}}},
    {"summary": {"status": "SUSPECT", "issue_counts": {"audit_error": 1}}},
])
def test_source_audit_integrity_failures_always_block(audit):
    quality_ok = {
        "mode": "apply", "status": "OK", "findings": 0,
        "conflicts": 0, "rolled_back": False, "reason": "",
    }

    assert source_audit_blocks_completion(audit, quality_ok) is True


def test_quality_final_requires_resolved_terminal_and_no_unresolved_events():
    clean = {
        "mode": "apply", "status": "OK", "findings": 0,
        "conflicts": 0, "rolled_back": False, "reason": "",
        "stop_reason": "resolved", "unresolved": [],
        "unresolved_events": [], "unresolved_count": 0,
        "unresolved_event_count": 0,
    }

    assert quality_final_is_conclusive(clean) is True
    assert quality_final_is_conclusive({**clean, "mode": "auto"}) is True
    assert quality_final_is_conclusive({
        **clean,
        "unresolved_events": [{"event_id": "e1", "status": "unresolved"}],
    }) is False
    assert quality_final_is_conclusive({
        **clean, "stop_reason": "max_rounds",
    }) is False
