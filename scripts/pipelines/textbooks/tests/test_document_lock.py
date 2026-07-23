import json

import pytest

from scripts.pipelines.textbooks.document_lock import (
    LOCK_FILENAME,
    DocumentLock,
    DocumentLockedError,
)


def test_second_handle_in_same_process_fails_immediately(tmp_path):
    work = tmp_path / "_work"
    first = DocumentLock(work, run_id="run-1")
    second = DocumentLock(work, run_id="run-2")

    with first:
        with pytest.raises(DocumentLockedError) as caught:
            second.acquire()

        assert caught.value.lock_path == work / LOCK_FILENAME
        assert caught.value.holder is not None
        assert caught.value.holder["run_id"] == "run-1"
        assert not second.acquired
        assert (work / LOCK_FILENAME).exists()


def test_released_lock_can_be_reacquired_without_deleting_file(tmp_path):
    work = tmp_path / "_work"

    with DocumentLock(work, run_id="first"):
        assert (work / LOCK_FILENAME).exists()

    assert (work / LOCK_FILENAME).exists()
    with DocumentLock(work, run_id="second") as lock:
        assert lock.acquired

    assert (work / LOCK_FILENAME).exists()
    metadata = json.loads((work / LOCK_FILENAME).read_text(encoding="utf-8"))
    assert metadata["pid"] > 0
    assert metadata["run_id"] == "second"


def test_context_manager_releases_after_exception(tmp_path):
    work = tmp_path / "_work"

    with pytest.raises(RuntimeError, match="boom"):
        with DocumentLock(work, run_id="failed") as lock:
            assert lock.acquired
            raise RuntimeError("boom")

    with DocumentLock(work, run_id="recovery") as recovered:
        assert recovered.acquired


def test_release_is_idempotent(tmp_path):
    lock = DocumentLock(tmp_path / "_work")
    lock.acquire()
    lock.release()
    lock.release()
    assert not lock.acquired


def test_custom_metadata_is_written(tmp_path):
    work = tmp_path / "_work"

    with DocumentLock(work, run_id="repair-7", metadata={"document": "book.md"}):
        pass
    metadata = json.loads((work / LOCK_FILENAME).read_text(encoding="utf-8"))

    assert metadata["run_id"] == "repair-7"
    assert metadata["document"] == "book.md"
    assert metadata["pid"] > 0
    assert metadata["hostname"]
    assert metadata["acquired_at"].endswith("+00:00")
