"""Audit helpers for the 39-image formula pressure benchmark."""

from __future__ import annotations

import argparse
import json
import unicodedata
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path


_TEXT_MACROS = (r"\text", r"\mbox", r"\operatorname")
_CONFIDENCE_RANK = {"low": 0, "medium": 1, "high": 2}


def _strip_outer_wrapper(text: str) -> str:
    stripped = text.strip()
    pairs = (("$$", "$$"), (r"\[", r"\]"), (r"\(", r"\)"), ("$", "$"))
    for opening, closing in pairs:
        if stripped.startswith(opening) and stripped.endswith(closing):
            inner = stripped[len(opening):len(stripped) - len(closing)]
            if opening != "$" or "$" not in inner:
                return inner.strip()
    return stripped


def normalize_latex(latex: str | None) -> str | None:
    if latex is None:
        return None
    text = unicodedata.normalize("NFC", latex).replace("\r\n", "\n").replace("\r", "\n")
    text = _strip_outer_wrapper(text)
    output: list[str] = []
    index = 0
    while index < len(text):
        macro = next((item for item in _TEXT_MACROS if text.startswith(item, index)), None)
        if macro is not None:
            output.append(macro)
            index += len(macro)
            while index < len(text) and text[index].isspace():
                index += 1
            if index < len(text) and text[index] == "{":
                depth = 0
                start = index
                while index < len(text):
                    char = text[index]
                    if char == "{" and (index == 0 or text[index - 1] != "\\"):
                        depth += 1
                    elif char == "}" and (index == 0 or text[index - 1] != "\\"):
                        depth -= 1
                        if depth == 0:
                            index += 1
                            break
                    index += 1
                output.append(text[start:index])
            continue
        if not text[index].isspace():
            output.append(text[index])
        index += 1
    return "".join(output)


def group_candidate_outputs(rows: list[tuple[str, dict]]) -> list[dict]:
    groups: list[dict] = []
    by_key: dict[tuple[str, str | None], dict] = {}
    for supporter, row in rows:
        key = (row["classification"], normalize_latex(row.get("latex")))
        group = by_key.get(key)
        if group is None:
            group = {
                "classification": row["classification"],
                "latex": row.get("latex"),
                "normalized_latex": key[1],
                "supporters": [],
                "min_confidence": row.get("confidence", ""),
                "notes": [],
            }
            by_key[key] = group
            groups.append(group)
        group["supporters"].append(supporter)
        confidence = row.get("confidence", "")
        current = group["min_confidence"]
        if _CONFIDENCE_RANK.get(confidence, -1) < _CONFIDENCE_RANK.get(current, -1):
            group["min_confidence"] = confidence
        note = str(row.get("note", "")).strip()
        if note and note not in group["notes"]:
            group["notes"].append(note)
    return groups


def build_comparison(baseline: list[dict], models: dict[str, list[dict]]) -> dict:
    model_maps = {
        name: {row["candidate_id"]: row for row in rows}
        for name, rows in models.items()
    }
    disagreements: list[dict] = []
    for root_row in baseline:
        candidate_id = root_row["candidate_id"]
        labelled = [("ROOT", {**root_row, "note": root_row.get("note", "")})]
        for name, rows_by_id in model_maps.items():
            labelled.append((name, rows_by_id[candidate_id]))
        groups = group_candidate_outputs(labelled)
        if len(groups) > 1:
            disagreements.append({"candidate_id": candidate_id, "groups": groups})
    return {
        "candidate_count": len(baseline),
        "model_count": len(models),
        "disagreement_count": len(disagreements),
        "candidates": disagreements,
    }


def _md_code(value: str | None) -> str:
    if value is None:
        return "`null`"
    return "`" + value.replace("|", r"\|").replace("\n", " ") + "`"


