"""Versioned page-level derived cache for textbooks reconstruction.

The cache is deliberately independent from ``convert.py``.  Callers provide
already-computed adoption decisions, reconstruction fragments, and page
Markdown; this module only normalizes, fingerprints, validates, and stores
those derived values.
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
from bisect import bisect_left
from dataclasses import asdict, is_dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


SCHEMA_VERSION = 1
CACHE_DIRNAME = "_derived_v1"
DOCUMENT_INDEX_FILENAME = "document_index.json"
_SHA256_LENGTH = 64
NEWLINE_LF = "LF"
NEWLINE_CRLF = "CRLF"
NEWLINE_STYLES = frozenset({NEWLINE_LF, NEWLINE_CRLF})


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def sha256_text(value: str) -> str:
    return sha256_bytes(value.encode("utf-8"))


def sha256_file(path: str | os.PathLike[str]) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sha256_json(value: Any) -> str:
    return sha256_text(_canonical_json(value))


def detect_newline_style(text: str) -> str:
    """Return the uniform document newline style; reject mixed/lone-CR text."""
    if not isinstance(text, str):
        raise ValueError("text must be a string")
    without_crlf = text.replace("\r\n", "")
    has_crlf = "\r\n" in text
    has_lf = "\n" in without_crlf
    if "\r" in without_crlf or (has_crlf and has_lf):
        raise ValueError("mixed or unsupported document newline style")
    return NEWLINE_CRLF if has_crlf else NEWLINE_LF


def normalize_document_newlines(text: str) -> tuple[str, str]:
    style = detect_newline_style(text)
    return (text.replace("\r\n", "\n") if style == NEWLINE_CRLF else text), style


def _apply_newline_style(text: str, newline_style: str) -> str:
    if newline_style not in NEWLINE_STYLES:
        raise ValueError(f"invalid newline_style: {newline_style!r}")
    if "\r" in text:
        raise ValueError("canonical page Markdown must use LF newlines")
    return text if newline_style == NEWLINE_LF else text.replace("\n", "\r\n")


def _valid_sha256(value: Any) -> bool:
    if not isinstance(value, str) or len(value) != _SHA256_LENGTH:
        return False
    try:
        int(value, 16)
    except ValueError:
        return False
    return True


def derived_dir(work_dir: str | os.PathLike[str]) -> Path:
    return Path(work_dir) / CACHE_DIRNAME


def page_cache_path(work_dir: str | os.PathLike[str], page: int) -> Path:
    _require_page(page)
    return derived_dir(work_dir) / f"page_{page:04d}.json"


def document_index_path(work_dir: str | os.PathLike[str]) -> Path:
    return derived_dir(work_dir) / DOCUMENT_INDEX_FILENAME


def _require_page(page: int) -> None:
    if isinstance(page, bool) or not isinstance(page, int) or page < 1:
        raise ValueError(f"page must be a positive integer, got {page!r}")


def _require_profile(name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")


def build_cache_key(
    *,
    stem: str,
    source_pdf_sha256: str,
    dpi: int,
    ocr_page_sha256: str,
    page_corrections: Any,
    page_overlay: Any,
    adoption_thresholds: Mapping[str, Any],
    reconstruct_profile: str,
    adoption_profile: str,
) -> dict[str, Any]:
    """Build a strong, inspectable cache key for one page.

    ``source_pdf_sha256`` should be computed once per document and reused for
    all pages.  Corrections and overlays are hashed from canonical JSON so
    mapping insertion order cannot cause false invalidation.
    """
    if not isinstance(stem, str) or not stem:
        raise ValueError("stem must be a non-empty string")
    if not _valid_sha256(source_pdf_sha256):
        raise ValueError("source_pdf_sha256 must be a 64-character SHA-256")
    if not _valid_sha256(ocr_page_sha256):
        raise ValueError("ocr_page_sha256 must be a 64-character SHA-256")
    if isinstance(dpi, bool) or not isinstance(dpi, int) or dpi <= 0:
        raise ValueError("dpi must be a positive integer")
    if not isinstance(adoption_thresholds, Mapping):
        raise ValueError("adoption_thresholds must be a mapping")
    _require_profile("reconstruct_profile", reconstruct_profile)
    _require_profile("adoption_profile", adoption_profile)

    components = {
        "schema_version": SCHEMA_VERSION,
        "stem": stem,
        "source_pdf_sha256": source_pdf_sha256.lower(),
        "dpi": dpi,
        "ocr_page_sha256": ocr_page_sha256.lower(),
        "page_corrections_sha256": sha256_json(page_corrections),
        "page_overlay_sha256": sha256_json(page_overlay),
        "adoption_thresholds": dict(adoption_thresholds),
        "reconstruct_profile": reconstruct_profile,
        "adoption_profile": adoption_profile,
    }
    return {**components, "digest": sha256_json(components)}


def build_cache_key_from_files(
    *,
    stem: str,
    source_pdf_path: str | os.PathLike[str],
    dpi: int,
    ocr_page_path: str | os.PathLike[str],
    page_corrections: Any,
    page_overlay: Any,
    adoption_thresholds: Mapping[str, Any],
    reconstruct_profile: str,
    adoption_profile: str,
) -> dict[str, Any]:
    """Convenience wrapper for tests, migration tools, and one-off callers.

    Production integration should hash the source PDF once, then call
    :func:`build_cache_key` for every page.
    """
    return build_cache_key(
        stem=stem,
        source_pdf_sha256=sha256_file(source_pdf_path),
        dpi=dpi,
        ocr_page_sha256=sha256_file(ocr_page_path),
        page_corrections=page_corrections,
        page_overlay=page_overlay,
        adoption_thresholds=adoption_thresholds,
        reconstruct_profile=reconstruct_profile,
        adoption_profile=adoption_profile,
    )


def _mapping(value: Any, *, field: str) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        value = asdict(value)
    if not isinstance(value, Mapping):
        raise ValueError(f"{field} entries must be mappings or dataclass instances")
    return dict(value)


def _normalize_decisions(decisions: Iterable[Any]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    seen: set[int] = set()
    for raw in decisions:
        item = _mapping(raw, field="adoption_decisions")
        block_id = item.get("block_id")
        if isinstance(block_id, bool) or not isinstance(block_id, int):
            raise ValueError("adoption decision block_id must be an integer")
        if block_id in seen:
            raise ValueError(f"duplicate adoption decision block_id: {block_id}")
        seen.add(block_id)
        normalized.append(
            {
                "block_id": block_id,
                "content_source": item.get("content_source"),
                "reasons": list(item.get("reasons") or []),
                "block_ned": item.get("block_ned"),
                "adopted_text": item.get("adopted_text"),
            }
        )
    return normalized


def _normalize_fragments(fragments: Iterable[Any]) -> tuple[list[dict[str, Any]], str]:
    normalized: list[dict[str, Any]] = []
    cursor = 0
    raw_fragments = list(fragments)
    for index, raw in enumerate(raw_fragments):
        item = _mapping(raw, field="fragments")
        block_ids = item.get("block_ids", item.get("bids", []))
        if not isinstance(block_ids, (list, tuple)):
            raise ValueError("fragment block_ids/bids must be a list or tuple")
        # Legacy/synthetic OCR blocks may omit the real block_id.  A fragment
        # can therefore carry ``None`` as an explicit "unlocatable" marker.
        # Adoption decisions remain strict integers in _normalize_decisions:
        # their IDs are executable source indexes, not optional provenance.
        if any(block_id is not None
               and (isinstance(block_id, bool) or not isinstance(block_id, int))
               for block_id in block_ids):
            raise ValueError("fragment block_ids must contain integers or None")
        md = item.get("md")
        if not isinstance(md, str):
            raise ValueError("fragment md must be a string")
        start = cursor
        end = start + len(md)
        normalized.append(
            {
                "block_ids": list(block_ids),
                "md": md,
                "local_start": start,
                "local_end": end,
            }
        )
        cursor = end + (2 if index + 1 < len(raw_fragments) else 0)
    page_md = "\n\n".join(item["md"] for item in normalized) + "\n"
    return normalized, page_md


def _record_hash(record: Mapping[str, Any]) -> str:
    payload = dict(record)
    payload.pop("record_sha256", None)
    return sha256_json(payload)


def materialize_page_cache(
    *,
    page: int,
    cache_key: Mapping[str, Any],
    adopted_decisions: Iterable[Any],
    fragments: Iterable[Any],
    page_markdown: str,
    warnings: Iterable[Mapping[str, Any]] = (),
    expected_assets: Iterable[str] = (),
    column_layout_suspected: bool = False,
    page_overlays: Iterable[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Create a validated page-cache record without writing it."""
    _require_page(page)
    if not isinstance(cache_key, Mapping) or not _valid_cache_key(cache_key):
        raise ValueError("cache_key is invalid")
    if not isinstance(page_markdown, str):
        raise ValueError("page_markdown must be a string")

    normalized_fragments, reconstructed = _normalize_fragments(fragments)
    if reconstructed != page_markdown:
        raise ValueError("page_markdown does not match the supplied fragments")

    record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "page": page,
        "reconstruct_profile": cache_key["reconstruct_profile"],
        "adoption_profile": cache_key["adoption_profile"],
        "cache_key": dict(cache_key),
        "adoption_decisions": _normalize_decisions(adopted_decisions),
        "fragments": normalized_fragments,
        "page_markdown": page_markdown,
        "warnings": [dict(item) for item in warnings],
        "expected_assets": list(expected_assets),
        "column_layout_suspected": bool(column_layout_suspected),
        "page_overlays": [dict(item) for item in page_overlays],
        "page_md_sha256": sha256_text(page_markdown),
    }
    record["record_sha256"] = _record_hash(record)
    return record


