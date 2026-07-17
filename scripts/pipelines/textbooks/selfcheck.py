"""Tier0 确定性自检:扫描件无源文本层,改用 block 覆盖率(每个有序块都进了 md)。"""
from __future__ import annotations

import re

from scripts.pipelines.textbooks.reconstruct import (
    KATEX_INCOMPAT_COMMANDS,
    _formula_body,
    _match_braced,
    _sanitize_markdown_math_spans,
    restore_emphasis_dots,
    sanitize_formula_number,
    sanitize_latex,
)


def katex_incompat_scan(md: str) -> list[str]:
    """Tier0 lint:md 不应残留已知 KaTeX 不兼容命令(与清洗层同源清单)。返回命中命令。"""
    return [c for c in KATEX_INCOMPAT_COMMANDS if c in md]


# 通常必带上下标的大算符:裸用(后面没跟 _/^)高度疑似 OCR 漏识别了积分限/围道/指标。
# 能正常渲染、不触发 KaTeX 硬报错,故只能靠这条确定性启发式标成"疑似",交人工核对。
# 阈值按 100 页语料实测校准(裸 \oint 13、\int 6、\lim 1;\iint/\sum 裸用为 0)。
# 新算符往这里加即可(同 KATEX_INCOMPAT_COMMANDS 的扩展方式)。
SUSPICIOUS_BARE_OPS = [
    r"\oint", r"\oiint", r"\oiiint",   # 闭合积分:几乎必带围道/曲面
    r"\int", r"\iint", r"\iiint",      # 积分(不定积分为合法裸用,故属中等置信)
    r"\sum", r"\prod", r"\coprod",     # 求和/求积:几乎必带指标
    r"\lim",                            # 极限:必带下标
]
# 算符名后紧跟(允许 \limits/\nolimits + 空白)若不是 _ 或 ^ 即判裸用;(?![a-zA-Z]) 词边界
# 防 \int 误伤 \intercal、\lim 误伤 \limits/\liminf。
_BARE_OP_RES = [
    (op, re.compile(re.escape(op) + r"(?![a-zA-Z])(?!\s*(?:\\limits|\\nolimits)?\s*[_^])"))
    for op in SUSPICIOUS_BARE_OPS
]


# 结构可疑:\frac 的分母是"带撇单字母"(c'、s')。撇号符号在本语料几乎总是积分
# 围道/曲面标号,不会当除数——引擎把 \oint_{c'}…/V 误解析成 \frac{…}{c'}(把低位的
# 围道当成了分母,还丢了真分母 V)。实测全书仅 8 处、集中 p49/p50/p53,0 误报。
_DENOM_PRIMED = re.compile(r"^[a-zA-Z](?:\^\{?\\prime\}?|')$")


def _denom_display(denom: str) -> str:
    return re.sub(r"\^\{?\\prime\}?", "'", denom)


def _frac_primed_denoms(text: str) -> list[dict]:
    r"""找 \frac{num}{denom} 中 denom 为带撇单字母的位置(括号配对扫描,非正则)。"""
    out: list[dict] = []
    i, n = 0, len(text)
    while True:
        j = text.find(r"\frac", i)
        if j == -1:
            break
        k = j + 5
        while k < n and text[k] == " ":
            k += 1
        if k >= n or text[k] != "{":
            i = j + 5
            continue
        ne = _match_braced(text, k)                     # 分子右括号后一位
        if ne == -1:
            i = j + 5
            continue
        m = ne
        while m < n and text[m] == " ":
            m += 1
        if m >= n or text[m] != "{":
            i = ne
            continue
        de = _match_braced(text, m)                     # 分母右括号后一位
        if de == -1:
            i = ne
            continue
        denom = text[m + 1:de - 1]
        if _DENOM_PRIMED.match(denom):
            disp = _denom_display(denom)
            out.append({"kind": "frac_primed_denom", "op": f"frac÷{disp}", "pos": j,
                        "detail": rf"\frac 分母是带撇标号 {disp},疑似把积分围道/曲面误当分母(应为下标)"})
        i = de
    return out


def scan_formula_suspicions(text: str) -> list[dict]:
    r"""扫描疑似识别错误,返回 [{"kind","op","detail","pos"}, ...](按位置排序)。不是硬
    报错,是给人工核对的候选。两类:
      bare_op         —— 通常带上下标的大算符却裸用(可能漏了积分限/围道/指标)。
      frac_primed_denom —— \frac 分母是带撇标号(围道/曲面被误当分母,结构错)。"""
    hits: list[dict] = []
    for op, pat in _BARE_OP_RES:
        for m in pat.finditer(text):
            hits.append({"kind": "bare_op", "op": op, "pos": m.start(),
                         "detail": rf"{op} 疑似缺上/下标(积分限/围道/指标)"})
    hits.extend(_frac_primed_denoms(text))
    hits.sort(key=lambda h: h["pos"])
    return hits


