import os
import fitz
from scripts.pipelines.textbooks.preprocess import pdf_to_pngs


def test_pdf_to_pngs(tmp_path):
    doc = fitz.open()
    doc.new_page(); doc.new_page()
    pdf = tmp_path / "two.pdf"
    doc.save(str(pdf))
    out = tmp_path / "png"
    pngs = pdf_to_pngs(str(pdf), str(out), dpi=100)
    assert len(pngs) == 2
    assert all(os.path.exists(p) and p.endswith(".png") for p in pngs)
    assert pngs == sorted(pngs)      # 有序
