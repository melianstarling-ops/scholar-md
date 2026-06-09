#!/usr/bin/env python3
"""
convert.py — general 管线的 Marker 转换封装(born-digital 优先文本层)。

对 born-digital(自带文本层)PDF,Marker 默认从文本层取字 + ML 版面识别,
不触发 OCR —— 文字零误差,版面/标题/列表/图片/锚点由模型还原。本封装固定
一组高质量参数,输出 Marker 默认的"每份一个子文件夹"结构,交由 typora_layout
再重排为 Typora 兼容布局。

调用方式特意走 `python -c convert_single_cli`,绕过 console_scripts 的 .exe
wrapper(其内嵌的解释器绝对路径在工作区被移动后会失效)。

用法:
    python convert.py --input "x.pdf" --output-dir "<out>"
    python convert.py --input "x.pdf" --output-dir "<out>" --force-ocr   # 仅扫描版降级
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

_CLI_BOOTSTRAP = "from marker.scripts.convert_single import convert_single_cli; convert_single_cli()"


def convert_pdf(pdf_path, output_dir, dpi: int = 300, force_ocr: bool = False,
                python_exe: str | None = None) -> dict:
    """转换单份 PDF。返回结果字典,output_subdir 为 Marker 生成的子文件夹。"""
    pdf_path = Path(pdf_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    cmd = [
        python_exe or sys.executable, "-c", _CLI_BOOTSTRAP,
        str(pdf_path),
        "--output_dir", str(output_dir),
        "--output_format", "markdown",
        "--highres_image_dpi", str(dpi),
    ]
    if force_ocr:
        cmd.append("--force_ocr")

    # 不捕获输出:让 Marker 的进度条直接透传到终端
    proc = subprocess.run(cmd, text=True, encoding="utf-8", errors="replace")
    return {
        "pdf": str(pdf_path),
        "returncode": proc.returncode,
        "success": proc.returncode == 0,
        "output_subdir": str(output_dir / pdf_path.stem),
    }


def main() -> int:
    ap = argparse.ArgumentParser(description="general 管线 Marker 转换封装(born-digital)")
    ap.add_argument("--input", "-i", required=True, help="输入 PDF")
    ap.add_argument("--output-dir", "-o", required=True, help="输出目录")
    ap.add_argument("--dpi", type=int, default=300, help="提取图片分辨率(默认 300)")
    ap.add_argument("--force-ocr", action="store_true",
                    help="强制整篇 OCR;仅扫描版需要,born-digital 切勿开启")
    args = ap.parse_args()

    pdf = Path(args.input)
    if not pdf.is_file() or pdf.suffix.lower() != ".pdf":
        print(f"错误: 不是有效 PDF: {pdf}", file=sys.stderr)
        return 1

    r = convert_pdf(pdf, args.output_dir, dpi=args.dpi, force_ocr=args.force_ocr)
    if r["success"]:
        print(f"✓ 转换完成 -> {r['output_subdir']}")
        return 0
    print(f"✗ 转换失败 rc={r['returncode']}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
