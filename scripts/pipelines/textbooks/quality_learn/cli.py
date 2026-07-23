"""Independent CLI for controlled detector/repairer promotion."""
from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path

from scripts.pipelines.textbooks.quality_repair.agents import AgentSpec
from scripts.pipelines.textbooks.quality_repair.reporting import write_json

from .engine import develop, resolve_repo_root, review, write_plan
from .models import LearnError


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="消费 quality-repair learning package")
    parser.add_argument("--run", action="append", required=True,
                        help="quality_repair run 目录；可重复")
    parser.add_argument("--mode", choices=("plan", "develop", "review"), default="plan")
    parser.add_argument("--learn-run-id", default=None)
    parser.add_argument("--cluster", default=None)
    parser.add_argument("--repo-root", default=None)
    parser.add_argument("--agent", action="append", default=[],
                        help="develop provider:model:effort；可重复作显式 fallback")
    parser.add_argument("--review-agent", action="append", default=[],
                        help="review provider:model:effort；必须与实际 develop Agent 不同")
    parser.add_argument("--agent-timeout", type=int, default=900)
    parser.add_argument("--test-timeout", type=int, default=600)
    return parser


def _root(run: Path) -> Path:
    return run / "quality_learn"


def _latest(root: Path) -> str:
    path = root / "latest.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        value = str(data["learn_run_id"])
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise LearnError("no latest plan; run --mode plan or provide --learn-run-id") from exc
    return value


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.agent_timeout <= 0 or args.test_timeout <= 0:
        raise ValueError("timeouts must be positive")
    source_runs = [Path(value).resolve() for value in args.run]
    root = _root(source_runs[0])
    if args.mode == "plan":
        learn_run_id = args.learn_run_id or datetime.now().strftime("%Y%m%dT%H%M%S")
    else:
        learn_run_id = args.learn_run_id or _latest(root)
    output_dir = root / learn_run_id
    try:
        if args.mode == "plan":
            plan = write_plan(source_runs, output_dir, learn_run_id)
            write_json(root / "latest.json", {"learn_run_id": learn_run_id})
            print(f"[quality_learn] plan clusters={len(plan.clusters)} report={output_dir}")
            return 0
        repo = resolve_repo_root(args.repo_root)
        if args.mode == "develop":
            report = develop(
                output_dir, repo, cluster_id=args.cluster,
                agent_specs=[AgentSpec.parse(value) for value in args.agent],
                agent_timeout=args.agent_timeout, test_timeout=args.test_timeout)
            print(f"[quality_learn] develop status={report['status']} report={output_dir}")
            return 0
        report = review(
            output_dir, repo,
            review_specs=[AgentSpec.parse(value) for value in args.review_agent],
            agent_timeout=args.agent_timeout, test_timeout=args.test_timeout)
        print(f"[quality_learn] review verdict={report['verdict']} report={output_dir}")
        return 0 if report["verdict"] == "approve" else 2
    except (LearnError, ValueError) as exc:
        write_json(output_dir / "failure.json", {
            "schema_version": 1, "mode": args.mode, "error": str(exc)})
        print(f"[quality_learn] BLOCKED: {exc}")
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
