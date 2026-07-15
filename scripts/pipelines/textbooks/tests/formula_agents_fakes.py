"""公式 Agent 测试替身。只供 tests/ 使用,绝不进生产包 formula_agents/。"""
from __future__ import annotations

import threading
import time

from scripts.pipelines.textbooks.formula_agents.protocol import RawResponse


class FakeAdapter:
    """可编程响应、可注入延迟、记录并发峰值。绝不 shell-out。

    responses 按调用序逐个返回;用尽后重复最后一个。空则返回 "[]"。
    peak_concurrency 用于证明同厂限流真的生效(F6)。
    """

    def __init__(self, name: str, responses: list[RawResponse] | None = None, *,
                 available: bool = True, delay: float = 0.0,
                 model: str = "fake", effort: str = "fake"):
        self.name = name
        self.model = model
        self.effort = effort
        self._responses = list(responses or [])
        self._available = available
        self._delay = delay
        self.calls = 0
        self.peak_concurrency = 0
        self._active = 0
        self._lock = threading.Lock()

    def probe(self) -> bool:
        return self._available

    def __call__(self, entries: list[dict], *, timeout: int = 300) -> RawResponse:
        with self._lock:
            self._active += 1
            self.peak_concurrency = max(self.peak_concurrency, self._active)
            self.calls += 1
            idx = self.calls - 1
        try:
            if self._delay:
                time.sleep(self._delay)
            if idx < len(self._responses):
                return self._responses[idx]
            if self._responses:
                return self._responses[-1]
            return RawResponse("[]", "", 0)
        finally:
            with self._lock:
                self._active -= 1
