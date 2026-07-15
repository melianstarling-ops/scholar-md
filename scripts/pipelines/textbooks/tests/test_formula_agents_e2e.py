"""端到端离线回放:39 个候选,fake adapter,不触 PDF/外部模型/网络。

核心不变量: 最终 md 要么被正确改动,要么逐字未变 —— 绝不出现"被改坏"的中间态。
"""
import json
import os

import pytest

from scripts.pipelines.textbooks.formula_agents.orchestrator import (
    chunk_candidates, run_agents,
)
from scripts.pipelines.textbooks.formula_agents.protocol import RawResponse
from scripts.pipelines.textbooks.paths import resolve_layout
from scripts.pipelines.textbooks.tests.formula_agents_fakes import FakeAdapter

MD_BEFORE = "原始 md 内容\n"


def _golden_39():
    return [{"candidate_id": f"p{i:04d}-b{i:04d}", "page": i, "block_id": i,
             "engine_latex": f"x^{{{i}}} + 1", "crop_path": f"/frozen/{i}.png",
             "reasons": ["worklist:render_error"]}
            for i in range(1, 40)]


def _payload(cands, verdict="correct", confidence=0.95):
    return RawResponse(json.dumps([
        {"candidate_id": c["candidate_id"], "verdict": verdict,
         "latex": c["engine_latex"].replace("+ 1", "+ 2"),
         "confidence": confidence, "note": ""} for c in cands]), "", 0)


def _layout(tmp_path):
    layout = resolve_layout("Golden", str(tmp_path / "d"), str(tmp_path / "w"))
    os.makedirs(layout.doc_deliverable_dir, exist_ok=True)
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    with open(layout.md_path, "w", encoding="utf-8") as f:
        f.write(MD_BEFORE)
    return layout


def _md(layout):
    with open(layout.md_path, encoding="utf-8") as f:
        return f.read()


def _kimi(cands, **kw):
    """按主流程同样的分批喂给 fake adapter。"""
    return [FakeAdapter("kimi", [_payload(b, **kw)
                                 for b in chunk_candidates(cands, 10)])]


def test_candidate_id_coverage_has_no_gap_no_duplicate_and_stable_order():
    cands = _golden_39()
    batches = chunk_candidates(cands, 10)
    assert [len(b) for b in batches] == [10, 10, 10, 9]
    flat = [c["candidate_id"] for b in batches for c in b]
    assert len(flat) == 39 and len(set(flat)) == 39
    assert flat == [c["candidate_id"] for c in cands]


def test_full_replay_applies_all_39_and_rebuilds(tmp_path):
    layout = _layout(tmp_path)
    cands = _golden_39()
    rebuilt = {"n": 0}

    def reassemble(lay, pdf, dpi):
        rebuilt["n"] += 1
        with open(lay.md_path, "w", encoding="utf-8") as f:
            f.write("重建后的 md\n")
        return lay.md_path

    report = run_agents(layout, adapters=_kimi(cands), pdf_path="",
                        today="2026-07-14", collect_fn=lambda _: cands,
                        scan_fn=lambda *a, **k: {"errors": []},
                        reassemble_fn=reassemble, circuit_ratio=1.0)

    assert report.applied == 39 and report.rejected == []
    assert rebuilt["n"] == 1

    with open(layout.corrections_path, encoding="utf-8") as f:
        corr = json.load(f)["corrections"]
    assert len(corr) == 39
    assert all(c["status"] == "accepted" for c in corr)
    assert len({c["candidate_id"] for c in corr}) == 39      # 无重复


def test_uncertain_items_leave_md_byte_identical(tmp_path):
    """未定案项绝不改变最终 Markdown。"""
    layout = _layout(tmp_path)
    cands = _golden_39()

    report = run_agents(
        layout, adapters=_kimi(cands, verdict="uncertain", confidence=0.1),
        pdf_path="", today="2026-07-14", collect_fn=lambda _: cands,
        scan_fn=lambda *a, **k: {"errors": []},
        reassemble_fn=lambda lay, p, d: pytest.fail("全 uncertain 时不该重建"),
        circuit_ratio=1.0)

    assert report.applied == 0
    assert len(report.pending_ids) == 39
    assert _md(layout) == MD_BEFORE


