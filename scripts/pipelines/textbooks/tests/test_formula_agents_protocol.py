import json

import pytest

from scripts.pipelines.textbooks.formula_agents.protocol import (
    ProtocolError, normalize_latex, validate_agent_payload,
)

IDS = ["p0001-b0001", "p0002-b0002"]


def _item(cid, verdict="correct", latex="x^2", confidence=0.9):
    return {"candidate_id": cid, "verdict": verdict, "latex": latex,
            "confidence": confidence, "note": "ok"}


def _dump(items):
    return json.dumps(items, ensure_ascii=False)


def test_valid_payload_returns_results_in_order():
    out = validate_agent_payload(_dump([_item(i) for i in IDS]), IDS)
    assert [r.candidate_id for r in out] == IDS
    assert out[0].verdict == "correct" and out[0].confidence == 0.9


def test_extracts_last_json_array_after_model_narration():
    stdout = "我先读图...\n工具调用完毕。\n" + _dump([_item(IDS[0])])
    assert validate_agent_payload(stdout, IDS[:1])[0].candidate_id == IDS[0]


def test_empty_latex_allowed_for_uncertain():
    out = validate_agent_payload(
        _dump([_item(IDS[0], verdict="uncertain", latex="")]), IDS[:1])
    assert out[0].verdict == "uncertain"


@pytest.mark.parametrize("stdout,ids,why", [
    (_dump([_item(IDS[0])]),                     IDS,      "覆盖"),   # 少项
    (_dump([_item(i) for i in IDS + ["p0009-b0009"]]), IDS, "覆盖"),   # 多项
    (_dump([_item(IDS[0]), _item(IDS[0])]),      IDS,      "重复"),   # 重复 id
    (_dump([_item(IDS[1]), _item(IDS[0])]),      IDS,      "顺序"),   # 错序
    (_dump([_item(IDS[0], verdict="looks_ok")]), IDS[:1],  "verdict"),
    (_dump([_item(IDS[0], latex="  ")]),         IDS[:1],  "latex"),  # correct 却空
    (_dump([_item(IDS[0], latex="\\alpha \theta")]), IDS[:1], "控制字符"),  # F5 转义损坏
    (_dump([_item(IDS[0], confidence=1.7)]),     IDS[:1],  "confidence"),
    ("{not json at all",                         IDS[:1],  "JSON"),
])
def test_protocol_violations_are_rejected(stdout, ids, why):
    """任一违规 → 整批拒收(调用方据此换下一 provider)。"""
    with pytest.raises(ProtocolError, match=why):
        validate_agent_payload(stdout, ids)


def test_normalize_latex_strips_wrappers_and_whitespace():
    assert normalize_latex("$$  x^2  +  1 $$") == normalize_latex("$x^2 + 1$")
    assert normalize_latex("\\[ x \\]") == "x"
