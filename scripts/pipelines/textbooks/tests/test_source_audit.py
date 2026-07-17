import json

import fitz

from scripts.pipelines.textbooks.source_audit import (
    PageGeometry,
    SourceWord,
    _is_bad_control,
    _is_pua,
    _is_unassigned,
    _valid_bbox,
    assign_source_words,
    assign_word_to_block,
    extract_numeric_tokens,
    extract_source_page,
    ngram_repetition_score,
    normalize_bbox,
    normalize_prose_for_compare,
    normalize_prose_for_content,
    overlap_ratio,
    page_geometry,
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


# ===========================================================================
# Task 4:PDF/OCR bbox 对齐(几何归一化 + word→block 归属)
# ---------------------------------------------------------------------------
# 几何测试用手工构造 words/blocks dict + 显式 PageGeometry(确定性、可读);
# page_geometry() 本身用 fitz 内存 PDF(不 mock fitz)。
# ===========================================================================


def _sw(text, bbox, block_no=0, line_no=0, word_no=0):
    return SourceWord(
        text=text, bbox=bbox, block_no=block_no, line_no=line_no, word_no=word_no
    )


def _block(label, bbox, content=""):
    return {"block_label": label, "block_content": content, "block_bbox": bbox}


def _geom(pdf_w, pdf_h, ocr_w, ocr_h, rotation=0, unscorable=False):
    return PageGeometry(
        pdf_width=pdf_w,
        pdf_height=pdf_h,
        ocr_width=ocr_w,
        ocr_height=ocr_h,
        rotation=rotation,
        unscorable=unscorable,
    )


# ---- normalize_bbox:绝对坐标 → [0,1] 分数,尺寸无关 -----------------------


def test_normalize_bbox_scales_to_unit_fractions():
    assert normalize_bbox((300, 400, 600, 800), 600, 800) == (0.5, 0.5, 1.0, 1.0)


def test_normalize_bbox_rejects_nonpositive_dims():
    import pytest

    with pytest.raises(ValueError):
        normalize_bbox((0, 0, 1, 1), 0, 100)
    with pytest.raises(ValueError):
        normalize_bbox((0, 0, 1, 1), 100, 0)


# ---- overlap_ratio:交叠占 word 面积的比例 ---------------------------------


def test_overlap_ratio_is_fraction_of_word_area():
    # word 面积 0.01,交叠一半 → 0.5
    assert overlap_ratio((0.0, 0.0, 0.1, 0.1), (0.0, 0.0, 0.05, 0.1)) == 0.5


def test_overlap_ratio_zero_when_disjoint():
    assert overlap_ratio((0.0, 0.0, 0.1, 0.1), (0.5, 0.5, 0.6, 0.6)) == 0.0


# ---- 失败测试清单 1:不同 PDF point / OCR pixel 尺寸的缩放 ------------------


def test_assign_scales_pdf_points_to_ocr_pixels():
    # PDF 600x800 pt,word 中心 (300,400) → 分数 (0.5,0.5)
    # OCR 1200x1600 px(2x),block 覆盖页面中心区域
    word = _sw("mid", (290, 390, 310, 410))
    blocks = [
        _block("text", [0, 0, 100, 100]),          # 左上角,不含中心
        _block("text", [300, 400, 900, 1200]),     # 覆盖中心 (0.5,0.5)
    ]
    geom = _geom(600, 800, 1200, 1600)
    res = assign_source_words([word], blocks, geom, overlap_threshold=0.5)
    assert res["assignments"].get(1) == [word]
    assert 0 not in res["assignments"]
    assert res["unassigned"] == []


# ---- 失败测试清单 2:中心点命中 -------------------------------------------


def test_center_point_hit_assigns():
    word = _sw("c", (450, 450, 550, 550))          # 中心 (0.5,0.5)
    blocks = [_block("text", [400, 400, 600, 600])]
    geom = _geom(1000, 1000, 1000, 1000)
    res = assign_source_words([word], blocks, geom, overlap_threshold=0.5)
    assert res["assignments"].get(0) == [word]


def test_assign_word_to_block_center_hit_primitive():
    # 纯几何原语:归一化空间内,中心命中优先于交叠
    word = (0.45, 0.45, 0.55, 0.55)          # 中心 (0.5,0.5)
    blocks = [
        (0.0, 0.0, 0.3, 0.3),                # 无关
        (0.4, 0.4, 0.6, 0.6),                # 含中心
    ]
    assert assign_word_to_block(word, blocks, overlap_threshold=0.5) == 1


# ---- 失败测试清单 3:多 block 命中选择更具体的小块 ------------------------


def test_nested_blocks_pick_smaller_more_specific():
    word = _sw("x", (490, 490, 510, 510))          # 中心 (0.5,0.5)
    blocks = [
        _block("text", [0, 0, 1000, 1000]),        # 整页大块,含中心
        _block("display_formula", [300, 300, 700, 700]),  # 小块,含中心
    ]
    geom = _geom(1000, 1000, 1000, 1000)
    res = assign_source_words([word], blocks, geom, overlap_threshold=0.5)
    assert res["assignments"].get(1) == [word]
    assert 0 not in res["assignments"]


# ---- 失败测试清单 4:弱交叠保持 unassigned --------------------------------


def test_weak_overlap_stays_unassigned():
    # 中心不落任何块,仅 20% 面积交叠 < 阈值 0.5 → unassigned
    word = _sw("w", (100, 100, 200, 200))          # 分数 (.1,.1,.2,.2) 中心 (.15,.15)
    blocks = [_block("text", [180, 100, 400, 400])]  # 分数 (.18,.1,.4,.4)
    geom = _geom(1000, 1000, 1000, 1000)
    res = assign_source_words([word], blocks, geom, overlap_threshold=0.5)
    assert res["assignments"] == {}
    assert res["unassigned"] == [word]


def test_overlap_fallback_branch_when_center_outside_all_blocks():
    # 归一化空间:word 中心 (0.15,0.15) 落在块外(块 x0=0.16 > 0.15),
    # 但 word 面积 40% 被覆盖——这是 assign_word_to_block 的"跨块归属"关键分支。
    import pytest

    word = (0.1, 0.1, 0.2, 0.2)                    # 中心 (0.15,0.15)
    blocks = [(0.16, 0.1, 0.4, 0.4)]              # 中心在块外;overlap_ratio=0.4
    assert overlap_ratio(word, blocks[0]) == pytest.approx(0.4)
    # 阈值 0.3 ≤ 0.4 → 走 overlap fallback 归属;阈值 0.5 > 0.4 → 弱交叠不猜 → None。
    # 后者返回 None 反证中心确实在块外(若中心命中则两阈值都会返回 0)。
    assert assign_word_to_block(word, blocks, overlap_threshold=0.3) == 0
    assert assign_word_to_block(word, blocks, overlap_threshold=0.5) is None


def test_overlap_fallback_assigns_end_to_end():
    # 端到端:word 中心 (0.15,0.15) 落在块外,40% 覆盖率 >= 注入阈值 0.3 → 归属。
    word = _sw("s", (100, 100, 200, 200))          # 分数 (.1,.1,.2,.2) 中心 (.15,.15)
    blocks = [_block("text", [160, 100, 400, 400])]  # 分数 (.16,.1,.4,.4),不含中心
    geom = _geom(1000, 1000, 1000, 1000)
    res = assign_source_words([word], blocks, geom, overlap_threshold=0.3)
    assert res["assignments"].get(0) == [word]


# ---- 失败测试清单 5:缺 bbox、畸形 bbox、缺 width/height -------------------


def test_valid_bbox_rejects_malformed():
    assert _valid_bbox((0, 0, 10, 10)) is True
    assert _valid_bbox((10, 0, 5, 10)) is False       # x1 < x0
    assert _valid_bbox((0, 10, 10, 5)) is False       # y1 < y0
    assert _valid_bbox((-1, 0, 10, 10)) is False      # 负值
    assert _valid_bbox(("a", 0, 10, 10)) is False     # 非数值
    assert _valid_bbox(None) is False                 # 缺 bbox
    assert _valid_bbox((0, 0, 10)) is False           # 元数不足
    assert _valid_bbox((0, 0, 0, 10)) is False        # 零宽退化
    assert _valid_bbox((True, 0, 10, 10)) is False    # bool 不算数值坐标


def test_malformed_and_missing_word_bboxes_go_unassigned():
    good = _sw("good", (490, 490, 510, 510))
    inverted = _sw("inv", (510, 490, 490, 510))       # x1<x0
    negative = _sw("neg", (-5, 490, 510, 510))
    nonnumeric = _sw("nan", ("x", 490, 510, 510))
    missing = _sw("none", None)
    blocks = [_block("text", [400, 400, 600, 600])]
    geom = _geom(1000, 1000, 1000, 1000)
    res = assign_source_words(
        [good, inverted, negative, nonnumeric, missing], blocks, geom,
        overlap_threshold=0.5,
    )
    assert res["assignments"].get(0) == [good]
    assert set(res["unassigned"]) == {inverted, negative, nonnumeric, missing}


def test_missing_ocr_dims_make_geometry_unscorable():
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    # OCR JSON 顶层缺 width/height
    geom = page_geometry(page, {"parsing_res_list": []})
    assert geom.unscorable is True
    doc.close()


def test_malformed_block_bbox_is_skipped_not_crashed():
    word = _sw("x", (490, 490, 510, 510))
    blocks = [
        _block("text", [510, 400, 400, 600]),      # 畸形 block(x1<x0),跳过
        _block("text", [400, 400, 600, 600]),      # 正常块含中心
    ]
    geom = _geom(1000, 1000, 1000, 1000)
    res = assign_source_words([word], blocks, geom, overlap_threshold=0.5)
    assert res["assignments"].get(1) == [word]
    assert 0 not in res["assignments"]


# ---- 失败测试清单 6:90/180/270 度旋转页 → geometry_unscorable -------------


def test_rotated_pages_are_unscorable():
    for angle in (90, 180, 270):
        doc = fitz.open()
        page = doc.new_page(width=600, height=800)
        page.set_rotation(angle)
        geom = page_geometry(page, {"width": 1200, "height": 1600})
        assert geom.rotation == angle
        assert geom.unscorable is True, f"rotation {angle} 必须 unscorable"
        doc.close()


def test_unrotated_normal_page_is_scorable():
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    geom = page_geometry(page, {"width": 1200, "height": 1600})
    assert geom.rotation == 0
    assert geom.unscorable is False
    assert geom.pdf_width == 600
    assert geom.pdf_height == 800
    assert geom.ocr_width == 1200
    assert geom.ocr_height == 1600
    doc.close()


def test_cropbox_anomaly_is_unscorable():
    doc = fitz.open()
    page = doc.new_page(width=600, height=800)
    page.set_cropbox(fitz.Rect(50, 50, 400, 600))   # cropbox != mediabox
    geom = page_geometry(page, {"width": 1200, "height": 1600})
    assert geom.unscorable is True
    doc.close()


# ---- 失败测试清单 7:table/formula bbox 中的 words 不进 prose bucket --------


def test_formula_block_words_keep_label_not_prose():
    word = _sw("E=mc^2", (490, 490, 510, 510))       # 中心落在公式块
    blocks = [
        _block("text", [0, 0, 400, 400]),            # 正文块(prose),不含中心
        _block("display_formula", [400, 400, 600, 600]),  # 公式块,含中心
    ]
    geom = _geom(1000, 1000, 1000, 1000)
    res = assign_source_words([word], blocks, geom, overlap_threshold=0.5)
    # 归属到公式块(index 1),不进正文块(index 0)
    assert res["assignments"].get(1) == [word]
    assert 0 not in res["assignments"]
    # label 信息保留:下游据此得知这是公式块而非 prose
    assert res["block_labels"][1] == "display_formula"
    assert res["block_labels"][0] == "text"


# ---- 失败测试清单 8:洗牌不变性(blocks 顺序变化归属结果不变) --------------


def test_block_shuffle_invariance():
    words = [
        _sw("top", (490, 90, 510, 110)),       # 中心 (.5,.1) → 上块
        _sw("mid", (490, 490, 510, 510)),      # 中心 (.5,.5) → 中块
        _sw("bot", (490, 890, 510, 910)),      # 中心 (.5,.9) → 下块
    ]
    top = _block("text", [0, 0, 1000, 300], content="TOP")
    mid = _block("display_formula", [0, 300, 1000, 700], content="MID")
    bot = _block("table", [0, 700, 1000, 1000], content="BOT")
    geom = _geom(1000, 1000, 1000, 1000)

    def by_content(res, blocks):
        out = {}
        for bi, ws in res["assignments"].items():
            out[blocks[bi]["block_content"]] = sorted(w.text for w in ws)
        return out

    order_a = [top, mid, bot]
    order_b = [bot, top, mid]
    res_a = assign_source_words(words, order_a, geom, overlap_threshold=0.5)
    res_b = assign_source_words(words, order_b, geom, overlap_threshold=0.5)
    assert by_content(res_a, order_a) == by_content(res_b, order_b) == {
        "TOP": ["top"],
        "MID": ["mid"],
        "BOT": ["bot"],
    }
    # 每个 word 恰好归属一次(无重复归属)
    for res in (res_a, res_b):
        assigned = [w for ws in res["assignments"].values() for w in ws]
        assert len(assigned) == 3
        assert len(assigned) + len(res["unassigned"]) == len(words)


# ---- 失败测试清单 9:geometry_unscorable 页向下游传播"禁止采信"信号 --------


def test_unscorable_geometry_propagates_adoption_forbidden():
    words = [_sw("a", (490, 490, 510, 510)), _sw("b", (10, 10, 20, 20))]
    blocks = [_block("text", [0, 0, 1000, 1000])]
    geom = _geom(1000, 1000, 1000, 1000, rotation=90, unscorable=True)
    res = assign_source_words(words, blocks, geom, overlap_threshold=0.5)
    assert res["geometry_unscorable"] is True
    assert res["adoption_forbidden"] is True
    # 不猜:unscorable 页不产出任何归属,全部 words 明确落 unassigned
    assert res["assignments"] == {}
    assert set(res["unassigned"]) == set(words)
