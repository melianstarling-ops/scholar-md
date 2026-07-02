import os
import fitz
from scripts.pipelines.textbooks.preprocess import pdf_to_pngs, pdf_page_to_png


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


def test_pdf_page_to_png_single(tmp_path):
    doc = fitz.open()
    doc.new_page(); doc.new_page(); doc.new_page()
    pdf = tmp_path / "three.pdf"
    doc.save(str(pdf))
    out = tmp_path / "work"
    p = pdf_page_to_png(str(pdf), 2, str(out), dpi=100)
    assert p.endswith("page_0002.png")
    assert os.path.exists(p)
    # 只产该页,不产其它
    produced = [f for f in os.listdir(str(out)) if f.endswith(".png")]
    assert produced == ["page_0002.png"]


def test_pdf_page_to_png_naming_4digits(tmp_path):
    doc = fitz.open(); doc.new_page()
    pdf = tmp_path / "one.pdf"
    doc.save(str(pdf))
    p = pdf_page_to_png(str(pdf), 1, str(tmp_path / "w"))
    assert os.path.basename(p) == "page_0001.png"
