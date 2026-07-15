import json
import os
import threading

from scripts.pipelines.textbooks.formula_agents.orchestrator import (
    DispatchState, chunk_candidates, dispatch_with_fallback, run_agents,
)
from scripts.pipelines.textbooks.formula_agents.protocol import RawResponse
from scripts.pipelines.textbooks.paths import resolve_layout
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


# --- run_agents 全流程(五道闸编排 + 自动应用 + 回归回滚)---

MD_BEFORE = "原始 md 内容\n"


def _layout(tmp_path):
    layout = resolve_layout("Book", str(tmp_path / "d"), str(tmp_path / "w"))
    os.makedirs(layout.doc_deliverable_dir, exist_ok=True)
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    with open(layout.md_path, "w", encoding="utf-8") as f:
        f.write(MD_BEFORE)
    return layout


def _md(layout):
    with open(layout.md_path, encoding="utf-8") as f:
        return f.read()


def _corrections(layout):
    with open(layout.corrections_path, encoding="utf-8") as f:
        return json.load(f)["corrections"]


def _run(layout, adapters, **kw):
    rebuilt = {"n": 0}

    def reassemble(lay, pdf, dpi):
        rebuilt["n"] += 1
        with open(lay.md_path, "w", encoding="utf-8") as f:
            f.write("重建后的 md\n")
        return lay.md_path

    kw.setdefault("collect_fn", lambda _: _cands(2))
    kw.setdefault("scan_fn", lambda *a, **k: {"errors": []})
    kw.setdefault("reassemble_fn", reassemble)
    kw.setdefault("circuit_ratio", 1.0)          # 多数用例不测熔断
    report = run_agents(layout, adapters=adapters, pdf_path="x.pdf",
                        today="2026-07-14", **kw)
    return report, rebuilt


def test_apply_mode_writes_accepted_corrections_and_rebuilds(tmp_path):
    layout = _layout(tmp_path)
    ads = [FakeAdapter("kimi", [_ok(_cands(2), confidence=0.95)])]

    report, rebuilt = _run(layout, ads)

    assert report.mode == "apply" and report.applied == 2
    assert rebuilt["n"] == 1
    assert all(c["status"] == "accepted" for c in _corrections(layout))


def test_dry_run_never_calls_any_adapter(tmp_path):
    layout = _layout(tmp_path)
    kimi = FakeAdapter("kimi", [_ok(_cands(2))])

    report, rebuilt = _run(layout, [kimi], mode="dry-run")

    assert kimi.calls == 0 and rebuilt["n"] == 0
    assert report.n_candidates == 2
    assert _md(layout) == MD_BEFORE


def test_katex_gate_rejection_excludes_that_candidate(tmp_path):
    """闸 1:agent 吐了 KaTeX 解析不了的东西 → 该条被拒,不进 corrections。"""
    layout = _layout(tmp_path)
    ads = [FakeAdapter("kimi", [_ok(_cands(2), confidence=0.95)])]

    def scan(md_path, out_path, **kw):
        with open(md_path, encoding="utf-8") as f:
            body = f.read()
        if "block_ids: 1" in body:                       # 这是闸 1 的探针 md
            return {"errors": [{"page": 1, "block_ids": [1], "error": "parse error"}]}
        return {"errors": []}                            # baseline / 回归检查

    report, _ = _run(layout, ads, scan_fn=scan)

    assert report.applied == 1
    assert any(r.gate == "katex" for r in report.rejected)


def test_circuit_breaker_downgrades_to_propose_and_md_stays_byte_identical(tmp_path):
    layout = _layout(tmp_path)
    ads = [FakeAdapter("kimi", [_ok(_cands(2), confidence=0.95)])]   # 2/2 = 100% > 60%

    report, rebuilt = _run(layout, ads, circuit_ratio=0.6)

    assert report.circuit_broken and report.mode == "propose"
    assert rebuilt["n"] == 0
    assert _md(layout) == MD_BEFORE                                   # 逐字未变
    assert all(c["status"] == "pending" for c in _corrections(layout))


def test_node_unavailable_downgrades_to_propose(tmp_path):
    """闸 1 无法执行 → 不在缺少校验能力时放行。"""
    layout = _layout(tmp_path)
    ads = [FakeAdapter("kimi", [_ok(_cands(2), confidence=0.95)])]

    report, rebuilt = _run(layout, ads, scan_fn=lambda *a, **k: None)

    assert report.mode == "propose"
    assert rebuilt["n"] == 0
    assert _md(layout) == MD_BEFORE


def test_regression_after_apply_triggers_automatic_rollback(tmp_path):
    layout = _layout(tmp_path)
    ads = [FakeAdapter("kimi", [_ok(_cands(2), confidence=0.95)])]
    calls = {"n": 0}

    def scan(md_path, out_path, **kw):
        calls["n"] += 1
        # 前两次(闸1探针 / baseline)干净;第三次(应用后回归)冒出硬错
        return {"errors": [{"error": "boom"}]} if calls["n"] >= 3 else {"errors": []}

    report, _ = _run(layout, ads, scan_fn=scan)

    assert report.rolled_back and report.applied == 0
    assert _md(layout) == MD_BEFORE                       # 已自动回滚


def test_all_providers_failing_leaves_md_byte_identical(tmp_path):
    """核心不变量:额度耗尽/调用全挂 → 最坏结果是"没改",绝不是"改坏"。"""
    layout = _layout(tmp_path)
    bad = RawResponse("Error: quota exhausted", "429", 1)
    ads = [FakeAdapter(n, [bad] * 5) for n in ("kimi", "gemini", "codex", "claude")]

    report, rebuilt = _run(layout, ads)

    assert report.applied == 0 and rebuilt["n"] == 0
    assert _md(layout) == MD_BEFORE
