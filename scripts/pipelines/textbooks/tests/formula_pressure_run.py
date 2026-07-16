from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path


MATRIX = [
    {"backend": "claude", "model": "claude-sonnet-4-6", "effort": "medium"},
    {"backend": "claude", "model": "claude-sonnet-4-6", "effort": "high"},
    {"backend": "claude", "model": "claude-haiku-4-5", "effort": "medium"},
    {"backend": "claude", "model": "claude-haiku-4-5", "effort": "high"},
    {"backend": "codex", "model": "gpt-5.6-terra", "effort": "medium"},
    {"backend": "codex", "model": "gpt-5.6-terra", "effort": "high"},
    {"backend": "codex", "model": "gpt-5.6-terra", "effort": "xhigh"},
    {"backend": "codex", "model": "gpt-5.6-terra", "effort": "max"},
    {"backend": "codex", "model": "gpt-5.6-terra", "effort": "ultra"},
    {"backend": "codex", "model": "gpt-5.6-luna", "effort": "medium"},
    {"backend": "codex", "model": "gpt-5.6-luna", "effort": "high"},
    {"backend": "codex", "model": "gpt-5.6-luna", "effort": "xhigh"},
    {"backend": "codex", "model": "gpt-5.6-luna", "effort": "max"},
    {"backend": "codex", "model": "gpt-5.5", "effort": "medium"},
    {"backend": "codex", "model": "gpt-5.5", "effort": "high"},
    {"backend": "codex", "model": "gpt-5.5", "effort": "xhigh"},
    {"backend": "agy", "model": "Gemini 3.5 Flash (Medium)", "effort": "medium"},
    {"backend": "agy", "model": "Gemini 3.5 Flash (High)", "effort": "high"},
    {"backend": "agy", "model": "Gemini 3.1 Pro (High)", "effort": "high"},
    {"backend": "kimi", "model": "kimi-code/kimi-for-coding", "effort": "thinking"},
]


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


def base_job_id(job: dict) -> str:
    return f'{job["backend"]}-{slug(job["model"])}-{job["effort"]}'


def next_job_id(job: dict, args: argparse.Namespace) -> str:
    base = base_job_id(job)
    jobs_dir = args.run_dir / "jobs"
    if not (jobs_dir / base).exists():
        return base
    attempt = 1
    while (jobs_dir / f"{base}-retry-{attempt}").exists():
        attempt += 1
    return f"{base}-retry-{attempt}"


def find_existing_valid_status(job: dict, args: argparse.Namespace,
                               expected_ids: list[str]) -> dict | None:
    base = base_job_id(job)
    jobs_dir = args.run_dir / "jobs"
    candidates = []
    if (jobs_dir / base).is_dir():
        candidates.append(jobs_dir / base)
    candidates.extend(sorted(jobs_dir.glob(f"{base}-retry-*"), reverse=True))
    for job_dir in candidates:
        status_path = job_dir / "status.json"
        result_path = job_dir / "result.json"
        if not status_path.is_file() or not result_path.is_file():
            continue
        try:
            status = json.loads(status_path.read_text(encoding="utf-8"))
            if not status.get("valid"):
                continue
            payload = json.loads(result_path.read_text(encoding="utf-8"))
            validate_payload(payload, expected_ids)
        except (OSError, ValueError, json.JSONDecodeError):
            continue
        return status
    return None