def test_resume_does_not_rerun_completed_batches(tmp_path):
    """成功批次不重跑;重跑也绝不清空 corrections.json(Fix A 的最小验证)。"""
    layout = _layout(tmp_path)
    cands = _golden_39()
    rebuild_calls = {"n": 0}

    def reassemble(lay, pdf, dpi):
        rebuild_calls["n"] += 1
        return lay.md_path

    common = dict(pdf_path="", today="2026-07-14", collect_fn=lambda _: cands,
                  scan_fn=lambda *a, **k: {"errors": []},
                  reassemble_fn=reassemble, circuit_ratio=1.0)

    run_agents(layout, adapters=_kimi(cands), **common)
    assert rebuild_calls["n"] == 1

    second = FakeAdapter("kimi", [_payload(_golden_39()[:10])])
    report2 = run_agents(layout, adapters=[second], **common)

    assert second.calls == 0            # 全部批次已 done,一次都不该再调
    assert rebuild_calls["n"] == 1      # 重跑不该再触发一次 reassemble
    assert report2.applied == 0

    with open(layout.corrections_path, encoding="utf-8") as f:
        corr = json.load(f)["corrections"]
    assert len(corr) == 39              # 重跑没有把 corrections.json 清空
    assert all(c["status"] == "accepted" for c in corr)


def test_idempotent_rerun_preserves_corrections_and_md(tmp_path):
    """Fix A 的完整回归:幂等重跑既不清空 corrections.json,也不动 md。

    触发路径 1(常见): 一次成功 apply 后再原样跑一次(所有批次已终态,todo=[])。
    在修复前,run_agents 会用本次(空的)outcomes 重建 results=[],走到
    `if not mutating` 分支,用空列表覆盖掉上一次运行已经落盘的 39 条 accepted
    corrections —— 方向虽"安全"(下次 reassemble 会把 md 还原成原始 OCR),
    但静默丢弃了已应用、已验证的修正,让 ledger 与交付 md 背离。
    """
    layout = _layout(tmp_path)
    cands = _golden_39()

    def reassemble(lay, pdf, dpi):
        with open(lay.md_path, "w", encoding="utf-8") as f:
            f.write("重建后的 md\n")
        return lay.md_path

    common = dict(pdf_path="", today="2026-07-14", collect_fn=lambda _: cands,
                  scan_fn=lambda *a, **k: {"errors": []},
                  reassemble_fn=reassemble, circuit_ratio=1.0)

    report1 = run_agents(layout, adapters=_kimi(cands), **common)
    assert report1.applied == 39

    with open(layout.corrections_path, encoding="utf-8") as f:
        corr1 = json.load(f)["corrections"]
    assert len(corr1) == 39 and all(c["status"] == "accepted" for c in corr1)
    md_after_run1 = _md(layout)
    assert md_after_run1 == "重建后的 md\n"

    # run2: 同一 layout(同一 ledger),同参数再跑一次。所有批次已终态,
    # todo 应为空 —— dispatch 不该发生,corrections.json/md 都不该被碰。
    rerun_adapter = FakeAdapter("kimi", [])
    called = {"reassemble": 0}

    def reassemble_should_not_run(lay, pdf, dpi):
        called["reassemble"] += 1
        return lay.md_path

    report2 = run_agents(layout, adapters=[rerun_adapter], pdf_path="",
                         today="2026-07-14", collect_fn=lambda _: cands,
                         scan_fn=lambda *a, **k: {"errors": []},
                         reassemble_fn=reassemble_should_not_run, circuit_ratio=1.0)

    assert rerun_adapter.calls == 0
    assert called["reassemble"] == 0
    assert report2.applied == 0

    with open(layout.corrections_path, encoding="utf-8") as f:
        corr2 = json.load(f)["corrections"]
    assert len(corr2) == 39             # 核心断言:重跑没有清空 corrections.json
    assert corr2 == corr1               # 逐字未变
    assert all(c["status"] == "accepted" for c in corr2)
    assert _md(layout) == md_after_run1  # md 逐字未变


