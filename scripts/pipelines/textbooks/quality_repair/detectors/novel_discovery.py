from __future__ import annotations

import statistics

from ..models import DetectorContext, Finding, Severity
from ._shared import page_number, page_result_paths, read_json


_VISUAL_LABELS = {"image", "chart", "table", "figure_title"}


def _page_chars(
        path,
) -> tuple[int, int, int, bool, bool, str, tuple[str, ...]]:
    blocks = read_json(path).get("parsing_res_list") or []
    ordered = [block for block in blocks
               if isinstance(block, dict) and block.get("block_order") is not None]
    contents = tuple(str(block.get("block_content") or "").strip() for block in ordered)
    text = "".join(contents)
    page = page_number(path)
    has_visual = any(isinstance(block, dict) and block.get("block_label") in _VISUAL_LABELS
                     for block in blocks)
    labels = {
        str(block.get("block_label") or "")
        for block in ordered if isinstance(block, dict)
    }
    sparse_front_title = (
        page <= 10 and len(ordered) <= 4 and "doc_title" in labels
    )
    return (
        page, len(text.strip()), len(ordered), has_visual,
        sparse_front_title, text, contents,
    )


def detect_novel_signals(context: DetectorContext) -> list[Finding]:
    stats = [_page_chars(path) for path in page_result_paths(context.work_dir)]
    positive = [chars for _, chars, _, _, _, _, _ in stats if chars > 0]
    if len(positive) < 3:
        return []
    median = statistics.median(positive)
    if median < 100:
        return []
    by_page = {page: chars for page, chars, _, _, _, _, _ in stats}
    findings: list[Finding] = []
    for (page, chars, blocks, has_visual, sparse_front_title,
         text, contents) in stats:
        previous = by_page.get(page - 1, 0)
        following = by_page.get(page + 1, 0)
        if (blocks and not has_visual and not sparse_front_title
                and chars < max(20, median * 0.15)
                and previous > median * 0.5 and following > median * 0.5):
            findings.append(Finding.create(
                capability="novel_discovery", kind="novel_page_text_collapse",
                severity=Severity.P1,
                message="页面文本量相对全书及相邻页异常塌缩",
                page=page,
                evidence={"chars": chars, "blocks": blocks,
                          "median_chars": median,
                          "previous_chars": previous, "next_chars": following},
            ))
        bad = sum(1 for char in text
                  if char == "\ufffd" or (ord(char) < 32 and char not in "\t\n\r"))
        if bad:
            findings.append(Finding.create(
                capability="novel_discovery", kind="novel_bad_character",
                severity=Severity.P1,
                message="页面有替换字符或非法控制字符",
                page=page, evidence={"bad_chars": bad, "chars": chars},
            ))
        best_sample = ""
        best_count = 0
        previous = None
        consecutive = 0
        for content in contents:
            if content and content == previous:
                consecutive += 1
            else:
                previous = content
                consecutive = 1
            if len(content) >= 30 and consecutive > best_count:
                best_sample, best_count = content, consecutive
        if best_count >= 3:
            findings.append(Finding.create(
                capability="novel_discovery", kind="novel_repeated_block_loop",
                severity=Severity.P1,
                message="同页长文本块重复出现，疑似 OCR 回环退化",
                page=page,
                evidence={"repeat_count": best_count, "sample": best_sample[:160]},
            ))
    return findings
