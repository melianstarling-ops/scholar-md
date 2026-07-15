import pytest

from scripts.pipelines.textbooks.corrections import apply_corrections
from scripts.pipelines.textbooks.formula_agents.corrections_map import (
    build_corrections_payload, to_correction,
)
from scripts.pipelines.textbooks.formula_agents.protocol import AgentResult

CAND = {"candidate_id": "p0001-b0001", "page": 1, "block_id": 1,
        "engine_latex": "r_{nf} + 1", "reasons": ["worklist:render_error"]}


def _res(verdict="correct", latex="r_{hf} + 1", **kw):
    return AgentResult(candidate_id="p0001-b0001", verdict=verdict, latex=latex,
                       confidence=0.95, note="下标是 h 不是 n", provider="kimi",
                       model="kimi-coding", effort="thinking", attempt=1, **kw)


def test_correct_maps_to_accepted_correction_with_provenance():
    c = to_correction(_res(cross_checked_by="gemini"), CAND, today="2026-07-14")
    assert c["status"] == "accepted"                    # 自动应用
    assert c["corrected_latex"] == "$$ r_{hf} + 1 $$"
    assert c["source"] == "agent:kimi:kimi-coding"
    assert c["cross_checked_by"] == "gemini"

    # propose / 熔断模式可强制 pending
    assert to_correction(_res(), CAND, today="2026-07-14",
                         status="pending")["status"] == "pending"


@pytest.mark.parametrize("verdict", ["accept", "uncertain", "not_formula_error"])
def test_only_correct_verdict_produces_a_correction(verdict):
    """红线一:其余 verdict 绝不改 md。"""
    payload = build_corrections_payload(
        [_res(verdict=verdict, latex="x")], {"p0001-b0001": CAND},
        stem="Book", today="2026-07-14")
    assert payload["corrections"] == []


def test_output_is_consumable_by_existing_apply_corrections():
    """红线二:本模块产出必须能被既有写 md 路径正确应用,且指纹漂移时不应用。"""
    payload = build_corrections_payload(
        [_res()], {"p0001-b0001": CAND}, stem="Book", today="2026-07-14")
    corr = payload["corrections"]

    applied = apply_corrections([{"block_id": 1, "block_content": "r_{nf} + 1"}], 1, corr)
    assert applied[0]["block_content"] == "$$ r_{hf} + 1 $$"

    # 指纹门:md 内容已漂移 → 不应用(宁可不修,不错配)
    drifted = apply_corrections([{"block_id": 1, "block_content": "完全不同"}], 1, corr)
    assert drifted[0]["block_content"] == "完全不同"
