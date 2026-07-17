import json

import fitz

from scripts.pipelines.textbooks.source_audit import (
    SourceWord,
    _is_bad_control,
    _is_pua,
    _is_unassigned,
    extract_numeric_tokens,
    extract_source_page,
    ngram_repetition_score,
    normalize_prose_for_compare,
    normalize_prose_for_content,
    source_health,
)


# ---------------------------------------------------------------------------
# 低层字符分类:PUA / U+FFFD / 非空白控制字符 / 未分配码点
# ---------------------------------------------------------------------------


def test_is_pua_detects_pua_ranges():
    assert _is_pua("")
    assert _is_pua("")
    assert _is_pua("\U000f0000")
    assert not _is_pua("A")
    assert not _is_pua("中")


def test_is_bad_control_excludes_whitespace_controls():
    # 换行、tab、回车是空白控制字符,不得误判成坏字符
    assert not _is_bad_control("\n")
    assert not _is_bad_control("\t")
    assert not _is_bad_control("\r")
    assert not _is_bad_control("A")
    # BEL 等非空白控制字符才算坏
    assert _is_bad_control("\x07")
    assert _is_bad_control("\x00")


def test_is_unassigned_detects_reserved_codepoint():
    assert _is_unassigned("͸")  # Unicode 永久保留未分配区
    assert not _is_unassigned("A")
    assert not _is_unassigned("�")


# ---------------------------------------------------------------------------
# source_health:PUA/U+FFFD/控制字符/未分配码点分别计数(synthetic dict 输入)
# ---------------------------------------------------------------------------


def _page(words, text="", fonts=None):
    return {"words": words, "text": text, "fonts": fonts or []}


def test_source_health_counts_pua_fffd_control_separately():
    words = [
        SourceWord(text="clean", bbox=(0, 0, 1, 1), block_no=0, line_no=0, word_no=0),
        SourceWord(text="bad", bbox=(0, 0, 1, 1), block_no=0, line_no=0, word_no=1),
        SourceWord(text="�", bbox=(0, 0, 1, 1), block_no=0, line_no=0, word_no=2),
        SourceWord(text="a\x07b", bbox=(0, 0, 1, 1), block_no=0, line_no=0, word_no=3),
        SourceWord(text="͸", bbox=(0, 0, 1, 1), block_no=0, line_no=0, word_no=4),
    ]
    h = source_health(_page(words))
    assert h["pua_count"] == 1
    assert h["fffd_count"] == 1
    assert h["control_char_count"] == 1
    assert h["unassigned_codepoint_count"] == 1


def test_source_health_newline_tab_not_counted_as_control():
    words = [
        SourceWord(
            text="line1\nline2\ttab",
            bbox=(0, 0, 1, 1),
            block_no=0,
            line_no=0,
            word_no=0,
        ),
    ]
    h = source_health(_page(words))
    assert h["control_char_count"] == 0
    # 非空白字符数只数字母,不数 \n \t 本身
    assert h["non_space_char_count"] == len("line1line2tab")


def test_source_health_ligature_expand_but_raw_count_kept():
    words = [SourceWord(text="ﬁnd", bbox=(0, 0, 1, 1), block_no=0, line_no=0, word_no=0)]
    h = source_health(_page(words))
    assert h["ligature_count"] == 1
    # raw 计数保留:连字算 1 个字符,不是展开后的 2 个("fi")
    assert h["non_space_char_count"] == 3
    assert normalize_prose_for_content("ﬁnd") == "find"


def test_source_health_blank_words_gives_empty_stats():
    h = source_health(_page([]))
    assert h["non_space_char_count"] == 0
    assert h["word_count"] == 0
    assert h["line_count"] == 0
    assert h["pua_count"] == 0
    assert h["fffd_count"] == 0
    assert h["control_char_count"] == 0
    assert h["unassigned_codepoint_count"] == 0
    assert h["repeated_line_candidates"] == []
    assert h["is_blank"] is True
    assert h["is_low_text"] is False


def test_source_health_low_text_flag():
    words = [SourceWord(text="ok", bbox=(0, 0, 1, 1), block_no=0, line_no=0, word_no=0)]
    h = source_health(_page(words))
    assert h["is_blank"] is False
    assert h["is_low_text"] is True


