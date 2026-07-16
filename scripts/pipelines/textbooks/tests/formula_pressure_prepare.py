from __future__ import annotations

import argparse
import hashlib
import json
from io import BytesIO
from pathlib import Path

import fitz
from PIL import Image


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", type=Path, required=True)
    parser.add_argument("--work-dir", type=Path, required=True)
    parser.add_argument("--pdf", type=Path, required=True)
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--pad", type=int, default=10)
    args = parser.parse_args()

    rows = [json.loads(line) for line in args.candidates.read_text(encoding="utf-8").splitlines()
            if line.strip()]
    if len(rows) != 39:
        raise SystemExit(f"expected 39 candidates, got {len(rows)}")
    ids = [row["candidate_id"] for row in rows]
    if len(set(ids)) != len(ids):
        raise SystemExit("duplicate candidate_id")
    if args.run_dir.exists():
        raise SystemExit(f"run directory already exists: {args.run_dir}")
    images_dir = args.run_dir / "images"
    images_dir.mkdir(parents=True)

    doc = fitz.open(args.pdf)
    frozen: list[dict] = []
    try:
        for row in rows:
            page = int(row["page"])
            block_id = int(row["block_id"])
            res_path = args.work_dir / f"page_{page:04d}_res.json"
            res = json.loads(res_path.read_text(encoding="utf-8"))
            res_width = float(res["width"])
            pix = doc[page - 1].get_pixmap(dpi=args.dpi, alpha=False)
            with Image.open(BytesIO(pix.tobytes("png"))) as page_image:
                page_image.load()
                scale = page_image.width / res_width
                x0, y0, x1, y1 = [float(value) for value in row["bbox"]]
                box = (
                    max(0, int(x0 * scale) - args.pad),
                    max(0, int(y0 * scale) - args.pad),
                    min(page_image.width, int(x1 * scale) + args.pad),
                    min(page_image.height, int(y1 * scale) + args.pad),
                )
                crop = page_image.crop(box)
                output = images_dir / f"{row['candidate_id']}.png"
                crop.save(output)
                width, height = crop.size
            digest = hashlib.sha256(output.read_bytes()).hexdigest()
            frozen.append({
                **row,
                "image_path": str(output.resolve()),
                "image_sha256": digest,
                "image_width": width,
                "image_height": height,
                "crop_box_300dpi": list(box),
            })
    finally:
        doc.close()

    manifest = {
        "run_id": args.run_dir.name,
        "candidate_count": len(frozen),
        "source_candidate_file": str(args.candidates.resolve()),
        "source_pdf": str(args.pdf.resolve()),
        "crop_dpi": args.dpi,
        "crop_pad": args.pad,
        "candidates": frozen,
    }
    (args.run_dir / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(json.dumps({
        "run_dir": str(args.run_dir.resolve()),
        "candidate_count": len(frozen),
        "unique_hashes": len({item["image_sha256"] for item in frozen}),
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
