from __future__ import annotations

import pytest
import subprocess

from scripts.pipelines.textbooks.quality_learn.models import LearnError
from scripts.pipelines.textbooks.quality_learn.packages import build_plan, load_package
from scripts.pipelines.textbooks.quality_learn.patches import git_apply, validate_patch_paths

from .conftest import make_package


def _patch(path: str) -> str:
    return "\n".join([
        f"diff --git a/{path} b/{path}",
        f"--- a/{path}", f"+++ b/{path}",
        "@@ -1 +1 @@", "-old", "+new", "",
    ])


def test_plan_clusters_same_issue_family_deterministically(tmp_path):
    run = tmp_path / "run"
    make_package(run, "b", "Lost Side Caption")
    make_package(run, "a", "lost-side-caption")
    plan = build_plan([run], "learn-1")
    assert len(plan.clusters) == 1
    assert [item.finding_id for item in plan.clusters[0].packages] == ["a", "b"]
    assert plan.clusters[0].cluster_id.startswith("lost-side-caption-")


def test_large_issue_family_is_split_into_bounded_clusters(tmp_path):
    run = tmp_path / "run"
    for index in range(9):
        make_package(run, f"f-{index:02d}", "same-family")
    plan = build_plan([run], "learn-1")
    assert [len(cluster.packages) for cluster in plan.clusters] == [8, 1]


def test_incomplete_package_fails_loud(tmp_path):
    package = make_package(tmp_path / "run")
    (package / "test_plan.md").unlink()
    with pytest.raises(LearnError, match="incomplete"):
        load_package(package)


def test_patch_validator_allows_planned_textbooks_path():
    paths = validate_patch_paths(
        _patch("scripts/pipelines/textbooks/quality_repair/detectors/new_kind.py"),
        ["scripts/pipelines/textbooks/"],
    )
    assert paths == ("scripts/pipelines/textbooks/quality_repair/detectors/new_kind.py",)


@pytest.mark.parametrize("path", [
    "02_Source/textbooks/book.pdf", "../escape.py", "README.md",
])
def test_patch_validator_rejects_forbidden_or_unplanned_paths(path):
    with pytest.raises(LearnError):
        validate_patch_paths(_patch(path), ["scripts/pipelines/textbooks/"])


def test_red_patch_is_confined_to_tests():
    with pytest.raises(LearnError, match="escaped tests root"):
        validate_patch_paths(
            _patch("scripts/pipelines/textbooks/quality_repair/repairers.py"),
            ["scripts/pipelines/textbooks/"], tests_only=True)


def test_delete_and_binary_patches_are_rejected():
    with pytest.raises(LearnError):
        validate_patch_paths(_patch("scripts/pipelines/textbooks/tests/test_x.py")
                             + "deleted file mode 100644\n",
                             ["scripts/pipelines/textbooks/"])
    with pytest.raises(LearnError):
        validate_patch_paths(_patch("scripts/pipelines/textbooks/tests/test_x.py")
                             + "GIT binary patch\n",
                             ["scripts/pipelines/textbooks/"])


def test_undeclared_second_file_header_is_rejected():
    malicious = _patch("scripts/pipelines/textbooks/tests/test_x.py") + "\n" + "\n".join([
        "--- a/README.md", "+++ b/README.md", "@@ -1 +1 @@", "-old", "+new",
    ])
    with pytest.raises(LearnError, match="lacks an approved diff"):
        validate_patch_paths(malicious, ["scripts/pipelines/textbooks/"])


def test_git_apply_check_and_apply_use_real_unified_diff(tmp_path):
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    target = tmp_path / "scripts/pipelines/textbooks/tests/test_x.py"
    target.parent.mkdir(parents=True)
    target.write_text("old\n", encoding="utf-8")
    patch = _patch("scripts/pipelines/textbooks/tests/test_x.py")
    assert git_apply(tmp_path, patch, check=True).exit_code == 0
    assert target.read_text(encoding="utf-8") == "old\n"
    assert git_apply(tmp_path, patch).exit_code == 0
    assert target.read_text(encoding="utf-8") == "new\n"
