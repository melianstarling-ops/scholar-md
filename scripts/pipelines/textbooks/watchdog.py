"""无人值守 supervisor:子进程反复拉起 convert,进程崩了自动续跑,直到跑完或超上限。"""
from __future__ import annotations

import argparse
import subprocess
import sys

from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks.power import keep_system_awake
from scripts.pipelines.textbooks.repair_policy import (
    CompletionStatus,
    add_repair_policy_arguments,
    repair_policy_from_namespace,
)

# 与 convert.py 的 BORN_DIGITAL_MODES 同步维护(独立常量,不跨模块 import
# convert.py 制造不必要耦合;风格同 batch.py 的 AUDIT_SCHEMA_VERSION 惯例)。
BORN_DIGITAL_MODES = ("defer", "ocr", "hybrid")

# 与 convert.py 的 FORMULA_REPAIR_MODES 同步维护(独立常量,同上惯例)。单本转换
# 收尾自动接公式修复环(Task B/Task 1):watchdog 只做 argv 透传给 convert.py 子进程。
QUALITY_DISCOVERY_MODES = ("off", "signals")
QUALITY_LEARN_MODES = ("off", "package")


def _default_runner(argv: list[str]) -> int:
    cmd = [sys.executable, "-m", "scripts.pipelines.textbooks.convert", *argv]
    return subprocess.run(cmd).returncode


def run_until_done(argv: list[str], max_restarts: int = cp.MAX_RESTARTS,
                   runner=None) -> int:
    """Run convert; restart every failure, and preserve terminal SUSPECT."""
    runner = runner or _default_runner
    rc = runner(argv)
    restarts = 0
    while rc not in (CompletionStatus.OK, CompletionStatus.SUSPECT):
        if restarts >= max_restarts:
            print(f"[watchdog] 超过 {max_restarts} 次重启仍未跑完,放弃。")
            return int(CompletionStatus.FAILED)
        restarts += 1
        print(f"[watchdog] convert 进程退出码 {rc},第 {restarts} 次重启续跑...")
        rc = runner(argv)
    if rc == CompletionStatus.OK:
        print("[watchdog] convert 跑完(exit 0)。")
        return int(CompletionStatus.OK)
    if rc == CompletionStatus.SUSPECT:
        print("[watchdog] convert 产物需复核(exit 2),停止重启。")
        return int(CompletionStatus.SUSPECT)
    return int(CompletionStatus.FAILED)


def main() -> None:
    ap = argparse.ArgumentParser(description="textbooks 无人值守转换(崩溃自动续跑)")
    ap.add_argument("--src", required=True, help="PDF 文件路径")
    ap.add_argument("--out", default=None, help="交付根(md+assets,默认就地)")
    ap.add_argument("--work-dir", default=None, help="过程根(默认 <out>/_work_root,转发给 convert.py)")
    ap.add_argument("--dpi", type=int, default=cp.DEFAULT_DPI, help="栅格化 DPI(默认150)")
    ap.add_argument("--force-ocr", action="store_true",
                    help="忽略优质文本层并强制逐页栅格化 OCR")
    ap.add_argument("--work-hours", type=float, default=6,
                    help="每轮连续 OCR 时长(小时，默认6)")
    ap.add_argument("--rest-minutes", type=float, default=40,
                    help="每轮结束后的 GPU 空闲时长(分钟，默认40)")
    ap.add_argument("--max-restarts", type=int, default=cp.MAX_RESTARTS,
                    help="累计重启兜底上限")
    ap.add_argument("--no-selfcheck-json", action="store_true",
                    help="不写 <stem>_selfcheck.json(转发给 convert.py)")
    ap.add_argument("--allow-sleep", action="store_true",
                    help="允许系统按电源计划睡眠(默认转换期间阻止睡眠)")
    ap.add_argument("--born-digital-mode", choices=list(BORN_DIGITAL_MODES), default="hybrid",
                    help="路线 B(born-digital)采信模式:hybrid=块级混合采信(默认)/"
                         "defer=登记不转(回退开关)/ocr=完全走 OCR 忽略文本层(回退开关,转发给 convert.py)")
    add_repair_policy_arguments(ap)
    ap.add_argument("--quality-discovery", choices=list(QUALITY_DISCOVERY_MODES),
                    default="signals")
    ap.add_argument("--quality-learn", choices=list(QUALITY_LEARN_MODES), default="off")
    ap.add_argument("--quality-agent-timeout", type=int, default=300)
    args = ap.parse_args()
    if args.work_hours <= 0 or args.rest_minutes <= 0:
        ap.error("--work-hours 与 --rest-minutes 必须大于 0")
    repair_policy = repair_policy_from_namespace(args)
    argv = ["--src", args.src, "--dpi", str(args.dpi)]
    if args.out:
        argv += ["--out", args.out]
    if args.work_dir:
        argv += ["--work-dir", args.work_dir]
    if args.no_selfcheck_json:
        argv.append("--no-selfcheck-json")
    if args.force_ocr:
        argv.append("--force-ocr")
    argv += ["--work-hours", str(args.work_hours),
             "--rest-minutes", str(args.rest_minutes)]
    if args.allow_sleep:
        argv.append("--allow-sleep")
    argv += ["--born-digital-mode", args.born_digital_mode]
    argv += ["--repair", repair_policy.mode,
             "--repair-workers", str(repair_policy.workers),
             "--repair-max-rounds", str(repair_policy.max_rounds)]
    for spec in repair_policy.formula_agents:
        argv += ["--repair-agent", spec.to_cli()]
    if args.formula_repair is not None:
        argv += ["--formula-repair", args.formula_repair]
    if args.quality_repair is not None:
        argv += ["--quality-repair", args.quality_repair]
    argv += ["--quality-discovery", args.quality_discovery,
             "--quality-learn", args.quality_learn,
             "--quality-agent-timeout", str(args.quality_agent_timeout)]
    for agent in args.quality_agent or []:
        argv += ["--quality-agent", agent]
    with keep_system_awake(enabled=not args.allow_sleep):
        rc = run_until_done(argv, max_restarts=args.max_restarts)
    sys.exit(rc)


if __name__ == "__main__":
    main()