def test_single_char_fragment_rate_and_line_end_hyphen_rate():
    words = [
        SourceWord(text="a", bbox=(0, 0, 1, 1), block_no=0, line_no=0, word_no=0),
        SourceWord(text="continu-", bbox=(0, 0, 1, 1), block_no=0, line_no=0, word_no=1),
        SourceWord(text="ing", bbox=(0, 0, 1, 1), block_no=0, line_no=1, word_no=0),
    ]
    h = source_health(_page(words))
    assert h["single_char_fragment_rate"] == 1 / 3
    assert h["line_end_hyphen_rate"] == 0.5


def test_repeated_line_candidates_detected():
    words = [
        SourceWord(text="dup", bbox=(0, 0, 1, 1), block_no=0, line_no=0, word_no=0),
        SourceWord(text="line", bbox=(0, 0, 1, 1), block_no=0, line_no=0, word_no=1),
        SourceWord(text="dup", bbox=(0, 0, 1, 1), block_no=1, line_no=0, word_no=0),
        SourceWord(text="line", bbox=(0, 0, 1, 1), block_no=1, line_no=0, word_no=1),
        SourceWord(text="unique", bbox=(0, 0, 1, 1), block_no=2, line_no=0, word_no=0),
    ]
    h = source_health(_page(words))
    assert h["repeated_line_candidates"] == ["dup line"]


def test_suspected_missing_tounicode_cid_from_fonts():
    fonts_bad = [{"type": "Type0", "encoding": "Identity-H", "has_tounicode": False}]
    fonts_ok = [{"type": "Type0", "encoding": "Identity-H", "has_tounicode": True}]
    fonts_unknown = [{"type": "Type0", "encoding": "Identity-H", "has_tounicode": None}]
    assert source_health(_page([], fonts=fonts_bad))["suspected_missing_tounicode_cid"] is True
    assert source_health(_page([], fonts=fonts_ok))["suspected_missing_tounicode_cid"] is False
    assert source_health(_page([], fonts=fonts_unknown))["suspected_missing_tounicode_cid"] is False


def test_repeated_line_candidates_preserve_unicode_utf8():
    words = [
        SourceWord(text="北京", bbox=(0, 0, 1, 1), block_no=0, line_no=0, word_no=0),
        SourceWord(text="北京", bbox=(0, 0, 1, 1), block_no=1, line_no=0, word_no=0),
    ]
    h = source_health(_page(words))
    assert "北京" in h["repeated_line_candidates"]
    payload = json.dumps(h, ensure_ascii=False)
    roundtripped = json.loads(payload.encode("utf-8").decode("utf-8"))
    assert roundtripped["repeated_line_candidates"] == h["repeated_line_candidates"]


# ---------------------------------------------------------------------------
# extract_source_page:真实 fitz.Page(内存新建 PDF),不 mock fitz
# ---------------------------------------------------------------------------


def test_extract_source_page_blank_page_gives_empty_stats():
    doc = fitz.open()
    page = doc.new_page()
    source_page = extract_source_page(page)
    assert source_page["words"] == []
    assert source_page["text"] == ""
    assert source_page["fonts"] == []
    h = source_health(source_page)
    assert h["is_blank"] is True
    assert h["word_count"] == 0
    assert h["line_count"] == 0
    doc.close()


def test_extract_source_page_words_have_expected_fields():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "alpha beta gamma", fontname="helv", fontsize=12)
    source_page = extract_source_page(page)
    words = source_page["words"]
    assert all(isinstance(w, SourceWord) for w in words)
    assert [w.text for w in words] == ["alpha", "beta", "gamma"]
    assert all(w.block_no == 0 and w.line_no == 0 for w in words)
    assert [w.word_no for w in words] == [0, 1, 2]
    for w in words:
        assert len(w.bbox) == 4
        assert all(isinstance(c, float) for c in w.bbox)
    doc.close()


def test_extract_source_page_multiple_blocks():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "top block", fontname="helv", fontsize=12)
    page.insert_text((72, 700), "bottom block", fontname="helv", fontsize=12)
    source_page = extract_source_page(page)
    block_nos = {w.block_no for w in source_page["words"]}
    assert block_nos == {0, 1}
    doc.close()


def test_extract_source_page_font_info_from_real_pdf():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_font(fontname="china-s")
    page.insert_text((72, 72), "hello", fontname="china-s", fontsize=12)
    source_page = extract_source_page(page)
    fonts = source_page["fonts"]
    assert len(fonts) == 1
    f = fonts[0]
    assert f["type"] == "Type0"
    assert f["encoding"] == "UniGB-UTF16-H"
    assert isinstance(f["has_tounicode"], bool)
    doc.close()


