from __future__ import annotations

import json

import pytest

from scripts.pipelines.textbooks.quality_repair.agents import (
    AgentProtocolError,
    AgentSpec,
    route_evidence,
    validate_agent_response,
    build_agent_prompt,
)
from scripts.pipelines.textbooks.quality_repair.models import EvidencePacket


def _packet() -> EvidencePacket:
    return EvidencePacket(
        finding_id="f-1", issue_kind="novel", severity="P1",
        md_excerpt="broken", source_evidence=("page crop",),
        target={"md_start": 0, "md_end": 6},
    )


def test_agent_spec_requires_exact_provider_model_effort():
    spec = AgentSpec.parse("codex:gpt-5.6-sol:high")
    assert (spec.provider, spec.model, spec.effort) == ("codex", "gpt-5.6-sol", "high")
    for bad in ("codex", "codex:model", "codex::high", ":model:high"):
        with pytest.raises(ValueError):
            AgentSpec.parse(bad)


def test_agent_response_protocol_accepts_free_form_novel_family():
    result = validate_agent_response(json.dumps({
        "verdict": "novel", "issue_family": "lost-side-caption",
        "severity": "P1", "source_evidence": ["visible in crop"],
        "target": {"md_start": 2, "md_end": 3},
        "replacement": "", "confidence": 0.91, "generalizable": True,
    }))
    assert result.verdict == "novel"
    assert result.issue_family == "lost-side-caption"


def test_agent_response_repair_requires_replacement_and_valid_confidence():
    with pytest.raises(AgentProtocolError):
        validate_agent_response('{"verdict":"repair","confidence":0.9}')
    with pytest.raises(AgentProtocolError):
        validate_agent_response(json.dumps({
            "verdict": "accept", "issue_family": "known", "severity": "P2",
            "source_evidence": [], "target": {}, "replacement": "",
            "confidence": 2.0, "generalizable": False,
        }))


def test_router_uses_only_explicit_specs_in_order_and_stops_on_valid_answer():
    calls: list[tuple[str, str, str]] = []

    def invoke(spec, packet, timeout):
        calls.append((spec.provider, spec.model, spec.effort))
        if spec.provider == "first":
            return "not-json"
        return json.dumps({
            "verdict": "uncertain", "issue_family": "unknown",
            "severity": "P1", "source_evidence": ["insufficient"],
            "target": dict(packet.target), "replacement": "",
            "confidence": 0.2, "generalizable": False,
        })

    result = route_evidence(
        _packet(),
        [AgentSpec.parse("first:m1:high"), AgentSpec.parse("second:m2:medium")],
        invoke=invoke,
    )
    assert result is not None and result.provider == "second"
    assert calls == [("first", "m1", "high"), ("second", "m2", "medium")]


def test_router_with_no_specs_makes_zero_calls():
    called = False

    def invoke(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError

    assert route_evidence(_packet(), [], invoke=invoke) is None
    assert called is False


def test_router_escalates_uncertain_to_next_explicit_agent():
    calls = []

    def invoke(spec, packet, timeout):
        calls.append(spec.provider)
        verdict = "uncertain" if spec.provider == "first" else "accept"
        return json.dumps({
            "verdict": verdict, "issue_family": "gap", "severity": "P1",
            "source_evidence": ["checked"], "target": dict(packet.target),
            "replacement": "", "confidence": 0.5, "generalizable": False,
        })

    result = route_evidence(
        _packet(), [AgentSpec.parse("first:m1:high"),
                    AgentSpec.parse("second:m2:high")], invoke=invoke)
    assert calls == ["first", "second"]
    assert result is not None and result.provider == "second" and result.verdict == "accept"


def test_generic_prompt_contains_only_packet_and_strict_contract():
    prompt = build_agent_prompt(_packet())
    assert '"verdict"' in prompt
    assert "accept|repair|novel|uncertain" in prompt
    assert "broken" in prompt
    assert "整本" not in prompt
