"""同一文档修复任务的跨进程排他锁。

锁文件只承载便于诊断的元数据；互斥语义来自操作系统持有的文件句柄锁。
因此进程正常退出或崩溃时，操作系统都会释放锁。锁文件本身不会被删除。
"""
from __future__ import annotations

import errno
import json
import os
import socket
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, BinaryIO, Mapping


LOCK_FILENAME = ".repair.lock"


class DocumentLockedError(RuntimeError):
    """目标文档已经被另一修复任务占用。"""

    def __init__(
        self,
        lock_path: str | os.PathLike[str],
        holder: Mapping[str, Any] | None = None,
    ) -> None:
        self.lock_path = Path(lock_path)
        self.holder = dict(holder) if holder is not None else None
        detail = ""
        if self.holder:
            pid = self.holder.get("pid")
            run_id = self.holder.get("run_id")
            fields = [
                f"pid={pid}" if pid is not None else "",
                f"run_id={run_id}" if run_id else "",
            ]
            rendered = ", ".join(field for field in fields if field)
            if rendered:
                detail = f" ({rendered})"
        super().__init__(f"document repair is already locked: {self.lock_path}{detail}")


def _read_metadata(handle: BinaryIO) -> dict[str, Any] | None:
    """Best-effort metadata read; malformed/stale contents never affect locking."""

    try:
        # Byte 0 is the Windows lock sentinel and may not be readable through
        # a competing handle. The JSON payload begins at byte 1.
        handle.seek(1)
        raw = handle.read()
        data = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _try_lock(handle: BinaryIO) -> None:
    """Acquire immediately or raise ``BlockingIOError``."""

    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN, errno.EDEADLK}:
                raise BlockingIOError(exc.errno, str(exc)) from exc
            raise
    else:
        import fcntl

        try:
            # flock is exposed by fcntl and, unlike POSIX record locks, also
            # rejects a second independently opened handle in this process.
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in {errno.EACCES, errno.EAGAIN}:
                raise BlockingIOError(exc.errno, str(exc)) from exc
            raise


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


class DocumentLock:
    """Non-blocking context manager for ``<doc_work>/.repair.lock``.

    Args:
        doc_work: This document's work directory.
        run_id: Optional repair-run identifier written to the lock metadata.
        metadata: Additional JSON-serializable diagnostic metadata.
    """

    def __init__(
        self,
        doc_work: str | os.PathLike[str],
        *,
        run_id: str | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self.doc_work = Path(doc_work)
        self.path = self.doc_work / LOCK_FILENAME
        self.run_id = run_id
        self.metadata = dict(metadata or {})
        self._handle: BinaryIO | None = None

    @property
    def acquired(self) -> bool:
        return self._handle is not None

    def acquire(self) -> "DocumentLock":
        if self._handle is not None:
            return self

        self.doc_work.mkdir(parents=True, exist_ok=True)
        try:
            handle = self.path.open("r+b", buffering=0)
        except FileNotFoundError:
            try:
                handle = self.path.open("x+b", buffering=0)
            except FileExistsError:
                # Another process created the stable lock file in between.
                handle = self.path.open("r+b", buffering=0)
        try:
            # msvcrt locks bytes from the current position. Ensure byte 0
            # exists before asking it to lock that one-byte range.
            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"{}")
            handle.flush()

            try:
                _try_lock(handle)
            except BlockingIOError as exc:
                holder = _read_metadata(handle)
                raise DocumentLockedError(self.path, holder) from exc

            payload: dict[str, Any] = {
                "pid": os.getpid(),
                "run_id": self.run_id,
                "hostname": socket.gethostname(),
                "acquired_at": datetime.now(timezone.utc).isoformat(),
                "argv": sys.argv,
            }
            payload.update(self.metadata)
            encoded = json.dumps(
                payload,
                ensure_ascii=False,
                indent=2,
                sort_keys=True,
            ).encode("utf-8")
            handle.seek(0)
            handle.truncate()
            # A leading JSON whitespace byte is both a valid on-disk JSON
            # prefix and a dedicated byte-range lock sentinel on Windows.
            handle.write(b" " + encoded)
            handle.flush()
            self._handle = handle
            return self
        except BaseException:
            if self._handle is None:
                handle.close()
            raise

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return
        self._handle = None
        try:
            _unlock(handle)
        finally:
            handle.close()

    def __enter__(self) -> "DocumentLock":
        return self.acquire()

    def __exit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self.release()


__all__ = ["DocumentLock", "DocumentLockedError", "LOCK_FILENAME"]
