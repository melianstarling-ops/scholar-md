"""KaTeX 硬报错扫描的 Python 薄壳:外调 debug_assets/scan_katex_errors.mjs 产
<stem>_render_errors.json。node 不在 PATH → 返回 None(优雅跳过,调用方打警告不失败)。"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess

_MJS = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                    "debug_assets", "scan_katex_errors.mjs")


def scan_katex(md_path: str, out_path: str, node_bin: str | None = None,
               timeout: int = 120) -> dict | None:
    node = node_bin or shutil.which("node")
    if not node:
        return None
    argv = [node, _MJS, "--md", md_path, "--out", out_path]
    subprocess.run(argv, capture_output=True, text=True,
                   encoding="utf-8", errors="replace", timeout=timeout)
    if not os.path.exists(out_path):
        return None
    with open(out_path, encoding="utf-8") as f:
        return json.load(f)


def main() -> None:
    ap = argparse.ArgumentParser(description="KaTeX 硬报错扫描(薄壳调 .mjs)")
    ap.add_argument("--md", required=True, help="输入 md 路径")
    ap.add_argument("--out", required=True, help="输出 render_errors.json 路径")
    args = ap.parse_args()
    result = scan_katex(args.md, args.out)
    if result is None:
        print("[katex_scan] node 缺失或未产出,已跳过")
    else:
        print(f"[katex_scan] {len(result.get('errors', []))} 处硬报错 → {args.out}")


if __name__ == "__main__":
    main()
