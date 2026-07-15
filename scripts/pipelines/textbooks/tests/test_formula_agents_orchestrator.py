import json
import threading

from scripts.pipelines.textbooks.formula_agents.orchestrator import (
    DispatchState, chunk_candidates, dispatch_with_fallback,
)
from scripts.pipelines.textbooks.formula_agents.protocol import RawResponse
from scripts.pipelines.textbooks.tests.formula_agents_fakes import FakeAdapter


def _cands(n, start=1):
    return [{"candidate_id": f"p{i:04d}-b{i:04d}", "page": i, "block_id": i,
             "engine_latex": f"x^{{{i}}} + 1", "crop_path": f"/tmp/{i}.png"}
            for i in range(start, start + n)]


def _ok(cands, verdict="correct", confidence=0.95, latex=None):
    return RawResponse(json.dumps([
        {"candidate_id": c["candidate_id"], "verdict": verdict,
         "latex": latex if latex is not None else c["engine_latex"].replace("+ 1", "+ 2"),
         "confidence": confidence, "note": ""}
        for c in cands]), "", 0)


def _go(batch, ads, **kw):
    """每次调用新建 DispatchState —— 天然隔离,无需 fixture。"""
    kw.setdefault("state", DispatchState.for_adapters(ads))
    return dispatch_with_fallback(batch, ads, **kw)


# --- 分批 ---

def test_chunk_39_into_10_10_10_9_preserving_order():
    cands = _cands(39)
    batches = chunk_candidates(cands, batch_size=10)
    assert [len(b) for b in batches] == [10, 10, 10, 9]
    flat = [c["candidate_id"] for b in batches for c in b]
    assert flat == [c["candidate_id"] for c in cands]


# --- 整批换下家 ---

def test_kimi_failure_falls_back_through_frozen_chain():
    batch = _cands(2)
    kimi = FakeAdapter("kimi", [RawResponse("boom, not json", "err", 1)])
    gemini = FakeAdapter("gemini", [RawResponse("still broken", "err", 1)])
    codex = FakeAdapter("codex", [_ok(batch)])
    claude = FakeAdapter("claude", [_ok(batch)])

    out = _go(batch, [kimi, gemini, codex, claude])

    assert (kimi.calls, gemini.calls, codex.calls, claude.calls) == (1, 1, 1, 0)
    assert len(out.resolved) == 2
    assert all(r.provider == "codex" for r in out.resolved)


def test_protocol_failure_rejects_whole_batch_not_partially():
    """少项 → 整批拒收,整批交下一家(不部分静默采用)。"""
    batch = _cands(3)
    kimi = FakeAdapter("kimi", [_ok(batch[:2])])          # 只回了 2/3 项
    gemini = FakeAdapter("gemini", [_ok(batch)])

    out = _go(batch, [kimi, gemini])

    assert out.attempts[0]["outcome"] == "protocol_fail"
    assert len(out.resolved) == 3
    assert all(r.provider == "gemini" for r in out.resolved)


def test_unavailable_provider_is_skipped_not_counted_as_failure():
    batch = _cands(1)
    kimi = FakeAdapter("kimi", available=False)
    gemini = FakeAdapter("gemini", [_ok(batch)])

    out = _go(batch, [kimi, gemini])

    assert kimi.calls == 0
    assert out.attempts[0]["outcome"] == "unavailable"
    assert len(out.resolved) == 1


def test_raw_stdout_stderr_preserved_on_failure():
    """现场不丢(F8):失败的 stdout/stderr/退出码全部入 ledger。"""
    batch = _cands(1)
    kimi = FakeAdapter("kimi", [RawResponse("garbage out", "the stderr", 1)])
    out = _go(batch, [kimi, FakeAdapter("gemini", [_ok(batch)])])

    a0 = out.attempts[0]
    assert a0["stdout"] == "garbage out" and a0["stderr"] == "the stderr"
    assert a0["exit_code"] == 1 and a0["error"]


# --- 单条升级 ---

def test_only_uncertain_candidates_are_re_dispatched():
    """一批 3 条,2 条定案 1 条 uncertain → 只有那 1 条重投下一家。"""
    batch = _cands(3)
    mixed = RawResponse(json.dumps([
        {"candidate_id": batch[0]["candidate_id"], "verdict": "accept",
         "latex": "x", "confidence": 0.95, "note": ""},
        {"candidate_id": batch[1]["candidate_id"], "verdict": "uncertain",
         "latex": "", "confidence": 0.2, "note": ""},
        {"candidate_id": batch[2]["candidate_id"], "verdict": "correct",
         "latex": "x^{3} + 2", "confidence": 0.95, "note": ""},
    ]), "", 0)
    kimi = FakeAdapter("kimi", [mixed])
    gemini = FakeAdapter("gemini", [_ok([batch[1]])])     # 只该收到 1 条

    out = _go(batch, [kimi, gemini])

    assert gemini.calls == 1
    assert out.attempts[0]["escalated_ids"] == [batch[1]["candidate_id"]]
    by_id = {r.candidate_id: r for r in out.resolved}
    assert len(by_id) == 3
    assert by_id[batch[0]["candidate_id"]].provider == "kimi"
    assert by_id[batch[1]["candidate_id"]].provider == "gemini"


