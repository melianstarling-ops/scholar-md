from contextlib import contextmanager

from scripts.pipelines.textbooks import power


def test_keep_system_awake_noop_off_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(power.os, "name", "posix")
    monkeypatch.setattr(power, "_set_thread_execution_state",
                        lambda flags: calls.append(flags) or 1)
    with power.keep_system_awake():
        pass
    assert calls == []


def test_keep_system_awake_sets_and_restores_on_windows(monkeypatch):
    calls = []
    monkeypatch.setattr(power.os, "name", "nt")
    monkeypatch.setattr(power, "_set_thread_execution_state",
                        lambda flags: calls.append(flags) or 1)
    with power.keep_system_awake():
        pass
    assert calls == [
        power.ES_CONTINUOUS | power.ES_SYSTEM_REQUIRED | power.ES_AWAYMODE_REQUIRED,
        power.ES_CONTINUOUS,
    ]


def test_keep_system_awake_disabled_does_nothing(monkeypatch):
    calls = []
    monkeypatch.setattr(power.os, "name", "nt")
    monkeypatch.setattr(power, "_set_thread_execution_state",
                        lambda flags: calls.append(flags) or 1)
    with power.keep_system_awake(enabled=False):
        pass
    assert calls == []
