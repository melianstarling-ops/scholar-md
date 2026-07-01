import fitz
from scripts.pipelines.textbooks import triage as triage_mod
from scripts.pipelines.textbooks.triage import (
    BADNESS_MAX,
    _is_bad_char,
    sample_text_coverage,
    text_badness,
    triage,
)


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
    # 中文间隔号(人名分隔/章节号)是合法字符,不得判坏
    # 回归:曾经生产逻辑为了迁就 PyMuPDF round-trip 测试而误判 · 为坏字符
    assert not _is_bad_char("·")


class _FakePage:
    def __init__(self, text: str):
        self._text = text

    def get_text(self) -> str:
        return self._text


class _FakeDoc:
    """伪造 fitz.Document:直接注入采样页文本,绕过 PyMuPDF round-trip 对
    PUA/替换符不可靠的渲染(实验证实:插入的 PUA/U+FFFD/生僻字形经 get_text()
    统一回读为字面 · U+00B7,无法用于区分真坏字符与合法间隔号)。
    """

    def __init__(self, texts):
        self.pages = [_FakePage(t) for t in texts]

    @property
    def page_count(self):
        return len(self.pages)

    def __getitem__(self, i):
        return self.pages[i]

    def close(self):
        pass


def _patch_fitz_open(monkeypatch, texts):
    fake = _FakeDoc(texts)
    monkeypatch.setattr(triage_mod.fitz, "open", lambda _path: fake)


def _make_pdf(tmp_path, texts):
    # 用内置 CJK 字体(china-s)写入,确保中文字符(含合法间隔号 ·)能
    # round-trip 回原字符,而非被 base14 字体替换成占位符 ·(见实验记录)。
    doc = fitz.open()
    rect = fitz.Rect(36, 36, 560, 780)
    for t in texts:
        pg = doc.new_page()
        if t:
            pg.insert_font(fontname="china-s")
            pg.insert_textbox(rect, t, fontname="china-s", fontsize=9)
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


def test_badness_high_for_garbled(monkeypatch):
    # 高 PUA/替换符密度(真实字符,非 PyMuPDF round-trip 渲染产物) → 坏度 >0.3
    junk = " ab " * 30
    _patch_fitz_open(monkeypatch, [junk] * 3)
    assert text_badness("fake.pdf") > 0.3


def test_triage_A_for_blank(tmp_path):
    assert triage(_make_pdf(tmp_path, ["", "", ""])) == "A"


def test_triage_B_for_clean(tmp_path):
    assert triage(_make_pdf(tmp_path, ["the quick brown fox jumps " * 20] * 3)) == "B"


def test_triage_C_for_garbled(monkeypatch):
    junk = "� CaSOS " * 40
    _patch_fitz_open(monkeypatch, [junk] * 3)
    assert triage("fake.pdf") == "C"


def test_badness_low_for_legit_middot(tmp_path):
    # 含人名间隔号的正常中文文本:坏度应低,不受·误判影响
    text = "卡尔·马克思与恩格斯合著《资本论》第1·2章讨论了历史唯物主义 " * 10
    pdf = _make_pdf(tmp_path, [text] * 3)
    assert text_badness(pdf) < BADNESS_MAX


def test_triage_B_for_legit_middot(tmp_path):
    # 含人名间隔号的正常中文书应判 B(优质层),不得因·被误判为坏字符而降级 C
    text = "卡尔·马克思与恩格斯合著《资本论》第1·2章讨论了历史唯物主义 " * 20
    pdf = _make_pdf(tmp_path, [text] * 3)
    assert triage(pdf) == "B"
