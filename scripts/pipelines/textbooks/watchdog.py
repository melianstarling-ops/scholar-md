"""无人值守 supervisor:子进程反复拉起 convert,进程崩了自动续跑,直到跑完或超上限。"""
from __future__ import annotations

import argparse
import subprocess
import sys

from scripts.pipelines.textbooks import checkpoint as cp


def _default_runner(argv: list[str]) -> int:
    cmd = [sys.executable, "-m", "scripts.pipelines.textbooks.convert", *argv]
    return subprocess.run(cmd).returncode


def run_until_done(argv: list[str], max_restarts: int = cp.MAX_RESTARTS,
                   runner=None) -> int:
    """跑 convert;返回 0 成功;非 0(进程崩)则重启续跑,超 max_restarts 放弃返回 1。"""
    runner = runner or _default_runner
    rc = runner(argv)
    restarts = 0
    while rc != 0:
        if restarts >= max_restarts:
            print(f"[watchdog] 超过 {max_restarts} 次重启仍未跑完,放弃。")
            return 1
        restarts += 1
        print(f"[watchdog] convert 进程退出码 {rc},第 {restarts} 次重启续跑...")
        rc = runner(argv)
    print("[watchdog] convert 跑完(exit 0)。")
    return 0


def main() -> None:
    ap = argparse.ArgumentParser(description="textbooks 无人值守转换(崩溃自动续跑)")
    ap.add_argument("--src", required=True, help="PDF 文件路径")
    ap.add_argument("--out", default=None, help="输出目录(默认就地)")
    ap.add_argument("--dpi", type=int, default=cp.DEFAULT_DPI, help="栅格化 DPI(默认150)")
    ap.add_argument("--max-restarts", type=int, default=cp.MAX_RESTARTS,
                    help="累计重启兜底上限")
    ap.add_argument("--no-selfcheck-json", action="store_true",
                    help="不写 <stem>_selfcheck.json(转发给 convert.py)")
    args = ap.parse_args()
    argv = ["--src", args.src, "--dpi", str(args.dpi)]
    if args.out:
        argv += ["--out", args.out]
    if args.no_selfcheck_json:
        argv.append("--no-selfcheck-json")
    rc = run_until_done(argv, max_restarts=args.max_restarts)
    sys.exit(rc)


if __name__ == "__main__":
    main()
