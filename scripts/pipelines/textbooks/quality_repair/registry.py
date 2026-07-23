"""Explicit capability registry; ordering never acts as hidden precedence."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable

from .models import DetectorContext, Finding


Detector = Callable[[DetectorContext], list[Finding]]


@dataclass(frozen=True)
class Capability:
    name: str
    detector: Detector
    schema_version: int = 1


class Registry:
    def __init__(self, capabilities: Iterable[Capability] = ()) -> None:
        caps = list(capabilities)
        names = [cap.name for cap in caps]
        if len(set(names)) != len(names):
            raise ValueError("duplicate capability name")
        self._capabilities = tuple(sorted(caps, key=lambda cap: cap.name))

    @property
    def capabilities(self) -> tuple[Capability, ...]:
        return self._capabilities

    def detect(self, context: DetectorContext) -> list[Finding]:
        findings: list[Finding] = []
        for capability in self._capabilities:
            emitted = capability.detector(context)
            for finding in emitted:
                if finding.capability != capability.name:
                    raise ValueError(
                        f"detector {capability.name} emitted capability {finding.capability}")
            findings.extend(emitted)
        return sorted(findings, key=lambda item: item.finding_id)