def write_json(path: Path, data: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def extract_last_json(text: str) -> object:
    decoder = json.JSONDecoder()
    candidates = [m.start() for m in re.finditer(r"[\[{]", text)]
    parsed: list[tuple[int, int, object]] = []
    for start in candidates:
        try:
            value, end = decoder.raw_decode(text[start:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, (dict, list)):
            parsed.append((start + end, -start, value))
    if not parsed:
        raise ValueError("no JSON object or array found")
    return max(parsed, key=lambda item: (item[0], item[1]))[2]


def result_payload(backend: str, stdout: str) -> object:
    results_start = stdout.rfind('{"results"')
    if results_start >= 0:
        try:
            direct, _end = json.JSONDecoder().raw_decode(stdout[results_start:])
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid results JSON: {exc}") from exc
        if isinstance(direct, dict) and "results" in direct:
            return direct
    outer = extract_last_json(stdout)
    if backend == "claude" and isinstance(outer, dict) and "result" in outer:
        result = outer["result"]
        if isinstance(result, str):
            return extract_last_json(result)
        return result
    return outer


def validate_payload(payload: object, expected_ids: list[str]) -> list[dict]:
    if isinstance(payload, dict):
        rows = payload.get("results")
    else:
        rows = payload
    if not isinstance(rows, list):
        raise ValueError("payload does not contain a results array")
    if len(rows) != len(expected_ids):
        raise ValueError(f"expected {len(expected_ids)} records, got {len(rows)}")
    by_id: dict[str, dict] = {}
    for row in rows:
        if not isinstance(row, dict):
            raise ValueError("result record is not an object")
        candidate_id = str(row.get("candidate_id", ""))
        if candidate_id in by_id:
            raise ValueError(f"duplicate candidate_id: {candidate_id}")
        if candidate_id not in expected_ids:
            raise ValueError(f"unknown candidate_id: {candidate_id}")
        classification = row.get("classification")
        if classification not in {"formula", "formula_number", "mixed_text"}:
            raise ValueError(f"bad classification for {candidate_id}: {classification}")
        latex = row.get("latex")
        if classification == "mixed_text":
            if latex not in (None, ""):
                raise ValueError(f"mixed_text latex must be null/empty: {candidate_id}")
        elif not isinstance(latex, str) or not latex.strip():
            raise ValueError(f"missing latex: {candidate_id}")
        by_id[candidate_id] = {
            "candidate_id": candidate_id,
            "classification": classification,
            "latex": latex,
            "confidence": row.get("confidence", ""),
            "note": row.get("note", ""),
        }
    missing = [candidate_id for candidate_id in expected_ids if candidate_id not in by_id]
    if missing:
        raise ValueError(f"missing candidate ids: {missing}")
    return [by_id[candidate_id] for candidate_id in expected_ids]


def build_prompt(candidates: list[dict]) -> str:
    listing = "\n".join(
        f'- candidate_id="{row["candidate_id"]}": {row["image_path"]}' for row in candidates
    )
    return f"""You are running a controlled formula-image transcription benchmark.

Read EACH of the following 39 independent image files separately. Do not infer from OCR text,
filenames, neighboring records, or outside sources. Use only what is visibly present in each image.
Most images contain one display formula. A few contain only a formula number/unit, and one may be a
false-positive mixed prose block. Preserve all visible subscripts, superscripts, hats, vectors,
primes, integration/summation limits, braces, matrices, underbrace labels, operators, and text labels.
Use \\mathcal{{E}}, \\mathcal{{H}}, and \\mathcal{{J}} for this book's script field symbols.

Classify each image as exactly one of:
- formula: a mathematical formula; return LaTeX without $$ wrappers.
- formula_number: a standalone parenthesized equation number or unit; return it as LaTeX.
- mixed_text: prose containing inline mathematics rather than a standalone formula; set latex to null
  and briefly note that it must not be replaced by a single formula.

Images:
{listing}

Return ONLY one JSON object, without markdown fences or explanation, in this exact shape:
{{"results":[{{"candidate_id":"p0000-b0000","classification":"formula|formula_number|mixed_text","latex":"LaTeX or null","confidence":"high|medium|low","note":"optional"}}]}}

Hard requirements: exactly 39 result records, exactly once per supplied candidate_id, in supplied order.
"""


def build_command(job: dict, args: argparse.Namespace, prompt: str,
                  image_paths: list[str], schema_path: Path) -> tuple[list[str], str | None]:
    backend = job["backend"]
    if backend == "claude":
        return ([args.claude, "--strict-mcp-config", "--disable-slash-commands",
                 "--no-session-persistence", "--permission-mode", "dontAsk",
                 "--add-dir", str(args.run_dir),
                 "--tools", "Read", "--allowedTools", "Read",
                 "--model", job["model"], "--effort", job["effort"],
                 "--output-format", "json", "-p"], prompt)
    if backend == "codex":
        command = [args.codex, "exec", "-", "--ephemeral", "--ignore-rules",
                   "--sandbox", "read-only", "--cd", str(args.repo),
                   "--model", job["model"],
                   "--config", f'model_reasoning_effort="{job["effort"]}"',
                   "--output-schema", str(schema_path), "--color", "never"]
        for path in image_paths:
            command.extend(["--image", path])
        return command, prompt
    if backend == "agy":
        return ([args.agy, "--sandbox", "--mode", "plan", "--add-dir", str(args.run_dir),
                 "--model", job["model"],
                 "--print-timeout", "30m", "--print", prompt], None)
    if backend == "kimi":
        return ([args.kimi, "--work-dir", str(args.repo), "--add-dir", str(args.run_dir),
                 "--model", job["model"], "--thinking", "--mcp-config", "{}", "--print",
                 "--input-format", "text", "--output-format", "text",
                 "--final-message-only"], prompt)
    raise ValueError(backend)


def run_jobs_by_provider(jobs: list[dict], runner, per_provider: int = 3,
                         on_result=None) -> list[dict]:
    """Run each provider in its own pool so one provider cannot block another."""
    grouped: dict[str, list[dict]] = {}
    for job in jobs:
        grouped.setdefault(job["backend"], []).append(job)
    executors = {
        backend: ThreadPoolExecutor(max_workers=per_provider)
        for backend in grouped
    }
    futures = []
    try:
        for backend, provider_jobs in grouped.items():
            executor = executors[backend]
            futures.extend(executor.submit(runner, job) for job in provider_jobs)
        results = []
        for future in as_completed(futures):
            result = future.result()
            results.append(result)
            if on_result is not None:
                on_result(result)
        return results
    finally:
        for executor in executors.values():
            executor.shutdown(wait=True)


def run_job(job: dict, args: argparse.Namespace, prompt: str, expected_ids: list[str],
            image_paths: list[str], schema_path: Path, semaphore: threading.Semaphore) -> dict:
    job_id = next_job_id(job, args)
    job_dir = args.run_dir / "jobs" / job_id
    job_dir.mkdir(parents=True, exist_ok=False)
    command, stdin = build_command(job, args, prompt, image_paths, schema_path)
    request = {**job, "job_id": job_id, "started_at": None, "command": command,
               "stdin_mode": stdin is not None, "candidate_count": len(expected_ids)}
    write_json(job_dir / "request.json", request)
    env = os.environ.copy()
    if env.get("USERPROFILE"):
        env["HOME"] = env["USERPROFILE"]
    env["PYTHONUTF8"] = "1"
    env["NO_COLOR"] = "1"
    with semaphore:
        started = utc_now()
        request["started_at"] = started
        write_json(job_dir / "request.json", request)
        print(f"[{started}] START {job_id}", flush=True)
        t0 = time.monotonic()
        try:
            proc = subprocess.run(command, input=stdin, cwd=args.repo, env=env,
                                  capture_output=True, text=True, encoding="utf-8",
                                  errors="replace", timeout=args.timeout)
            stdout = proc.stdout or ""
            stderr = proc.stderr or ""
            exit_code = proc.returncode
            timed_out = False
        except subprocess.TimeoutExpired as exc:
            stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
            exit_code = None
            timed_out = True
        duration = round(time.monotonic() - t0, 3)
        (job_dir / "stdout.txt").write_text(stdout, encoding="utf-8")
        (job_dir / "stderr.txt").write_text(stderr, encoding="utf-8")
        status = {
            "job_id": job_id,
            "backend": job["backend"],
            "model": job["model"],
            "effort": job["effort"],
            "started_at": started,
            "finished_at": utc_now(),
            "duration_seconds": duration,
            "exit_code": exit_code,
            "timed_out": timed_out,
            "valid": False,
            "error": "",
        }
        try:
            if timed_out:
                raise ValueError("timeout")
            if exit_code != 0:
                raise ValueError(f"nonzero exit code: {exit_code}")
            rows = validate_payload(result_payload(job["backend"], stdout), expected_ids)
            write_json(job_dir / "result.json", {"results": rows})
            status["valid"] = True
        except Exception as exc:  # benchmark rule: record failure, never retry
            status["error"] = f"{type(exc).__name__}: {exc}"
        write_json(job_dir / "status.json", status)
        print(f'[{status["finished_at"]}] END   {job_id} valid={status["valid"]} '
              f'duration={duration}s error={status["error"]}', flush=True)
        return status


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--baseline", type=Path, required=True)
    parser.add_argument("--claude", required=True)
    parser.add_argument("--codex", required=True)
    parser.add_argument("--agy", required=True)
    parser.add_argument("--kimi", required=True)
    parser.add_argument("--timeout", type=int, default=2700)
    args = parser.parse_args()
    args.repo = args.repo.resolve()
    args.run_dir = args.run_dir.resolve()

    manifest = json.loads((args.run_dir / "manifest.json").read_text(encoding="utf-8"))
    candidates = manifest["candidates"]
    expected_ids = [row["candidate_id"] for row in candidates]
    if len(expected_ids) != 39 or len(set(expected_ids)) != 39:
        raise SystemExit("frozen manifest must contain 39 unique candidate ids")
    baseline = json.loads(args.baseline.read_text(encoding="utf-8"))
    validate_payload({"results": baseline}, expected_ids)
    write_json(args.run_dir / "root_baseline.json", {"results": baseline})
    write_json(args.run_dir / "matrix.json", {"jobs": MATRIX})

    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["results"],
        "properties": {
            "results": {
                "type": "array", "minItems": 39, "maxItems": 39,
                "items": {
                    "type": "object", "additionalProperties": False,
                    "required": ["candidate_id", "classification", "latex", "confidence", "note"],
                    "properties": {
                        "candidate_id": {"type": "string"},
                        "classification": {"enum": ["formula", "formula_number", "mixed_text"]},
                        "latex": {"type": ["string", "null"]},
                        "confidence": {"enum": ["high", "medium", "low"]},
                        "note": {"type": "string"},
                    },
                },
            },
        },
    }
    schema_path = args.run_dir / "output_schema.json"
    write_json(schema_path, schema)
    prompt = build_prompt(candidates)
    (args.run_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
    image_paths = [row["image_path"] for row in candidates]
    semaphores = {backend: threading.Semaphore(3) for backend in {j["backend"] for j in MATRIX}}
    print(f"PRESSURE_START jobs={len(MATRIX)} candidates={len(expected_ids)}", flush=True)
    statuses: list[dict] = []
    pending_jobs: list[dict] = []
    for job in MATRIX:
        existing = find_existing_valid_status(job, args, expected_ids)
        if existing is None:
            pending_jobs.append(job)
        else:
            statuses.append(existing)
            print(f'REUSE {existing["job_id"]}', flush=True)
    def record(status: dict) -> None:
        statuses.append(status)
        write_json(args.run_dir / "live_summary.json", {
            "finished": len(statuses),
            "valid": sum(item["valid"] for item in statuses),
            "failed": sum(not item["valid"] for item in statuses),
            "statuses": sorted(statuses, key=lambda item: item["job_id"]),
        })

    run_jobs_by_provider(
        pending_jobs,
        lambda job: run_job(job, args, prompt, expected_ids, image_paths,
                            schema_path, semaphores[job["backend"]]),
        per_provider=3,
        on_result=record,
    )
    write_json(args.run_dir / "run_summary.json", {
        "finished": len(statuses),
        "valid": sum(item["valid"] for item in statuses),
        "failed": sum(not item["valid"] for item in statuses),
        "statuses": sorted(statuses, key=lambda item: item["job_id"]),
    })
    print(f'PRESSURE_END valid={sum(item["valid"] for item in statuses)} '
          f'failed={sum(not item["valid"] for item in statuses)}', flush=True)


if __name__ == "__main__":
    main()