def test_crash_resume_merges_prior_batches_into_corrections(tmp_path):
    """Fix B 的回归:崩溃续跑要把先前批次的 resolved 从 ledger 读回合并。

    触发路径 2: 首轮只跑完批次 1-3(30 条,ledger 已记 done + resolved)后停止
    (模拟崩溃/中断);续跑时只有批次 4(剩余 9 条)进 todo,resume_pending 把
    批次 1-3 排除在外。在修复前,run_agents 只从本次(只含批次 4)的 outcomes
    重建 results,corrections.json 被整文件覆盖成只有 9 条 —— 前 30 条已判定的
    修正丢失。用 collect_fn 分两次调用(先给前 30 条把批次 1-3 跑完成 done,
    再给全部 39 条只让剩余批次 4 进 todo)复现"先前批次已终态但本次 outcomes
    不含它们"的场景,不必手工拼 ledger 记录。
    """
    layout = _layout(tmp_path)
    cands = _golden_39()
    first_30 = cands[:30]
    last_9 = cands[30:]
    assert len(last_9) == 9

    # 第一轮:只喂前 30 条给 collect_fn,批次 1-3(各 10 条)全部跑完落 ledger。
    report1 = run_agents(
        layout, adapters=_kimi(first_30), pdf_path="", today="2026-07-14",
        collect_fn=lambda _: first_30,
        scan_fn=lambda *a, **k: {"errors": []},
        reassemble_fn=lambda lay, p, d: lay.md_path, circuit_ratio=1.0)
    assert report1.applied == 30

    # 第二轮(模拟崩溃续跑):collect_fn 现在给全部 39 条。resume_pending 用同样
    # 的 batch_size 重新切批,批次 1-3(candidate_ids 与第一轮完全一致)已在
    # ledger 里终态,todo 只剩批次 4(last_9)。adapter 只需要能服务这 9 条。
    resume_adapter = FakeAdapter("kimi", [_payload(last_9)])

    report2 = run_agents(
        layout, adapters=[resume_adapter], pdf_path="", today="2026-07-14",
        collect_fn=lambda _: cands,
        scan_fn=lambda *a, **k: {"errors": []},
        reassemble_fn=lambda lay, p, d: lay.md_path, circuit_ratio=1.0)

    assert resume_adapter.calls == 1    # 只调用了一次,只服务了批次 4
    assert report2.applied == 39        # 30 条读回合并 + 9 条本次新跑

    with open(layout.corrections_path, encoding="utf-8") as f:
        corr = json.load(f)["corrections"]
    assert len(corr) == 39              # 核心断言:不是只有 9 条
    ids = {c["candidate_id"] for c in corr}
    assert ids == {c["candidate_id"] for c in cands}
    assert len(ids) == 39               # 无重复(current_batch_ids 去重生效)


def test_run_agents_accepts_dict_shaped_collect_fn_return(tmp_path):
    """默认 collect_fn(真实 collect_formula_candidates)返回
    {"candidates": [...], "summary": {...}} 字典,不是 list。orchestrator 里的
    _as_candidate_list 兼容处理专门取 raw["candidates"];这条形状此前没有测试
    直接覆盖过 —— 全部既有测试的 fake collect_fn 都直接返回 list。用一个模拟
    真实返回形状的 fake collect_fn(而非改造 collect_formula_candidates 本身
    所需的 worklist/render_errors 输入文件)覆盖这条 dict 分支,足以在有人
    改动候选收集函数的返回形状时挡住无声破坏,且不必构造更重的输入夹具。
    """
    layout = _layout(tmp_path)
    cands = _golden_39()
    rebuilt = {"n": 0}

    def reassemble(lay, pdf, dpi):
        rebuilt["n"] += 1
        with open(lay.md_path, "w", encoding="utf-8") as f:
            f.write("重建后的 md\n")
        return lay.md_path

    def collect_dict_shaped(_layout):
        return {"candidates": cands, "summary": {"deduped_count": len(cands)}}

    report = run_agents(layout, adapters=_kimi(cands), pdf_path="",
                        today="2026-07-14", collect_fn=collect_dict_shaped,
                        scan_fn=lambda *a, **k: {"errors": []},
                        reassemble_fn=reassemble, circuit_ratio=1.0)

    assert report.n_candidates == 39
    assert report.applied == 39 and report.rejected == []
    assert rebuilt["n"] == 1
