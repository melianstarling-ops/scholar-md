from __future__ import annotations

import json

from scripts.pipelines.textbooks.quality_learn.cli import main

from .conftest import make_package


def test_plan_cli_writes_ordered_artifacts_and_latest_pointer(tmp_path):
    run = tmp_path / "quality-run"
    make_package(run)
    assert main([
        "--run", str(run), "--mode", "plan", "--learn-run-id", "learn-fixed",
    ]) == 0
    output = run / "quality_learn" / "learn-fixed"
    plan = json.loads((output / "plan.json").read_text(encoding="utf-8"))
    assert plan["learn_run_id"] == "learn-fixed"
    assert len(plan["clusters"]) == 1
    latest = json.loads((run / "quality_learn" / "latest.json").read_text(encoding="utf-8"))
    assert latest == {"learn_run_id": "learn-fixed"}
