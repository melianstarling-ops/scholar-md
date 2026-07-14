import pytest

from scripts.pipelines.textbooks.formula_agents.gates import (
    circuit_breaker, degenerate_gate, similarity_gate,
)
from scripts.pipelines.textbooks.formula_agents.protocol import AgentResult


def _r(latex, verdict="correct", cid="p0001-b0001"):
    return AgentResult(candidate_id=cid, verdict=verdict, latex=latex,
                       confidence=0.9, note="")


@pytest.mark.parametrize("junk", [
    "   ",                          # 空
    "Error: rate limit exceeded",   # 额度耗尽
    "quota exhausted",
    "I cannot read the image",      # 拒答
    "抱歉,我无法处理",
])
def test_degenerate_gate_rejects_non_formula_text(junk):
    assert degenerate_gate(_r(junk)) is not None


def test_degenerate_gate_passes_real_latex():
    assert degenerate_gate(_r("\\int_0^1 x^2 \\, dx")) is None


@pytest.mark.parametrize("verdict", ["accept", "uncertain", "not_formula_error"])
def test_gates_skip_verdicts_that_never_change_md(verdict):
    """只有 correct 会改 md,其余 verdict 不该被闸门误杀。"""
    assert degenerate_gate(_r("", verdict=verdict)) is None
    assert similarity_gate(_r("anything", verdict=verdict), "x + 1") is None


def test_similarity_gate_passes_small_repair():
    """典型修正:改一个下标字母。"""
    assert similarity_gate(_r("r_{hf} + 1"), "r_{nf} + 1") is None


@pytest.mark.parametrize("latex,engine,why", [
    ("x", "\\int_0^1 f(x) \\, dx + \\sum_n a_n", "太短"),
    ("\\int_0^1 f(x)\\,dx + \\sum_n a_n + \\prod_k b_k", "x + 1", "太长"),
    ("\\alpha\\beta\\gamma\\delta", "w + x + y + z", "符号不重合"),
])
def test_similarity_gate_rejects_hallucination(latex, engine, why):
    rej = similarity_gate(_r(latex), engine)
    assert rej is not None, why
    assert rej.gate == "similarity"


@pytest.mark.parametrize("n,total,tripped", [
    (7, 10, True),     # 70% > 60%
    (6, 10, False),    # 60% 不算超
    (2, 10, False),
    (0, 0, False),     # 空候选不熔断
])
def test_circuit_breaker(n, total, tripped):
    assert (circuit_breaker(n, total, ratio=0.6) is not None) is tripped