def test_source_health_end_to_end_repeated_line_real_pdf():
    doc = fitz.open()
    page = doc.new_page()
    page.insert_text((72, 72), "same line", fontname="helv", fontsize=12)
    page.insert_text((72, 90), "same line", fontname="helv", fontsize=12)
    source_page = extract_source_page(page)
    h = source_health(source_page)
    assert "same line" in h["repeated_line_candidates"]
    doc.close()


# ---------------------------------------------------------------------------
# 归一化:NFC 内容视图 vs NFKC 比较视图,互不污染
# ---------------------------------------------------------------------------


def test_hyphen_vs_minus_prose_compare_vs_content():
    variants = ["a-b", "a−b", "a–b"]
    compare_outputs = {normalize_prose_for_compare(v) for v in variants}
    assert compare_outputs == {"a-b"}
    # 内容视图保守——不折叠连字符类字符的差异
    content_outputs = [normalize_prose_for_content(v) for v in variants]
    assert content_outputs == variants


def test_content_and_compare_normalization_diverge_and_dont_pollute():
    raw = "10² units"
    content_view = normalize_prose_for_content(raw)
    compare_view = normalize_prose_for_compare(raw)
    assert content_view == "10² units"
    assert compare_view == "102 units"
    assert content_view != compare_view
    # 两次调用互不干扰(纯函数,无隐藏共享状态)
    assert normalize_prose_for_content(raw) == content_view
    assert normalize_prose_for_compare(raw) == compare_view


def test_normalize_functions_handle_unicode_and_ligatures_together():
    assert normalize_prose_for_content("北京ﬁ") == "北京fi"
    assert normalize_prose_for_compare("北京ﬁ") == "北京fi"


# ---------------------------------------------------------------------------
# extract_numeric_tokens
# ---------------------------------------------------------------------------


def test_numeric_tokens_distinguish_sign_from_hyphenated_word():
    tokens = extract_numeric_tokens("value is −5 not well-known-5")
    assert tokens == ["-5", "5"]


def test_superscript_exponent_extracted_before_nfkc_folds_it():
    assert extract_numeric_tokens("10²") == ["10^2"]
    assert extract_numeric_tokens("102") == ["102"]
    assert extract_numeric_tokens("10²") != extract_numeric_tokens("102")
    # 比较视图会把两者折叠成同一个字符串——数值语义因此不能依赖比较视图
    assert normalize_prose_for_compare("10²") == normalize_prose_for_compare("102") == "102"


def test_negative_exponent_superscript():
    assert extract_numeric_tokens("10⁻³") == ["10^-3"]


def test_scientific_notation_percent_and_unit_tokens():
    text = (
        "efficiency is 45% at 5 W/kg and 2.4GHz, "
        "or 6.022×10²³ particles, or 6.02e23 alt"
    )
    tokens = extract_numeric_tokens(text)
    assert "45%" in tokens
    assert "W/kg" in tokens
    assert "GHz" in tokens
    assert "6.022×10^23" in tokens
    assert "6.02e23" in tokens
    # 结构启发式:纯小写普通词不应被误判成单位 token
    assert "alt" not in tokens


def test_numeric_tokens_range_symbol_preserved():
    tokens = extract_numeric_tokens("pages 10-20 were revised")
    assert "10-20" in tokens
    # 范围符号不得被误判成负号,产出的不应含裸的 "-20"
    assert "-20" not in tokens


def test_numeric_tokens_empty_text():
    assert extract_numeric_tokens("") == []


# ---------------------------------------------------------------------------
# ngram_repetition_score
# ---------------------------------------------------------------------------


def test_ngram_repetition_score_empty_text_is_zero():
    assert ngram_repetition_score("") == 0.0


def test_ngram_repetition_score_short_text_is_zero():
    assert ngram_repetition_score("short", n=8) == 0.0


def test_ngram_repetition_score_high_for_loops_low_for_normal_prose():
    looped = "the cat sat on the mat. " * 60
    normal = (
        "Photosynthesis converts light energy into chemical energy stored in "
        "glucose molecules, releasing oxygen as a byproduct through a series "
        "of reactions occurring within chloroplasts of plant cells during the "
        "daylight hours when sunlight is available for capture by pigments."
    )
    loop_score = ngram_repetition_score(looped)
    normal_score = ngram_repetition_score(normal)
    assert loop_score > 0.6
    assert normal_score < 0.3
    assert loop_score > normal_score
