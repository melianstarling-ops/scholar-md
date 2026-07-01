import fitz
from scripts.pipelines.textbooks.triage import _is_bad_char, sample_text_coverage, text_badness, triage


def test_is_bad_char_detects_pua_and_replacement():
    # 私用区(PUA)字符 → 坏
    assert _is_bad_char("")        # BMP PUA 起点
    assert _is_bad_char("")        # BMP PUA 终点
    assert _is_bad_char("\U000F0000")    # Plane-15 PUA
    assert _is_bad_char("�")             # 替换符
    # 普通字符 → 不坏
    assert not _is_bad_char("A")
    assert not _is_bad_char("中")
    assert not _is_bad_char("5")


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


def test_badness_low_for_clean(tmp_path):
    pdf = _make_pdf(tmp_path, ["the quick brown fox jumps over the lazy dog " * 10] * 3)
    assert text_badness(pdf) < 0.2


def test_badness_high_for_garbled(tmp_path):
    # 高替换符密度(U+FFFD) → PyMuPDF 提取为·,坏度 >0.3
    junk = "████ ab " * 30
    pdf = _make_pdf(tmp_path, [junk] * 3)
    assert text_badness(pdf) > 0.3


def test_triage_A_for_blank(tmp_path):
    assert triage(_make_pdf(tmp_path, ["", "", ""])) == "A"


def test_triage_B_for_clean(tmp_path):
    assert triage(_make_pdf(tmp_path, ["the quick brown fox jumps " * 20] * 3)) == "B"


def test_triage_C_for_garbled(tmp_path):
    junk = "�� CaSOS " * 40
    assert triage(_make_pdf(tmp_path, [junk] * 3)) == "C"
