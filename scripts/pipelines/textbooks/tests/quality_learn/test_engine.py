from __future__ import annotations

import json
from pathlib import Path

import pytest

from scripts.pipelines.textbooks.quality_learn.engine import develop, review, write_plan
from scripts.pipelines.textbooks.quality_learn.models import CommandResult, LearnError
from scripts.pipelines.textbooks.quality_repair.agents import AgentSpec

from .conftest import make_package


TEST_PATH = "scripts/pipelines/textbooks/tests/quality_repair/test_new_kind.py"
PROD_PATH = "scripts/pipelines/textbooks/quality_repair/repairers.py"


def _patch(path: str, old: str, new: str) -> str:
    return "\n".join([
        f"diff --git a/{path} b/{path}", f"--- a/{path}", f"+++ b/{path}",
        "@@ -1 +1 @@", f"-{old}", f"+{new}", "",
    ])


def _response():
    return json.dumps({
        "issue_family": "lost-side-caption",
        "test_patch": _patch(TEST_PATH, "old test", "red test"),
        "implementation_patch": _patch(PROD_PATH, "old prod", "green prod"),
        "target_tests": [TEST_PATH], "notes": ["minimal rule"],
    })


class SequenceRunner:
    def __init__(self, pytest_codes, diff="diff", status=""):
        self.pytest_codes = iter(pytest_codes)
        self.diff = diff
        self.status = status
        self.calls = []

    def __call__(self, argv, cwd, timeout):
        self.calls.append(tuple(argv))
        if argv[:2] == ["git", "status"]:
            return CommandResult(tuple(argv), 0, self.status, "")
        if argv[:2] == ["git", "diff"]:
            return CommandResult(tuple(argv), 0, self.diff, "")
        return CommandResult(tuple(argv), next(self.pytest_codes), "test output", "")


def _setup(tmp_path):
    source_run = tmp_path / "source-run"
    make_package(source_run)
    output = source_run / "quality_learn" / "learn-1"
    write_plan([source_run], output, "learn-1")
    repo = tmp_path / "repo"
    test_file = repo / TEST_PATH
    prod_file = repo / PROD_PATH
    test_file.parent.mkdir(parents=True)
    prod_file.parent.mkdir(parents=True)
    test_file.write_text("old test\n", encoding="utf-8")
    prod_file.write_text("old prod\n", encoding="utf-8")
    return output, repo, test_file, prod_file


def test_develop_enforces_red_green_and_full_regression(tmp_path):
    output, repo, _, _ = _setup(tmp_path)
    runner = SequenceRunner([0, 1, 0, 0])
    patch_calls = []

    def patcher(repo, patch, check):
        patch_calls.append(check)
        return CommandResult(("git", "apply"), 0, "", "")

    report = develop(
        output, repo, cluster_id=None,
        agent_specs=[AgentSpec.parse("fake:model:high")],
        invoke=lambda *_: _response(), runner=runner, patcher=patcher)
    assert report["status"] == "passed"
    assert patch_calls == [True, False, True, False]
    assert report["red_test"]["exit_code"] == 1
    assert report["regression_test"]["exit_code"] == 0


def test_develop_rolls_back_files_when_green_test_fails(tmp_path):
    output, repo, test_file, prod_file = _setup(tmp_path)
    runner = SequenceRunner([0, 1, 1])

    def patcher(_repo, patch, check):
        if check:
            return CommandResult(("git", "apply"), 0, "", "")
        if TEST_PATH in patch:
            test_file.write_text("red test\n", encoding="utf-8")
        else:
            prod_file.write_text("green prod\n", encoding="utf-8")
        return CommandResult(("git", "apply"), 0, "", "")

    with pytest.raises(LearnError, match="still fail"):
        develop(
            output, repo, cluster_id=None,
            agent_specs=[AgentSpec.parse("fake:model:high")],
            invoke=lambda *_: _response(), runner=runner, patcher=patcher)
    assert test_file.read_text(encoding="utf-8") == "old test\n"
    assert prod_file.read_text(encoding="utf-8") == "old prod\n"
    report = json.loads((output / "develop_report.json").read_text(encoding="utf-8"))
    assert report["rolled_back"] is True


def test_baseline_failure_prevents_agent_call(tmp_path):
    output, repo, _, _ = _setup(tmp_path)
    called = False

    def invoke(*_):
        nonlocal called
        called = True
        return _response()

    with pytest.raises(LearnError, match="baseline"):
        develop(
            output, repo, cluster_id=None,
            agent_specs=[AgentSpec.parse("fake:model:high")],
            invoke=invoke, runner=SequenceRunner([1]))
    assert called is False


def test_review_requires_independent_agent(tmp_path):
    output, repo, _, _ = _setup(tmp_path)
    (output / "develop_report.json").write_text(json.dumps({
        "status": "passed", "agent": {"provider": "fake", "model": "m", "effort": "high"},
        "after_hashes": {TEST_PATH: __import__("hashlib").sha256(
            (repo / TEST_PATH).read_bytes()).hexdigest()},
    }), encoding="utf-8")
    with pytest.raises(LearnError, match="must differ"):
        review(output, repo, review_specs=[AgentSpec.parse("fake:m:high")],
               runner=SequenceRunner([]))


def test_review_runs_regression_then_records_agent_verdict(tmp_path):
    output, repo, _, _ = _setup(tmp_path)
    (output / "red_test.patch").write_text("red", encoding="utf-8")
    (output / "implementation.patch").write_text("impl", encoding="utf-8")
    (output / "develop_report.json").write_text(json.dumps({
        "status": "passed", "agent": {"provider": "dev", "model": "m", "effort": "high"},
        "cluster_id": json.loads((output / "plan.json").read_text(encoding="utf-8"))[
            "clusters"][0]["cluster_id"],
        "after_hashes": {TEST_PATH: __import__("hashlib").sha256(
            (repo / TEST_PATH).read_bytes()).hexdigest()},
    }), encoding="utf-8")
    response = json.dumps({
        "verdict": "approve", "findings": [], "confidence": 0.95,
        "summary": "evidence and tests agree",
    })
    result = review(
        output, repo, review_specs=[AgentSpec.parse("review:m:high")],
        invoke=lambda *_: response, runner=SequenceRunner(
            [0], diff="candidate", status=f" M {TEST_PATH}\n"))
    assert result["verdict"] == "approve"
    assert (output / "review_report.json").is_file()
