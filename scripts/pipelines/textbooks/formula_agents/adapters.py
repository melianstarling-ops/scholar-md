"""Adapter 层:唯一 shell-out 处。core 只吃 RawResponse.stdout,自己做校验。

冻结模型链(2026-07-13 benchmark 定稿,不可改序):
    Kimi > Gemini(Antigravity) > Codex > Claude
CLI argv 参考 tests/formula_pressure_run.py::build_command —— 参考,不 import
(benchmark 不得成为生产依赖)。

本模块不含任何测试替身;FakeAdapter 在 tests/formula_agents_fakes.py。
"""
from __future__ import annotations

import json
import shutil
import subprocess
from typing import Protocol

from scripts.pipelines.textbooks.formula_agents.protocol import RawResponse
from scripts.pipelines.textbooks.vision_repair import resolve_claude_bin

FROZEN_CHAIN = ["kimi", "gemini", "codex", "claude"]

_MODELS = {
    "kimi":   {"model": "kimi-coding",    "effort": "thinking"},
    "gemini": {"model": "gemini-3.1-pro", "effort": "medium"},
    "codex":  {"model": "gpt-5.6-terra",  "effort": "medium"},
    "claude": {"model": "sonnet",         "effort": "medium"},
}


class AgentAdapter(Protocol):
    name: str
    model: str
    effort: str

    def probe(self) -> bool: ...
    def __call__(self, entries: list[dict], *, timeout: int = 300) -> RawResponse: ...


def build_prompt(entries: list[dict]) -> str:
    """索取严格 JSON 数组:逐候选 verdict + latex + confidence + note,顺序固定。"""
    lines = [
        "你是数学公式视觉校对员。逐一查看下列公式裁图,与给出的当前 LaTeX 比对。",
        "",
        "对每个候选判定 verdict:",
        "  accept            —— 当前 LaTeX 与图片一致,无需修改",
        "  correct           —— 当前 LaTeX 有误,给出正确的 LaTeX",
        "  uncertain         —— 图片不清或无法确定,不要猜",
        "  not_formula_error —— 这是表格/版面/标号问题,不是公式 OCR 错误",
        "",
        "判定标准以数学意义为准:空格、等价环境、不明显的粗体差异不算错误;",
        "符号、上下标、正负号、积分域、撇号、运算结构改变才算错误。",
        "",
        "候选列表:",
    ]
    for i, e in enumerate(entries, 1):
        crop = e.get("crop_path") or ""
        lines.append(f"{i}. candidate_id={e['candidate_id']}")
        if crop:
            lines.append(f"   裁图(用 Read 工具读取此路径): {crop}")
        else:
            lines.append("   裁图: 无(仅凭当前 LaTeX 文本判断;拿不准就返回 uncertain)")
        lines.append(f"   当前 LaTeX: {e.get('engine_latex', '')}")
    ids = ", ".join(f'"{e["candidate_id"]}"' for e in entries)
    lines += [
        "",
        "有裁图的候选,先用 Read 工具读取上面给的裁图路径,再与当前 LaTeX 比对。",
        "",
        "只输出一个 JSON 数组,不要任何其他文字。数组必须恰好包含下列 candidate_id,",
        f"顺序完全一致: [{ids}]",
        "",
        "每项格式:",
        '{"candidate_id": "...", "verdict": "accept|correct|uncertain|not_formula_error",',
        ' "latex": "...", "confidence": 0.0, "note": "锚定可见符号的简短证据"}',
        "",
        "confidence 为 0.0-1.0 数值。verdict 为 accept/correct 时 latex 必须非空。",
        "latex 字段只放公式本体,不要用 $$ 或 \\[ \\] 包裹,不要用字面换行。",
        "LaTeX 反斜杠必须按 JSON 规则正确转义。",
    ]
    return "\n".join(lines)


def build_argv(provider: str) -> list[str]:
    """各厂 CLI 的无头调用前缀。prompt 走 stdin。"""
    model = _MODELS[provider]["model"]
    effort = _MODELS[provider]["effort"]
    if provider == "claude":
        # 复用 vision_repair 的 Windows .cmd shim 绕行方案(F8)。
        # --allowedTools Read:无头模式预授权 Read 工具,否则读裁图会被权限提示拦下
        # (2026-07-14 真机 smoke 实测:缺此 flag 时 claude 返回“permission not granted”)。
        return resolve_claude_bin() + [
            "--allowedTools", "Read",
            "--strict-mcp-config", "--output-format", "json", "-p"]
    if provider == "codex":
        return ["codex", "exec", "--model", model,
                "-c", f"model_reasoning_effort={effort}", "-"]
    if provider == "gemini":
        return ["agy", "--model", model, "--effort", effort, "-p"]
    if provider == "kimi":
        return ["kimi", "--model", model, "-p"]
    raise ValueError(f"未知 provider: {provider}")


class CliAdapter:
    """真实外部 CLI adapter。唯一 shell-out 处。"""

    def __init__(self, name: str, argv: list[str] | None = None):
        self.name = name
        self.model = _MODELS[name]["model"]
        self.effort = _MODELS[name]["effort"]
        self._argv = argv or build_argv(name)

    def probe(self) -> bool:
        exe = self._argv[0]
        return bool(shutil.which(exe)) or exe.endswith(".cjs") or "node" in exe

    def __call__(self, entries: list[dict], *, timeout: int = 300) -> RawResponse:
        prompt = build_prompt(entries)
        try:
            proc = subprocess.run(self._argv, input=prompt, capture_output=True,
                                  text=True, encoding="utf-8", errors="replace",
                                  timeout=timeout)
        except subprocess.TimeoutExpired as e:
            return RawResponse("", f"timeout after {timeout}s: {e}", 124)
        except OSError as e:
            return RawResponse("", f"launch failed: {e}", 127)
        stdout = _unwrap_stdout(self.name, proc.stdout or "")
        return RawResponse(stdout, proc.stderr or "", proc.returncode)


def _unwrap_stdout(provider: str, stdout: str) -> str:
    """把各 CLI 的输出信封剥成“含 JSON 数组的纯文本”交给核心校验。

    claude `--output-format json` 把真正结果包在 {"result": "<文本>"} 里,数组的 `[`
    在信封字符串内部,顶层扫描找不到——须先取 result 字段(同 vision_repair 的做法)。
    其余 CLI 暂原样返回。
    """
    if provider != "claude":
        return stdout
    try:
        env = json.loads(stdout)
    except (json.JSONDecodeError, TypeError):
        return stdout
    if isinstance(env, dict) and "result" in env:
        return str(env.get("result") or "")
    return stdout


def default_adapters() -> list[CliAdapter]:
    """按冻结链顺序返回真实 adapter。"""
    return [CliAdapter(name) for name in FROZEN_CHAIN]
