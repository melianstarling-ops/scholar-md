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
    """成功批次不重跑。"""
    layout = _layout(tmp_path)
    cands = _golden_39()
    common = dict(pdf_path="", today="2026-07-14", collect_fn=lambda _: cands,
                  scan_fn=lambda *a, **k: {"errors": []},
                  reassemble_fn=lambda lay, p, d: lay.md_path, circuit_ratio=1.0)

    run_agents(layout, adapters=_kimi(cands), **common)

    second = FakeAdapter("kimi", [_payload(_golden_39()[:10])])
    run_agents(layout, adapters=[second], **common)

    assert second.calls == 0            # 全部批次已 done,一次都不该再调


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
