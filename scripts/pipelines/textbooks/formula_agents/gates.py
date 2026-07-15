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


def is_pure_token_reorder(new_latex: str, old_latex: str) -> bool:
    """归一化后 token 集合相同、但序列不同 = 纯符号重排/身份对调。

    这是 similarity_gate 的已知盲区(a+b=c -> c+b=a: 新符号 0、重合度 1.0 全过)。
    命中者应被强制交叉验证,不因高置信直接采用。
    """
    new_seq, old_seq = _tokens(new_latex), _tokens(old_latex)
    return bool(new_seq) and set(new_seq) == set(old_seq) and new_seq != old_seq


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

    已知边界(明知故犯,不再堆第四个阈值):
    本闸是基于 token **集合**的粗筛,只拦得住"长度爆炸""大部分是新符号"
    这类明显捏造。它原理上无法识别符号重排/身份对调类捏造——例如
    "a+b=c" 被改成 "c+b=a"(变量身份对调)或改成 "a+a=a"(退化成同一
    变量):这两种情况新符号数都是 0、token 重合度都是 1.0、长度比也
    正常,三重判据全部放行。根源是集合判据看不见符号在公式结构里各自
    扮演的角色,"模型正确读出 c+b=a" 与 "模型编造 c+b=a" 在字符串层面
    完全无法区分。不要为了堵这个口子再加第四个阈值——每加一个新判据
    都会在别处开新口子(参见上面 _MAX_ABS_DELTA / _MAX_NEW_TOKEN_FLOOR
    各自留下又被后一道判据堵上的漏洞史)。结构性捏造的真正防线在别处:
    低置信路径交给 MathML 交叉验证;高置信单模型路径下这类风险是已知
    情况下接受的残余风险,不在这道闸的职责范围内。
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


# 候选数低于此值时比例判据无统计意义(1/1=100%、2/3=67% 都是噪声),不熔断。
_CIRCUIT_MIN_CANDIDATES = 5


def circuit_breaker(n_mutating: int, n_candidates: int, *,
                    ratio: float = 0.6) -> str | None:
    """闸 4:一轮里被提议修改的比例过高 = 模型状态可能不对。

    漏斗里本就混有"虚惊一场"的条目,正常修改率不应过半。
    返回原因字符串 = 熔断(整轮降级 propose);None = 未触发。

    候选数太少(< _CIRCUIT_MIN_CANDIDATES)时比例无意义(小书 1 个候选只要改
    就是 100%),直接不熔断——此时逐条闸 1/2/5 仍在把关,不靠比例这道粗网。
    """
    if n_candidates < _CIRCUIT_MIN_CANDIDATES:
        return None
    actual = n_mutating / n_candidates
    if actual > ratio:
        return (f"熔断:本轮 {n_mutating}/{n_candidates} = {actual:.0%} 的候选被提议修改,"
                f"超过阈值 {ratio:.0%};疑为模型状态异常,整轮不自动应用")
    return None


class KatexUnavailable(RuntimeError):
    """node/KaTeX 不可用 —— 没有校验能力就不放行。"""


def build_katex_probe_md(results: list[AgentResult],
                         candidates_by_id: dict[str, dict]) -> str:
    """把所有会改 md 的建议拼成一个探针 md,复用 katex_scan 的页/块归属注释约定,
    使一次 node 调用即可校验整轮。"""
    parts: list[str] = []
    for r in results:
        if r.verdict not in _MUTATING:
            continue
        cand = candidates_by_id.get(r.candidate_id)
        if cand is None:
            continue
        parts.append(f"<!-- page: {cand['page']} block_ids: {cand['block_id']} -->\n"
                     f"$$\n{r.latex}\n$$")
    return "\n\n".join(parts) + ("\n" if parts else "")