def summarize_suspicions(md: str) -> list[dict]:
    """文档级疑似漏识别汇总(供 selfcheck.json)。返回 [{"op","count"}] 按数量降序。"""
    counts: dict[str, int] = {}
    for s in scan_formula_suspicions(md):
        counts[s["op"]] = counts.get(s["op"], 0) + 1
    return [{"op": op, "count": n} for op, n in sorted(counts.items(), key=lambda x: (-x[1], x[0]))]


def _probe(content: str) -> str:
    """取块内容一段稳定的可检子串(去 LaTeX 包裹与空白,取前 12 个非空字符)。"""
    s = re.sub(r"[\s$]", "", content or "")
    return s[:12]


def _normalized_block_content(block: dict) -> str:
    """按 reconstruct 的同一路径规范化探针，避免清洗前后等价文本被误报 missing。"""
    content = block.get("block_content", "") or ""
    label = block.get("block_label")
    if label == "display_formula":
        return sanitize_latex(_formula_body(content))
    if label in ("text", "abstract", "reference_content", "paragraph_title"):
        return _sanitize_markdown_math_spans(restore_emphasis_dots(content))
    if label == "inline_formula":
        return _sanitize_markdown_math_spans(content)
    if label == "formula_number":
        # reconstruct 把编号吸收成 \tag{...}；须复用同一清洗路径，尤其是全角括号
        # 与单位/脚注符号，否则会把已存在的编号误报为 missing。
        return sanitize_formula_number(content)
    return content


def block_coverage(blocks: list[dict], md: str) -> dict:
    ordered = [b for b in blocks if b.get("block_order") is not None]
    md_flat = re.sub(r"[\s$]", "", md)
    missing = []
    in_md = 0
    skipped_empty = 0
    for b in ordered:
        content = _normalized_block_content(b)
        probes = [_probe(content)]
        if b.get("block_label") == "formula_number":
            # 邻接显示公式时编号会被清洗后吸收到 \tag；落单编号则由 reconstruct
            # 原样输出。Tier0 同时接受两种真实渲染路径，避免全角括号假缺失。
            probes.append(_probe(b.get("block_content", "") or ""))
        probes = [probe for probe in dict.fromkeys(probes) if probe]
        if not probes:
            # block_content 本身为空(OCR 未识别出文字,常见于 text/seal 等 label):
            # 探针恒空,不能算"丢失"(没内容可核对),但也不能悄悄不计数——单独归入
            # skipped_empty,使 total 恒等于 in_md+missing+skipped_empty,不留隐藏数字。
            skipped_empty += 1
            continue
        if any(probe in md_flat for probe in probes):
            in_md += 1
        else:
            missing.append((b.get("block_content") or "")[:40])
    return {"total": len(ordered), "in_md": in_md, "missing": missing,
            "skipped_empty": skipped_empty}


def detect_column_layout(blocks: list[dict]) -> bool:
    """双栏启发式(spec §5.6):同页 ordered 的 text/display_formula 块两两比较,存在一对
    y 区间重叠比例 > 0.5(相对较矮块的高度)且 x 区间完全分离 → 判定疑似双栏。"""
    candidates = [b for b in blocks
                  if b.get("block_label") in ("text", "display_formula")
                  and b.get("block_order") is not None
                  and isinstance(b.get("block_bbox"), (list, tuple)) and len(b.get("block_bbox")) == 4]
    for i in range(len(candidates)):
        x0a, y0a, x1a, y1a = candidates[i]["block_bbox"]
        for j in range(i + 1, len(candidates)):
            x0b, y0b, x1b, y1b = candidates[j]["block_bbox"]
            overlap = min(y1a, y1b) - max(y0a, y0b)
            if overlap <= 0:
                continue
            shorter = min(y1a - y0a, y1b - y0b)
            if shorter <= 0:
                continue
            if overlap / shorter > 0.5 and (x1a < x0b or x1b < x0a):
                return True
    return False


def aggregate_warnings(warnings: list[dict]) -> dict:
    """reconstruct_markdown 逐页告警汇总成 selfcheck 报告字段(spec §5.5/§5.6):
    unhandled_labels 专指没见过的 label(按 label 分组计数);visual_warnings 是
    "认识的 label 但行为超预期"(缺 bbox / 意外带文本),原样列出不聚合。"""
    unhandled_labels: dict[str, dict] = {}
    visual_warnings: list[dict] = []
    for w in warnings:
        if w["kind"] == "unhandled_label":
            entry = unhandled_labels.setdefault(w["label"], {"count": 0, "sample": w["sample"]})
            entry["count"] += 1
        else:
            visual_warnings.append(w)
    return {"unhandled_labels": unhandled_labels, "visual_warnings": visual_warnings}
