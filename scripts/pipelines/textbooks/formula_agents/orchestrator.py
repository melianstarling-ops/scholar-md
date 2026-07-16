"""调度核心:分批 → 同厂独立限流 → 按冻结链 fallback → 单条升级/交叉验证。

F6: 限流键是 provider,不是整场运行 —— 每个 provider 独立 Semaphore(3),
    厂商之间并行,绝不设全局共享三槽。
"""
from __future__ import annotations

import datetime as _dt
import os
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace

from scripts.pipelines.textbooks.formula_agents import gates
from scripts.pipelines.textbooks.formula_agents.corrections_map import (
    build_corrections_payload, write_corrections,
)
from scripts.pipelines.textbooks.formula_agents.latex_equiv import latex_equiv
from scripts.pipelines.textbooks.formula_agents.ledger import (
    append_ledger, batch_id, load_ledger, resume_pending,
)
from scripts.pipelines.textbooks.formula_agents.protocol import (
    AgentResult, ProtocolError, validate_agent_payload,
)
from scripts.pipelines.textbooks.formula_candidates import collect_formula_candidates

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

            if (r.verdict == _CORRECT
                    and gates.is_pure_token_reorder(
                        r.latex, by_id[cid].get("engine_latex", ""))):
                # 高置信但属"保序重排"—— 闸2(similarity_gate)的已知盲区,
                # 强制交叉验证,不因高置信直接采用。
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


@dataclass
class RunReport:
    stem: str
    mode: str
    n_candidates: int = 0
    applied: int = 0
    rejected: list = field(default_factory=list)      # list[gates.GateRejection]
    pending_ids: list[str] = field(default_factory=list)
    circuit_broken: bool = False
    rolled_back: bool = False
    reason: str | None = None


def _as_candidate_list(raw) -> list[dict]:
    """collect_fn 的默认实现(collect_formula_candidates)返回
    {"candidates": [...], "summary": {...}};测试用的 fake collect_fn 直接返回
    list[dict]。两种形状都接受,不因调用方是谁而崩。"""
    if isinstance(raw, dict) and "candidates" in raw:
        return list(raw["candidates"])
    return list(raw)


