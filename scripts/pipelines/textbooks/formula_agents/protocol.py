"""Agent 输出契约:类型、LaTeX 归一化、严格协议校验。

校验失败一律抛 ProtocolError —— 调用方据此判定"整批拒收、换下一 provider"。
协议失败 != 公式识别错(F8):不进准确率分母,也绝不猜测修补无法唯一确定的转义(F5)。
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass

VERDICTS = frozenset({"accept", "correct", "uncertain", "not_formula_error"})
_LATEX_REQUIRED = frozenset({"accept", "correct"})

# LaTeX 里出现裸控制字符 = JSON/LaTeX 双重转义损坏(F5):
# 模型写 \theta,JSON 解码成制表符(\x09);写 \right,解码成回车。
# 合法 LaTeX 源码不应含任何裸控制字符,故整个 C0 控制区(含 \t \n)+ DEL 全部视为损坏信号。
_RAW_CONTROL = re.compile(r"[\x00-\x1f\x7f]")

_WRAPPERS = ("$$", "\\[", "\\]", "$")


class ProtocolError(ValueError):
    """Agent 返回不满足协议。整批拒收的信号。"""


@dataclass(frozen=True)
class AgentResult:
    candidate_id: str
    verdict: str
    latex: str
    confidence: float
    note: str
    provider: str = ""
    model: str = ""
    effort: str = ""
    attempt: int = 0
    cross_checked_by: str | None = None


@dataclass(frozen=True)
class RawResponse:
    stdout: str
    stderr: str
    exit_code: int


def normalize_latex(text: str) -> str:
    """只做包裹符与空白归一,不碰数学内容 —— 用于判两家模型是否给出等价答案。"""
    s = (text or "").strip()
    for _ in range(3):
        for w in _WRAPPERS:
            if s.startswith(w):
                s = s[len(w):].strip()
            if s.endswith(w):
                s = s[: -len(w)].strip()
    return re.sub(r"\s+", " ", s).strip()


def _extract_json_array(stdout: str) -> list:
    """单遍顺序扫描 stdout,取最后一个顶层 JSON 数组(容忍模型前置叙述/工具日志)。

    只有当扫描头不处于任何字符串内部时,遇到的 `[` 才会被当作候选顶层数组的
    起点——字符串内部的 `[`(例如 LaTeX 区间记号 `x \\in [0, 1]`)不会被误当
    成起点,不会导致真数组被漏检或误判。
    每个候选起点会一路消费到与之配对的 `]` 为止(按括号深度匹配);无论该
    候选区间是否能成功 json.loads,下一个候选起点都从这段已消费区域*之后*
    继续找,绝不会从消费区域内部的嵌套 `[` 重新起扫——因此额外字段里携带的
    "看起来合规"的嵌套数组不会顶替/劫持真正的顶层数组。
    """
    text = stdout or ""
    n = len(text)
    found: list[list] = []
    i = 0
    in_str, esc = False, False
    while i < n:
        ch = text[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            i += 1
            continue
        if ch == '"':
            in_str = True
            i += 1
            continue
        if ch != "[":
            i += 1
            continue

        # 候选顶层数组起点:此处确定不在任何字符串内部。
        start = i
        depth = 0
        j = i
        in_str2 = esc2 = False
        end = None
        while j < n:
            c = text[j]
            if in_str2:
                if esc2:
                    esc2 = False
                elif c == "\\":
                    esc2 = True
                elif c == '"':
                    in_str2 = False
            elif c == '"':
                in_str2 = True
            elif c == "[":
                depth += 1
            elif c == "]":
                depth -= 1
                if depth == 0:
                    end = j
                    break
            j += 1

        if end is None:
            # 这个 `[` 一路扫到文本末尾都没配对上 `]`,不是完整数组;
            # 从下一个字符继续找(不算已消费,允许其后仍有独立的合法数组)。
            i = start + 1
            continue

        try:
            arr = json.loads(text[start:end + 1])
        except json.JSONDecodeError:
            arr = None
        if isinstance(arr, list):
            found.append(arr)
        # 无论本次是否解析成功,都从已消费区域之后继续——绝不再深入其内部
        # 的嵌套 `[` 重新起扫。
        i = end + 1

    if not found:
        raise ProtocolError(
            "stdout 中找不到可解析的顶层 JSON 数组"
            "(已排除字符串内部的 `[` 与嵌套数组,只在顶层扫描)")
    return found[-1]


def validate_agent_payload(stdout: str, expected_ids: list[str]) -> list[AgentResult]:
    """严格校验;任一违规抛 ProtocolError = 整批拒收。"""
    arr = _extract_json_array(stdout)

    items: list[dict] = []
    for idx, it in enumerate(arr):
        if not isinstance(it, dict):
            raise ProtocolError(f"顶层数组第 {idx} 项不是对象(须为 dict): {it!r}")
        items.append(it)

    got = [str(it.get("candidate_id", "")) for it in items]

    if len(set(got)) != len(got):
        dupes = sorted({c for c in got if got.count(c) > 1})
        raise ProtocolError(f"candidate_id 重复: {dupes}")
    if set(got) != set(expected_ids):
        missing = sorted(set(expected_ids) - set(got))
        extra = sorted(set(got) - set(expected_ids))
        raise ProtocolError(f"candidate_id 覆盖不符 (缺={missing} 多={extra})")
    if got != list(expected_ids):
        raise ProtocolError(f"candidate_id 顺序错 (期望={list(expected_ids)} 实得={got})")

    out: list[AgentResult] = []
    for it in items:
        cid = str(it["candidate_id"])
        verdict = it.get("verdict")
        if verdict not in VERDICTS:
            raise ProtocolError(f"{cid}: 非法 verdict {verdict!r},须为 {sorted(VERDICTS)}")

        latex = it.get("latex") or ""
        if not isinstance(latex, str):
            raise ProtocolError(f"{cid}: latex 不是字符串")
        if verdict in _LATEX_REQUIRED and not latex.strip():
            raise ProtocolError(f"{cid}: verdict={verdict} 但 latex 为空")
        if _RAW_CONTROL.search(latex):
            raise ProtocolError(
                f"{cid}: latex 含裸控制字符,判定 JSON/LaTeX 转义损坏(F5),整批拒收")

        conf = it.get("confidence", 0.0)
        if isinstance(conf, bool) or not isinstance(conf, (int, float)):
            raise ProtocolError(f"{cid}: confidence 不是数值")
        if not (0.0 <= float(conf) <= 1.0):
            raise ProtocolError(f"{cid}: confidence {conf} 超出 [0.0, 1.0]")

        out.append(AgentResult(
            candidate_id=cid, verdict=verdict, latex=latex,
            confidence=float(conf), note=str(it.get("note") or ""),
        ))
    return out
