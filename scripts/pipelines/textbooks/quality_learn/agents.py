"""Strict coding/review Agent protocols for quality learning."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Callable, Iterable

from scripts.pipelines.textbooks.quality_repair.agents import AgentSpec
from scripts.pipelines.textbooks.formula_agents.adapters import run_prompt

from .models import DevelopResponse, LearnError, ReviewResponse


Invoke = Callable[[AgentSpec, str, tuple[str, ...], int], str]


def invoke_cli(spec: AgentSpec, prompt: str, image_paths: tuple[str, ...], timeout: int) -> str:
    response = run_prompt(spec.provider, prompt, model=spec.model, effort=spec.effort,
                          image_paths=image_paths, timeout=timeout)
    if response.exit_code != 0:
        raise LearnError(f"{spec.provider} exited {response.exit_code}: {response.stderr[:300]}")
    return response.stdout


def _object(stdout: str) -> dict:
    try:
        value = json.loads(stdout)
    except (json.JSONDecodeError, TypeError) as exc:
        raise LearnError(f"Agent response is not one JSON object: {exc}") from exc
    if not isinstance(value, dict):
        raise LearnError("Agent response must be one JSON object")
    return value


def parse_develop_response(stdout: str) -> DevelopResponse:
    data = _object(stdout)
    family = data.get("issue_family")
    test_patch = data.get("test_patch")
    implementation_patch = data.get("implementation_patch")
    tests = data.get("target_tests")
    notes = data.get("notes", [])
    if not isinstance(family, str) or not family.strip():
        raise LearnError("develop response needs issue_family")
    if not isinstance(test_patch, str) or not isinstance(implementation_patch, str):
        raise LearnError("develop response needs test_patch and implementation_patch")
    if not isinstance(tests, list) or not tests or not all(isinstance(v, str) for v in tests):
        raise LearnError("develop response needs a non-empty target_tests array")
    if not isinstance(notes, list) or not all(isinstance(v, str) for v in notes):
        raise LearnError("develop response notes must be a string array")
    return DevelopResponse(family.strip(), test_patch, implementation_patch,
                           tuple(tests), tuple(notes))


def parse_review_response(stdout: str) -> ReviewResponse:
    data = _object(stdout)
    verdict = data.get("verdict")
    findings = data.get("findings")
    confidence = data.get("confidence")
    summary = data.get("summary")
    if verdict not in {"approve", "revise", "reject"}:
        raise LearnError("review verdict must be approve, revise, or reject")
    if not isinstance(findings, list) or not all(isinstance(v, str) for v in findings):
        raise LearnError("review findings must be a string array")
    if (not isinstance(confidence, (int, float)) or isinstance(confidence, bool)
            or not 0 <= float(confidence) <= 1):
        raise LearnError("review confidence must be between 0 and 1")
    if not isinstance(summary, str):
        raise LearnError("review summary must be a string")
    return ReviewResponse(verdict, tuple(findings), float(confidence), summary)


def package_context(package_paths: Iterable[str]) -> str:
    sections: list[str] = []
    budget = 120_000
    for raw in package_paths:
        root = Path(raw)
        sections.append(f"## Package {root.name}")
        for name in ("finding.json", "evidence_manifest.json", "current_md.txt",
                     "expected_behavior.md", "fixture_plan.md", "test_plan.md",
                     "lesson_draft.md", "development_brief.md"):
            text = (root / name).read_text(encoding="utf-8")
            section = f"### {name}\n{text[:4000]}"
            if sum(len(value) for value in sections) + len(section) > budget:
                sections.append("[evidence context truncated at deterministic budget]")
                return "\n\n".join(sections)
            sections.append(section)
    return "\n\n".join(sections)


def package_images(cluster: dict) -> tuple[str, ...]:
    """Return only existing raster evidence; PDFs are deliberately never authorized."""
    images: list[str] = []
    for item in cluster.get("packages") or []:
        root = Path(item["path"])
        try:
            manifest = json.loads((root / "evidence_manifest.json").read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            continue
        paths = manifest.get("image_paths") or []
        for raw in paths if isinstance(paths, list) else []:
            path = Path(str(raw)).resolve()
            if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp"}:
                continue
            if path.is_file() and str(path) not in images:
                images.append(str(path))
    return tuple(images)


def build_develop_prompt(cluster: dict, allowed_roots: list[str]) -> str:
    paths = [item["path"] for item in cluster["packages"]]
    return "\n".join([
        "You are promoting one confirmed PDF-to-Markdown issue into deterministic code.",
        "Inspect the repository read-only. Return exactly one JSON object and do not edit files.",
        "The controller will validate and apply two git-style unified diffs.",
        "test_patch must contain only regression tests and must fail against current production code.",
        "implementation_patch is applied after test_patch and must make those tests pass.",
        "Do not delete, rename, or emit binary files. Never touch 02_Source.",
        f"Allowed roots: {json.dumps(allowed_roots, ensure_ascii=False)}",
        "Required JSON keys: issue_family, test_patch, implementation_patch, target_tests, notes.",
        "target_tests must be repository-relative pytest paths under scripts/pipelines/textbooks/tests/.",
        f"Cluster: {json.dumps(cluster, ensure_ascii=False, indent=2)}",
        package_context(paths),
    ])


def build_review_prompt(plan: dict, develop_report: dict, diff_text: str) -> str:
    return "\n".join([
        "You are the independent reviewer for a quality-repair rule promotion.",
        "Review evidence, red-to-green proof, safety boundaries, conflicts, and regression impact.",
        "Return exactly one JSON object; do not edit files.",
        "Required keys: verdict (approve|revise|reject), findings (string array), confidence, summary.",
        f"Plan: {json.dumps(plan, ensure_ascii=False, indent=2)[:20000]}",
        f"Develop report: {json.dumps(develop_report, ensure_ascii=False, indent=2)[:20000]}",
        f"Candidate diff:\n{diff_text[:50000]}",
    ])


def invoke_first_valid(specs: list[AgentSpec], prompt: str, *,
                       image_paths: tuple[str, ...] = (), timeout: int,
                       parser: Callable[[str], object], invoke: Invoke) -> tuple[AgentSpec, object]:
    errors: list[str] = []
    for spec in specs:
        try:
            return spec, parser(invoke(spec, prompt, image_paths, timeout))
        except (LearnError, OSError, TimeoutError) as exc:
            errors.append(f"{spec.provider}:{spec.model}:{spec.effort}: {exc}")
    raise LearnError("all explicitly configured agents failed: " + " | ".join(errors))
