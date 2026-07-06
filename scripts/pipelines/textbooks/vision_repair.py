"""公式视觉修复第二步:无头 `claude -p` 读裁图 → 修正 LaTeX → corrections.json。

只接 claude 后端(所有者决定:Kimi 暂缓,等实际调试阶段再评估是否接入)。调用套路
照抄 Project_MRI_Safety `kb_core.py` 的 subprocess-via-stdin 手法,已用真实裁图
(p49 eq_1.58,曲面 s' 案例)验证过无头 CLI 能读本地图片并正确转写 LaTeX。
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from scripts.pipelines.textbooks.paths import DocLayout, resolve_layout


def content_fingerprint(text: str) -> str:
    """引擎块内容的短哈希,供 corrections.json 与当前 res.json 块内容比对防漂移。"""
    return hashlib.sha256((text or "").encode("utf-8")).hexdigest()[:16]


def _strip_fence(s: str) -> str:
    s = re.sub(r"^```(?:json)?\s*", "", s.strip())
    return re.sub(r"\s*```$", "", s).strip()


def parse_vision_response(stdout: str) -> dict:
    """解 `claude -p --output-format json` 的输出:外层信封的 result 字段是一段
    JSON 字符串(模型按 prompt 要求回 {"latex","confidence"}),取出 latex/confidence,
    连同信封自带的 cost 一起返回。任一层解析失败都抛 ValueError(不静默吞错,调用方
    决定要不要重试/回炉)。"""
    try:
        envelope = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"claude -p 输出不是合法 JSON 信封:{e}") from e
    inner_text = envelope.get("result", "")
    try:
        inner = json.loads(_strip_fence(inner_text))
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"信封 result 字段不是合法 JSON:{e}") from e
    return {
        "latex": inner.get("latex", ""),
        "confidence": inner.get("confidence", ""),
        "cost_usd": envelope.get("total_cost_usd", 0) or 0,
    }


def _extract_json_array(text: str) -> list:
    """从模型输出里抓最后一个配平的 JSON 数组(同 kb_core.extract_json_array 手法:
    agent 有时会在数组前后加寒暄/围栏,真答案在最末尾,从末尾向前找配平的 [...] 再验证)。"""
    s = _strip_fence(text)
    end = s.rfind("]")
    while end != -1:
        depth = 0
        for i in range(end, -1, -1):
            if s[i] == "]":
                depth += 1
            elif s[i] == "[":
                depth -= 1
                if depth == 0:
                    try:
                        arr = json.loads(s[i:end + 1])
                        if isinstance(arr, list):
                            return arr
                    except json.JSONDecodeError:
                        pass
                    break
        end = s.rfind("]", 0, end)
    raise ValueError("未找到可解析的 JSON 数组")


def build_vision_prompt(crop_path: str) -> str:
    return (
        "Read the image file at this exact path using your Read tool:\n"
        f"{crop_path}\n\n"
        "It shows a single mathematical formula, cropped from an OCR engine's output "
        "page. The engine may have mis-structured it (e.g. treating an integral "
        "contour/surface label like c' or s' as a fraction denominator and dropping "
        "the real denominator, or dropping sub/superscripts on a big operator like "
        "\\oint/\\int/\\sum/\\lim). Transcribe the formula exactly as shown in the "
        "image into correct LaTeX. For script-style capital letters (e.g. a "
        "calligraphic E for electric field), use \\mathcal{} — this book's "
        "convention — not \\mathscr{} or \\mathfrak{}.\n\n"
        "Respond with ONLY a single JSON object, no markdown fences, no explanation, "
        "in this exact shape:\n"
        '{"latex": "<LaTeX source, without $$ wrappers>", '
        '"confidence": "high|medium|low"}'
    )


def build_batch_vision_prompt(entries: list[dict]) -> str:
    """把多张裁图打包进一次调用:每张图标一个 key,模型逐张用 Read 工具读完,回一个
    JSON 数组(一图一条),而不是一图一进程(省重复启动/系统提示开销,同 kb_shallow_batch
    对文本的打包手法,这里换成图片路径)。"""
    listing = "\n".join(f'- key="{e["key"]}": {e["crop_path"]}' for e in entries)
    return (
        "You will be given several cropped formula images from an OCR-scanned "
        "textbook page. For EACH one, read the image file at its given path using "
        "your Read tool, then transcribe the formula into correct LaTeX. The engine "
        "may have mis-structured it (e.g. treating an integral contour/surface "
        "label like c' or s' as a fraction denominator and dropping the real "
        "denominator, or dropping sub/superscripts on a big operator like "
        "\\oint/\\int/\\sum/\\lim). For script-style capital letters (e.g. a "
        "calligraphic E for electric field), use \\mathcal{} — this book's "
        "convention — not \\mathscr{} or \\mathfrak{}.\n\n"
        "Images:\n"
        f"{listing}\n\n"
        "Respond with ONLY a single JSON array, no markdown fences, no explanation, "
        "one object per image in this exact shape:\n"
        '[{"key": "<same key as given above>", '
        '"latex": "<LaTeX source, without $$ wrappers>", '
        '"confidence": "high|medium|low"}, ...]'
    )


def parse_batch_vision_response(stdout: str) -> dict:
    """解批量调用的信封,取 result 里的 JSON 数组,按 key 转成 dict。"""
    try:
        envelope = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as e:
        raise ValueError(f"claude -p 输出不是合法 JSON 信封:{e}") from e
    arr = _extract_json_array(envelope.get("result", ""))
    return {
        str(item.get("key")): {"latex": item.get("latex", ""),
                               "confidence": item.get("confidence", "")}
        for item in arr if isinstance(item, dict) and item.get("key") is not None
    }


def _resolve_claude_bin() -> list[str]:
    """解析可被 subprocess 直接调用的 claude 前缀。

    Windows 上 `claude` 是 npm 的 `.cmd` shim,Python subprocess(CreateProcess)不认
    `.cmd`;优先 `node <node_modules 入口.cjs>` 绕开 shim,同 Project_MRI_Safety
    `kb_core.resolve_backend_argv` 的踩坑与解法(K7:.cmd/.bat 才需 `cmd /c`,.exe 直呼)。
    """
    shim = shutil.which("claude") or shutil.which("claude.cmd")
    node = shutil.which("node")
    if shim and node:
        entry = Path(shim).parent.joinpath(
            "node_modules", "@anthropic-ai", "claude-code", "cli-wrapper.cjs")
        if entry.exists():
            return [node, str(entry)]
    if shim and os.name == "nt":
        if shim.lower().endswith(".exe"):
            return [shim]
        return ["cmd", "/c", shim]
    return [shim or "claude"]


def call_claude_vision(crop_path: str, timeout: int = 120,
                       claude_argv: list[str] | None = None) -> dict:
    """无头调 `claude -p` 读一张裁图,返回 parse_vision_response 的结果。

    prompt 走 stdin(同 kb_core 手法);`--strict-mcp-config` 禁 MCP 防慢/防炸进程,
    不影响内置 Read 工具读图(已实测验证,见 2026-07-04 交接讨论)。
    """
    prompt = build_vision_prompt(crop_path)
    argv = (claude_argv or _resolve_claude_bin()) + \
        ["--strict-mcp-config", "--output-format", "json", "-p"]
    proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=timeout)
    return parse_vision_response(proc.stdout or "")


def call_claude_vision_batch(entries: list[dict], timeout: int = 300,
                             claude_argv: list[str] | None = None) -> dict:
    """一次调用读多张裁图(entries: [{"key","crop_path"}, ...]),返回 key→{latex,confidence}。"""
    prompt = build_batch_vision_prompt(entries)
    argv = (claude_argv or _resolve_claude_bin()) + \
        ["--strict-mcp-config", "--output-format", "json", "-p"]
    proc = subprocess.run(argv, input=prompt, capture_output=True, text=True,
                         encoding="utf-8", errors="replace", timeout=timeout)
    return parse_batch_vision_response(proc.stdout or "")


def _chunk(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _correction_record(item: dict, result: dict, today: str) -> dict:
    return {
        "page": item["page"],
        "block_id": item["block_id"],
        "kind": "+".join(item.get("kinds", [])),
        "engine_latex": item["engine_latex"],
        "corrected_latex": f"$$ {result['latex']} $$",
        "source": "claude-vision",
        "confidence": result.get("confidence", ""),
        "content_fingerprint": content_fingerprint(item["engine_latex"]),
        "status": "pending",   # 人工确认门(红线):新产出的修正一律待审,不自动生效
        "ts": today,
    }


def run_vision_repair(layout: DocLayout, batch_fn=call_claude_vision_batch,
                      vision_fn=call_claude_vision, batch_size: int = 5,
                      parallel: int = 3, timeout: int = 300) -> dict:
    """读 Module1 产出的 worklist.json,把疑似块按 batch_size 打包,并发(parallel)
    调 batch_fn 一次读多张裁图(省重复启动/系统提示开销,同 SOP-Batch_Agent_Run 的
    "适度并发 + 别裸 for 循环"规程)。批里没拿到结果的 key(模型漏答/整批调用异常)
    回炉成单图调用(vision_fn);单图也失败才计入 failed,不掀翻整批。写
    `<stem>_corrections.json`(§2 叠加层 schema)。"""
    stem = layout.stem
    worklist_path = layout.worklist_path
    if not os.path.exists(worklist_path):
        raise ValueError(f"缺 worklist.json,先跑 build_repair_worklist:{worklist_path}")
    with open(worklist_path, encoding="utf-8") as f:
        worklist = json.load(f)

    items = worklist.get("items", [])
    for item in items:
        item["key"] = f"{item['page']}_{item['block_id']}"
    batches = _chunk(items, batch_size)

    def run_one_batch(batch):
        try:
            return batch_fn(
                [{"key": it["key"], "crop_path": it["crop_path"]} for it in batch],
                timeout=timeout)
        except Exception:                                        # noqa: BLE001 整批失败转单图回炉
            return {}

    n = len(batches)
    results_by_key: dict = {}
    with ThreadPoolExecutor(max_workers=max(1, parallel)) as ex:
        futs = {ex.submit(run_one_batch, b): i for i, b in enumerate(batches)}
        for fut in as_completed(futs):
            i = futs[fut]
            results_by_key.update(fut.result())
            print(f"[vision_repair] 批 {i + 1}/{n} 完成", flush=True)

    today = date.today().isoformat()
    corrections: list[dict] = []
    failed: list[dict] = []
    for item in items:
        result = results_by_key.get(item["key"])
        if result is None:                                       # 批里漏了 → 单图回炉
            try:
                result = vision_fn(item["crop_path"], timeout=timeout)
            except Exception as e:                                # noqa: BLE001 单项失败不掀翻整批
                failed.append({"page": item["page"], "block_id": item["block_id"],
                               "error": f"{type(e).__name__}: {e}"})
                continue
        corrections.append(_correction_record(item, result, today))

    corrections_path = layout.corrections_path
    os.makedirs(layout.doc_work_dir, exist_ok=True)
    with open(corrections_path, "w", encoding="utf-8") as f:
        json.dump({"stem": stem, "corrections": corrections}, f, ensure_ascii=False, indent=2)
    return {"corrections_path": corrections_path, "count": len(corrections), "failed": failed}


def main() -> None:
    ap = argparse.ArgumentParser(description="疑似公式:无头 claude -p 读裁图 → corrections.json")
    ap.add_argument("--out", required=True, help="交付根(md+assets)")
    ap.add_argument("--work-dir", default=None, help="过程根(默认 <out>/_work_root)")
    ap.add_argument("--stem", required=True, help="文档 stem")
    ap.add_argument("--batch-size", type=int, default=5, help="每次调用打包几张裁图(默认5)")
    ap.add_argument("--parallel", type=int, default=3, help="批间并发数(默认3,对齐 SOP)")
    ap.add_argument("--timeout", type=int, default=300, help="单批调用超时秒(默认300)")
    args = ap.parse_args()
    layout = resolve_layout(args.stem, args.out, args.work_dir)
    result = run_vision_repair(layout, batch_size=args.batch_size,
                               parallel=args.parallel, timeout=args.timeout)
    print(f"[vision_repair] {result['count']} 条修正 → {result['corrections_path']}")
    if result["failed"]:
        print(f"[vision_repair] {len(result['failed'])} 项失败:", result["failed"])


if __name__ == "__main__":
    main()
