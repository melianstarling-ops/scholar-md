"""五道准入闸。

置信度门/交叉验证挡的是"模型看错了";本模块挡的是另一类失败面:
模型抽风、额度耗尽、调用失效、幻觉、整轮跑歪。

不变量: 所有失败路径的最坏结果都是"这条没改",绝不是"这条被改坏了"。
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from dataclasses import dataclass

from scripts.pipelines.textbooks.formula_agents.protocol import (
    AgentResult, normalize_latex,
)
from scripts.pipelines.textbooks.katex_scan import scan_katex

# 只有会真正改写 md 的 verdict 才需要过闸 1/2/3
_MUTATING = frozenset({"correct"})

# 闸 3: 错误话术特征(额度耗尽 / rate limit / 拒答 / CLI 报错文本冒充公式)
_ERROR_PHRASES = (
    "error", "quota", "rate limit", "ratelimit", "timeout", "unauthorized",
    "forbidden", "i cannot", "i can't", "i'm sorry", "sorry,", "unable to",
    "抱歉", "无法", "失败", "错误",
)

_TOKEN = re.compile(r"\\[a-zA-Z]+|[a-zA-Z0-9]+|[^\s\\a-zA-Z0-9]")

# 短公式的长度比是坏判据:"x" -> "x_i" 长度比 3.0,却是最常见的合法补下标。
# 长度比检查只在绝对变化量也大时才生效,避免把"改一个字符"的合法修补
# 跟长度比阈值锁死;真正的幻觉(换成一个不相干的庞然大物)绝对变化量必大。
_MAX_ABS_DELTA = 12

# _MAX_ABS_DELTA 单独存在时留了个漏洞:重合度公式 overlap = |new∩old|/|old|
# 分母是原文 token 数,原文只有 1~3 个 token 时分母极小,新公式只要还带着
# 原来那个符号,重合度就轻易 >= min_overlap,长度比闸又被 abs_delta 豁免,
# 于是短公式场景下"捏造"能全须全尾地滑过去(如 "x" -> "x^2+x-y+1")。
# 真正区分"合法增补"(补下标/补重音/补撇号)与"凭空捏造"的信号是新引入
# 了多少个原文里没有的不同符号:补下标 "x"->"x_i" 只新增 2 个 token,
# 捏造 "x"->"x^2+x-y+1" 新增 6 个且含全新变量。预算下限设 3,覆盖补下标
# /补重音/补撇号这类最常见的合法增补(至多需要 3 个新 token)。
_MAX_NEW_TOKEN_FLOOR = 3


@dataclass(frozen=True)
class GateRejection:
    candidate_id: str
    gate: str
    reason: str


def _tokens(latex: str) -> list[str]:
    return _TOKEN.findall(normalize_latex(latex))


def degenerate_gate(result: AgentResult) -> GateRejection | None:
    """闸 3:空值 / 错误话术 / 拒答文本冒充公式。返回 None = 通过。"""
    if result.verdict not in _MUTATING:
        return None
    latex = (result.latex or "").strip()
    if not latex:
        return GateRejection(result.candidate_id, "degenerate", "latex 为空")
    low = latex.lower()
    for phrase in _ERROR_PHRASES:
        if phrase in low:
            return GateRejection(
                result.candidate_id, "degenerate",
                f"latex 含错误话术特征 {phrase!r},疑为额度耗尽/调用失效的错误文本")
    return None


def similarity_gate(result: AgentResult, engine_latex: str, *,
                    min_ratio: float = 0.5, max_ratio: float = 2.0,
                    min_overlap: float = 0.5) -> GateRejection | None:
    """闸 2:公式修正应是"修补"而非"换一个公式"。返回 None = 通过。

    挡的是: 模型把 A 候选的答案填进 B 候选;模型凭空编了个不相干的公式。

    长度比检查只在绝对变化量也大(> _MAX_ABS_DELTA)时才生效,见该常量注释。

    min_overlap 默认 0.5(而非更宽松的 0.3):挡的是退化复读这类幻觉——
    比如把 "x = y + z" 复读成 "x = x = x",归一化后长度比 1.0 能骗过长度
    闸,旧阈值 0.3 下符号重合度 0.4 也能骗过重合度闸,而这种退化复读是
    合法 LaTeX,下游 KaTeX 闸也拦不住,必须在这里拦。0.5 经验证不会误伤
    改下标/符号反转/补撇号等真实修复(重合度普遍 ≥0.75)。
    """
    if result.verdict not in _MUTATING:
        return None

    new = normalize_latex(result.latex)
    old = normalize_latex(engine_latex)
    if not old:                       # 原文为空 → 无从比较,交给后续闸门
        return None
    if not new:
        return GateRejection(result.candidate_id, "similarity", "建议 latex 归一化后为空")

    ratio = len(new) / len(old)
    abs_delta = abs(len(new) - len(old))
    if not (min_ratio <= ratio <= max_ratio) and abs_delta > _MAX_ABS_DELTA:
        return GateRejection(result.candidate_id, "similarity",
                             f"长度比 {ratio:.2f} 超出 [{min_ratio}, {max_ratio}] "
                             f"且绝对变化量 {abs_delta} > {_MAX_ABS_DELTA}")

    new_t, old_t = set(_tokens(new)), set(_tokens(old))
    if not old_t:
        return None
    overlap = len(new_t & old_t) / len(old_t)
    if overlap < min_overlap:
        return GateRejection(result.candidate_id, "similarity",
                             f"符号重合度 {overlap:.2f} < {min_overlap},疑为幻觉或候选串位")

    # 新符号预算:挡住"短公式因分母小而重合度虚高"的捏造漏洞,见
    # _MAX_NEW_TOKEN_FLOOR 注释。这道检查会连带拒收"引擎乱码被模型救回"
    # 这类大改写(如 "0 infty e x2 dx" -> "\int_0^\infty e^{-x^2}\,dx")——
    # 有意为之:字符串层面无法区分"乱码被救回"与"凭空捏造",按"绝不改
    # 坏"优先于"尽量多修"的第一原则,这类条目该拒收进 uncertain 报告,
    # 而不是被悄悄自动应用。
    new_only = new_t - old_t
    budget = max(_MAX_NEW_TOKEN_FLOOR, len(old_t))
    if len(new_only) > budget:
        return GateRejection(result.candidate_id, "similarity",
                             f"引入 {len(new_only)} 个原文没有的新符号,超出预算 {budget},"
                             f"疑为捏造而非修补")
    return None


def circuit_breaker(n_mutating: int, n_candidates: int, *,
                    ratio: float = 0.6) -> str | None:
    """闸 4:一轮里被提议修改的比例过高 = 模型状态可能不对。

    漏斗里本就混有"虚惊一场"的条目,正常修改率不应过半。
    返回原因字符串 = 熔断(整轮降级 propose);None = 未触发。
    """
    if n_candidates <= 0:
        return None
    actual = n_mutating / n_candidates
    if actual > ratio:
        return (f"熔断:本轮 {n_mutating}/{n_candidates} = {actual:.0%} 的候选被提议修改,"
                f"超过阈值 {ratio:.0%};疑为模型状态异常,整轮不自动应用")
    return None