def test_exhausted_uncertain_ends_pending_and_is_never_applied():
    batch = _cands(1)
    unc = _ok(batch, verdict="uncertain", confidence=0.1, latex="")
    ads = [FakeAdapter(n, [unc]) for n in ("kimi", "gemini", "codex", "claude")]

    out = _go(batch, ads)

    assert out.pending_ids == [batch[0]["candidate_id"]]
    assert out.resolved == []


# --- 交叉验证 ---

def test_high_confidence_correct_uses_first_provider_only():
    """不做四模型投票:第一家高置信就定案。"""
    batch = _cands(1)
    kimi = FakeAdapter("kimi", [_ok(batch, confidence=0.95)])
    gemini = FakeAdapter("gemini", [_ok(batch)])

    out = _go(batch, [kimi, gemini])

    assert gemini.calls == 0
    assert out.resolved[0].provider == "kimi"


def test_low_confidence_correct_accepted_when_second_provider_agrees():
    batch = _cands(1)
    kimi = FakeAdapter("kimi", [_ok(batch, confidence=0.5, latex="x^{1} + 2")])
    gemini = FakeAdapter("gemini", [_ok(batch, confidence=0.95, latex="x^{1} + 2")])

    out = _go(batch, [kimi, gemini], confidence_threshold=0.8)

    assert len(out.resolved) == 1
    assert out.resolved[0].cross_checked_by == "gemini"


def test_low_confidence_correct_never_applied_when_providers_disagree():
    """两家不一致 → 不敢改。宁可不改,不可瞎改。"""
    batch = _cands(1)
    ads = [
        FakeAdapter("kimi",   [_ok(batch, confidence=0.5, latex="x^{1} + 2")]),
        FakeAdapter("gemini", [_ok(batch, confidence=0.9, latex="x^{1} + 99")]),
        FakeAdapter("codex",  [_ok(batch, confidence=0.9, latex="x^{1} + 77")]),
        FakeAdapter("claude", [_ok(batch, confidence=0.9, latex="x^{1} + 55")]),
    ]

    out = _go(batch, ads, confidence_threshold=0.8)

    assert out.resolved == []
    assert out.pending_ids == [batch[0]["candidate_id"]]


# --- 并发隔离(F6:历史上踩过全局三槽的坑)---

def test_per_provider_concurrency_capped_at_three():
    batches = [_cands(1, start=i) for i in range(1, 7)]      # 6 个批次并发
    kimi = FakeAdapter("kimi", [_ok(b) for b in batches], delay=0.05)
    state = DispatchState.for_adapters([kimi], per_provider=3)

    threads = [threading.Thread(target=dispatch_with_fallback, args=(b, [kimi]),
                                kwargs={"state": state}) for b in batches]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert kimi.calls == 6
    assert kimi.peak_concurrency <= 3


def test_providers_have_independent_semaphores_not_a_shared_pool():
    state = DispatchState.for_adapters(
        [FakeAdapter("kimi"), FakeAdapter("gemini")], per_provider=3)
    assert state.semaphores["kimi"] is not state.semaphores["gemini"]


# --- provider 熔断 ---

def test_provider_blocked_after_three_consecutive_failures():
    """失败计数跨批共享(同一个 state),但不跨 run —— 无进程级全局。"""
    batch = _cands(1)
    bad = RawResponse("nope", "", 1)
    kimi = FakeAdapter("kimi", [bad] * 5)
    gemini = FakeAdapter("gemini", [_ok(batch)] * 5)
    ads = [kimi, gemini]
    state = DispatchState.for_adapters(ads)

    for _ in range(3):
        dispatch_with_fallback(batch, ads, state=state)
    assert state.is_blocked("kimi")

    before = kimi.calls
    dispatch_with_fallback(batch, ads, state=state)
    assert kimi.calls == before          # 已 blocked,不再调用


def test_success_resets_the_consecutive_failure_streak():
    """"连续"是字面意思:中间成功一次就清零,不该累积到熔断。"""
    batch = _cands(1)
    bad, good = RawResponse("nope", "", 1), _ok(batch)
    kimi = FakeAdapter("kimi", [bad, bad, good, bad, bad])
    ads = [kimi, FakeAdapter("gemini", [_ok(batch)] * 5)]
    state = DispatchState.for_adapters(ads)

    for _ in range(5):
        dispatch_with_fallback(batch, ads, state=state)

    assert not state.is_blocked("kimi")   # 2 次失败 → 成功清零 → 又 2 次,从未连续 3 次