def render_first_round_report(baseline: list[dict], grades: dict,
                              run_summary: dict) -> str:
    lines = [
        "# Formula CLI Pressure Test — First Round",
        "",
        "计分口径：原始裁图优先；空格与无害换行等价。可见的箭头、粗体/花体、大小写、prime、underbrace、项、上下限与矩阵结构必须保留。",
        "",
        "## 1. ROOT 完整审阅",
        "",
        "| candidate_id | 分类 | ROOT LaTeX |",
        "|---|---|---|",
    ]
    for row in baseline:
        lines.append(
            f'| {row["candidate_id"]} | {row["classification"]} | {_md_code(row.get("latex"))} |'
        )

    models = sorted(grades["models"], key=lambda item: (-item["correct"], item["label"]))
    total = len(baseline)
    lines.extend([
        "",
        "## 2. 模型严格计分",
        "",
        "结构有效只表示返回了完整 JSON，不代表公式正确。首轮没有模型达到 100%。",
        "",
        "| 模型 | 正确 | 错误 | 正确率 |",
        "|---|---:|---:|---:|",
    ])
    for model in models:
        correct = model["correct"]
        errors = len(model["errors"])
        lines.append(
            f'| {model["label"]} | {correct}/{total} | {errors} | {correct / total:.1%} |'
        )

    failures_by_candidate: dict[str, list[str]] = {}
    for model in models:
        for candidate_id in model["errors"]:
            failures_by_candidate.setdefault(candidate_id, []).append(model["label"])
    baseline_by_id = {row["candidate_id"]: row for row in baseline}
    lines.extend([
        "",
        "## 3. 归并后仅看差异",
        "",
        "下表只列至少一个模型被判错的候选；未出现的候选在所有纳入严格计分的模型中均通过。",
        "",
        "| candidate_id | ROOT | 判错模型数 | 判错模型 |",
        "|---|---|---:|---|",
    ])
    for candidate_id in [row["candidate_id"] for row in baseline if row["candidate_id"] in failures_by_candidate]:
        failed = failures_by_candidate[candidate_id]
        lines.append(
            f'| {candidate_id} | {_md_code(baseline_by_id[candidate_id].get("latex"))} | '
            f'{len(failed)} | {"; ".join(failed)} |'
        )

    lines.extend([
        "",
        "## 4. 调用与协议可靠性",
        "",
        f'首轮主矩阵：{run_summary["finished"]} 个配置；结构有效 {run_summary["valid"]}，失败 {run_summary["failed"]}。',
        "Claude Haiku High 在去除 plan 后重试通过 39 条结构校验并纳入语义计分；Haiku Medium 重试仍有 5 条 formula 返回 null，未纳入。",
        "Agy Flash Medium 返回了 39 条但 JSON 含非法反斜杠转义；Agy Flash High 没有最终 JSON，二者均按协议失败处理。",
        "",
        "| job | 秒 | 协议状态 | 错误 |",
        "|---|---:|---|---|",
    ])
    for status in sorted(run_summary["statuses"], key=lambda item: item["job_id"]):
        error = str(status.get("error", "")).replace("|", r"\|")
        lines.append(
            f'| {status["job_id"]} | {status["duration_seconds"]:.3f} | '
            f'{"valid" if status["valid"] else "failed"} | {error} |'
        )

    lines.extend([
        "",
        "## 5. 工程结论",
        "",
        "- 调度必须按提供商建立独立 3 路线程池，不能让一个全局队列造成队头阻塞。",
        "- Claude 纯读图任务不能使用 plan 权限模式；应使用 dontAsk 与 Read-only 工具白名单。",
        "- 父线程独占汇总文件写入；子任务只写各自 job 目录。",
        "- JSON 合同失败与公式语义失败分开记分；退出码 0 不能替代结构校验。",
        "- 39 图单请求可行，但首轮证明任何单模型都不能直接假定 100% 正确。",
        "",
        "机器可读附件：`validated_summary.json`、`disagreements.json`、`manual_grades.json`。",
        "",
    ])
    return "\n".join(lines)


def _write_json(path: Path, data: object) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--harness", type=Path, required=True)
    parser.add_argument("--baseline", type=Path)
    parser.add_argument("--grades", type=Path)
    args = parser.parse_args()

    spec = spec_from_file_location("formula_pressure_harness", args.harness)
    if spec is None or spec.loader is None:
        raise SystemExit(f"cannot load harness: {args.harness}")
    harness = module_from_spec(spec)
    spec.loader.exec_module(harness)

    run_dir = args.run_dir.resolve()
    if args.baseline is not None:
        baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
        _write_json(run_dir / "root_baseline.json", {"results": baseline})
    else:
        baseline = json.loads((run_dir / "root_baseline.json").read_text(encoding="utf-8"))["results"]
    expected_ids = [row["candidate_id"] for row in baseline]
    summary = json.loads((run_dir / "run_summary.json").read_text(encoding="utf-8"))
    models: dict[str, list[dict]] = {}
    validation_records: list[dict] = []
    for status in summary["statuses"]:
        record = dict(status)
        if status["valid"]:
            result_path = run_dir / "jobs" / status["job_id"] / "result.json"
            rows = json.loads(result_path.read_text(encoding="utf-8"))["results"]
            harness.validate_payload({"results": rows}, expected_ids)
            label = f'{status["backend"]}:{status["model"]}:{status["effort"]}'
            models[label] = rows
        validation_records.append(record)

    for effort in ("high", "medium"):
        job_id = f"claude-claude-haiku-4-5-{effort}-retry-no-plan"
        stdout_path = run_dir / "jobs" / job_id / "stdout.txt"
        record = {
            "job_id": job_id,
            "backend": "claude",
            "model": "claude-haiku-4-5",
            "effort": effort,
            "valid": False,
            "error": "",
        }
        try:
            stdout = stdout_path.read_text(encoding="utf-8")
            rows = harness.validate_payload(
                harness.result_payload("claude", stdout), expected_ids
            )
            record["valid"] = True
            label = f"claude:claude-haiku-4-5:{effort}:retry-no-plan"
            models[label] = rows
            _write_json(run_dir / "jobs" / job_id / "result.json", {"results": rows})
        except Exception as exc:
            record["error"] = f"{type(exc).__name__}: {exc}"
        validation_records.append(record)

    validated_summary = {
        "strict_valid_models": len(models),
        "records": validation_records,
    }
    _write_json(run_dir / "validated_summary.json", validated_summary)
    _write_json(run_dir / "disagreements.json", build_comparison(baseline, models))
    if args.grades is not None:
        grades = json.loads(args.grades.read_text(encoding="utf-8"))
        _write_json(run_dir / "manual_grades.json", grades)
        (run_dir / "first_round_report.md").write_text(
            render_first_round_report(baseline, grades, summary), encoding="utf-8"
        )
    print(json.dumps({
        "strict_valid_models": len(models),
        "disagreement_count": build_comparison(baseline, models)["disagreement_count"],
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
