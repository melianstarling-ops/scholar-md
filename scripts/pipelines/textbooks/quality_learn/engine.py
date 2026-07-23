"""Plan, red-green develop, and independent review orchestration."""
from __future__ import annotations

import json
import hashlib
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Callable

from scripts.pipelines.textbooks.quality_repair.agents import AgentSpec
from scripts.pipelines.textbooks.quality_repair.reporting import write_json, write_text

from .agents import (
    Invoke, build_develop_prompt, build_review_prompt, invoke_cli, package_images,
    invoke_first_valid, parse_develop_response, parse_review_response,
)
from .models import CommandResult, DevelopmentPlan, LearnError
from .packages import build_plan
from .patches import WorkspaceBackup, git_apply, validate_patch_paths


CommandRunner = Callable[[list[str], Path, int], CommandResult]
PatchApplier = Callable[[Path, str, bool], CommandResult]


def run_command(argv: list[str], cwd: Path, timeout: int) -> CommandResult:
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        return CommandResult(tuple(argv), 124, exc.stdout or "", exc.stderr or "timeout")
    return CommandResult(tuple(argv), proc.returncode, proc.stdout or "", proc.stderr or "")


def _apply(repo: Path, patch: str, check: bool) -> CommandResult:
    return git_apply(repo, patch, check=check)


def _git(repo: Path, args: list[str], runner: CommandRunner, timeout: int = 30) -> CommandResult:
    return runner(["git", *args], repo, timeout)


def resolve_repo_root(candidate: str | Path | None, *,
                      runner: CommandRunner = run_command) -> Path:
    start = Path(candidate or Path.cwd()).resolve()
    result = _git(start, ["rev-parse", "--show-toplevel"], runner)
    if result.exit_code != 0:
        raise LearnError(f"not inside a git worktree: {result.stderr[:300]}")
    return Path(result.stdout.strip()).resolve()


def require_clean_worktree(repo: Path, *, runner: CommandRunner = run_command) -> None:
    result = _git(repo, ["status", "--porcelain", "--untracked-files=all"], runner)
    if result.exit_code != 0:
        raise LearnError(f"git status failed: {result.stderr[:300]}")
    if result.stdout.strip():
        raise LearnError("quality_learn develop requires a clean worktree")