def run_agents(layout, *, adapters, pdf_path: str, dpi: int = 300,
               batch_size: int = 10, per_provider: int = 3,
               confidence_threshold: float = 0.8, circuit_ratio: float = 0.6,
               mode: str = "apply", collect_fn=None, scan_fn=None,
               reassemble_fn=None, today: str | None = None) -> RunReport:
    """公式 Agent 终检全流程。

    不变量:所有失败路径的最坏结果都是"这条没改",绝不是"这条被改坏了"。
    """
    from scripts.pipelines.textbooks.convert import reassemble_md

    collect = collect_fn or collect_formula_candidates
    reassemble = reassemble_fn or reassemble_md
    scan = scan_fn or gates.scan_katex
    today = today or _dt.date.today().isoformat()

    report = RunReport(stem=layout.stem, mode=mode)

    candidates = _as_candidate_list(collect(layout))
    report.n_candidates = len(candidates)
    if not candidates:
        report.reason = "无候选,无需处理"
        return report

    if mode == "dry-run":
        report.reason = f"dry-run:{len(candidates)} 个候选,未调用模型"
        return report

    by_id = {c["candidate_id"]: c for c in candidates}
    ledger_path = os.path.join(layout.repair_dir, "formula_agent_ledger.jsonl")
    verdicts_path = os.path.join(layout.repair_dir, "formula_agent_verdicts.jsonl")

    # --- 调度(断点续跑 + 逐批落 ledger)---
    todo = resume_pending(load_ledger(ledger_path), candidates, batch_size=batch_size)

    # Fix A: 所有批次已在先前运行终态(done/blocked)—— 本次没有新东西要跑。
    # corrections.json 是整轮产物(write_corrections 整文件覆盖),若继续往下走,
    # `results` 只会从本次(空的)outcomes 重建,进而用空列表覆盖掉先前运行已经
    # 落盘、已验证的 corrections.json,导致下次 reassemble_md 把已应用的修正
    # 全部还原成 OCR。这里直接早返回:不碰 corrections.json,不 reassemble,
    # 也不重新评估闸门/熔断(那些已经在产出 corrections.json 的那次运行里判过了)。
    if not todo:
        report.reason = "所有批次已在先前运行完成,沿用既有 corrections.json,md 不改动"
        report.applied = 0
        return report

    batches = chunk_candidates(todo, batch_size)
    state = DispatchState.for_adapters(adapters, per_provider=per_provider)

    outcomes: list[BatchOutcome] = []
    with ThreadPoolExecutor(max_workers=max(1, len(batches))) as pool:
        futures = [pool.submit(dispatch_with_fallback, b, adapters, state=state,
                               confidence_threshold=confidence_threshold)
                   for b in batches]
        for fut in futures:
            oc = fut.result()
            outcomes.append(oc)
            append_ledger(ledger_path, {
                "batch_id": oc.batch_id, "candidate_ids": oc.candidate_ids,
                "attempts": oc.attempts,
                "resolved": [r.__dict__ for r in oc.resolved],
                "pending_ids": oc.pending_ids, "status": oc.status,
            })

    # Fix B: 崩溃续跑时,本次 outcomes 只覆盖 todo(本次要跑的批次)。若之前的运行
    # 已经把某些批次跑到终态(done/blocked)但那次运行本身没能把 corrections.json
    # 写完整(例如中途崩溃),这些批次的 resolved 只存在于 ledger 里,从未被读回。
    # 从 ledger(此刻已含刚 append 的 + 更早的)读回本次没跑到的终态批次,
    # 与本次 outcomes 的 resolved 合并,corrections.json 才能覆盖全部批次。
    current_resolved = [r for oc in outcomes for r in oc.resolved]
    current_batch_ids = {oc.batch_id for oc in outcomes}

    prior_resolved: list[AgentResult] = []
    seen = set(current_batch_ids)
    for row in load_ledger(ledger_path):
        bid = row.get("batch_id")
        if row.get("status") in ("done", "blocked") and bid not in seen:
            seen.add(bid)
            for d in row.get("resolved", []):
                prior_resolved.append(AgentResult(**d))

    results = current_resolved + prior_resolved
    report.pending_ids = [cid for oc in outcomes for cid in oc.pending_ids]

    # 全部 verdict 落证据台账(不进 md)
    for r in results:
        append_ledger(verdicts_path, r.__dict__)

    # --- 闸 3 → 闸 2 → 闸 1 ---
    survivors: list[AgentResult] = []
    for r in results:
        rej = gates.degenerate_gate(r)
        if rej is None:
            rej = gates.similarity_gate(
                r, (by_id.get(r.candidate_id) or {}).get("engine_latex", ""))
        if rej is not None:
            report.rejected.append(rej)
        else:
            survivors.append(r)

    try:
        survivors, katex_rejected = gates.katex_gate(
            survivors, by_id, work_dir=layout.repair_dir, scan_fn=scan_fn)
        report.rejected.extend(katex_rejected)
    except gates.KatexUnavailable as e:
        mode = report.mode = "propose"
        report.reason = str(e)

    mutating = [r for r in survivors if r.verdict == _CORRECT]

    # --- 闸 4: 全局熔断 ---
    if mode == "apply":
        tripped = gates.circuit_breaker(len(mutating), len(candidates),
                                        ratio=circuit_ratio)
        if tripped:
            mode = report.mode = "propose"
            report.circuit_broken = True
            report.reason = tripped

    # --- propose: 只落 pending,md 一字不动 ---
    if mode != "apply":
        write_corrections(layout.corrections_path, build_corrections_payload(
            survivors, by_id, stem=layout.stem, today=today, status="pending"))
        return report

    # --- 无可应用条目(如全 provider 失败/全部 pending/全被前四闸拒收):
    #     不动 md、不重建、不做回归检查 —— 没有要写的东西就不该碰 md,
    #     这正是"最坏结果是没改"这条不变量本身,不是可选优化。---
    if not mutating:
        write_corrections(layout.corrections_path, build_corrections_payload(
            survivors, by_id, stem=layout.stem, today=today, status="accepted"))
        report.applied = 0
        return report

    # --- apply: baseline → 快照 → 写 accepted → 重建 → 闸 5 ---
    base = scan(layout.md_path,
                os.path.join(layout.repair_dir, ".katex_baseline.json"))
    baseline = len(base.get("errors", [])) if base else 0

    snap = gates.snapshot_md(layout.md_path)
    corr_snap = gates.snapshot_corrections(layout.corrections_path)

    write_corrections(layout.corrections_path, build_corrections_payload(
        survivors, by_id, stem=layout.stem, today=today, status="accepted"))
    reassemble(layout, pdf_path, dpi)

    regression = gates.regression_guard(
        layout.md_path, work_dir=layout.repair_dir,
        baseline_hard_errors=baseline, scan_fn=scan_fn)
    if regression:
        if snap:
            gates.rollback_md(layout.md_path, snap)
        gates.rollback_corrections(layout.corrections_path, corr_snap)
        report.rolled_back = True
        report.reason = regression
        report.applied = 0
        return report

    report.applied = len(mutating)
    return report
