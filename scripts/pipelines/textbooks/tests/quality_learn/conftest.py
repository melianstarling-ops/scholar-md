from __future__ import annotations

import json
from pathlib import Path


REQUIRED_TEXT = {
    "current_md.txt": "broken text\n",
    "expected_behavior.md": "# Expected\n\nKeep the missing caption.\n",
    "fixture_plan.md": "# Fixture\n",
    "test_plan.md": "# Tests\n",
    "lesson_draft.md": "# Lesson\n",
    "development_brief.md": "# Brief\n",
}


def make_package(run: Path, finding_id: str = "f-001",
                 family: str = "lost-side-caption", severity: str = "P1") -> Path:
    package = run / "learning_packages" / finding_id
    package.mkdir(parents=True)
    (package / "finding.json").write_text(json.dumps({
        "finding_id": finding_id, "kind": "novel_gap", "severity": severity,
    }), encoding="utf-8")
    (package / "evidence_manifest.json").write_text(json.dumps({
        "finding_id": finding_id,
        "agent_decision": {"issue_family": family, "severity": severity},
    }), encoding="utf-8")
    for name, text in REQUIRED_TEXT.items():
        (package / name).write_text(text, encoding="utf-8")
    return package
