import json

from scripts.pipelines.textbooks.formula_agents.adapters import (
    FROZEN_CHAIN, _unwrap_stdout, build_prompt,
)
from scripts.pipelines.textbooks.formula_agents.ledger import (
    append_ledger, batch_id, load_ledger, resume_pending,
)


def _cands(n):
    return [{"candidate_id": f"p{i:04d}-b{i:04d}"} for i in range(1, n + 1)]


def test_claude_json_envelope_is_unwrapped_to_inner_result():
    """claude --output-format json 把结果数组包在 {"result": "<文本>"} 里,
    须先拆出 result 交给核心校验(真机 smoke:不拆则找不到顶层数组、整批误判协议失败)。"""
    arr = '[{"candidate_id": "p0001-b0001", "verdict": "accept", "latex": "x", "confidence": 0.9, "note": ""}]'
    envelope = json.dumps({"type": "result", "is_error": False, "result": arr})
    assert _unwrap_stdout("claude", envelope) == arr


def test_unwrap_is_noop_for_non_claude_and_non_json():
    raw = '[{"candidate_id": "p0001-b0001"}]'
    assert _unwrap_stdout("kimi", raw) == raw          # 非 claude 原样
    assert _unwrap_stdout("claude", "not json at all") == "not json at all"  # 非 JSON 原样
    assert _unwrap_stdout("claude", '{"type":"x"}') == '{"type":"x"}'        # 无 result 字段原样


def test_ledger_roundtrip_and_skips_corrupt_lines(tmp_path):
    path = str(tmp_path / "sub" / "ledger.jsonl")       # 父目录自动建
    assert load_ledger(path) == []                       # 缺文件 → []
    append_ledger(path, {"batch_id": "a", "status": "done"})
    with open(path, "a", encoding="utf-8") as f:
        f.write("{corrupt line\n")                       # 损坏行不该崩掉续跑
    append_ledger(path, {"batch_id": "b", "status": "blocked"})
    assert [r["batch_id"] for r in load_ledger(path)] == ["a", "b"]


def test_batch_id_is_deterministic_and_order_sensitive():
    ids = ["p0001-b0001", "p0002-b0002"]
    assert batch_id(ids) == batch_id(list(ids))
    assert batch_id(ids) != batch_id(list(reversed(ids)))


def test_resume_skips_terminal_batches_and_reruns_interrupted():
    """成功/blocked 批次不重跑;无终态记录的批次续跑。"""
    cands = _cands(20)                                   # 拆成 10 + 10
    first = [c["candidate_id"] for c in cands[:10]]
    ledger = [{"batch_id": batch_id(first), "status": "done"}]

    pending = resume_pending(ledger, cands, batch_size=10)
    assert [c["candidate_id"] for c in pending] == \
           [c["candidate_id"] for c in cands[10:]]       # 只剩第二批

    assert len(resume_pending([], cands, batch_size=10)) == 20   # 空 ledger → 全跑

    blocked = [{"batch_id": batch_id(first), "status": "blocked"}]
    assert len(resume_pending(blocked, cands[:10], batch_size=10)) == 0

    running = [{"batch_id": batch_id(first), "status": "running"}]   # 非终态
    assert len(resume_pending(running, cands[:10], batch_size=10)) == 10


def test_frozen_chain_order_is_locked():
    """压测冻结的调用顺序,改动须重新走 benchmark。"""
    assert FROZEN_CHAIN == ["kimi", "gemini", "codex", "claude"]


def test_build_prompt_carries_ids_crops_and_verdict_vocabulary():
    entries = [{"candidate_id": "p0001-b0001", "crop_path": "/x/1.png",
                "engine_latex": "x^2"}]
    prompt = build_prompt(entries)
    assert "p0001-b0001" in prompt and "/x/1.png" in prompt
    for v in ("accept", "correct", "uncertain", "not_formula_error"):
        assert v in prompt
