"""Process-scoped keep-awake guard for long textbook conversions."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import os

ES_CONTINUOUS = 0x80000000
ES_SYSTEM_REQUIRED = 0x00000001
ES_AWAYMODE_REQUIRED = 0x00000040


def _set_thread_execution_state(flags: int) -> int:
    return ctypes.windll.kernel32.SetThreadExecutionState(flags)  # type: ignore[attr-defined]


@contextmanager
def keep_system_awake(enabled: bool = True):
    """Prevent Windows system sleep while this process is running.

    This does not change the user's power plan permanently. On non-Windows
    platforms it is a no-op. Display sleep is intentionally not blocked.
    """
    active = False
    if enabled and os.name == "nt":
        flags = ES_CONTINUOUS | ES_SYSTEM_REQUIRED | ES_AWAYMODE_REQUIRED
        active = bool(_set_thread_execution_state(flags))
        if active:
            print("[power] 已阻止系统睡眠(进程退出后恢复)。", flush=True)
        else:
            print("[power] 阻止系统睡眠失败,继续转换。", flush=True)
    try:
        yield
    finally:
        if active:
            _set_thread_execution_state(ES_CONTINUOUS)
            print("[power] 已恢复系统睡眠策略。", flush=True)
