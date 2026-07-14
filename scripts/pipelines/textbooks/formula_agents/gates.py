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
                    min_overlap: float = 0.3) -> GateRejection | None:
    """闸 2:公式修正应是"修补"而非"换一个公式"。返回 None = 通过。

    挡的是: 模型把 A 候选的答案填进 B 候选;模型凭空编了个不相干的公式。
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
    if not (min_ratio <= ratio <= max_ratio):
        return GateRejection(result.candidate_id, "similarity",
                             f"长度比 {ratio:.2f} 超出 [{min_ratio}, {max_ratio}]")

    new_t, old_t = set(_tokens(new)), set(_tokens(old))
    if not old_t:
        return None
    overlap = len(new_t & old_t) / len(old_t)
    if overlap < min_overlap:
        return GateRejection(result.candidate_id, "similarity",
                             f"符号重合度 {overlap:.2f} < {min_overlap},疑为幻觉或候选串位")
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
