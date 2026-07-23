"""Strict unified-diff validation and recoverable patch application."""
from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterable

from .models import CommandResult, LearnError


_DIFF_HEADER = re.compile(r"^diff --git a/(\S+) b/(\S+)$")
_FILE_HEADER = re.compile(r"^(?:---|\+\+\+) ([ab]/\S+|/dev/null)(?:\t.*)?$")
MAX_PATCH_BYTES = 2 * 1024 * 1024
MAX_PATCH_PATHS = 50


def patch_paths(patch: str) -> tuple[str, ...]:
    if not patch.strip():
        raise LearnError("agent returned an empty patch")
    if len(patch.encode("utf-8")) > MAX_PATCH_BYTES:
        raise LearnError("agent patch exceeds the 2 MiB safety limit")
    if any(marker in patch for marker in ("GIT binary patch", "Binary files ",
                                           "deleted file mode", "rename from ",
                                           "copy from ", "new file mode 120000",
                                           "old mode 120000", "new mode 120000")):
        raise LearnError("binary, delete, rename, and copy patches are forbidden")
    paths: list[str] = []
    for line in patch.splitlines():
        match = _DIFF_HEADER.match(line)
        if not match:
            continue
        old, new = match.groups()
        if old != new:
            raise LearnError("patch may not rename files")
        candidate = PurePosixPath(new)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise LearnError(f"unsafe patch path: {new}")
        paths.append(candidate.as_posix())
    if not paths:
        raise LearnError("response is not a git-style unified diff")
    unique = tuple(dict.fromkeys(paths))
    if len(unique) > MAX_PATCH_PATHS:
        raise LearnError("agent patch touches more than 50 files")
    declared = set(unique)
    for line in patch.splitlines():
        if not line.startswith(("--- ", "+++ ")):
            continue
        header = _FILE_HEADER.match(line)
        if not header:
            raise LearnError(f"unsafe or malformed file header: {line[:160]}")
        value = header.group(1)
        if value == "/dev/null":
            continue
        if value[2:] not in declared:
            raise LearnError(f"file header lacks an approved diff declaration: {value[2:]}")
    return unique


def validate_patch_paths(patch: str, allowed_roots: Iterable[str], *,
                         tests_only: bool = False) -> tuple[str, ...]:
    paths = patch_paths(patch)
    roots = tuple(root.replace("\\", "/") for root in allowed_roots)
    for path in paths:
        if path.startswith("02_Source/") or path == "02_Source":
            raise LearnError("patch may never touch 02_Source")
        if tests_only and not path.startswith("scripts/pipelines/textbooks/tests/"):
            raise LearnError(f"red-test patch escaped tests root: {path}")
        if not any(path == root.rstrip("/") or path.startswith(root) for root in roots):
            raise LearnError(f"patch path is outside the approved plan: {path}")
    return paths


@dataclass
class WorkspaceBackup:
    repo_root: Path
    entries: dict[str, bytes | None]

    @classmethod
    def capture(cls, repo_root: Path, paths: Iterable[str]) -> "WorkspaceBackup":
        entries: dict[str, bytes | None] = {}
        for relative in paths:
            target = repo_root / Path(relative)
            entries[relative] = target.read_bytes() if target.is_file() else None
        return cls(repo_root, entries)

    def restore(self) -> None:
        for relative, content in self.entries.items():
            target = self.repo_root / Path(relative)
            if content is None:
                if target.exists():
                    target.unlink()
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            temp = target.with_suffix(target.suffix + ".quality-learn-restore.tmp")
            temp.write_bytes(content)
            os.replace(temp, target)


def git_apply(repo_root: Path, patch: str, *, check: bool = False) -> CommandResult:
    argv = ["git", "apply", "--recount", "--whitespace=nowarn"]
    if check:
        argv.append("--check")
    proc = subprocess.run(argv, cwd=repo_root, input=patch, text=True,
                          encoding="utf-8", errors="replace", capture_output=True)
    result = CommandResult(tuple(argv), proc.returncode, proc.stdout or "", proc.stderr or "")
    if proc.returncode != 0:
        raise LearnError(f"git apply {'check ' if check else ''}failed: {result.stderr[:500]}")
    return result
