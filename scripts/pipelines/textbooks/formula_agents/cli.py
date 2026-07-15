"""公式 Agent 终检 CLI。默认全自动应用。"""
from __future__ import annotations

import argparse
import os
import sys

from scripts.pipelines.textbooks.formula_agents import gates
from scripts.pipelines.textbooks.formula_agents.adapters import default_adapters
from scripts.pipelines.textbooks.formula_agents.orchestrator import run_agents
from scripts.pipelines.textbooks.formula_candidates import collect_formula_candidates
from scripts.pipelines.textbooks.paths import resolve_layout


def crops_only_collect(layout) -> dict:
    """只保留带裁图的候选(可视觉核对的真可疑公式)。

    大书里候选常被数百个 KaTeX 严格模式警告(无裁图、多半无害)灌爆;视觉 Agent
    对无裁图项只能瞎猜。过滤到带裁图 = render_errors + worklist 的真可疑集,
    既省调用又聚焦。crop-less 项(多为 katex_warning)留给确定性处理,不进视觉。
    """
    out = collect_formula_candidates(layout)
    cands = out["candidates"] if isinstance(out, dict) else list(out)
    kept = [c for c in cands if c.get("crop_path")]
    return {"candidates": kept,
            "summary": (out.get("summary", {}) if isinstance(out, dict) else {})}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="textbooks 公式 Agent 终检(默认全自动应用)")
    ap.add_argument("--stem", required=True)
    ap.add_argument("--deliverables-root", required=True)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--pdf", default=None, help="源 PDF(重建时需要)")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--batch-size", type=int, default=10)
    ap.add_argument("--per-provider", type=int, default=3)
    ap.add_argument("--confidence-threshold", type=float, default=0.8)
    ap.add_argument("--circuit-breaker-ratio", type=float, default=0.6)
    ap.add_argument("--dry-run", action="store_true", help="只估算,不调模型")
    ap.add_argument("--propose", action="store_true", help="只落 pending,不改 md")
    ap.add_argument("--rollback", action="store_true", help="回滚最近一轮")
    ap.add_argument("--crops-only", action="store_true",
                    help="只处理带裁图候选(滤掉无裁图的 KaTeX 警告,大书必用)")
    args = ap.parse_args(argv)

    layout = resolve_layout(args.stem, args.deliverables_root, args.work_dir)

    if args.rollback:
        snap = layout.md_path + ".pre_agent.bak"
        if not os.path.exists(snap):
            print(f"[formula_agents] 无快照可回滚: {snap}", file=sys.stderr)
            return 1
        gates.rollback_md(layout.md_path, snap)
        print(f"[formula_agents] 已回滚 → {layout.md_path}")
        return 0

    mode = "dry-run" if args.dry_run else ("propose" if args.propose else "apply")

    report = run_agents(
        layout, adapters=default_adapters(), pdf_path=args.pdf or "", dpi=args.dpi,
        batch_size=args.batch_size, per_provider=args.per_provider,
        confidence_threshold=args.confidence_threshold,
        circuit_ratio=args.circuit_breaker_ratio, mode=mode,
        collect_fn=crops_only_collect if args.crops_only else None)

    print(f"[formula_agents] stem={report.stem} mode={report.mode}")
    print(f"  候选: {report.n_candidates}    已应用: {report.applied}")
    print(f"  被闸门拒收: {len(report.rejected)}")
    for rej in report.rejected:
        print(f"    - {rej.candidate_id} [{rej.gate}] {rej.reason}")
    print(f"  未定案(md 未改动): {len(report.pending_ids)}")
    if report.circuit_broken:
        print(f"  ⚠ 熔断: {report.reason}")
    if report.rolled_back:
        print(f"  ⚠ 已自动回滚: {report.reason}")
        return 2
    if report.reason and not report.circuit_broken:
        print(f"  说明: {report.reason}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
