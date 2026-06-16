"""Tier0 确定性自检（零成本、无幻觉）。

1) 内容覆盖：比较"解析器保留的文字层词"与"最终 markdown"的字符多重集
   （字符级，对空格/连字符/标点附着鲁棒），精确捕获漏字/串栏丢词。
2) 结构 lint：残留行号、页眉泄漏、claims 编号连续性（硬问题，触发 SUSPECT）；
   FIG 引用解析为【软提示】，不触发 SUSPECT（图整页在位，仅图题 OCR 保真度问题）。

产出报告 dict；missing 比例超阈值或有硬结构问题即标记为需人工/AI 复核。
"""
from __future__ import annotations

import re
from collections import Counter

from profiles import LayoutProfile

_ALNUM_RE = re.compile(r"[0-9a-z]")
_WORD_RE = re.compile(r"[A-Za-z0-9]+")


def _char_counter(text: str) -> Counter:
    return Counter(c for c in text.lower() if _ALNUM_RE.match(c))


def check_coverage(expected_words: list[str], output_text: str) -> dict:
    exp = _char_counter(" ".join(expected_words))
    act = _char_counter(output_text)
    missing = exp - act          # 期望有、输出缺 → 真丢失
    total = sum(exp.values()) or 1
    miss_n = sum(missing.values())
    return {
        "expected_chars": total,
        "missing_chars": miss_n,
        "missing_ratio": round(miss_n / total, 5),
        "missing_sample": dict(missing.most_common(12)),
    }


def lint(output_text: str, claims_md: str, fig_labels_all: list[str],
         profile: LayoutProfile) -> tuple[list[str], list[str]]:
    """结构 lint，返回 (issues, fig_notes)。

    issues    = 触发 SUSPECT 的硬结构问题（残留页眉/孤立行号/claims 非连续）。
    fig_notes = 图号交叉引用【软提示】，不触发 SUSPECT。图纸页按整页渲染必在位
        （presence 由 FIGURE 页型分类 + 渲染保证）；此项只衡量"图题文字 OCR 是否
        捕获了图号"，而图像版专利的旋转图题 OCR 必有零星认错（3B→33）、子图字母
        粒度（正文 FIG.1 对图注 1A/1B）等系统性误报。内容完整性由 missing_ratio 兜底。

    FIG 引用解析复用 profile 图号正则，故全词 "FIGURE N" 也能与图注 "FIG. N" 对上：

    >>> from profiles import get_profile
    >>> p = get_profile()
    >>> lint("As shown in FIGURE 29 and FIGS. 7", "", ["FIG. 29", "FIG. 7"], p)
    ([], [])
    >>> lint("see FIG. 5", "", ["FIG. 29"], p)
    ([], ["图题OCR未捕获(图整页在位,非缺图): FIG ['5']"])
    """
    issues: list[str] = []
    fig_notes: list[str] = []

    # 残留运行页眉：仅当"短独立行"整体像页眉时才报（避开正文内 "U.S. patent
    # application" 与图片路径里的专利号等行内误报）
    no_img = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", output_text)
    leaked = []
    for raw in no_img.splitlines():
        line = raw.strip().strip("*#>").strip()
        if line and len(line) <= 45 and not line[0].islower() and profile.running_header_re.search(line):
            leaked.append(line)
    for frag in sorted(set(leaked)):
        issues.append(f"可能残留页眉/页脚: {frag!r}")

    # 残留孤立行号（整行仅一个 5 的倍数）
    lone = [ln.strip() for ln in output_text.splitlines()
            if re.fullmatch(r"\d{1,2}", ln.strip() or "x") and int(ln.strip()) % 5 == 0]
    if lone:
        issues.append(f"疑似残留行号 {len(lone)} 处: {lone[:8]}")

    # claims 编号连续性
    nums = [int(m.group(1)) for m in re.finditer(r"(?m)^(\d{1,3})\.\s", claims_md or "")]
    if nums:
        expected = list(range(1, len(nums) + 1))
        if nums != expected:
            issues.append(f"claims 编号非连续 1..N: 实际 {nums}")

    # FIG 引用是否都有对应附图。引用端复用 profile 的图号正则（认 FIG./FIGURE/FIGS，
    # 守词边界不吃 configure）；have 端 fig_labels 已由 _fig_labels 统一格式化为 "FIG. N"。
    referenced = set(profile.figure_caption_re.findall(output_text))
    have = set(re.findall(r"FIG\.\s*([0-9]+[A-Z]?)", " ".join(fig_labels_all), re.IGNORECASE))
    unresolved = {r for r in referenced if r.upper() not in {h.upper() for h in have}}
    if unresolved and have:
        fig_notes.append(f"图题OCR未捕获(图整页在位,非缺图): FIG {sorted(unresolved)[:8]}")

    return issues, fig_notes


def run(
    expected_words: list[str],
    final_markdown: str,
    claims_md: str,
    fig_labels_all: list[str],
    profile: LayoutProfile,
    missing_threshold: float = 0.005,
    lint_text: str | None = None,
) -> dict:
    """Tier0 自检汇总。SUSPECT 由 missing 超阈值或硬结构问题触发；
    图号交叉引用是软提示（fig_label_notes），不翻 SUSPECT。

    >>> from profiles import get_profile
    >>> p = get_profile()
    >>> r = run(["alpha"], "alpha see FIG. 3", "", ["FIG. 1"], p)   # 仅图号未解析
    >>> r["passed"], r["issues"], bool(r["fig_label_notes"])
    (True, [], True)
    >>> r2 = run(["x"], "x\\n5\\n", "", [], p)                       # 硬问题:残留行号
    >>> r2["passed"], bool(r2["issues"])
    (False, True)
    """
    cov = check_coverage(expected_words, final_markdown)
    # lint 只扫正文（摘要+说明书+claims），不扫前置引用附录（其中合法含 "U.S. Patent"）
    issues, fig_notes = lint(
        lint_text if lint_text is not None else final_markdown,
        claims_md, fig_labels_all, profile,
    )
    suspect = cov["missing_ratio"] > missing_threshold or bool(issues)
    return {
        "coverage": cov,
        "issues": issues,
        "fig_label_notes": fig_notes,
        "passed": not suspect,
        "suspect": suspect,
    }