def write_plan(run_dirs: list[str | Path], output_dir: Path,
               learn_run_id: str) -> DevelopmentPlan:
    plan = build_plan(run_dirs, learn_run_id)
    write_json(output_dir / "plan.json", plan.to_dict())
    lines = ["# quality_learn plan", "", f"Run: `{learn_run_id}`", "",
             f"Clusters: {len(plan.clusters)}", ""]
    for cluster in plan.clusters:
        lines.append(f"- `{cluster.cluster_id}` — {cluster.issue_family} "
                     f"({len(cluster.packages)} package(s), {', '.join(cluster.severities)})")
    write_text(output_dir / "plan.md", "\n".join(lines) + "\n")
    return plan


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise LearnError(f"cannot read {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise LearnError(f"expected JSON object: {path}")
    return value


def _select_cluster(plan: dict, cluster_id: str | None) -> dict:
    clusters = plan.get("clusters")
    if not isinstance(clusters, list) or not clusters:
        raise LearnError("plan contains no clusters")
    if cluster_id:
        matches = [item for item in clusters if item.get("cluster_id") == cluster_id]
        if len(matches) != 1:
            raise LearnError(f"unknown cluster: {cluster_id}")
        return matches[0]
    if len(clusters) != 1:
        raise LearnError("plan has multiple clusters; select one with --cluster")
    return clusters[0]


def _pytest_argv(paths: list[str] | None = None) -> list[str]:
    return [sys.executable, "-X", "utf8", "-m", "pytest",
            *(paths or ["scripts/pipelines/textbooks/tests"]), "-q"]


def _validate_test_paths(paths: tuple[str, ...]) -> list[str]:
    valid: list[str] = []
    for raw in paths:
        normalized = raw.replace("\\", "/")
        if (not normalized.startswith("scripts/pipelines/textbooks/tests/")
                or ".." in Path(normalized).parts):
            raise LearnError(f"unsafe target test path: {raw}")
        valid.append(normalized)
    return valid


def _file_hashes(repo: Path, paths: tuple[str, ...]) -> dict[str, str | None]:
    hashes: dict[str, str | None] = {}
    for relative in paths:
        target = repo / relative
        hashes[relative] = (hashlib.sha256(target.read_bytes()).hexdigest()
                            if target.is_file() else None)
    return hashes


def _visible_changed_paths(repo: Path, runner: CommandRunner) -> set[str]:
    result = _git(repo, ["status", "--porcelain", "--untracked-files=all"], runner)
    if result.exit_code != 0:
        raise LearnError(f"git status failed during review: {result.stderr[:300]}")
    paths: set[str] = set()
    for line in result.stdout.splitlines():
        if len(line) < 4:
            continue
        value = line[3:].strip().replace("\\", "/")
        if " -> " in value:
            value = value.split(" -> ", 1)[1]
        paths.add(value.strip('"'))
    return paths


def develop(output_dir: Path, repo: Path, *, cluster_id: str | None,
            agent_specs: list[AgentSpec], invoke: Invoke = invoke_cli,
            runner: CommandRunner = run_command,
            patcher: PatchApplier = _apply,
            agent_timeout: int = 900, test_timeout: int = 600) -> dict:
    if not agent_specs:
        raise LearnError("develop requires at least one explicit --agent")
    plan = _read_json(output_dir / "plan.json")
    cluster = _select_cluster(plan, cluster_id)
    allowed = plan.get("allowed_roots")
    if not isinstance(allowed, list) or not all(isinstance(v, str) for v in allowed):
        raise LearnError("plan allowed_roots is invalid")
    require_clean_worktree(repo, runner=runner)
    baseline = runner(_pytest_argv(), repo, test_timeout)
    write_json(output_dir / "baseline_test.json", baseline.to_dict())
    if baseline.exit_code != 0:
        raise LearnError("baseline tests failed; develop did not call an Agent")
    require_clean_worktree(repo, runner=runner)

    prompt = build_develop_prompt(cluster, allowed)
    write_text(output_dir / "develop_prompt.md", prompt)
    spec, parsed = invoke_first_valid(
        agent_specs, prompt, timeout=agent_timeout,
        image_paths=package_images(cluster),
        parser=parse_develop_response, invoke=invoke)
    response = parsed
    if response.issue_family.casefold() != str(cluster["issue_family"]).casefold():
        raise LearnError("Agent response issue_family does not match selected cluster")
    test_paths = validate_patch_paths(response.test_patch, allowed, tests_only=True)
    implementation_paths = validate_patch_paths(response.implementation_patch, allowed)
    target_tests = _validate_test_paths(response.target_tests)
    all_paths = tuple(dict.fromkeys((*test_paths, *implementation_paths)))
    backup = WorkspaceBackup.capture(repo, all_paths)
    write_text(output_dir / "red_test.patch", response.test_patch)
    write_text(output_dir / "implementation.patch", response.implementation_patch)
    report = {
        "schema_version": 1, "status": "failed", "cluster_id": cluster["cluster_id"],
        "agent": asdict(spec), "test_paths": list(test_paths),
        "implementation_paths": list(implementation_paths),
        "target_tests": target_tests, "notes": list(response.notes),
        "baseline_test": baseline.to_dict(),
    }
    try:
        patcher(repo, response.test_patch, True)
        patcher(repo, response.test_patch, False)
        red = runner(_pytest_argv(target_tests), repo, test_timeout)
        report["red_test"] = red.to_dict()
        write_json(output_dir / "red_test_result.json", red.to_dict())
        if red.exit_code == 0:
            raise LearnError("red-test patch passed before implementation")

        patcher(repo, response.implementation_patch, True)
        patcher(repo, response.implementation_patch, False)
        green = runner(_pytest_argv(target_tests), repo, test_timeout)
        report["green_test"] = green.to_dict()
        write_json(output_dir / "green_test_result.json", green.to_dict())
        if green.exit_code != 0:
            raise LearnError("target tests still fail after implementation")
        regression = runner(_pytest_argv(), repo, test_timeout)
        report["regression_test"] = regression.to_dict()
        write_json(output_dir / "regression_test_result.json", regression.to_dict())
        if regression.exit_code != 0:
            raise LearnError("full textbooks regression failed after implementation")
        report["after_hashes"] = _file_hashes(repo, all_paths)
        report["status"] = "passed"
        write_json(output_dir / "develop_report.json", report)
        return report
    except Exception as exc:
        backup.restore()
        report["error"] = str(exc)
        report["rolled_back"] = True
        write_json(output_dir / "develop_report.json", report)
        if isinstance(exc, LearnError):
            raise
        raise LearnError(str(exc)) from exc


def review(output_dir: Path, repo: Path, *, review_specs: list[AgentSpec],
           invoke: Invoke = invoke_cli, runner: CommandRunner = run_command,
           agent_timeout: int = 900, test_timeout: int = 600) -> dict:
    if not review_specs:
        raise LearnError("review requires at least one explicit --review-agent")
    plan = _read_json(output_dir / "plan.json")
    develop_report = _read_json(output_dir / "develop_report.json")
    if develop_report.get("status") != "passed":
        raise LearnError("review requires a successful develop report")
    dev_agent = develop_report.get("agent") or {}
    for spec in review_specs:
        if asdict(spec) == dev_agent:
            raise LearnError("review Agent must differ from the development Agent")
    expected_hashes = develop_report.get("after_hashes")
    if not isinstance(expected_hashes, dict) or not expected_hashes:
        raise LearnError("develop report lacks after_hashes")
    actual_hashes = _file_hashes(repo, tuple(expected_hashes))
    if actual_hashes != expected_hashes:
        raise LearnError("workspace drifted after develop; review stopped")
    expected_visible = {path for path in expected_hashes if path.startswith("scripts/")}
    changed_visible = _visible_changed_paths(repo, runner)
    if changed_visible != expected_visible:
        raise LearnError(
            "workspace change set differs from the developed patch: "
            f"expected={sorted(expected_visible)} actual={sorted(changed_visible)}")

    regression = runner(_pytest_argv(), repo, test_timeout)
    write_json(output_dir / "review_regression_test.json", regression.to_dict())
    if regression.exit_code != 0:
        raise LearnError("review regression failed before Agent review")
    diff = _git(repo, ["diff", "--no-ext-diff", "--"], runner, test_timeout)
    if diff.exit_code != 0:
        raise LearnError(f"cannot read candidate diff: {diff.stderr[:300]}")
    candidate = "\n".join([
        (output_dir / "red_test.patch").read_text(encoding="utf-8"),
        (output_dir / "implementation.patch").read_text(encoding="utf-8"),
        diff.stdout,
    ])
    prompt = build_review_prompt(plan, develop_report, candidate)
    write_text(output_dir / "review_prompt.md", prompt)
    spec, parsed = invoke_first_valid(
        review_specs, prompt, timeout=agent_timeout,
        image_paths=package_images(_select_cluster(plan, develop_report.get("cluster_id"))),
        parser=parse_review_response, invoke=invoke)
    response = parsed
    result = {
        "schema_version": 1, "review_agent": asdict(spec),
        "verdict": response.verdict, "findings": list(response.findings),
        "confidence": response.confidence, "summary": response.summary,
        "regression_test": regression.to_dict(),
    }
    write_json(output_dir / "review_report.json", result)
    return result
