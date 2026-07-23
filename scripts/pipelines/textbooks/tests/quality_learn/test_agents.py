from __future__ import annotations

import json

import pytest

from scripts.pipelines.textbooks.quality_learn.agents import (
    invoke_first_valid, parse_develop_response, parse_review_response,
)
from scripts.pipelines.textbooks.quality_learn.models import LearnError
from scripts.pipelines.textbooks.quality_repair.agents import AgentSpec


def _develop(**overrides):
    data = {
        "issue_family": "lost-side-caption", "test_patch": "test",
        "implementation_patch": "impl", "target_tests": ["tests/test_x.py"],
        "notes": [],
    }
    data.update(overrides)
    return json.dumps(data)


def test_develop_protocol_is_strict():
    parsed = parse_develop_response(_develop())
    assert parsed.issue_family == "lost-side-caption"
    with pytest.raises(LearnError):
        parse_develop_response(_develop(target_tests=[]))
    with pytest.raises(LearnError):
        parse_develop_response("not json")


def test_review_protocol_is_strict():
    parsed = parse_review_response(json.dumps({
        "verdict": "approve", "findings": [], "confidence": 0.9,
        "summary": "safe",
    }))
    assert parsed.verdict == "approve"
    with pytest.raises(LearnError):
        parse_review_response(json.dumps({
            "verdict": "yes", "findings": [], "confidence": 1,
            "summary": "",
        }))


def test_explicit_agent_chain_falls_back_only_on_failure():
    specs = [AgentSpec.parse("first:m:high"), AgentSpec.parse("second:m:high")]
    calls = []

    def invoke(spec, prompt, image_paths, timeout):
        calls.append(spec.provider)
        return "bad" if spec.provider == "first" else _develop()

    chosen, _ = invoke_first_valid(
        specs, "prompt", timeout=1, parser=parse_develop_response, invoke=invoke)
    assert calls == ["first", "second"]
    assert chosen.provider == "second"
