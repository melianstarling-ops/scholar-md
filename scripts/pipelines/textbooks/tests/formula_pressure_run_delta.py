from __future__ import annotations

from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


HARNESS_PATH = Path(__file__).with_name("formula_pressure_run.py")
SPEC = spec_from_file_location("formula_pressure_run", HARNESS_PATH)
assert SPEC is not None and SPEC.loader is not None
harness = module_from_spec(SPEC)
SPEC.loader.exec_module(harness)

harness.MATRIX = [
    {"backend": "claude", "model": "claude-sonnet-5", "effort": "medium"},
    {"backend": "claude", "model": "claude-sonnet-5", "effort": "high"},
    {"backend": "claude", "model": "claude-fable-5", "effort": "medium"},
    {"backend": "claude", "model": "claude-fable-5", "effort": "high"},
    {"backend": "kimi", "model": "kimi-code/kimi-for-coding", "effort": "thinking"},
    {"backend": "kimi", "model": "kimi-code/kimi-for-coding-highspeed", "effort": "thinking"},
]


if __name__ == "__main__":
    harness.main()
