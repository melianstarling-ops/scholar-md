from __future__ import annotations

import re

from scripts.pipelines.textbooks.selfcheck import inline_math_delimiter_ws_scan

from ..models import DetectorContext, Finding, Severity


_CURRENCY_BODY = re.compile(r"^\d+(?:[.,]\d+)?(?:\s|$)")


def _inline_dollar_positions(line: str) -> list[int]:
    positions: list[int] = []
    i = 0
    while i < len(line):
        if line[i] == "\\":
            i += 2
            continue
        if line.startswith("$$", i):
            i += 2
            continue
        if line[i] == "$":
            positions.append(i)
        i += 1
    return positions


def detect_final_delimiters(context: DetectorContext) -> list[Finding]:
    text = context.md_path.read_text(encoding="utf-8")
    findings: list[Finding] = []
    ws = inline_math_delimiter_ws_scan(text)
    if ws["count"]:
        findings.append(Finding.create(
            capability="final_delimiters", kind="inline_math_delimiter_whitespace",
            severity=Severity.P1,
            message=f"发现 {ws['count']} 处行内公式定界符空白",
            evidence=ws,
        ))
    for line_no, line in enumerate(text.splitlines(), 1):
        positions = _inline_dollar_positions(line)
        if len(positions) % 2:
            unmatched = positions[-1]
            suffix = line[unmatched + 1:]
            currency = bool(_CURRENCY_BODY.match(suffix))
            kind = "ambiguous_currency_delimiter" if currency else "unpaired_math_delimiter"
            findings.append(Finding.create(
                capability="final_delimiters", kind=kind,
                severity=Severity.P2 if currency else Severity.P1,
                message=("孤立 $ 后接金额，需区分货币符号与数学定界符"
                         if currency else "行内数学定界符未配对"),
                target={"line": line_no}, evidence={"sample": line[:160]},
            ))
    return findings
