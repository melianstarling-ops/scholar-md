"""KaTeX 硬错分桶巡检:scan → 按错误签名分桶 → 出"视觉修复工单"。

转换收尾用:一本书 OCR→md 后跑本命令,它把 KaTeX 硬错按签名归桶,告诉你
  - 哪些是**确定性签名**(命令映射/双脚本/text 模式 —— 若高频且签名精确才值得加 sanitize 规则)
  - 哪些是**需视觉**(结构碎裂/编号乱码/括号不配对 —— 直接派 agent 或人看裁图逐个修)
并写出一份工单(每条附 页/块/原始 latex/裁图路径),可直接喂给 agent 或人工。

判断规范(加不加规则、读源不猜、别 agent+自己重复)见:
  04_Docs/lessons/lessons_katex_error_triage.md 与 01_System/SOP-09_KaTeX_Error_Triage.md

用法:
  python -m scripts.pipelines.textbooks.katex_triage --stem <书> --deliverables-root <dir> [--out <worklist.json>]
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys

from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks import katex_scan
from scripts.pipelines.textbooks import reconstruct as R
from scripts.pipelines.textbooks.paths import resolve_layout

# 桶定义:key -> (中文名, 是否"需视觉")。确定性桶=False(可评估加规则),视觉桶=True。
_BUCKETS = {
    "undefined_command": ("未定义命令(命令映射候选)", False),
    "math_in_text":      ("数学误入 text 模式", False),
    "double_script":     ("相邻双下标/上标", False),
    "tag_garble":        ("公式编号 \\tag 乱码", True),
    "structural_env":    ("array/bmatrix 结构碎裂", True),
    "brace_mismatch":    ("括号/宏参数不配对", True),
    "other":             ("其它(需视觉)", True),
}

_UNDEF_RE = re.compile(r"Undefined control sequence:?\s*(\\[a-zA-Z]+)")
# \tag{...} 里出现 \命令 或 _(如 \tag{(2.1 \dot{5}6)}、\tag{3_1606})= 编号 OCR 乱码。
# 干净编号(\tag{2.46b})不含 \ 或 _,不误触。
_TAG_GARBLE_RE = re.compile(r"\\tag\{[^}]*[\\_]")


def classify_error(error_msg: str, latex: str) -> tuple[str, str | None]:
    r"""按错误信息 + latex 把一个硬错归桶。返回 (bucket_key, 细分标签或 None)。

    纯函数,不碰 IO,便于回归测试。细分标签用于同类计数(如未定义命令具体是哪个)。
    编号乱码优先判定:\dot 落在 \tag 里会报 "text mode",但本质是编号问题、需读原页,
    故 tag_garble 排在 math_in_text 之前。
    """
    e = error_msg or ""
    lx = latex or ""
    if _TAG_GARBLE_RE.search(e) or _TAG_GARBLE_RE.search(lx):
        return "tag_garble", None
    if "text mode" in e:
        return "math_in_text", None
    if "Double subscript" in e or "Double superscript" in e:
        return "double_script", None
    m = _UNDEF_RE.search(e)
    if m:
        return "undefined_command", m.group(1)
    if "\\begin{array" in lx or "\\begin{bmatrix" in lx or "\\end{array" in lx or "\\end{bmatrix" in lx:
        return "structural_env", None
    if ("Expected '}'" in e or "Unexpected end of input" in e
            or "macro argument" in e or "Expected group" in e):
        return "brace_mismatch", None
    return "other", None


def _sanitized_of(block: dict) -> str:
    label = block.get("block_label", "")
    content = (block.get("block_content") or "").strip()
    if label == "display_formula":
        return R.sanitize_latex(R._formula_body(content))
    return R._sanitize_markdown_math_spans(content)


def _attribute(layout, errors: list[dict]) -> None:
    """就地给每个 error 填 page/block_id/crop_path(尽力,靠 latex_head 完整包含匹配)。
    撞车(多块命中)或无命中则不填裁图,但 latex/error 一定在,视觉仍可修。"""
    manifest = cp.load_manifest(layout.work_dir)
    if not manifest:
        return
    total = manifest["fingerprint"]["page_count"]
    blocks = []
    for pg in range(1, total + 1):
        for b in cp.load_page_blocks(layout.work_dir, pg):
            blocks.append((pg, b))
    crops_dir = os.path.join(layout.repair_dir, "crops")
    for e in errors:
        head = (e.get("latex_head") or "").strip()
        hits = []
        if head:
            for pg, b in blocks:
                try:
                    if head in _sanitized_of(b):
                        hits.append((pg, b.get("block_id")))
                except Exception:
                    continue
        if len(hits) == 1:
            pg, bid = hits[0]
            crop = os.path.join(crops_dir, f"page_{pg:04d}_block_{bid}.png")
            e["page"], e["block_id"] = pg, bid
            e["crop_path"] = crop if os.path.exists(crop) else None
            e["attribution"] = "matched"
        else:
            e["page"] = e["block_id"] = e["crop_path"] = None
            e["attribution"] = "ambiguous" if hits else "unmatched"


def triage(layout, *, scan_fn=None, attribute: bool = True) -> dict:
    """扫 deliverable md → 分桶 → (可选)填块归属。返回 report。"""
    scan = scan_fn or katex_scan.scan_katex
    scan_out = os.path.join(layout.repair_dir, ".katex_triage.json")
    os.makedirs(layout.repair_dir, exist_ok=True)
    d = scan(layout.md_path, scan_out)
    if d is None:
        return {"stem": layout.stem, "scanned": False, "errors": [], "warnings": 0,
                "buckets": {}, "worklist": []}
    errors = d.get("errors", [])
    for e in errors:
        bucket, tag = classify_error(e.get("error", ""), e.get("latex_head", ""))
        e["bucket"] = bucket
        e["signature"] = tag
        e["needs_vision"] = _BUCKETS[bucket][1]
    if attribute and errors:
        _attribute(layout, errors)

    bucket_counts: dict[str, int] = {}
    undef_counts: dict[str, int] = {}
    for e in errors:
        bucket_counts[e["bucket"]] = bucket_counts.get(e["bucket"], 0) + 1
        if e["bucket"] == "undefined_command" and e.get("signature"):
            undef_counts[e["signature"]] = undef_counts.get(e["signature"], 0) + 1
    worklist = [e for e in errors if e.get("needs_vision")]
    return {
        "stem": layout.stem, "scanned": True,
        "hard_errors": len(errors), "warnings": len(d.get("warnings", [])),
        "buckets": bucket_counts, "undefined_commands": undef_counts,
        "errors": errors, "worklist": worklist,
    }


def _print_report(rep: dict, worklist_path: str | None) -> None:
    if not rep["scanned"]:
        print(f"[katex_triage] {rep['stem']}: 扫描失败(node 缺失?)", file=sys.stderr)
        return
    print(f"[katex_triage] {rep['stem']}  硬错 {rep['hard_errors']} | 警告 {rep['warnings']}")
    if not rep["hard_errors"]:
        print("  ✓ 无硬错,无需修复")
        return
    print("  按签名分桶:")
    for key, n in sorted(rep["buckets"].items(), key=lambda kv: -kv[1]):
        name, needs = _BUCKETS[key]
        flag = "需视觉" if needs else "确定性(评估加规则)"
        extra = ""
        if key == "undefined_command":
            top = sorted(rep["undefined_commands"].items(), key=lambda kv: -kv[1])
            extra = "  " + ", ".join(f"{c}×{n2}" for c, n2 in top[:6])
        print(f"    {name:22} {n:3}  [{flag}]{extra}")
    nv = len(rep["worklist"])
    print(f"  视觉工单: {nv} 条" + (f" -> {worklist_path}" if worklist_path else ""))
    print("  判断规范: 01_System/SOP-09_KaTeX_Error_Triage.md"
          " · 04_Docs/lessons/lessons_katex_error_triage.md")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="KaTeX 硬错分桶巡检 + 视觉工单")
    ap.add_argument("--stem", required=True)
    ap.add_argument("--deliverables-root", required=True)
    ap.add_argument("--work-dir", default=None)
    ap.add_argument("--out", default=None, help="视觉工单 JSON 输出路径(默认写 repair 目录)")
    ap.add_argument("--no-attribute", action="store_true", help="跳过块/裁图归属(更快)")
    args = ap.parse_args(argv)

    layout = resolve_layout(args.stem, args.deliverables_root, args.work_dir)
    rep = triage(layout, attribute=not args.no_attribute)
    worklist_path = None
    if rep["scanned"] and rep["worklist"]:
        worklist_path = args.out or os.path.join(layout.repair_dir, f"{args.stem}_vision_worklist.json")
        with open(worklist_path, "w", encoding="utf-8") as f:
            json.dump({"stem": args.stem, "worklist": rep["worklist"]}, f, ensure_ascii=False, indent=2)
    _print_report(rep, worklist_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
