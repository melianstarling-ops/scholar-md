"""AgentResult → 既有 corrections.json 记录。

只有 verdict == "correct" 才产 correction;accept / uncertain / not_formula_error
一律不产 —— 它们不改 md。

status 默认 "accepted"(已通过五道准入闸,自动生效);propose/熔断模式传 "pending"。
"""
from __future__ import annotations

import json
import os

from scripts.pipelines.textbooks.formula_agents.protocol import AgentResult
from scripts.pipelines.textbooks.vision_repair import content_fingerprint

_MUTATING = "correct"

_MATH_WRAPPERS = ("$$", "\\[", "\\]", "$")


def _strip_math_wrappers(latex: str) -> str:
    """剥掉模型可能自带的外层数学定界符,避免与下面的 $$ 包裹重复成双层。

    prompt 已要求 latex 只放本体,但模型未必遵守(真机 smoke 见 claude 自带 $$);
    这里兜底,只剥外层、不动公式内容。
    """
    s = (latex or "").strip()
    for _ in range(3):
        changed = False
        for w in _MATH_WRAPPERS:
            if s.startswith(w):
                s = s[len(w):].strip(); changed = True
            if s.endswith(w):
                s = s[: -len(w)].strip(); changed = True
        if not changed:
            break
    return s


def to_correction(result: AgentResult, candidate: dict, *, today: str,
                  status: str = "accepted") -> dict:
    """扩展既有 correction record 形状,补 provenance(provider/model/effort/attempt)。"""
    engine_latex = candidate.get("engine_latex") or ""
    return {
        "page": candidate["page"],
        "block_id": candidate["block_id"],
        "kind": "+".join(candidate.get("reasons", [])),
        "engine_latex": engine_latex,
        "corrected_latex": f"$$ {_strip_math_wrappers(result.latex)} $$",
        "source": f"agent:{result.provider}:{result.model}",
        "confidence": result.confidence,
        "content_fingerprint": content_fingerprint(engine_latex),
        "status": status,
        "ts": today,
        # provenance —— 事后可追、可回滚
        "candidate_id": result.candidate_id,
        "provider": result.provider,
        "model": result.model,
        "effort": result.effort,
        "attempt": result.attempt,
        "verdict": result.verdict,
        "cross_checked_by": result.cross_checked_by,
        "note": result.note,
    }


def build_corrections_payload(results: list[AgentResult],
                              candidates_by_id: dict[str, dict], *,
                              stem: str, today: str,
                              status: str = "accepted") -> dict:
    """只有 correct 进 corrections;其余 verdict 不改 md。"""
    corrections = [
        to_correction(r, candidates_by_id[r.candidate_id], today=today, status=status)
        for r in results
        if r.verdict == _MUTATING and r.candidate_id in candidates_by_id
    ]
    return {"stem": stem, "corrections": corrections}


def write_corrections(path: str, payload: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
