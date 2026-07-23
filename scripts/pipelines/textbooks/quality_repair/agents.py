"""Generic Agent configuration and strict open-world response protocol."""
from __future__ import annotations

import json
from dataclasses import dataclass, replace
from typing import Callable, Mapping, Any

from .models import EvidencePacket


_VERDICTS = {"accept", "repair", "novel", "uncertain"}
_SEVERITIES = {"P0", "P1", "P2"}


class AgentProtocolError(ValueError):
    pass


@dataclass(frozen=True)
class AgentSpec:
    provider: str
    model: str
    effort: str

    @classmethod
    def parse(cls, value: str) -> "AgentSpec":
        parts = value.split(":")
        if len(parts) != 3 or not all(part.strip() for part in parts):
            raise ValueError("agent must be provider:model:effort")
        return cls(*(part.strip() for part in parts))


@dataclass(frozen=True)
class AgentDecision:
    verdict: str
    issue_family: str
    severity: str
    source_evidence: tuple[str, ...]
    target: Mapping[str, Any]
    replacement: str
    confidence: float
    generalizable: bool
    provider: str = ""
    model: str = ""
    effort: str = ""


def build_agent_prompt(packet: EvidencePacket) -> str:
    payload = json.dumps(packet.to_dict(), ensure_ascii=False, indent=2)
    instructions = [
        "你是 PDF→Markdown 转换质量审阅员。只依据以下最小 EvidencePacket 判定。",
        "不要猜测看不见的内容；证据不足返回 uncertain。",
        "verdict 只能是 accept|repair|novel|uncertain。",
        "只输出一个 JSON 对象，不要 Markdown 代码围栏或解释。",
        "必需字段：\"verdict\", \"issue_family\", \"severity\",",
        "\"source_evidence\", \"target\", \"replacement\",",
        "\"confidence\", \"generalizable\"。repair 必须给 replacement。",
    ]
    if (packet.issue_kind.startswith("source_audit_")
            and packet.target.get("scope") == "block"):
        instructions.extend([
            "这是 source-grounded block 修复：repair 的 replacement 必须是完整的",
            "corrected block_content，不是最终 Markdown 片段；target 必须原样保留",
            "page 与 block_id。系统会绑定 raw fingerprint 并仅重建该页。",
        ])
    instructions.extend(["EvidencePacket:", payload])
    return "\n".join(instructions)


def invoke_cli(spec: AgentSpec, packet: EvidencePacket, timeout: int) -> str:
    """Invoke exactly one explicitly selected provider/model/effort."""
    from scripts.pipelines.textbooks.formula_agents.adapters import run_prompt

    response = run_prompt(
        spec.provider, build_agent_prompt(packet), model=spec.model,
        effort=spec.effort, image_paths=packet.image_paths, timeout=timeout)
    if response.exit_code != 0:
        raise RuntimeError(
            f"{spec.provider} exited {response.exit_code}: {response.stderr[:300]}")
    return response.stdout


def validate_agent_response(stdout: str) -> AgentDecision:
    try:
        data = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise AgentProtocolError(f"response is not one JSON object: {exc}") from exc
    if not isinstance(data, dict):
        raise AgentProtocolError("response must be one JSON object")
    verdict = data.get("verdict")
    if verdict not in _VERDICTS:
        raise AgentProtocolError(f"invalid verdict: {verdict!r}")
    family = data.get("issue_family")
    severity = data.get("severity")
    evidence = data.get("source_evidence")
    target = data.get("target")
    confidence = data.get("confidence")
    generalizable = data.get("generalizable")
    replacement_text = data.get("replacement", "")
    if not isinstance(family, str) or not family.strip():
        raise AgentProtocolError("issue_family must be a non-empty string")
    if severity not in _SEVERITIES:
        raise AgentProtocolError("severity must be P0, P1, or P2")
    if not isinstance(evidence, list) or not all(isinstance(v, str) for v in evidence):
        raise AgentProtocolError("source_evidence must be a string array")
    if not isinstance(target, dict):
        raise AgentProtocolError("target must be an object")
    if not isinstance(confidence, (int, float)) or isinstance(confidence, bool) \
            or not 0.0 <= float(confidence) <= 1.0:
        raise AgentProtocolError("confidence must be between 0 and 1")
    if not isinstance(generalizable, bool):
        raise AgentProtocolError("generalizable must be boolean")
    if not isinstance(replacement_text, str):
        raise AgentProtocolError("replacement must be a string")
    if verdict == "repair" and not replacement_text:
        raise AgentProtocolError("repair verdict requires replacement")
    return AgentDecision(
        verdict=verdict, issue_family=family.strip(), severity=severity,
        source_evidence=tuple(evidence), target=dict(target),
        replacement=replacement_text, confidence=float(confidence),
        generalizable=generalizable,
    )


Invoke = Callable[[AgentSpec, EvidencePacket, int], str]


def route_evidence(packet: EvidencePacket, specs: list[AgentSpec], *,
                   invoke: Invoke, timeout: int = 300) -> AgentDecision | None:
    """Try exactly the user-provided chain; no implicit provider is appended."""
    last_uncertain: AgentDecision | None = None
    for spec in specs:
        try:
            decision = validate_agent_response(invoke(spec, packet, timeout))
        except (AgentProtocolError, OSError, TimeoutError, RuntimeError):
            continue
        decision = replace(decision, provider=spec.provider, model=spec.model,
                           effort=spec.effort)
        if decision.verdict == "uncertain":
            last_uncertain = decision
            continue
        return decision
    return last_uncertain
