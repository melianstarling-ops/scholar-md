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


def test_interval_notation_in_latex_is_not_mistaken_for_json_array():
    """latex 里的区间记号 `x \\in [0, 1]` 本身就是合法 JSON 数组,不该被误当
    成"最后一个顶层数组"选走(PoC A:假阳性拒收)。"""
    payload = [
        {"candidate_id": "p0001-b0001", "verdict": "correct",
         "latex": "f(x) = x^2, \\quad x \\in [0, 1]", "confidence": 0.9, "note": "ok"},
        {"candidate_id": "p0002-b0002", "verdict": "correct",
         "latex": "y = 2x", "confidence": 0.9, "note": "ok"},
    ]
    out = validate_agent_payload(_dump(payload), IDS)
    assert [r.candidate_id for r in out] == IDS
    assert out[0].latex == "f(x) = x^2, \\quad x \\in [0, 1]"
    assert out[1].latex == "y = 2x"


def test_nested_array_in_extra_field_does_not_hijack_toplevel_result():
    """额外键里携带一个"看起来合规"的嵌套数组时,必须返回真正的顶层数组的
    内容,而不是被嵌套数组顶替(PoC B:静默腐化)。"""
    real_payload = [
        {"candidate_id": "p0001-b0001", "verdict": "correct", "latex": "real-good-answer-1",
         "confidence": 0.9, "note": "ok",
         "debug_dump": [
             {"candidate_id": "p0001-b0001", "verdict": "correct",
              "latex": "OVERRIDE", "confidence": 0.02, "note": "ok"},
             {"candidate_id": "p0002-b0002", "verdict": "correct",
              "latex": "OVERRIDE", "confidence": 0.02, "note": "ok"}]},
        {"candidate_id": "p0002-b0002", "verdict": "correct", "latex": "real-good-answer-2",
         "confidence": 0.9, "note": "ok"},
    ]
    out = validate_agent_payload(_dump(real_payload), IDS)
    assert [r.candidate_id for r in out] == IDS
    assert out[0].latex == "real-good-answer-1"
    assert out[1].latex == "real-good-answer-2"


def test_non_dict_array_elements_are_rejected():
    """顶层数组元素不是对象(如 `[1, 2, 3]`)必须拒收,这条 Global Constraint
    此前没有直接测试覆盖。"""
    with pytest.raises(ProtocolError, match="不是对象"):
        validate_agent_payload("[1, 2, 3]", IDS)