def katex_gate(results: list[AgentResult], candidates_by_id: dict[str, dict], *,
               work_dir: str, scan_fn=None
               ) -> tuple[list[AgentResult], list[GateRejection]]:
    """闸 1:建议 LaTeX 必须能被 KaTeX 解析,否则丢弃该条。

    只调一次 node(整轮批量),复用与浏览器同版本的 KaTeX oracle。
    scan_fn 返回 None(node 缺失) → 抛 KatexUnavailable,调用方整轮降级 propose。
    """
    scan = scan_fn or scan_katex
    if not any(r.verdict in _MUTATING for r in results):
        return list(results), []

    md = build_katex_probe_md(results, candidates_by_id)
    os.makedirs(work_dir, exist_ok=True)
    fd, probe_md = tempfile.mkstemp(prefix=".katex_probe_", suffix=".md", dir=work_dir)
    os.close(fd)
    probe_out = probe_md + ".json"
    try:
        with open(probe_md, "w", encoding="utf-8") as f:
            f.write(md)
        report = scan(probe_md, probe_out)
    finally:
        for p in (probe_md, probe_out):
            if os.path.exists(p):
                os.remove(p)

    if report is None:
        raise KatexUnavailable(
            "node 不可用,KaTeX 准入闸无法执行 —— 拒绝在无校验能力时自动应用")

    bad: dict[tuple[int, int], str] = {}
    for err in report.get("errors", []):
        page = err.get("page")
        for bid in (err.get("block_ids") or []):
            try:
                key = (int(page), int(bid))
            except (TypeError, ValueError):
                # 无法解析的错误记录跳过映射即可:映射不上就不会误拒任何候选。
                continue
            bad[key] = str(err.get("error") or "KaTeX 解析失败")

    passed: list[AgentResult] = []
    rejected: list[GateRejection] = []
    for r in results:
        if r.verdict not in _MUTATING:
            passed.append(r)
            continue
        cand = candidates_by_id.get(r.candidate_id)
        if cand is not None:
            try:
                key = (int(cand["page"]), int(cand["block_id"]))
            except (TypeError, ValueError):
                # 候选自身 page/block_id 畸形,无法确定归属 —— 保守拒收,
                # 绝不放行(放行=可能把未经校验的公式写进书),也绝不崩溃。
                rejected.append(GateRejection(
                    r.candidate_id, "katex", "候选 page/block_id 非法,无法确定页/块归属,保守拒收"))
                continue
        else:
            key = None
        if key is not None and key in bad:
            rejected.append(GateRejection(r.candidate_id, "katex", bad[key]))
        else:
            passed.append(r)
    return passed, rejected


def snapshot_md(md_path: str) -> str | None:
    """闸 5 前置:应用前对最终 md 做快照。md 不存在返回 None。"""
    if not os.path.exists(md_path):
        return None
    snap = md_path + ".pre_agent.bak"
    shutil.copy2(md_path, snap)
    return snap


def rollback_md(md_path: str, snapshot_path: str) -> None:
    shutil.copy2(snapshot_path, md_path)


def regression_guard(md_path: str, *, work_dir: str, baseline_hard_errors: int,
                     scan_fn=None) -> str | None:
    """闸 5:应用并重建后,KaTeX 硬错不得增加。

    返回原因字符串 = 出现回归(调用方须自动回滚);None = 通过。
    node 不可用时同样判为不通过 —— 无法验证就不敢放行(同闸 1 的保守原则)。
    (Tier0 missing_chars 回归由收尾的 SOP-03 另行检查。)
    """
    scan = scan_fn or scan_katex
    os.makedirs(work_dir, exist_ok=True)
    out = os.path.join(work_dir, ".katex_regression.json")
    try:
        report = scan(md_path, out)
    finally:
        if os.path.exists(out):
            os.remove(out)

    if report is None:
        return "回归检查无法执行(node 不可用),保守判定为不通过"

    hard = len(report.get("errors", []))
    if hard > baseline_hard_errors:
        return f"回归:应用后 KaTeX 硬错 {baseline_hard_errors} → {hard},自动回滚整轮"
    return None
