"""调度核心:分批 → 同厂独立限流 → 按冻结链 fallback → 单条升级/交叉验证。

F6: 限流键是 provider,不是整场运行 —— 每个 provider 独立 Semaphore(3),
    厂商之间并行,绝不设全局共享三槽。
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field, replace

from scripts.pipelines.textbooks.formula_agents.latex_equiv import latex_equiv
from scripts.pipelines.textbooks.formula_agents.ledger import batch_id
from scripts.pipelines.textbooks.formula_agents.protocol import (
    AgentResult, ProtocolError, validate_agent_payload,
)

_UNCERTAIN = "uncertain"
_CORRECT = "correct"


@dataclass
class BatchOutcome:
    batch_id: str
    candidate_ids: list[str]
    resolved: list[AgentResult] = field(default_factory=list)
    pending_ids: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    status: str = "done"          # "done" | "blocked"


def chunk_candidates(candidates: list[dict], batch_size: int = 10) -> list[list[dict]]:
    """默认每批 10(F4:一次 39 张会把识图、工具循环、上下文和序列化混在一起)。"""
    if batch_size <= 0:
        raise ValueError("batch_size 必须为正")
    items = list(candidates)
    return [items[i:i + batch_size] for i in range(0, len(items), batch_size)]


@dataclass
class DispatchState:
    """跨批共享的调度状态:同厂限流槽、provider 连续失败计数、已停用 provider。

    每次 run 新建一个并显式传入 —— 不用模块级全局:那会让不同 run/不同测试互相污染,
    还得配一个只为测试存在的 reset 函数。
    """
    semaphores: dict[str, threading.Semaphore]
    blocked: set[str] = field(default_factory=set)
    failure_counts: dict[str, int] = field(default_factory=dict)
    lock: threading.Lock = field(default_factory=threading.Lock)

    @classmethod
    def for_adapters(cls, adapters, per_provider: int = 3) -> "DispatchState":
        """每 provider 一个独立 Semaphore —— 不是一个全局池(F6)。"""
        return cls(semaphores={a.name: threading.Semaphore(per_provider)
                               for a in adapters})

    def is_blocked(self, provider: str) -> bool:
        with self.lock:
            return provider in self.blocked

    def note_failure(self, provider: str, limit: int) -> None:
        with self.lock:
            n = self.failure_counts.get(provider, 0) + 1
            self.failure_counts[provider] = n
            if n >= limit:
                self.blocked.add(provider)

    def note_success(self, provider: str) -> None:
        """"连续"失败:成功一次即清零。"""
        with self.lock:
            self.failure_counts[provider] = 0


def _entries(candidates: list[dict]) -> list[dict]:
    return [{"candidate_id": c["candidate_id"], "crop_path": c.get("crop_path", ""),
             "engine_latex": c.get("engine_latex", "")} for c in candidates]


def dispatch_with_fallback(batch: list[dict], adapters, *, state: DispatchState,
                           confidence_threshold: float = 0.8,
                           max_consecutive_failures: int = 3,
                           timeout: int = 300) -> BatchOutcome:
    """把一批候选按冻结链投给 provider,直到全部定案或链耗尽。

    整批换下家: probe 失败 / 协议失败。
    单条升级:   verdict=uncertain;verdict=correct 但 confidence < 阈值(交叉验证)。
    链耗尽仍未定案 → pending(md 不动)。
    """
    ids = [c["candidate_id"] for c in batch]
    out = BatchOutcome(batch_id=batch_id(ids), candidate_ids=list(ids))

    by_id = {c["candidate_id"]: c for c in batch}
    remaining = list(batch)
    awaiting_cross: dict[str, AgentResult] = {}   # 低置信待交叉验证的上一家结果

    for attempt_no, adapter in enumerate(adapters, 1):
        if not remaining:
            break
        if state.is_blocked(adapter.name):
            out.attempts.append({
                "provider": adapter.name, "outcome": "blocked",
                "escalated_ids": [c["candidate_id"] for c in remaining],
                "error": "该 provider 已因连续失败被停用"})
            continue
        if not adapter.probe():
            out.attempts.append({
                "provider": adapter.name, "outcome": "unavailable",
                "escalated_ids": [c["candidate_id"] for c in remaining],
                "error": "CLI 不可用(probe 失败)"})
            continue

        started = time.time()
        with state.semaphores[adapter.name]:      # F6: 同厂独立限流
            raw = adapter(_entries(remaining), timeout=timeout)
        ended = time.time()

        rec = {
            "provider": adapter.name,
            "model": getattr(adapter, "model", ""),
            "effort": getattr(adapter, "effort", ""),
            "attempt": attempt_no,
            "started": started, "ended": ended,
            "exit_code": raw.exit_code,
            "stdout": raw.stdout, "stderr": raw.stderr,
            "sent_ids": [c["candidate_id"] for c in remaining],
        }

        try:
            results = validate_agent_payload(
                raw.stdout, [c["candidate_id"] for c in remaining])
        except ProtocolError as e:
            rec.update({"outcome": "protocol_fail", "valid": False, "error": str(e),
                        "escalated_ids": [c["candidate_id"] for c in remaining]})
            out.attempts.append(rec)
            state.note_failure(adapter.name, max_consecutive_failures)
            continue                              # 整批拒收,原样交下一家

        state.note_success(adapter.name)

        settled: list[AgentResult] = []
        escalate: list[dict] = []

        for r in results:
            r = replace(r, provider=adapter.name,
                        model=getattr(adapter, "model", ""),
                        effort=getattr(adapter, "effort", ""),
                        attempt=attempt_no)
            cid = r.candidate_id

            prior = awaiting_cross.pop(cid, None)
            if prior is not None:
                # 本轮是交叉验证:两家 MathML 等价才采用(不是字符串相等 —— 容忍
                # \dfrac/\frac、\left(/(、x^2/x^{2} 这类写法差异)。equiv 返回 None
                # (node 缺失)保守当"不等价",不改。
                equiv = latex_equiv(r.latex, prior.latex) if r.verdict == _CORRECT else False
                if equiv is True:
                    settled.append(replace(
                        prior, confidence=max(prior.confidence, r.confidence),
                        cross_checked_by=adapter.name))
                else:
                    escalate.append(by_id[cid])
                    awaiting_cross[cid] = prior   # 留待更后面的 provider 再验
                continue

            if r.verdict == _UNCERTAIN:
                escalate.append(by_id[cid])
                continue

            if r.verdict == _CORRECT and r.confidence < confidence_threshold:
                awaiting_cross[cid] = r
                escalate.append(by_id[cid])
                continue

            settled.append(r)   # accept / not_formula_error / 高置信 correct

        out.resolved.extend(settled)
        rec.update({"outcome": "resolved" if not escalate else "partial",
                    "valid": True, "error": None,
                    "resolved_ids": [r.candidate_id for r in settled],
                    "escalated_ids": [c["candidate_id"] for c in escalate]})
        out.attempts.append(rec)
        remaining = escalate

    out.pending_ids = [c["candidate_id"] for c in remaining]
    if adapters and all(state.is_blocked(a.name) for a in adapters):
        out.status = "blocked"
    return out