def _valid_cache_key(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    required = {
        "schema_version", "stem", "source_pdf_sha256", "dpi",
        "ocr_page_sha256", "page_corrections_sha256", "page_overlay_sha256",
        "adoption_thresholds", "reconstruct_profile", "adoption_profile",
        "digest",
    }
    if not required.issubset(value):
        return False
    if value.get("schema_version") != SCHEMA_VERSION:
        return False
    if not all(_valid_sha256(value.get(name)) for name in (
        "source_pdf_sha256", "ocr_page_sha256", "page_corrections_sha256",
        "page_overlay_sha256", "digest",
    )):
        return False
    components = dict(value)
    digest = components.pop("digest")
    return sha256_json(components) == digest


def _valid_page_record(record: Any, *, page: int | None = None) -> bool:
    if not isinstance(record, Mapping) or record.get("schema_version") != SCHEMA_VERSION:
        return False
    record_page = record.get("page")
    if isinstance(record_page, bool) or not isinstance(record_page, int) or record_page < 1:
        return False
    if page is not None and record_page != page:
        return False
    if not _valid_cache_key(record.get("cache_key")):
        return False
    if record.get("reconstruct_profile") != record["cache_key"].get("reconstruct_profile"):
        return False
    if record.get("adoption_profile") != record["cache_key"].get("adoption_profile"):
        return False
    page_md = record.get("page_markdown")
    if not isinstance(page_md, str) or sha256_text(page_md) != record.get("page_md_sha256"):
        return False
    if not isinstance(record.get("fragments"), list):
        return False
    try:
        fragments, reconstructed = _normalize_fragments(record["fragments"])
    except ValueError:
        return False
    if fragments != record["fragments"] or reconstructed != page_md:
        return False
    if not isinstance(record.get("adoption_decisions"), list):
        return False
    try:
        if _normalize_decisions(record["adoption_decisions"]) != record["adoption_decisions"]:
            return False
    except ValueError:
        return False
    if not isinstance(record.get("warnings"), list):
        return False
    if not isinstance(record.get("expected_assets"), list):
        return False
    if not isinstance(record.get("page_overlays", []), list):
        return False
    return _valid_sha256(record.get("record_sha256")) and \
        _record_hash(record) == record["record_sha256"]


def page_cache_is_fresh(record: Any, expected_key: Mapping[str, Any]) -> bool:
    return _valid_page_record(record) and _valid_cache_key(expected_key) and \
        dict(record["cache_key"]) == dict(expected_key)


def _atomic_write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            newline="\n",
            dir=path.parent,
            prefix=".tmp-",
            suffix=".json",
            delete=False,
        ) as handle:
            temporary = handle.name
            json.dump(value, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            try:
                os.remove(temporary)
            except FileNotFoundError:
                pass


def write_page_cache(
    work_dir: str | os.PathLike[str],
    record: Mapping[str, Any],
) -> Path:
    if not _valid_page_record(record):
        raise ValueError("refusing to write invalid page cache record")
    path = page_cache_path(work_dir, int(record["page"]))
    _atomic_write_json(path, record)
    return path


def _read_json_object(path: Path) -> dict[str, Any] | None:
    try:
        with path.open("r", encoding="utf-8") as handle:
            value = json.load(handle)
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def read_page_cache(
    work_dir: str | os.PathLike[str],
    page: int,
    *,
    expected_key: Mapping[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Read one cache page, returning ``None`` for every stale/corrupt state."""
    path = page_cache_path(work_dir, page)
    record = _read_json_object(path)
    if not _valid_page_record(record, page=page):
        return None
    if expected_key is not None and not page_cache_is_fresh(record, expected_key):
        return None
    return record


def assemble_document(
        page_records: Sequence[Mapping[str, Any]], *,
        newline_style: str = NEWLINE_LF) -> str:
    """Join cached pages byte-for-byte like the current ``assemble()`` path."""
    ordered = sorted(page_records, key=lambda item: item["page"])
    page_markdown = [
        str(item["page_markdown"])
        for item in ordered
        if str(item["page_markdown"]).strip()
    ]
    canonical = "\n\n".join(page_markdown) + "\n"
    return _apply_newline_style(canonical, newline_style)


def _key_with_page_overlays(
        cache_key: Mapping[str, Any],
        page_overlays: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    components = dict(cache_key)
    components.pop("digest", None)
    components["page_overlay_sha256"] = sha256_json(list(page_overlays))
    return {**components, "digest": sha256_json(components)}


def _line_offsets(lines: Sequence[str]) -> list[int]:
    offsets = [0]
    for line in lines:
        offsets.append(offsets[-1] + len(line))
    return offsets


def _page_for_change(entries: Sequence[Mapping[str, Any]],
                     start: int, end: int) -> int | None:
    matches: list[int] = []
    for entry in entries:
        page_start = entry["document_start"]
        page_end = entry["document_end"]
        if page_start is None or page_end is None:
            continue
        if start == end:
            if page_start < start < page_end:
                matches.append(entry["page"])
        elif page_start <= start and end <= page_end:
            matches.append(entry["page"])
    return matches[0] if len(matches) == 1 else None


def reconcile_page_overlays(
        page_records: Sequence[Mapping[str, Any]],
        *, current_final_markdown: str) -> list[dict[str, Any]]:
    """Map an exact final-MD diff to unambiguous single-page overlays.

    The baseline is assembled from ``page_records``.  Each non-equal diff
    opcode must lie wholly inside one page.  Insertions at page boundaries,
    cross-page replacements, and boundaries touched by a change fail loud.
    Returned records are in-memory candidates; this function writes nothing.
    """
    records = sorted((dict(item) for item in page_records), key=lambda item: item["page"])
    canonical_current, newline_style = normalize_document_newlines(
        current_final_markdown)
    baseline = assemble_document(records)
    if baseline == canonical_current:
        return records
    baseline_index = build_document_index(records, final_markdown=baseline)
    entries = baseline_index["pages"]
    base_lines = baseline.splitlines(keepends=True)
    current_lines = canonical_current.splitlines(keepends=True)
    base_offsets = _line_offsets(base_lines)
    current_offsets = _line_offsets(current_lines)
    matcher = SequenceMatcher(None, base_lines, current_lines, autojunk=False)
    opcodes = matcher.get_opcodes()
    changed_pages: set[int] = set()
    for tag, i1, i2, _j1, _j2 in opcodes:
        if tag == "equal":
            continue
        page = _page_for_change(entries, base_offsets[i1], base_offsets[i2])
        if page is None:
            raise RuntimeError(
                "legacy_cache_migration_unresolved: diff 跨页或位于页边界")
        changed_pages.add(page)

    def map_line_boundary(index: int) -> int:
        if index == 0:
            return 0
        if index == len(base_lines):
            return len(current_lines)
        candidates: set[int] = set()
        for tag, i1, i2, j1, _j2 in opcodes:
            if tag == "equal" and i1 <= index <= i2:
                candidates.add(j1 + (index - i1))
        if len(candidates) != 1:
            raise RuntimeError(
                "legacy_cache_migration_unresolved: 页边界被修改或映射不唯一")
        return candidates.pop()

    entry_by_page = {entry["page"]: entry for entry in entries}
    record_by_page = {record["page"]: record for record in records}
    for page in sorted(changed_pages):
        entry = entry_by_page[page]
        start = entry["document_start"]
        end = entry["document_end"]
        start_line = bisect_left(base_offsets, start)
        end_line = bisect_left(base_offsets, end)
        if base_offsets[start_line] != start or base_offsets[end_line] != end:
            raise RuntimeError(
                "legacy_cache_migration_unresolved: 页 span 不是稳定行边界")
        current_start = current_offsets[map_line_boundary(start_line)]
        current_end = current_offsets[map_line_boundary(end_line)]
        replacement = canonical_current[current_start:current_end]
        original = record_by_page[page]
        overlay = {
            "schema_version": SCHEMA_VERSION,
            "kind": "exact_page_replacement",
            "page": page,
            "baseline_page_sha256": original["page_md_sha256"],
            "replacement_page_sha256": sha256_text(replacement),
            "replacement_page_markdown": replacement,
            "baseline_final_sha256": sha256_text(baseline),
            "source_final_sha256": sha256_text(current_final_markdown),
            "document_newline_style": newline_style,
        }
        overlays = list(original.get("page_overlays") or []) + [overlay]
        block_ids = [
            block_id
            for fragment in original["fragments"]
            for block_id in fragment["block_ids"]
        ]
        record_by_page[page] = materialize_page_cache(
            page=page,
            cache_key=_key_with_page_overlays(original["cache_key"], overlays),
            adopted_decisions=original["adoption_decisions"],
            fragments=[{"block_ids": block_ids, "md": replacement[:-1]
                        if replacement.endswith("\n") else replacement}],
            page_markdown=replacement,
            warnings=original["warnings"],
            expected_assets=original["expected_assets"],
            column_layout_suspected=original["column_layout_suspected"],
            page_overlays=overlays,
        )

    reconciled = [record_by_page[record["page"]] for record in records]
    if assemble_document(
            reconciled, newline_style=newline_style) != current_final_markdown:
        raise RuntimeError(
            "legacy_cache_migration_unresolved: page overlays 无法精确重建当前 MD")
    return reconciled


def snapshot_cache_files(
        work_dir: str | os.PathLike[str],
        pages: Iterable[int]) -> dict[Path, bytes | None]:
    """Capture cache bytes for a small rollback transaction."""
    paths = [page_cache_path(work_dir, page) for page in sorted(set(pages))]
    paths.append(document_index_path(work_dir))
    return {path: (path.read_bytes() if path.is_file() else None) for path in paths}


def restore_cache_files(snapshot: Mapping[Path, bytes | None]) -> None:
    for path, content in snapshot.items():
        if content is None:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(path.name + ".rollback.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def snapshot_cache_directory(
        work_dir: str | os.PathLike[str]) -> dict[str, bytes]:
    """Capture every committed derived-cache JSON for formula rollback."""
    root = derived_dir(work_dir)
    if not root.is_dir():
        return {}
    paths = list(root.glob("page_*.json"))
    index = document_index_path(work_dir)
    if index.is_file():
        paths.append(index)
    return {path.name: path.read_bytes() for path in sorted(set(paths))}


def restore_cache_directory(
        work_dir: str | os.PathLike[str],
        snapshot: Mapping[str, bytes]) -> None:
    """Restore a complete cache snapshot and remove later cache commits."""
    root = derived_dir(work_dir)
    current = list(root.glob("page_*.json")) if root.is_dir() else []
    index = document_index_path(work_dir)
    if index.is_file():
        current.append(index)
    for path in current:
        if path.name not in snapshot:
            path.unlink()
    for name, content in snapshot.items():
        if (not isinstance(name, str)
                or (name != DOCUMENT_INDEX_FILENAME
                    and not (name.startswith("page_") and name.endswith(".json")))):
            raise ValueError(f"invalid derived cache snapshot entry: {name!r}")
        root.mkdir(parents=True, exist_ok=True)
        path = root / name
        temporary = path.with_name(path.name + ".rollback.tmp")
        try:
            temporary.write_bytes(content)
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def build_document_index(
    page_records: Sequence[Mapping[str, Any]],
    *,
    final_markdown: str,
) -> dict[str, Any]:
    """Build a document index and exact final-document page spans."""
    if not isinstance(final_markdown, str):
        raise ValueError("final_markdown must be a string")
    records = sorted(page_records, key=lambda item: item.get("page", 0))
    if not records or any(not _valid_page_record(record) for record in records):
        raise ValueError("page_records must contain valid page cache records")
    pages = [record["page"] for record in records]
    if len(pages) != len(set(pages)):
        raise ValueError("page_records contain duplicate pages")
    canonical_final, newline_style = normalize_document_newlines(final_markdown)
    if assemble_document(records) != canonical_final:
        raise ValueError("final_markdown does not match cached pages")

    entries: list[dict[str, Any]] = []
    cursor = 0
    nonempty_indexes = [
        index for index, record in enumerate(records)
        if record["page_markdown"].strip()
    ]
    last_nonempty = nonempty_indexes[-1] if nonempty_indexes else None
    for index, record in enumerate(records):
        page_md = record["page_markdown"]
        if page_md.strip():
            canonical_start = cursor
            canonical_end = canonical_start + len(page_md)
            cursor = canonical_end + (2 if index != last_nonempty else 0)
            if newline_style == NEWLINE_CRLF:
                start = canonical_start + canonical_final[:canonical_start].count("\n")
                end = canonical_end + canonical_final[:canonical_end].count("\n")
            else:
                start, end = canonical_start, canonical_end
        else:
            start = end = None
        entries.append(
            {
                "page": record["page"],
                "cache_key_digest": record["cache_key"]["digest"],
                "page_md_sha256": record["page_md_sha256"],
                "document_start": start,
                "document_end": end,
            }
        )

    index_record: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "join_profile": "assemble-v1",
        "newline_style": newline_style,
        "pages": entries,
        "final_md_sha256": sha256_text(final_markdown),
    }
    index_record["record_sha256"] = _record_hash(index_record)
    return index_record


def _valid_document_index(record: Any) -> bool:
    if not isinstance(record, Mapping) or record.get("schema_version") != SCHEMA_VERSION:
        return False
    if record.get("join_profile") != "assemble-v1":
        return False
    if record.get("newline_style") not in NEWLINE_STYLES:
        return False
    if not _valid_sha256(record.get("final_md_sha256")):
        return False
    pages = record.get("pages")
    if not isinstance(pages, list):
        return False
    previous_page = 0
    previous_end = 0
    for entry in pages:
        if not isinstance(entry, Mapping):
            return False
        page = entry.get("page")
        if isinstance(page, bool) or not isinstance(page, int) or page <= previous_page:
            return False
        previous_page = page
        if not _valid_sha256(entry.get("cache_key_digest")) or \
                not _valid_sha256(entry.get("page_md_sha256")):
            return False
        start, end = entry.get("document_start"), entry.get("document_end")
        if start is None or end is None:
            if start is not None or end is not None:
                return False
            continue
        if (isinstance(start, bool) or isinstance(end, bool)
                or not isinstance(start, int) or not isinstance(end, int)
                or start < previous_end or end < start):
            return False
        previous_end = end
    return _valid_sha256(record.get("record_sha256")) and \
        _record_hash(record) == record["record_sha256"]


def write_document_index(
    work_dir: str | os.PathLike[str],
    record: Mapping[str, Any],
) -> Path:
    if not _valid_document_index(record):
        raise ValueError("refusing to write invalid document index")
    path = document_index_path(work_dir)
    _atomic_write_json(path, record)
    return path


def read_document_index(
    work_dir: str | os.PathLike[str],
    *,
    expected_final_sha256: str | None = None,
) -> dict[str, Any] | None:
    record = _read_json_object(document_index_path(work_dir))
    if not _valid_document_index(record):
        return None
    if expected_final_sha256 is not None:
        if not _valid_sha256(expected_final_sha256):
            raise ValueError("expected_final_sha256 must be a 64-character SHA-256")
        if record["final_md_sha256"] != expected_final_sha256.lower():
            return None
    return record
