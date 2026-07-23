from scripts.pipelines.textbooks.quality_repair.gates import (
    asset_regression_gate,
    delimiter_regression_gate,
)
from scripts.pipelines.textbooks.quality_repair.models import PatchPlan


EMPTY_PLAN = PatchPlan(baseline_sha256="x", proposals=(), conflicts=())


def test_delimiter_gate_allows_improvement_and_rejects_regression():
    assert delimiter_regression_gate("$ x $", "$x$", EMPTY_PLAN).passed
    result = delimiter_regression_gate("$x$", "$ x $", EMPTY_PLAN)
    assert not result.passed and "inline" in result.detail


def test_asset_gate_rejects_new_missing_link(tmp_path):
    gate = asset_regression_gate(tmp_path)
    assert gate("plain", "plain", EMPTY_PLAN).passed
    result = gate("plain", "![](missing.png)", EMPTY_PLAN)
    assert not result.passed and "missing" in result.detail
