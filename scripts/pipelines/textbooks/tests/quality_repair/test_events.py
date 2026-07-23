from __future__ import annotations

import json

import pytest

from scripts.pipelines.textbooks.quality_repair.events import (
    DetectionBatch,
    RepairEvent,
    write_detection_reports,
)
from scripts.pipelines.textbooks.quality_repair.models import Severity


def _event(
    index: int = 1,
    *,
    message: str = "needs review",
    crop_path: str = "old/crop.png",
    route: str = "quality_agent",
    status: str = "unresolved",
    input_fingerprint: str = "page-json-sha256",
) -> RepairEvent:
    return RepairEvent.create(
        capability="unordered_blocks",
        kind="missing_unordered_content",
        severity=Severity.P1,
        route=route,
        status=status,
        input_fingerprint=input_fingerprint,
        page=index,
        block_id=index * 10,
        bbox=[10, 20, 30, 40],
        target={"scope": "block", "page": index, "block_id": index * 10},
        message=message,
        evidence={"crop_path": crop_path, "content_sample": "valuable note"},
    )


def test_event_id_uses_stable_target_and_input_not_presentation_or_route():
    first = _event(message="first wording", crop_path="run-1/a.png",
                   route="quality_agent", status="unresolved")
    second = _event(message="new wording", crop_path="run-2/b.png",
                    route="deterministic", status="resolved")

    assert first.event_id == second.event_id


def test_event_id_changes_when_target_or_input_changes():
    original = _event()
    changed_target = _event(2)
    changed_input = _event(input_fingerprint="different-page-json-sha256")

    assert original.event_id != changed_target.event_id
    assert original.event_id != changed_input.event_id


def test_detection_batch_rejects_duplicate_occurrence_and_bad_metrics():
    event = _event()
    with pytest.raises(ValueError, match="duplicate event_id"):
        DetectionBatch.create(
            stem="Demo", baseline_sha256="md-sha", events=[event, event]
        )
    with pytest.raises(ValueError, match="non-negative integers"):
        DetectionBatch.create(
            stem="Demo", baseline_sha256="md-sha", events=[], metrics={"furniture": -1}
        )


def test_reports_keep_all_events_but_bound_human_summary(tmp_path):
    events = [_event(index) for index in range(1, 101)]
    batch = DetectionBatch.create(
        stem="Demo",
        baseline_sha256="md-sha",
        events=events,
        metrics={"known_furniture": 1200, "empty_visual": 40},
    )

    events_path, summary_path = write_detection_reports(
        tmp_path, batch, sample_limit=3, page_sample_limit=4, group_limit=5
    )

    records = [
        json.loads(line)
        for line in events_path.read_text(encoding="utf-8").splitlines()
    ]
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    assert len(records) == 100
    assert summary["event_count"] == 100
    assert summary["unresolved"] == 100
    assert len(summary["samples"]) == 3
    assert summary["samples_truncated"] is True
    assert summary["affected_pages"] == {
        "count": 100, "sample": [1, 2, 3, 4], "truncated": True,
    }
    assert summary["metrics"] == {"known_furniture": 1200, "empty_visual": 40}
    assert "crop_path" not in json.dumps(summary)


def test_summary_bounds_high_cardinality_groups_without_losing_total():
    events = [
        RepairEvent.create(
            capability=f"cap-{index}",
            kind=f"kind-{index}",
            severity=Severity.P2,
            route=f"route-{index}",
            input_fingerprint="same-input",
            target={"scope": "md_span", "occurrence": index},
        )
        for index in range(10)
    ]
    batch = DetectionBatch.create(
        stem="Demo", baseline_sha256="md-sha", events=events
    )

    summary = batch.summary(group_limit=3)

    assert len(summary["counts_by_kind"]) == 4
    assert summary["counts_by_kind"]["__other__"] == 7
    assert sum(summary["counts_by_kind"].values()) == 10
