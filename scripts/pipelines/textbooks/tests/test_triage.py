import fitz
from scripts.pipelines.textbooks.triage import sample_text_coverage


def _make_pdf(tmp_path, texts):
    doc = fitz.open()
    for t in texts:
        pg = doc.new_page()
        if t:
            pg.insert_text((72, 72), t)
    p = tmp_path / "x.pdf"
    doc.save(str(p))
    return str(p)


def test_coverage_zero_for_blank(tmp_path):
    pdf = _make_pdf(tmp_path, ["", "", ""])
    assert sample_text_coverage(pdf) == 0.0


def test_coverage_high_for_text(tmp_path):
    pdf = _make_pdf(tmp_path, ["hello world " * 20] * 3)
    assert sample_text_coverage(pdf) > 100
