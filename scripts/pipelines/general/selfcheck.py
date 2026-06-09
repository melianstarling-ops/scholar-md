#!/usr/bin/env python3
"""
selfcheck.py — general 管线 Tier0 形态自检(零成本、确定性、无幻觉)。

对接 SOP-03,逐份核查 Typora 重排后的结构(<root>/<name>.md + <root>/<name>.assets/):

  [error]   content_coverage  内容覆盖校验:源 PDF 文字层 vs md 字符多重集求差,
                              missing_ratio > 0.5% 即疑似内容丢失(SOP-03 核心闸门)
  [error]   file_empty        md 为空
  [error]   broken_image      md 引用的本地图片在 .assets/ 中不存在(断图链)
  [error]   picture_omitted   残留 'picture omitted' 占位
  [error]   base64_image      图片以 base64 内嵌(应外置为文件)
  [warning] orphan_image      .assets/ 中存在但 md 未引用的图片
  [warning] garbage_chars     U+FFFD 等替换字符达到阈值(疑似乱码)
  [warning] coverage_skipped  未找到同名源 PDF 或 PyMuPDF 不可用,跳过覆盖校验

边界(SOP-03 §2):覆盖校验可靠捕获"内容丢失",但抓不到"一字未丢、阅读顺序乱掉"
(字符多重集相同)。后者归可选 Tier1 AI 审查,不在 Tier0 职责内。

用法:
    python selfcheck.py --dir "D:/.../References"
    python selfcheck.py --dir "D:/.../References" --report "<报告.md>" --json "<结果.json>"

退出码: 存在 error -> 1; 仅 warning 或全通过 -> 0
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

IMAGE_SUFFIXES = {
    ".avif", ".bmp", ".gif", ".jpeg", ".jpg",
    ".png", ".svg", ".tif", ".tiff", ".webp",
}
MD_IMAGE_RE = re.compile(r"!\[[^\]]*\]\((?P<target>[^)\s]+)(?:\s+[^)]*)?\)")
HTML_IMG_RE = re.compile(r"<img\b[^>]*?\bsrc=[\"'](?P<target>[^\"']+)[\"']", re.IGNORECASE)
BASE64_RE = re.compile(r"data:image/[^;]+;base64,[A-Za-z0-9+/=]+")
_EXTERNAL_PREFIXES = ("http://", "https://", "data:", "mailto:", "#")

COVERAGE_THRESHOLD = 0.005  # SOP-03: missing_ratio > 0.5% 标记内容丢失


@dataclass
class Finding:
    file: str
    check: str
    message: str
    severity: str  # "error" | "warning"

    def __str__(self) -> str:
        icon = "❌" if self.severity == "error" else "⚠️"
        return f"{icon} [{self.check}] {self.file}: {self.message}"


def _is_local_image(target: str) -> bool:
    t = target.strip()
    if not t or t.lower().startswith(_EXTERNAL_PREFIXES):
        return False
    return Path(t.split("#")[0].split("?")[0]).suffix.lower() in IMAGE_SUFFIXES


def _local_image_refs(text: str) -> list[str]:
    """提取 md 中所有本地图片引用的目标(原始字符串,未解码)。"""
    refs = [m.group("target") for m in MD_IMAGE_RE.finditer(text)]
    refs += [m.group("target") for m in HTML_IMG_RE.finditer(text)]
    return [r for r in refs if _is_local_image(r)]


def _alnum_counter(text: str) -> Counter:
    """字母数字字符的多重集(字符级,对空格/连字符/标点附着鲁棒)。"""
    return Counter(c for c in text.lower() if c.isalnum())


def check_coverage(md_text: str, source_pdf: Path, threshold: float = COVERAGE_THRESHOLD) -> dict | None:
    """SOP-03 内容覆盖校验:源 PDF 文字层 ground truth 与 md 比字符多重集。

    返回 None 表示无法校验(PyMuPDF 不可用或源 PDF 不存在)。
    born-digital 文档文字层即 ground truth,无需剔除策略,直接全量比对。
    """
    try:
        import fitz
    except ImportError:
        return None
    if not source_pdf.is_file():
        return None

    doc = fitz.open(source_pdf)
    try:
        src_text = " ".join(page.get_text("text") for page in doc)
    finally:
        doc.close()

    exp = _alnum_counter(src_text)
    act = _alnum_counter(md_text)
    missing = exp - act  # 源有、输出缺 -> 真丢失
    total = sum(exp.values()) or 1
    miss_n = sum(missing.values())
    ratio = miss_n / total
    return {
        "expected_chars": total,
        "missing_chars": miss_n,
        "missing_ratio": round(ratio, 5),
        "passed": ratio <= threshold,
        "missing_sample": dict(missing.most_common(10)),
    }


def check_one(md: Path, root: Path) -> list[Finding]:
    name = md.stem
    findings: list[Finding] = []
    text = md.read_text(encoding="utf-8", errors="replace")

    if not text.strip():
        findings.append(Finding(md.name, "file_empty", "文件为空", "error"))
        return findings

    if "picture omitted" in text.lower():
        n = text.lower().count("picture omitted")
        findings.append(Finding(md.name, "picture_omitted", f"发现 {n} 处 'picture omitted'", "error"))

    if BASE64_RE.search(text):
        n = len(BASE64_RE.findall(text))
        findings.append(Finding(md.name, "base64_image", f"发现 {n} 处 base64 内嵌图片", "error"))

    n_garbage = text.count("�")
    if n_garbage >= 10:
        findings.append(Finding(md.name, "garbage_chars", f"发现 {n_garbage} 个 U+FFFD 替换字符", "warning"))

    # 断图链 + orphan:解析引用,与 <name>.assets/ 实体比对
    refs = _local_image_refs(text)
    referenced = {Path(unquote(r)).name for r in refs}

    assets_dir = root / f"{name}.assets"
    existing = set()
    if assets_dir.is_dir():
        existing = {p.name for p in assets_dir.iterdir()
                    if p.is_file() and p.suffix.lower() in IMAGE_SUFFIXES}

    missing = sorted(referenced - existing)
    if missing:
        sample = ", ".join(missing[:5]) + (" ..." if len(missing) > 5 else "")
        findings.append(Finding(
            md.name, "broken_image",
            f"{len(missing)} 个引用图片在 {assets_dir.name}/ 中缺失 ({sample})", "error"))

    orphan = sorted(existing - referenced)
    if orphan:
        sample = ", ".join(orphan[:5]) + (" ..." if len(orphan) > 5 else "")
        findings.append(Finding(
            md.name, "orphan_image",
            f"{len(orphan)} 张图片未被 md 引用 ({sample})", "warning"))

    # SOP-03 内容覆盖校验:源 PDF 取同目录同名 <name>.pdf
    cov = check_coverage(text, md.with_suffix(".pdf"))
    if cov is None:
        findings.append(Finding(
            md.name, "coverage_skipped",
            "未找到同名源 PDF 或 PyMuPDF 不可用,跳过内容覆盖校验", "warning"))
    elif not cov["passed"]:
        findings.append(Finding(
            md.name, "content_coverage",
            f"疑似内容丢失 {cov['missing_ratio'] * 100:.2f}% "
            f"(缺 {cov['missing_chars']}/{cov['expected_chars']} 字符, 阈值 0.5%), "
            f"样本 {cov['missing_sample']}", "error"))

    return findings


def build_report(per_file: dict[str, list[Finding]]) -> str:
    n_err = sum(1 for fs in per_file.values() for f in fs if f.severity == "error")
    n_warn = sum(1 for fs in per_file.values() for f in fs if f.severity == "warning")
    lines = [
        "# general 管线 Tier0 自检报告",
        "",
        f"- 生成时间: {datetime.now().isoformat(timespec='seconds')}",
        f"- 检查文件数: {len(per_file)}",
        f"- 错误: {n_err} · 警告: {n_warn}",
        "- 规程: SOP-03(内容覆盖校验 + 形态检查)",
        "",
        "## 逐份结果",
        "",
    ]
    for fname in sorted(per_file):
        fs = per_file[fname]
        if not fs:
            lines.append(f"- [x] **{fname}** — 通过")
        else:
            lines.append(f"- [ ] **{fname}**")
            for f in fs:
                lines.append(f"    - {f}")
    lines += ["", "## 结论", ""]
    lines.append("- [x] **通过** — 内容完整、形态正确,可直接使用" if n_err == 0
                 else "- [ ] **不通过** — 存在 error,需修复后重转")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description="general 管线 Tier0 形态 + 内容覆盖自检(SOP-03)")
    ap.add_argument("--dir", "-d", required=True, help="重排后的根目录(含 <name>.md + <name>.assets/ + 同名源 PDF)")
    ap.add_argument("--report", "-r", help="Markdown 报告输出路径")
    ap.add_argument("--json", help="JSON 结果输出路径")
    args = ap.parse_args()

    root = Path(args.dir).resolve()
    if not root.is_dir():
        print(f"错误: 目录不存在: {root}", file=sys.stderr)
        return 2

    md_files = sorted(p for p in root.glob("*.md"))
    if not md_files:
        print(f"未发现 .md 文件: {root}", file=sys.stderr)
        return 2

    per_file: dict[str, list[Finding]] = {md.name: check_one(md, root) for md in md_files}

    print(f"[{datetime.now().isoformat(timespec='seconds')}] Tier0 自检: {len(md_files)} 份")
    all_findings = [f for fs in per_file.values() for f in fs]
    if not all_findings:
        print("✅ 全部通过")
    else:
        for f in all_findings:
            print(f"  {f}")

    if args.report:
        rp = Path(args.report)
        rp.parent.mkdir(parents=True, exist_ok=True)
        rp.write_text(build_report(per_file), encoding="utf-8")
        print(f"报告已保存: {rp}")

    if args.json:
        jp = Path(args.json)
        jp.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "files_checked": len(md_files),
            "findings": [
                {"file": f.file, "check": f.check, "message": f.message, "severity": f.severity}
                for f in all_findings
            ],
            "passed": not any(f.severity == "error" for f in all_findings),
        }
        jp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"JSON 已保存: {jp}")

    return 1 if any(f.severity == "error" for f in all_findings) else 0


if __name__ == "__main__":
    sys.exit(main())
