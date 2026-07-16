"""Build a self-contained visual review report for formula CLI benchmarks.

This module consumes frozen benchmark crops/results only.  It is deliberately
independent from ``debug_view`` and never reads a source PDF.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import re
import unicodedata
from pathlib import Path


HERE = Path(__file__).resolve().parent
ASSETS = HERE / "formula_benchmark_assets"
VENDOR = HERE / "debug_assets" / "vendor"
_TEXT_MACROS = (r"\text", r"\mbox", r"\operatorname")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _strip_outer_wrapper(text: str) -> str:
    stripped = text.strip()
    for opening, closing in (("$$", "$$"), (r"\[", r"\]"),
                             (r"\(", r"\)"), ("$", "$")):
        if stripped.startswith(opening) and stripped.endswith(closing):
            inner = stripped[len(opening):len(stripped) - len(closing)]
            if opening != "$" or "$" not in inner:
                return inner.strip()
    return stripped


def normalize_latex(latex: str | None) -> str | None:
    """Normalize wrappers and insignificant whitespace, not mathematical style."""
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


def _status_records(run_dir: Path) -> list[dict]:
    validated = run_dir / "validated_summary.json"
    summary = run_dir / "run_summary.json"
    if validated.exists():
        return list(_read_json(validated).get("records", []))
    if summary.exists():
        return list(_read_json(summary).get("statuses", []))
    raise ValueError(f"missing validated_summary.json/run_summary.json: {run_dir}")


def _model_label(round_index: int, status: dict) -> str:
    backend = {"claude": "Claude", "codex": "Codex", "agy": "Antigravity",
               "kimi": "Kimi"}.get(status.get("backend"), str(status.get("backend", "?")))
    return f'R{round_index} · {backend} · {status.get("model", "?")} · {status.get("effort", "?")}'


def _validate_results(rows: list[dict], expected_ids: list[str], result_path: Path) -> None:
    ids = [row.get("candidate_id") for row in rows]
    if ids != expected_ids:
        raise ValueError(f"candidate order mismatch: {result_path}")
    for row in rows:
        if row.get("classification") not in {"formula", "formula_number", "mixed_text"}:
            raise ValueError(f"invalid classification in {result_path}: {row}")


def _image_b64(candidate: dict, run_dir: Path) -> str:
    raw = candidate.get("image_path")
    if not raw:
        raise ValueError(f'missing image_path for {candidate.get("candidate_id")}')
    path = Path(raw)
    if not path.exists():
        path = run_dir / "images" / f'{candidate["candidate_id"]}.png'
    if not path.exists():
        raise ValueError(f"frozen crop missing: {path}")
    return base64.b64encode(path.read_bytes()).decode("ascii")


def _display_run_ids(run_dirs: list[Path]) -> list[str]:
    """Use directory names when a copied manifest repeats an earlier run id."""
    seen: set[str] = set()
    output: list[str] = []
    for run_dir in run_dirs:
        manifest_id = str(_read_json(run_dir / "manifest.json").get("run_id", run_dir.name))
        display_id = run_dir.name if manifest_id in seen else manifest_id
        seen.add(display_id)
        output.append(display_id)
    return output


def _load_models(run_dirs: list[Path], run_ids: list[str],
                 expected_ids: list[str]) -> tuple[list[dict], list[dict]]:
    models: list[dict] = []
    statuses: list[dict] = []
    for round_index, run_dir in enumerate(run_dirs, 1):
        run_id = run_ids[round_index - 1]
        for source in _status_records(run_dir):
            status = dict(source)
            status["round_index"] = round_index
            status["round_id"] = run_id
            status["label"] = _model_label(round_index, status)
            status["model_key"] = f'r{round_index}:{status.get("job_id", "unknown")}'
            result_path = run_dir / "jobs" / str(status.get("job_id")) / "result.json"
            if status.get("valid") and not result_path.exists():
                status["valid"] = False
                status["error"] = "valid status has no result.json"
            if status.get("valid"):
                rows = _read_json(result_path).get("results", [])
                _validate_results(rows, expected_ids, result_path)
                model = dict(status)
                model["results"] = {row["candidate_id"]: row for row in rows}
                models.append(model)
            statuses.append({key: value for key, value in status.items()
                             if key not in {"results"}})
    return models, statuses


def _group_outputs(candidate_id: str, root: dict, models: list[dict]) -> tuple[list[dict], list[dict]]:
    grouped: dict[tuple[str, str | None], dict] = {}
    all_models: list[dict] = []
    root_key = (root["classification"], normalize_latex(root.get("latex")))
    for model in models:
        row = model["results"][candidate_id]
        item = {
            "model_key": model["model_key"],
            "label": model["label"],
            "round_index": model["round_index"],
            "backend": model.get("backend", ""),
            "model": model.get("model", ""),
            "effort": model.get("effort", ""),
            "classification": row["classification"],
            "latex": row.get("latex"),
            "confidence": row.get("confidence", ""),
            "note": row.get("note", ""),
        }
        all_models.append(item)
        key = (row["classification"], normalize_latex(row.get("latex")))
        group = grouped.setdefault(key, {
            "classification": row["classification"],
            "latex": row.get("latex"),
            "normalized_latex": key[1],
            "matches_root": key == root_key,
            "supporters": [],
        })
        group["supporters"].append({
            "model_key": model["model_key"],
            "label": model["label"],
            "confidence": row.get("confidence", ""),
            "note": row.get("note", ""),
        })
    groups = list(grouped.values())
    groups.sort(key=lambda group: (not group["matches_root"], -len(group["supporters"]),
                                   str(group["normalized_latex"])))
    return groups, all_models


def load_report_data(
    run_dirs: list[Path],
    confirmed_errors: dict[str, set[str]] | None = None,
) -> dict:
    """Load frozen runs into the JSON-ready report payload."""
    if not run_dirs:
        raise ValueError("at least one run directory is required")
    run_dirs = [Path(path).resolve() for path in run_dirs]
    first_manifest = _read_json(run_dirs[0] / "manifest.json")
    source_candidates = first_manifest.get("candidates", [])
    root_path = run_dirs[0] / "root_baseline.json"
    if not root_path.exists():
        raise ValueError(f"first run has no root_baseline.json: {run_dirs[0]}")
    root_rows = _read_json(root_path).get("results", [])
    expected_ids = [row["candidate_id"] for row in source_candidates]
    _validate_results(root_rows, expected_ids, root_path)
    root_by_id = {row["candidate_id"]: row for row in root_rows}
    run_ids = _display_run_ids(run_dirs)
    models, statuses = _load_models(run_dirs, run_ids, expected_ids)

    candidates = []
    disagreement_count = 0
    for source in source_candidates:
        candidate_id = source["candidate_id"]
        root = root_by_id[candidate_id]
        candidate_models = models
        if confirmed_errors is not None:
            candidate_models = [
                model for model in models
                if candidate_id in confirmed_errors.get(model["model_key"], set())
            ]
            if not candidate_models:
                continue
        groups, all_models = _group_outputs(candidate_id, root, candidate_models)
        has_disagreement = len(groups) != 1 or not groups[0]["matches_root"]
        if confirmed_errors is not None:
            has_disagreement = True
        disagreement_count += int(has_disagreement)
        candidates.append({
            "candidate_id": candidate_id,
            "page": source.get("page"),
            "block_id": source.get("block_id"),
            "block_label": source.get("block_label", ""),
            "reasons": source.get("reasons", []),
            "image_b64": _image_b64(source, run_dirs[0]),
            "root": root,
            "groups": groups,
            "all_models": all_models,
            "has_disagreement": has_disagreement,
        })

    run_meta = []
    for index, run_id in enumerate(run_ids, 1):
        run_meta.append({"index": index, "run_id": run_id})
    return {
        "title": "39 Formula Crops · Multi-model Review",
        "generated": dt.datetime.now().astimezone().isoformat(timespec="seconds"),
        "runs": run_meta,
        "summary": {
            "candidate_count": len(candidates),
            "valid_model_count": len(models),
            "failed_model_count": sum(not status.get("valid") for status in statuses),
            "run_count": len(run_dirs),
        },
        "disagreement_count": disagreement_count,
        "models": statuses,
        "candidates": candidates,
    }


def _read(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def render_report(data: dict) -> str:
    payload_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")
    template = _read(ASSETS / "template.html")
    replacements = {
        "{{TITLE}}": str(data.get("title", "Formula benchmark review")),
        "{{KATEX_CSS}}": _read(VENDOR / "katex.inline.css"),
        "{{REPORT_CSS}}": _read(ASSETS / "report.css"),
        "{{KATEX_JS}}": _read(VENDOR / "katex.min.js"),
        "{{REPORT_JS}}": _read(ASSETS / "report.js"),
        "{{PAYLOAD_JSON}}": payload_json,
    }
    for marker, value in replacements.items():
        template = template.replace(marker, value)
    if re.search(r"\{\{[A-Z_]+\}\}", template):
        raise ValueError("unresolved template marker")
    return template


def write_report(
    run_dirs: list[Path],
    output_path: Path,
    confirmed_errors: dict[str, set[str]] | None = None,
) -> Path:
    output_path = Path(output_path).resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        render_report(load_report_data(run_dirs, confirmed_errors=confirmed_errors)),
        encoding="utf-8",
    )
    return output_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a self-contained formula benchmark review HTML")
    parser.add_argument("--run-dir", action="append", type=Path, required=True,
                        help="benchmark directory; repeat in chronological order")
    parser.add_argument("--out", type=Path, required=True, help="output HTML path")
    parser.add_argument("--confirmed-errors", type=Path,
                        help="JSON object mapping model_key to confirmed-error candidate IDs")
    args = parser.parse_args()
    confirmed_errors = None
    if args.confirmed_errors:
        raw_errors = _read_json(args.confirmed_errors)
        confirmed_errors = {key: set(value) for key, value in raw_errors.items()}
    output = write_report(args.run_dir, args.out, confirmed_errors=confirmed_errors)
    data = load_report_data(args.run_dir, confirmed_errors=confirmed_errors)
    print(f"[formula_benchmark_report] {output}")
    print(f'  candidates={data["summary"]["candidate_count"]} '
          f'valid_models={data["summary"]["valid_model_count"]} '
          f'failed_models={data["summary"]["failed_model_count"]}')


if __name__ == "__main__":
    main()
