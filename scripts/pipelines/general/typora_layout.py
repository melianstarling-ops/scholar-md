#!/usr/bin/env python3
"""
typora_layout.py — 把 Marker 的输出重排成 Typora 兼容结构。

Marker 默认结构(每份一个子文件夹):
    <root>/<name>/
        <name>.md
        _page_*_Picture_*.jpeg ...
        <name>_meta.json

重排后(VS Code + Typora 双兼容):
    <root>/<name>.md                  ← md 提到根
    <root>/<name>.assets/             ← 图片单独文件夹(Typora 默认约定)
        _page_*_Picture_*.jpeg ...
        <name>_meta.json              ← 转换元数据一并归档到 .assets
    md 内图片引用改写为  <name>.assets/<file>  (路径空格 URL 编码为 %20)

只改写本地图片引用(http/https/data/锚点不动),同时支持 Markdown 与 HTML <img>。
原子文件夹处理完即删除。重排幂等:产物来自子文件夹(裸文件名引用),不会二次加前缀。

用法:
    python typora_layout.py --dir "D:/.../References"
    python typora_layout.py --dir "D:/.../References" --dry-run
"""
from __future__ import annotations

import argparse
import re
import shutil
import sys
from dataclasses import dataclass, field
from pathlib import Path

IMAGE_SUFFIXES = {
    ".avif", ".bmp", ".gif", ".jpeg", ".jpg",
    ".png", ".svg", ".tif", ".tiff", ".webp",
}

# Markdown 图片  ![alt](target "title")  与 HTML <img src="target">
MD_IMAGE_RE = re.compile(r"!\[(?P<alt>[^\]]*)\]\((?P<target>[^)\s]+)(?P<tail>(?:\s+[^)]*)?)\)")
HTML_IMG_RE = re.compile(r"(?P<pre><img\b[^>]*?\bsrc=[\"'])(?P<target>[^\"']+)(?P<post>[\"'][^>]*>)", re.IGNORECASE)

_EXTERNAL_PREFIXES = ("http://", "https://", "data:", "mailto:", "#")


@dataclass
class RelayoutResult:
    name: str
    md_out: Path
    assets_dir: Path
    n_images_moved: int = 0
    n_refs_rewritten: int = 0
    skipped: list[str] = field(default_factory=list)


def _is_local_image(target: str) -> bool:
    """判断引用是否指向本地图片文件(需要改写/搬移的对象)。"""
    t = target.strip()
    if not t or t.lower().startswith(_EXTERNAL_PREFIXES):
        return False
    suffix = Path(t.split("#")[0].split("?")[0]).suffix.lower()
    return suffix in IMAGE_SUFFIXES


def _quote_dir(name: str) -> str:
    """assets 文件夹名做最小 URL 编码:仅空格 -> %20(Typora 与 VS Code 均识别)。"""
    return name.replace(" ", "%20")


def find_marker_outputs(root: Path) -> list[tuple[Path, Path]]:
    """找出 root 下所有 Marker 子文件夹产物。

    判定:子文件夹内存在与其同名的 <name>.md。
    返回 [(folder, md_path), ...]
    """
    out = []
    for sub in sorted(p for p in root.iterdir() if p.is_dir()):
        if sub.name.endswith(".assets"):
            continue
        md = sub / f"{sub.name}.md"
        if md.is_file():
            out.append((sub, md))
    return out


def _rewrite_refs(text: str, assets_ref: str) -> tuple[str, int, set[str]]:
    """把本地图片引用改写为 assets_ref/<file>;返回(新文本, 改写数, 引用到的文件名集)。"""
    referenced: set[str] = set()
    count = 0

    def repl_md(m: re.Match) -> str:
        nonlocal count
        target = m.group("target")
        if not _is_local_image(target):
            return m.group(0)
        fname = Path(target).name
        referenced.add(fname)
        count += 1
        new_target = f"{assets_ref}/{fname.replace(' ', '%20')}"
        return f"![{m.group('alt')}]({new_target}{m.group('tail')})"

    def repl_html(m: re.Match) -> str:
        nonlocal count
        target = m.group("target")
        if not _is_local_image(target):
            return m.group(0)
        fname = Path(target).name
        referenced.add(fname)
        count += 1
        new_target = f"{assets_ref}/{fname.replace(' ', '%20')}"
        return f"{m.group('pre')}{new_target}{m.group('post')}"

    text = MD_IMAGE_RE.sub(repl_md, text)
    text = HTML_IMG_RE.sub(repl_html, text)
    return text, count, referenced


def relayout_one(folder: Path, md_path: Path, root: Path, dry_run: bool = False) -> RelayoutResult:
    name = folder.name
    md_out = root / f"{name}.md"
    assets_dir = root / f"{name}.assets"
    assets_ref = f"{_quote_dir(name)}.assets"

    text = md_path.read_text(encoding="utf-8")
    new_text, n_refs, referenced = _rewrite_refs(text, assets_ref)

    res = RelayoutResult(name=name, md_out=md_out, assets_dir=assets_dir, n_refs_rewritten=n_refs)

    # 收集子文件夹内要归档的文件:图片 + meta.json(排除 md 本身)
    movable = [
        p for p in folder.iterdir()
        if p.is_file() and p.name != md_path.name
        and (p.suffix.lower() in IMAGE_SUFFIXES or p.name.endswith("_meta.json"))
    ]
    n_images = sum(1 for p in movable if p.suffix.lower() in IMAGE_SUFFIXES)
    res.n_images_moved = n_images

    if dry_run:
        return res

    assets_dir.mkdir(parents=True, exist_ok=True)
    for p in movable:
        dst = assets_dir / p.name
        if dst.exists():          # 幂等:就地重跑时覆盖同名旧图,避免 Windows 下 move 报错
            dst.unlink()
        shutil.move(str(p), str(dst))
    md_out.write_text(new_text, encoding="utf-8")

    # 删除已清空的原子文件夹(若仍有残留文件则保留并记录)
    leftover = [p for p in folder.iterdir()]
    # md 原文件已被改写写到根,源 md 可删
    if md_path.exists():
        md_path.unlink()
    leftover = [p for p in folder.iterdir()]
    if not leftover:
        folder.rmdir()
    else:
        res.skipped.append(f"原子文件夹非空,保留: {folder.name} (残留 {len(leftover)} 项)")
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description="Marker 输出 → Typora 兼容结构重排")
    ap.add_argument("--dir", "-d", required=True, help="Marker 输出根目录(含若干子文件夹产物)")
    ap.add_argument("--dry-run", action="store_true", help="仅预览,不移动/改写")
    args = ap.parse_args()

    root = Path(args.dir).resolve()
    if not root.is_dir():
        print(f"错误: 目录不存在: {root}", file=sys.stderr)
        return 1

    outputs = find_marker_outputs(root)
    if not outputs:
        print(f"未发现 Marker 子文件夹产物(子文件夹内含同名 .md): {root}")
        return 0

    print(f"发现 {len(outputs)} 份待重排{'(dry-run)' if args.dry_run else ''}:")
    for folder, md in outputs:
        res = relayout_one(folder, md, root, dry_run=args.dry_run)
        flag = "[预览]" if args.dry_run else "[完成]"
        print(f"  {flag} {res.name}")
        print(f"         md  -> {res.md_out.name}")
        print(f"         图片 -> {res.assets_dir.name}/  ({res.n_images_moved} 张),改写引用 {res.n_refs_rewritten} 处")
        for s in res.skipped:
            print(f"         ⚠️ {s}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
