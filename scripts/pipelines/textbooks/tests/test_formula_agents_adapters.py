"""adapters.py 的 build_invocation / _image_flags / _crop_dirs 单测。

只断言 argv/stdin 结构,绝不 shell-out 真实 CLI(纯字符串装配,离线可跑)。
argv 已按真机逐一实测通过的调用校准(见 adapters.py 模块 docstring)。
"""
import os

from scripts.pipelines.textbooks.formula_agents.adapters import (
    _MODELS, _crop_dirs, _image_flags, build_invocation,
)


def _abs(p: str) -> str:
    return os.path.abspath(p)


def test_codex_uses_image_flag_per_crop_and_stdin_prompt():
    entries = [
        {"candidate_id": "p0001-b0001", "crop_path": "crops/a/1.png"},
        {"candidate_id": "p0002-b0002", "crop_path": "crops/b/2.png"},
    ]
    argv, stdin = build_invocation("codex", entries, "PROMPT")
    assert argv[:5] == ["cmd", "/c", "codex", "exec", "-"]
    assert argv.count("--image") == 2
    idx1 = argv.index("--image")
    assert argv[idx1 + 1] == _abs("crops/a/1.png")
    idx2 = argv.index("--image", idx1 + 1)
    assert argv[idx2 + 1] == _abs("crops/b/2.png")
    assert "--add-dir" not in argv
    assert stdin == "PROMPT"


def test_agy_gemini_uses_plan_mode_and_prompt_in_argv_not_stdin():
    entries = [{"candidate_id": "p0001-b0001", "crop_path": "crops/a/1.png"}]
    argv, stdin = build_invocation("gemini", entries, "PROMPT")
    assert argv[:4] == ["agy", "--sandbox", "--mode", "plan"]
    assert "--add-dir" in argv
    assert argv[-2:] == ["--print", "PROMPT"]
    assert stdin is None
    assert "--dangerously-skip-permissions" not in argv


def test_kimi_has_print_and_stdio_flags_and_add_dir_and_stdin_prompt():
    entries = [{"candidate_id": "p0001-b0001", "crop_path": "crops/a/1.png"}]
    argv, stdin = build_invocation("kimi", entries, "PROMPT")
    assert "--print" in argv
    assert "--final-message-only" in argv
    assert "--output-format" in argv and "text" in argv
    assert "--add-dir" in argv
    assert stdin == "PROMPT"


def test_claude_has_permission_and_read_tool_flags_and_stdin_prompt():
    entries = [{"candidate_id": "p0001-b0001", "crop_path": "crops/a/1.png"}]
    argv, stdin = build_invocation("claude", entries, "PROMPT")
    assert "--permission-mode" in argv and "dontAsk" in argv
    assert "--allowedTools" in argv and "Read" in argv
    assert stdin == "PROMPT"


def test_image_flags_dedupe_crop_dirs_for_add_dir_providers():
    entries = [
        {"candidate_id": "a", "crop_path": "crops/shared/1.png"},
        {"candidate_id": "b", "crop_path": "crops/shared/2.png"},
    ]
    flags = _image_flags("kimi", entries)
    assert flags.count("--add-dir") == 1
    assert flags == ["--add-dir", _abs("crops/shared")]


def test_image_flags_produce_one_image_flag_per_crop_for_codex():
    entries = [
        {"candidate_id": "a", "crop_path": "crops/shared/1.png"},
        {"candidate_id": "b", "crop_path": "crops/shared/2.png"},
    ]
    flags = _image_flags("codex", entries)
    assert flags == ["--image", _abs("crops/shared/1.png"),
                     "--image", _abs("crops/shared/2.png")]


def test_image_flags_skip_empty_crop_path():
    entries = [{"candidate_id": "a", "crop_path": ""},
               {"candidate_id": "b"}]
    assert _image_flags("codex", entries) == []
    assert _image_flags("claude", entries) == []
    assert _crop_dirs(entries) == []


def test_model_provenance_matches_verified_pressure_run():
    assert _MODELS["kimi"]["model"] == "kimi-code/kimi-for-coding"
    assert _MODELS["gemini"]["model"] == "Gemini 3.1 Pro (High)"
    assert _MODELS["codex"]["model"] == "gpt-5.6-terra"
    assert _MODELS["claude"]["model"] == "claude-sonnet-4-6"
