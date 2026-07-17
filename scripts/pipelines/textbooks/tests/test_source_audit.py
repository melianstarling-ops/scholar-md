import json
import os

import fitz

from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks.paths import resolve_layout
from scripts.pipelines.textbooks.prose_adoption import AdoptionDecision
from scripts.pipelines.textbooks.source_audit import (
    ROUTE_B_V1_UNCALIBRATED_THRESHOLDS,
    THRESHOLD_PROFILE_UNCALIBRATED,
    AuditThresholds,
    PageGeometry,
    SourceWord,
    _is_bad_control,
    _is_pua,
    _is_unassigned,
    _valid_bbox,
    assign_source_words,
    assign_word_to_block,
    audit_document,
    audit_prose,
    extract_numeric_tokens,
    extract_source_page,
    main as source_audit_main,
    ngram_repetition_score,
    normalize_bbox,
    normalize_prose_for_compare,
    normalize_prose_for_content,
    overlap_ratio,
    page_geometry,
    source_health,
    write_audit_report,
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



# ===========================================================================
# Task 6:正文双向对账审计 audit_prose(计划 §6.4)
# ---------------------------------------------------------------------------
# SourceWord 的 block_no/line_no/word_no 保持 Task 3 原义(fitz 原生段落/行/
# 词分组),不重解释。块内归属源 words 由显式构造的 assignment(模拟
# assign_source_words 的返回结构)提供——assignment["assignments"][block_index]
# (与 AdoptionDecision.block_id 同一索引空间)、assignment["unassigned"]、
# assignment["geometry_unscorable"]。纯 synthetic 构造,不接触 fitz.Page。
# ===========================================================================


def _audit_page(words, text=""):
    return {"page_number": 0, "words": words, "text": text, "fonts": []}


def _assignment(assignments, *, unassigned=None, geometry_unscorable=False):
    return {
        "geometry_unscorable": geometry_unscorable,
        "adoption_forbidden": geometry_unscorable,
        "assignments": assignments,
        "block_labels": {},
        "unassigned": unassigned or [],
    }


def _decision(block_id, content_source, reasons=(), block_ned=None, adopted_text=None):
    return AdoptionDecision(
        block_id=block_id,
        content_source=content_source,
        reasons=list(reasons),
        block_ned=block_ned,
        adopted_text=adopted_text,
    )


def _audit_thresholds(**overrides):
    base = dict(
        minimum_reliable_chars=10,
        maximum_bad_char_ratio=0.1,
        maximum_block_ned=0.3,
        minimum_char_recall=0.8,
        minimum_token_recall=0.8,
        minimum_numeric_token_recall=0.8,
        maximum_addition_ratio=0.2,
        maximum_repetition_score=0.3,
        minimum_single_column_sequence_ratio=0.8,
    )
    base.update(overrides)
    return AuditThresholds(**base)


def _issue_codes(result):
    return {iss["code"] for iss in result["issues"]}


# ---- 1. 回退块:OCR 漏整句 → missing_prose ---------------------------------


def test_audit_prose_missing_sentence_flags_missing_prose():
    words = [
        _sw("Sentence", (0, 0, 1, 1), line_no=0, word_no=0),
        _sw("one", (0, 0, 1, 1), line_no=0, word_no=1),
        _sw("is", (0, 0, 1, 1), line_no=0, word_no=2),
        _sw("here", (0, 0, 1, 1), line_no=0, word_no=3),
        _sw("today", (0, 0, 1, 1), line_no=0, word_no=4),
        _sw("Sentence", (0, 0, 1, 1), line_no=1, word_no=0),
        _sw("two", (0, 0, 1, 1), line_no=1, word_no=1),
        _sw("follows", (0, 0, 1, 1), line_no=1, word_no=2),
        _sw("right", (0, 0, 1, 1), line_no=1, word_no=3),
        _sw("after", (0, 0, 1, 1), line_no=1, word_no=4),
    ]
    blocks = [_block("text", [0, 0, 100, 100], content="Sentence one is here today")]
    decisions = [_decision(0, "ocr", reasons=["adoption_disagreement"])]
    assignment = _assignment({0: words})
    thresholds = _audit_thresholds(
        minimum_reliable_chars=5,
        maximum_block_ned=0.9,
        minimum_char_recall=0.7,
        minimum_token_recall=0.7,
        maximum_addition_ratio=0.9,
    )
    result = audit_prose(_audit_page(words), blocks, decisions, assignment, thresholds)
    assert _issue_codes(result) == {"missing_prose"}
    issue = next(iss for iss in result["issues"] if iss["code"] == "missing_prose")
    assert issue["block_id"] == 0
    assert result["status"] == "SUSPECT"


# ---- 2. 回退块:OCR 多出整句 → ocr_addition,审计不改内容 -------------------


def test_audit_prose_extra_sentence_flags_ocr_addition_without_mutating_blocks():
    words = [
        _sw("Alpha", (0, 0, 1, 1), line_no=0, word_no=0),
        _sw("beta", (0, 0, 1, 1), line_no=0, word_no=1),
        _sw("gamma", (0, 0, 1, 1), line_no=0, word_no=2),
        _sw("delta", (0, 0, 1, 1), line_no=0, word_no=3),
        _sw("epsilon", (0, 0, 1, 1), line_no=0, word_no=4),
    ]
    ocr_text = (
        "Alpha beta gamma delta epsilon zeta eta theta iota kappa "
        "lambda mu nu xi omicron pi"
    )
    blocks = [_block("text", [0, 0, 100, 100], content=ocr_text)]
    decisions = [_decision(0, "ocr", reasons=["adoption_disagreement"])]
    assignment = _assignment({0: words})
    thresholds = _audit_thresholds(
        minimum_reliable_chars=5,
        maximum_block_ned=0.9,
        minimum_char_recall=0.5,
        minimum_token_recall=0.5,
        maximum_addition_ratio=0.2,
    )
    blocks_before = [dict(b) for b in blocks]
    result = audit_prose(_audit_page(words), blocks, decisions, assignment, thresholds)
    assert _issue_codes(result) == {"ocr_addition"}
    issue = next(iss for iss in result["issues"] if iss["code"] == "ocr_addition")
    assert issue["block_id"] == 0
    # 只读:审计绝不改内容,blocks 原样
    assert blocks == blocks_before


# ---- 3. 回退块:OCR 只改变空格/断行/普通连字 → 无 issue --------------------


def test_audit_prose_whitespace_hyphen_only_diff_no_issue():
    words = [
        _sw("Researchers", (0, 0, 1, 1), line_no=0, word_no=0),
        _sw("co‐operate", (0, 0, 1, 1), line_no=1, word_no=0),
        _sw("across", (0, 0, 1, 1), line_no=2, word_no=0),
        _sw("many", (0, 0, 1, 1), line_no=2, word_no=1),
        _sw("countries", (0, 0, 1, 1), line_no=3, word_no=0),
    ]
    ocr_text = "Researchers\nco-operate across\nmany  countries"
    blocks = [_block("text", [0, 0, 100, 100], content=ocr_text)]
    decisions = [_decision(0, "ocr", reasons=["adoption_disagreement"])]
    assignment = _assignment({0: words})
    thresholds = _audit_thresholds(
        minimum_reliable_chars=5,
        maximum_block_ned=0.05,
        minimum_char_recall=0.95,
        minimum_token_recall=0.95,
        maximum_addition_ratio=0.05,
    )
    result = audit_prose(_audit_page(words), blocks, decisions, assignment, thresholds)
    assert result["issues"] == []
    assert result["status"] == "OK"


# ---- 4. 回退块:OCR 改数字(0.042→0.42)→ numeric_mismatch -------------------


def test_audit_prose_numeric_change_flags_numeric_mismatch():
    words = [
        _sw("The", (0, 0, 1, 1), line_no=0, word_no=0),
        _sw("measured", (0, 0, 1, 1), line_no=0, word_no=1),
        _sw("value", (0, 0, 1, 1), line_no=0, word_no=2),
        _sw("is", (0, 0, 1, 1), line_no=0, word_no=3),
        _sw("0.042", (0, 0, 1, 1), line_no=0, word_no=4),
        _sw("units", (0, 0, 1, 1), line_no=0, word_no=5),
        _sw("exactly", (0, 0, 1, 1), line_no=0, word_no=6),
    ]
    ocr_text = "The measured value is 0.42 units exactly"
    blocks = [_block("text", [0, 0, 100, 100], content=ocr_text)]
    decisions = [_decision(0, "ocr", reasons=["adoption_disagreement"])]
    assignment = _assignment({0: words})
    thresholds = _audit_thresholds(
        minimum_reliable_chars=5,
        maximum_block_ned=0.9,
        minimum_char_recall=0.5,
        minimum_token_recall=0.5,
        maximum_addition_ratio=0.9,
        minimum_numeric_token_recall=0.9,
    )
    result = audit_prose(_audit_page(words), blocks, decisions, assignment, thresholds)
    assert _issue_codes(result) == {"numeric_mismatch"}
    issue = next(iss for iss in result["issues"] if iss["code"] == "numeric_mismatch")
    assert issue["block_id"] == 0
    assert result["block_metrics"][0]["numeric_token_recall"] == 0.0


# ---- 5. 主指标为逐块 NED:多重集召回 100% 但语序打乱 → prose_mismatch ------


def test_audit_prose_ned_catches_reordering_that_multiset_recall_misses():
    tokens = [
        "Zephyr", "quietly", "traversed", "distant", "valleys",
        "beneath", "glowing", "amber", "skies", "today",
    ]
    words = [_sw(t, (0, 0, 1, 1), line_no=0, word_no=i) for i, t in enumerate(tokens)]
    ocr_text = " ".join(reversed(tokens))
    blocks = [_block("text", [0, 0, 100, 100], content=ocr_text)]
    decisions = [_decision(0, "ocr", reasons=["adoption_disagreement"])]
    assignment = _assignment({0: words})
    thresholds = _audit_thresholds(
        minimum_reliable_chars=5,
        maximum_block_ned=0.3,
        minimum_char_recall=0.99,
        minimum_token_recall=0.99,
        maximum_addition_ratio=0.5,
    )
    result = audit_prose(_audit_page(words), blocks, decisions, assignment, thresholds)
    # 多重集召回满分(完全同一组词),不得触发 missing_prose
    assert result["block_metrics"][0]["char_recall"] == 1.0
    assert result["block_metrics"][0]["token_recall"] == 1.0
    assert _issue_codes(result) == {"prose_mismatch"}


# ---- 6. 采信块:只记录 NED 分布,不产生 missing/addition 告警 ---------------


def test_audit_prose_adopted_block_only_records_ned_no_issue():
    blocks = [_block("text", [0, 0, 100, 100], content="clean adopted prose here")]
    decisions = [
        _decision(
            0, "source_text", reasons=[], block_ned=0.15,
            adopted_text="clean adopted prose here",
        )
    ]
    assignment = _assignment({})
    # 阈值刻意比记录的 NED(0.15)严得多——证明采信块真的不重复告警,
    # 不是恰好阈值宽松才侥幸不报。
    thresholds = _audit_thresholds(maximum_block_ned=0.01)
    result = audit_prose(_audit_page([]), blocks, decisions, assignment, thresholds)
    assert result["issues"] == []
    assert result["status"] == "OK"
    assert result["block_metrics"][0] == {
        "content_source": "source_text",
        "block_ned": 0.15,
    }


# ---- 7. unassigned 源 words 聚簇 → possible_missing_block -----------------


def test_audit_prose_unassigned_cluster_flags_possible_missing_block():
    clean_words = [
        _sw("clean", (0, 0, 1, 1), line_no=0, word_no=0),
        _sw("text", (0, 0, 1, 1), line_no=0, word_no=1),
    ]
    stray_words = [
        _sw("orphan1", (0, 0, 1, 1), block_no=7, line_no=0, word_no=0),
        _sw("orphan2", (0, 0, 1, 1), block_no=7, line_no=0, word_no=1),
        _sw("orphan3", (0, 0, 1, 1), block_no=7, line_no=0, word_no=2),
    ]
    blocks = [_block("text", [0, 0, 100, 100], content="clean text")]
    decisions = [
        _decision(0, "source_text", reasons=[], block_ned=0.0, adopted_text="clean text")
    ]
    thresholds = _audit_thresholds()
    assignment = _assignment({0: clean_words}, unassigned=stray_words)
    result = audit_prose(
        _audit_page(clean_words + stray_words), blocks, decisions, assignment, thresholds
    )
    assert "possible_missing_block" in _issue_codes(result)
    issue = next(
        iss for iss in result["issues"] if iss["code"] == "possible_missing_block"
    )
    assert issue["block_id"] is None

    # 反证:孤立的单个 unassigned word(非聚簇)不得触发——避免降级成"任何漏
    # 归属都报警"的粗暴规则。
    lone_word = [_sw("lone", (0, 0, 1, 1), block_no=7, line_no=0, word_no=0)]
    assignment_lone = _assignment({0: clean_words}, unassigned=lone_word)
    result_lone = audit_prose(
        _audit_page(clean_words + lone_word), blocks, decisions, assignment_lone, thresholds
    )
    assert "possible_missing_block" not in _issue_codes(result_lone)


# ---- 8. 重复环 OCR 输出 → ocr_degeneration(无需源文本参与断言) -------------


def test_audit_prose_repetition_loop_flags_ocr_degeneration_without_source():
    looped = "the cat sat on the mat. " * 60
    blocks = [_block("text", [0, 0, 100, 100], content=looped)]
    decisions = [_decision(0, "ocr", reasons=["adoption_disagreement"])]
    assignment = _assignment({})
    thresholds = _audit_thresholds(maximum_repetition_score=0.3)
    result = audit_prose(_audit_page([]), blocks, decisions, assignment, thresholds)
    assert "ocr_degeneration" in _issue_codes(result)
    issue = next(iss for iss in result["issues"] if iss["code"] == "ocr_degeneration")
    assert issue["block_id"] is None
    # 空源 words → 该块字符量不足,独立判 source_unreliable;两个 issue 并存
    # 时页面仍是 SUSPECT(而非被 source_unreliable 特例掩盖成 UNSCORABLE)。
    assert result["status"] == "SUSPECT"


# ---- 9. 公式/table 块内容不污染 prose 指标 ---------------------------------


def test_audit_prose_formula_block_does_not_pollute_prose_metrics():
    words = [
        _sw("clean", (0, 0, 1, 1), line_no=0, word_no=0),
        _sw("prose", (0, 0, 1, 1), line_no=0, word_no=1),
        _sw("here", (0, 0, 1, 1), line_no=0, word_no=2),
    ]
    text_block = _block("text", [0, 0, 100, 100], content="clean prose here")
    thresholds = _audit_thresholds()

    blocks_without = [text_block]
    decisions_without = [
        _decision(0, "source_text", reasons=[], block_ned=0.0, adopted_text="clean prose here")
    ]
    assignment_without = _assignment({0: words})
    result_without = audit_prose(
        _audit_page(words), blocks_without, decisions_without, assignment_without, thresholds
    )

    formula_block = _block("display_formula", [0, 100, 100, 200], content=r"$E=mc^2$")
    blocks_with = [text_block, formula_block]
    decisions_with = [
        _decision(0, "source_text", reasons=[], block_ned=0.0, adopted_text="clean prose here"),
        _decision(1, "ocr", reasons=["label_not_adoptable"]),
    ]
    assignment_with = _assignment({0: words})
    result_with = audit_prose(
        _audit_page(words), blocks_with, decisions_with, assignment_with, thresholds
    )

    assert result_without["issues"] == result_with["issues"] == []
    assert result_without["block_metrics"][0] == result_with["block_metrics"][0]
    assert 1 not in result_with["block_metrics"]
    assert result_without["metrics"]["prose_block_count"] == result_with["metrics"]["prose_block_count"]


# ---- 10. 多栏代理条件不满足时 sequence_ratio 缺席且不产生 issue ------------


def test_audit_prose_sequence_ratio_absent_when_multi_column_proxy_fails():
    # 交错:源 words 原生 block_no 序列非单调(1,0,1)→ 单栏代理条件不满足。
    # 这与 OCR 块归属(assignment)相互独立——归属仍正常给出。
    words = [
        _sw("first", (0, 0, 1, 1), block_no=1, line_no=0, word_no=0),
        _sw("second", (0, 0, 1, 1), block_no=0, line_no=0, word_no=0),
        _sw("third", (0, 0, 1, 1), block_no=1, line_no=0, word_no=1),
    ]
    blocks = [
        _block("text", [0, 0, 100, 100], content="second"),
        _block("text", [0, 0, 100, 100], content="first third"),
    ]
    decisions = [
        _decision(0, "source_text", reasons=[], block_ned=0.0, adopted_text="second"),
        _decision(1, "source_text", reasons=[], block_ned=0.0, adopted_text="first third"),
    ]
    assignment = _assignment({0: [words[1]], 1: [words[0], words[2]]})
    thresholds = _audit_thresholds()
    result = audit_prose(_audit_page(words), blocks, decisions, assignment, thresholds)
    assert "sequence_ratio" not in result["metrics"]
    assert not any("sequence" in iss["code"] for iss in result["issues"])


def test_audit_prose_sequence_ratio_present_when_single_column_confirmed():
    # 正例对照:block_no 单调不减(全序)→ 单栏代理条件成立,ratio 应出现
    # 且是真实计算值(块 0 的源序秩中位数 0.5 < 块 1 的 2.5,顺序一致)。
    words = [
        _sw("a", (0, 0, 1, 1), block_no=0, line_no=0, word_no=0),
        _sw("b", (0, 0, 1, 1), block_no=0, line_no=0, word_no=1),
        _sw("c", (0, 0, 1, 1), block_no=1, line_no=0, word_no=0),
        _sw("d", (0, 0, 1, 1), block_no=1, line_no=0, word_no=1),
    ]
    blocks = [
        _block("text", [0, 0, 100, 100], content="a b"),
        _block("text", [0, 0, 100, 100], content="c d"),
    ]
    decisions = [
        _decision(0, "source_text", reasons=[], block_ned=0.0, adopted_text="a b"),
        _decision(1, "source_text", reasons=[], block_ned=0.0, adopted_text="c d"),
    ]
    assignment = _assignment({0: [words[0], words[1]], 1: [words[2], words[3]]})
    thresholds = _audit_thresholds()
    result = audit_prose(_audit_page(words), blocks, decisions, assignment, thresholds)
    assert result["metrics"]["sequence_ratio"] == 1.0
    assert result["issues"] == []
    assert result["status"] == "OK"


def test_audit_prose_reversed_block_order_flags_sequence_disorder():
    # 单栏代理条件成立(alpha_words 全在前、beta_words 全在后,block_no 序列
    # [0,0,0,0,1,1,1,1] 单调不减),但 OCR 块归属被人为"倒序"——OCR 块 0 拿到
    # 的是源里排在后面的 beta 段,OCR 块 1 拿到的是源里排在前面的 alpha 段。
    # 两块各自内容与其归属源 words 完全匹配(recall/NED 均满分),不放行
    # 任何其它 issue,专测 sequence_disorder 是否真的走到目标分支。
    alpha_words = [
        _sw(t, (0, 0, 1, 1), block_no=0, line_no=0, word_no=i)
        for i, t in enumerate("Alpha comes first here".split())
    ]
    beta_words = [
        _sw(t, (0, 0, 1, 1), block_no=1, line_no=0, word_no=i)
        for i, t in enumerate("Beta comes second here".split())
    ]
    blocks = [
        _block("text", [0, 0, 100, 100], content="Beta comes second here"),
        _block("text", [0, 100, 100, 200], content="Alpha comes first here"),
    ]
    decisions = [
        _decision(0, "ocr", reasons=["adoption_disagreement"]),
        _decision(1, "ocr", reasons=["adoption_disagreement"]),
    ]
    # 归属"倒序":OCR 块 0 ← beta(源序靠后),OCR 块 1 ← alpha(源序靠前)。
    assignment = _assignment({0: beta_words, 1: alpha_words})
    thresholds = _audit_thresholds(
        minimum_reliable_chars=5,
        maximum_bad_char_ratio=0.5,
        maximum_block_ned=0.9,
        minimum_char_recall=0.5,
        minimum_token_recall=0.5,
        maximum_addition_ratio=0.5,
        maximum_repetition_score=0.9,
        minimum_single_column_sequence_ratio=0.8,
    )
    result = audit_prose(
        _audit_page(alpha_words + beta_words), blocks, decisions, assignment, thresholds
    )
    # 两块内容各自完美匹配自己的归属 words——确认其它 issue 门确实放行,
    # 负例真的是靠"顺序"这一条命中,不是被其它门顺带带出来的。
    assert result["block_metrics"][0]["char_recall"] == 1.0
    assert result["block_metrics"][1]["char_recall"] == 1.0
    assert result["metrics"]["sequence_ratio"] == 0.0
    assert _issue_codes(result) == {"sequence_disorder"}
    assert result["status"] == "SUSPECT"


# ---- 11. 源坏码过多 → 块 source_unreliable;全页如此 → 页 UNSCORABLE -------


def test_audit_prose_all_source_unreliable_page_is_unscorable_not_ocr_failed():
    # 每块字符量(11 = 5 个 clean + 6 个坏码)已达 minimum_reliable_chars(5),
    # 但坏码占比(6/11≈0.55)远超 maximum_bad_char_ratio——专测"坏码超阈值"
    # 这条独立于字符量不足的分支。
    bad_words_1 = [
        _sw("clean", (0, 0, 1, 1), line_no=0, word_no=0),
        _sw("", (0, 0, 1, 1), line_no=0, word_no=1),
    ]
    bad_words_2 = [
        _sw("clean", (0, 0, 1, 1), line_no=0, word_no=0),
        _sw("", (0, 0, 1, 1), line_no=0, word_no=1),
    ]
    blocks = [
        _block("text", [0, 0, 100, 100], content="normal length prose content here"),
        _block("text", [0, 100, 100, 200], content="another block of ordinary prose"),
    ]
    decisions = [
        _decision(0, "ocr", reasons=["bad_source_chars"]),
        _decision(1, "ocr", reasons=["bad_source_chars"]),
    ]
    assignment = _assignment({0: bad_words_1, 1: bad_words_2})
    thresholds = _audit_thresholds(minimum_reliable_chars=5, maximum_bad_char_ratio=0.1)
    result = audit_prose(
        _audit_page(bad_words_1 + bad_words_2), blocks, decisions, assignment, thresholds
    )
    assert result["status"] == "UNSCORABLE"
    assert _issue_codes(result) == {"source_unreliable"}
    assert len(result["issues"]) == 2
    assert result["block_metrics"][0]["source_unreliable"] is True
    assert result["block_metrics"][1]["source_unreliable"] is True


def test_audit_prose_geometry_unscorable_page_is_unscorable():
    # assignment["geometry_unscorable"]=True(权威页级信号)→ 归属块全部
    # source_unreliable,不猜测,不得把 OCR 判错。
    words = [
        _sw("perfectly", (0, 0, 1, 1), line_no=0, word_no=0),
        _sw("healthy", (0, 0, 1, 1), line_no=0, word_no=1),
        _sw("source", (0, 0, 1, 1), line_no=0, word_no=2),
        _sw("text", (0, 0, 1, 1), line_no=0, word_no=3),
    ]
    blocks = [_block("text", [0, 0, 100, 100], content="perfectly healthy source text")]
    decisions = [_decision(0, "ocr", reasons=["geometry_unscorable"])]
    assignment = _assignment({0: words}, geometry_unscorable=True)
    thresholds = _audit_thresholds()
    result = audit_prose(_audit_page(words), blocks, decisions, assignment, thresholds)
    assert result["status"] == "UNSCORABLE"
    assert _issue_codes(result) == {"source_unreliable"}
    assert result["block_metrics"][0]["source_unreliable"] is True


def test_audit_prose_geometry_unscorable_flag_forces_unscorable_independent_of_issues():
    # 直达测试(Minor 3):页面唯一的块是非正文 label(table),会在门 0 就被
    # 跳过、完全不参与 prose 对账——因此不会有任何 block 产生
    # source_unreliable/其它 issue,issues 列表天然为空。若 status 判定只
    # 依赖 issues 推导,这种页会被误判成 OK。assignment["geometry_unscorable"]
    # 是独立于 decisions/issues 的权威页级信号,必须单独短路成 UNSCORABLE,
    # 不依赖"每个受影响块的 decision.reasons 都正确传播了 geometry_unscorable"
    # 这条 Task 5 上游不变量。
    blocks = [_block("table", [0, 0, 100, 100], content="| a | b |")]
    decisions = [_decision(0, "ocr", reasons=["label_not_adoptable"])]
    assignment = _assignment({}, geometry_unscorable=True)
    thresholds = _audit_thresholds()
    result = audit_prose(_audit_page([]), blocks, decisions, assignment, thresholds)
    assert result["issues"] == []
    assert result["block_metrics"] == {}
    assert result["status"] == "UNSCORABLE"


# ---- 12. 字符量加权:长块丢失在页级指标可见 ---------------------------------


def test_audit_prose_weighted_aggregate_reveals_long_block_char_loss():
    short_words = [
        _sw("Hi", (0, 0, 1, 1), line_no=0, word_no=0),
        _sw("there", (0, 0, 1, 1), line_no=0, word_no=1),
    ]
    long_source_sentence = (
        "This lengthy paragraph documents an extended discussion covering "
        "numerous distinct topics across many carefully constructed sentences "
        "that together form a substantial block of source text content"
    )
    long_words = [
        _sw(t, (0, 0, 1, 1), line_no=0, word_no=i)
        for i, t in enumerate(long_source_sentence.split())
    ]
    blocks = [
        _block("text", [0, 0, 100, 100], content="Hi there"),
        _block("text", [0, 100, 100, 200], content="Unrelated short OCR fragment"),
    ]
    decisions = [
        _decision(0, "ocr", reasons=["adoption_disagreement"]),
        _decision(1, "ocr", reasons=["adoption_disagreement"]),
    ]
    assignment = _assignment({0: short_words, 1: long_words})
    thresholds = _audit_thresholds(
        minimum_reliable_chars=5,
        maximum_block_ned=0.99,
        minimum_char_recall=0.01,  # 关掉逐块 issue,只看聚合数字本身
        minimum_token_recall=0.01,
        maximum_addition_ratio=0.99,
    )
    result = audit_prose(
        _audit_page(short_words + long_words), blocks, decisions, assignment, thresholds
    )
    assert result["block_metrics"][0]["char_recall"] == 1.0
    long_recall = result["block_metrics"][1]["char_recall"]
    assert long_recall < 0.3
    naive_average = (1.0 + long_recall) / 2
    weighted = result["metrics"]["weighted_char_recall"]
    # 加权聚合必须显著低于"每块平均"——不能被短块的满分掩盖长块的丢失
    assert weighted < naive_average - 0.2
    assert weighted < 0.3


# ---- 13. missing_samples/added_samples:定位样本(计划 §6.4 补齐规格) -------
# controller 漏传的计划 §6.4 条款:"missing_samples/added_samples:最多保存
# 少量定位样本,不把全文重复写入 JSON"。


def test_audit_prose_missing_prose_block_records_missing_samples():
    # 复用清单 1(漏整句)的 fixture:missing_samples 必须含缺失片段的子串。
    words = [
        _sw("Sentence", (0, 0, 1, 1), line_no=0, word_no=0),
        _sw("one", (0, 0, 1, 1), line_no=0, word_no=1),
        _sw("is", (0, 0, 1, 1), line_no=0, word_no=2),
        _sw("here", (0, 0, 1, 1), line_no=0, word_no=3),
        _sw("today", (0, 0, 1, 1), line_no=0, word_no=4),
        _sw("Sentence", (0, 0, 1, 1), line_no=1, word_no=0),
        _sw("two", (0, 0, 1, 1), line_no=1, word_no=1),
        _sw("follows", (0, 0, 1, 1), line_no=1, word_no=2),
        _sw("right", (0, 0, 1, 1), line_no=1, word_no=3),
        _sw("after", (0, 0, 1, 1), line_no=1, word_no=4),
    ]
    blocks = [_block("text", [0, 0, 100, 100], content="Sentence one is here today")]
    decisions = [_decision(0, "ocr", reasons=["adoption_disagreement"])]
    assignment = _assignment({0: words})
    thresholds = _audit_thresholds(
        minimum_reliable_chars=5,
        maximum_block_ned=0.9,
        minimum_char_recall=0.7,
        minimum_token_recall=0.7,
        maximum_addition_ratio=0.9,
    )
    result = audit_prose(_audit_page(words), blocks, decisions, assignment, thresholds)
    assert "missing_prose" in _issue_codes(result)
    samples = result["block_metrics"][0]["missing_samples"]
    assert samples  # 非空
    assert any("two" in s and "follows" in s for s in samples)
    # 样本是短片段,不是整块/整页原文
    assert all(len(s) <= 80 for s in samples)
    assert "added_samples" not in result["block_metrics"][0]


def test_audit_prose_ocr_addition_block_records_added_samples():
    # 复用清单 2(多整句)的 fixture:added_samples 必须含多出片段的子串。
    words = [
        _sw("Alpha", (0, 0, 1, 1), line_no=0, word_no=0),
        _sw("beta", (0, 0, 1, 1), line_no=0, word_no=1),
        _sw("gamma", (0, 0, 1, 1), line_no=0, word_no=2),
        _sw("delta", (0, 0, 1, 1), line_no=0, word_no=3),
        _sw("epsilon", (0, 0, 1, 1), line_no=0, word_no=4),
    ]
    ocr_text = (
        "Alpha beta gamma delta epsilon zeta eta theta iota kappa "
        "lambda mu nu xi omicron pi"
    )
    blocks = [_block("text", [0, 0, 100, 100], content=ocr_text)]
    decisions = [_decision(0, "ocr", reasons=["adoption_disagreement"])]
    assignment = _assignment({0: words})
    thresholds = _audit_thresholds(
        minimum_reliable_chars=5,
        maximum_block_ned=0.9,
        minimum_char_recall=0.5,
        minimum_token_recall=0.5,
        maximum_addition_ratio=0.2,
    )
    result = audit_prose(_audit_page(words), blocks, decisions, assignment, thresholds)
    assert "ocr_addition" in _issue_codes(result)
    samples = result["block_metrics"][0]["added_samples"]
    assert samples
    assert any("zeta" in s and "omicron" in s for s in samples)
    assert all(len(s) <= 80 for s in samples)
    assert "missing_samples" not in result["block_metrics"][0]


def test_audit_prose_missing_samples_truncated_to_maximum_samples_per_block():
    # 4 段各自独立的缺失(锚点 AA/BB/CC/DD/EE 都在,gap 全被 OCR 漏掉)→
    # difflib 应产出 4 个独立 delete run;maximum_samples_per_block=2 → 只
    # 保留前 2 条,验证真的被截断而不是恰好只有 2 条。
    tokens = [
        "AA", "gapone", "BB", "gaptwo", "CC", "gapthree", "DD", "gapfour", "EE",
    ]
    words = [
        _sw(t, (0, 0, 1, 1), line_no=0, word_no=i) for i, t in enumerate(tokens)
    ]
    ocr_text = "AA BB CC DD EE"
    blocks = [_block("text", [0, 0, 100, 100], content=ocr_text)]
    decisions = [_decision(0, "ocr", reasons=["adoption_disagreement"])]
    assignment = _assignment({0: words})
    # 默认 minimum_char_recall/minimum_token_recall(0.8)足以让这份严重残缺
    # 的 recall(约 0.27/0.56)触发 missing_prose;只覆盖 maximum_samples_per_block。
    thresholds = _audit_thresholds(minimum_reliable_chars=5, maximum_samples_per_block=2)
    result = audit_prose(_audit_page(words), blocks, decisions, assignment, thresholds)
    assert "missing_prose" in _issue_codes(result)
    samples = result["block_metrics"][0]["missing_samples"]
    assert len(samples) == 2
    assert samples == ["gapone", "gaptwo"]


def test_audit_prose_clean_block_has_no_sample_fields():
    # 清单 3(空格/断行/连字差异,无 issue)的 fixture:干净块不产样本字段
    # (不产字段,而不是空列表——契约锁定这一种,另一种不得出现)。
    words = [
        _sw("Researchers", (0, 0, 1, 1), line_no=0, word_no=0),
        _sw("co‐operate", (0, 0, 1, 1), line_no=1, word_no=0),
        _sw("across", (0, 0, 1, 1), line_no=2, word_no=0),
        _sw("many", (0, 0, 1, 1), line_no=2, word_no=1),
        _sw("countries", (0, 0, 1, 1), line_no=3, word_no=0),
    ]
    ocr_text = "Researchers\nco-operate across\nmany  countries"
    blocks = [_block("text", [0, 0, 100, 100], content=ocr_text)]
    decisions = [_decision(0, "ocr", reasons=["adoption_disagreement"])]
    assignment = _assignment({0: words})
    thresholds = _audit_thresholds(
        minimum_reliable_chars=5,
        maximum_block_ned=0.05,
        minimum_char_recall=0.95,
        minimum_token_recall=0.95,
        maximum_addition_ratio=0.05,
    )
    result = audit_prose(_audit_page(words), blocks, decisions, assignment, thresholds)
    assert result["issues"] == []
    block_metrics = result["block_metrics"][0]
    assert "missing_samples" not in block_metrics
    assert "added_samples" not in block_metrics


def test_audit_prose_sample_string_truncated_to_eighty_chars():
    # 单个很长的连续缺失 run(20 个长 token)→ 拼接后远超 80 字符,验证样本
    # 本身被截断(不是恰好 diff 出短样本)。
    long_gap_tokens = [f"longwordnumber{i:02d}" for i in range(20)]
    tokens = ["keepstart"] + long_gap_tokens + ["keepend"]
    words = [
        _sw(t, (0, 0, 1, 1), line_no=0, word_no=i) for i, t in enumerate(tokens)
    ]
    ocr_text = "keepstart keepend"
    blocks = [_block("text", [0, 0, 100, 100], content=ocr_text)]
    decisions = [_decision(0, "ocr", reasons=["adoption_disagreement"])]
    assignment = _assignment({0: words})
    # 默认 recall 阈值(0.8)足以让这份几乎全丢的内容(22 词只剩 2 词)触发
    # missing_prose。
    thresholds = _audit_thresholds(minimum_reliable_chars=5)
    result = audit_prose(_audit_page(words), blocks, decisions, assignment, thresholds)
    full_gap = " ".join(long_gap_tokens)
    assert len(full_gap) > 80  # 反证:不截断的话原本会超长
    samples = result["block_metrics"][0]["missing_samples"]
    assert len(samples) == 1
    assert samples[0] == full_gap[:80]
    assert len(samples[0]) == 80


# ===========================================================================
# Task 8:文档级聚合 audit_document + 原子写 write_audit_report + 独立 CLI。
# ---------------------------------------------------------------------------
# 铁律:本节测试全部只构造 tmp 内的 PDF + 手写 OCR res JSON(不跑真实引擎、
# 不 import engine.py);audit_document 独立重跑绝不改写 Markdown/任何产物。
# ===========================================================================


def _make_pdf(path, page_texts, width=600, height=800):
    """构造一份 tmp PDF:每页可选插入一行纯 ASCII 文本(顶左原点,与既有
    extract_source_page 测试的坐标惯例一致)。"""
    doc = fitz.open()
    for spec in page_texts:
        page = doc.new_page(width=width, height=height)
        for text, pos in spec:
            page.insert_text(pos, text, fontname="helv", fontsize=12)
    doc.save(path)
    doc.close()


def _write_manifest(work_dir, page_count, dpi=150, failed_pages=None):
    os.makedirs(work_dir, exist_ok=True)
    manifest = {
        "pdf_path": "x",
        "fingerprint": {"page_count": page_count},
        "dpi": dpi,
        "route": "B",
        "failed_pages": failed_pages or [],
        "in_progress": None,
        "attempts_by_page": {},
        "restarts": 0,
    }
    with open(os.path.join(work_dir, "manifest.json"), "w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False)


def _write_page_res(work_dir, page, blocks, width=600, height=800):
    os.makedirs(work_dir, exist_ok=True)
    with open(cp.page_res_path(work_dir, page), "w", encoding="utf-8") as f:
        json.dump(
            {"width": width, "height": height, "parsing_res_list": blocks},
            f,
            ensure_ascii=False,
        )


def _doc_layout(tmp_path, stem="Book"):
    return resolve_layout(stem, str(tmp_path / "out"), str(tmp_path / "work"))


# ---- 1. 多页聚合:一页采信、一页回退 → summary 与页级结果一致 ----------------


def test_audit_document_aggregates_adoption_across_pages(tmp_path):
    layout = _doc_layout(tmp_path)
    pdf_path = str(tmp_path / "book.pdf")
    _make_pdf(
        pdf_path,
        [
            [("hello world", (72, 72))],       # page 1:与 OCR 完全一致 → 采信
            [("alpha beta", (72, 72))],         # page 2:与 OCR 严重不符 → 回退
        ],
    )
    _write_manifest(layout.work_dir, page_count=2)
    _write_page_res(
        layout.work_dir, 1,
        [{"block_label": "text", "block_content": "hello world",
          "block_bbox": [0, 0, 600, 800]}],
    )
    _write_page_res(
        layout.work_dir, 2,
        [{"block_label": "text", "block_content": "zzzzzzzzzzzzzzzzzzzz",
          "block_bbox": [0, 0, 600, 800]}],
    )

    report = audit_document(
        pdf_path, layout, ROUTE_B_V1_UNCALIBRATED_THRESHOLDS, decisions_by_page=None
    )

    summary = report["summary"]
    assert summary["pages"] == 2
    assert summary["scorable_pages"] == 2
    assert summary["adoption"]["prose_blocks"] == 2
    assert summary["adoption"]["adopted"] == 1
    assert summary["adoption"]["fallback_ocr"] == 1
    assert summary["adoption"]["fallback_reasons"] == {"char_ratio_out_of_range": 1}

    page1, page2 = report["pages"]
    assert page1["status"] == "OK"
    assert page1["blocks"][0]["content_source"] == "source_text"
    assert page1["blocks"][0]["reasons"] == []
    assert page2["blocks"][0]["content_source"] == "ocr"
    assert page2["blocks"][0]["reasons"] == ["char_ratio_out_of_range"]
    # page2 的回退块与 OCR 严重不符,audit_prose 必须真实报出对账问题(page 级
    # SUSPECT 与 issue_counts 应与页级结果一致,而不是聚合层凭空编数字)。
    assert page2["status"] == "SUSPECT"
    assert summary["suspect_pages"] == [2]
    assert summary["issue_counts"] == {
        code: sum(1 for iss in page2["issues"] if iss["code"] == code)
        for code in {iss["code"] for iss in page2["issues"]}
    }
    assert summary["status"] == "SUSPECT"


# ---- 1b. Minor 4(review 裁定):fallback_ocr 与 fallback_reasons 单一来源,
# 不变量恒成立 --------------------------------------------------------------


def test_adoption_fallback_ocr_equals_sum_of_fallback_reasons(tmp_path):
    layout = _doc_layout(tmp_path)
    pdf_path = str(tmp_path / "book.pdf")
    _make_pdf(
        pdf_path,
        [
            [("hello world", (72, 72))],              # page 1:采信
            [("alpha beta", (72, 72))],                 # page 2:回退(字符比不符)
            [("value here today", (72, 72))],           # page 3:回退(公式痕迹)
        ],
    )
    _write_manifest(layout.work_dir, page_count=3)
    _write_page_res(
        layout.work_dir, 1,
        [{"block_label": "text", "block_content": "hello world",
          "block_bbox": [0, 0, 600, 800]}],
    )
    _write_page_res(
        layout.work_dir, 2,
        [{"block_label": "text", "block_content": "zzzzzzzzzzzzzzzzzzzz",
          "block_bbox": [0, 0, 600, 800]}],
    )
    _write_page_res(
        layout.work_dir, 3,
        # OCR 内容带 $...$ 数学痕迹 → 门 4(_has_math)拒绝,回退原因
        # math_in_prose_block,与 page 2 的 char_ratio_out_of_range 是不同 reason
        # code,用来证明多种 fallback reason 分布下 fallback_ocr 仍等于总和。
        [{"block_label": "text", "block_content": "$value here today$",
          "block_bbox": [0, 0, 600, 800]}],
    )

    report = audit_document(
        pdf_path, layout, ROUTE_B_V1_UNCALIBRATED_THRESHOLDS, decisions_by_page=None
    )
    adoption = report["summary"]["adoption"]
    # 两种不同 fallback 原因真实分布(不是凑巧都是 0 或都是同一个 code)。
    assert adoption["fallback_reasons"] == {
        "char_ratio_out_of_range": 1,
        "math_in_prose_block": 1,
    }
    # 不变量:fallback_ocr 与 fallback_reasons 求和恒一致(单一来源推导,不是
    # 两套独立计数偶然对上)。
    assert adoption["fallback_ocr"] == sum(adoption["fallback_reasons"].values())
    assert adoption["fallback_ocr"] == 2
    assert adoption["adopted"] == 1


# ---- 2. 页缺 res JSON(非 manifest 失败页) → page_incomplete + UNSCORABLE ---


def test_audit_document_missing_res_json_reports_page_incomplete(tmp_path):
    layout = _doc_layout(tmp_path)
    pdf_path = str(tmp_path / "book.pdf")
    _make_pdf(pdf_path, [[("hello world", (72, 72))], [("second page", (72, 72))]])
    _write_manifest(layout.work_dir, page_count=2)
    _write_page_res(
        layout.work_dir, 1,
        [{"block_label": "text", "block_content": "hello world",
          "block_bbox": [0, 0, 600, 800]}],
    )
    # page 2 的 res JSON 故意不写(既非合法空页,也未被 manifest 记为失败页)。

    report = audit_document(
        pdf_path, layout, ROUTE_B_V1_UNCALIBRATED_THRESHOLDS, decisions_by_page=None
    )

    page1, page2 = report["pages"]
    assert page1["status"] == "OK"
    assert page2["status"] == "UNSCORABLE"
    assert [iss["code"] for iss in page2["issues"]] == ["page_incomplete"]
    assert report["summary"]["scorable_pages"] == 1
    # 文档不崩:page1 仍完整聚合出 adoption 统计。
    assert report["summary"]["adoption"]["adopted"] == 1


# ---- 3. 失败页(manifest failed_pages)与"缺 res JSON 但未记失败"语义区分 ----


def test_audit_document_distinguishes_failed_page_from_incomplete_page(tmp_path):
    layout = _doc_layout(tmp_path)
    pdf_path = str(tmp_path / "book.pdf")
    _make_pdf(pdf_path, [[("a", (72, 72))], [("b", (72, 72))], [("c", (72, 72))]])
    _write_manifest(
        layout.work_dir, page_count=3,
        failed_pages=[{"page": 2, "error": "boom", "kind": "process-killed"}],
    )
    _write_page_res(
        layout.work_dir, 1,
        [{"block_label": "text", "block_content": "a", "block_bbox": [0, 0, 600, 800]}],
    )
    # page 2:manifest 记为失败(毒页),无 res JSON。
    # page 3:既无 res JSON,也未被 manifest 记为失败 → 视为独立重跑发现的缺页。

    report = audit_document(
        pdf_path, layout, ROUTE_B_V1_UNCALIBRATED_THRESHOLDS, decisions_by_page=None
    )

    _, page2, page3 = report["pages"]
    assert page2["status"] == "UNSCORABLE"
    assert page3["status"] == "UNSCORABLE"
    codes2 = [iss["code"] for iss in page2["issues"]]
    codes3 = [iss["code"] for iss in page3["issues"]]
    assert codes2 == ["page_failed"]
    assert codes3 == ["page_incomplete"]
    assert codes2 != codes3
    assert "process-killed" in page2["issues"][0]["detail"]


# ---- 3b. Minor 3(review 裁定):issue_counts 真正跨页分布聚合,断言手写期望
# 值(不得从 report 自己的 pages 反算,消除循环断言) -------------------------


def test_issue_counts_aggregates_same_code_across_multiple_pages(tmp_path):
    layout = _doc_layout(tmp_path)
    pdf_path = str(tmp_path / "book.pdf")
    _make_pdf(pdf_path, [[], [], []])  # 3 页空白 PDF,内容不重要——3 页全部缺
    _write_manifest(
        layout.work_dir, page_count=3,
        failed_pages=[{"page": 3, "error": "boom", "kind": "process-killed"}],
    )
    # page 1、page 2:均缺 res JSON 且都未被 manifest 记为失败 → 各产生恰好
    # 一条 page_incomplete(同一 code 跨两页)。
    # page 3:manifest 记为失败 → 恰好一条 page_failed(不同 code)。
    # 不写任何 page res JSON。

    report = audit_document(
        pdf_path, layout, ROUTE_B_V1_UNCALIBRATED_THRESHOLDS, decisions_by_page=None
    )

    # 手写期望值——不是 `{code: sum(...) for page in report["pages"] ...}` 这种
    # 从 report 自己反算的循环断言;这里的 2/1 是由上面的 fixture 构造直接决定
    # 的独立事实。
    assert report["summary"]["issue_counts"] == {
        "page_incomplete": 2,
        "page_failed": 1,
    }


# ---- 4. 合法空页(parsing_res_list=[])→ OK,不计入 suspect --------------------


def test_audit_document_legit_empty_page_is_ok_not_suspect(tmp_path):
    layout = _doc_layout(tmp_path)
    pdf_path = str(tmp_path / "book.pdf")
    _make_pdf(pdf_path, [[("hello world", (72, 72))], []])
    _write_manifest(layout.work_dir, page_count=2)
    _write_page_res(
        layout.work_dir, 1,
        [{"block_label": "text", "block_content": "hello world",
          "block_bbox": [0, 0, 600, 800]}],
    )
    cp.write_empty_page(layout.work_dir, 2)  # 合法空白页哨兵(真实产线落盘方式)

    report = audit_document(
        pdf_path, layout, ROUTE_B_V1_UNCALIBRATED_THRESHOLDS, decisions_by_page=None
    )

    page2 = report["pages"][1]
    assert page2["status"] == "OK"
    assert page2["blocks"] == []
    assert page2["issues"] == []
    assert 2 not in report["summary"]["suspect_pages"]
    assert report["summary"]["scorable_pages"] == 2
    assert report["summary"]["status"] == "OK"


# ---- 5. 一页内 prose + formula + table 三类块的块级 provenance/审计记录 -----


def test_audit_document_records_formula_and_table_block_audits(tmp_path):
    layout = _doc_layout(tmp_path)
    pdf_path = str(tmp_path / "book.pdf")
    _make_pdf(
        pdf_path,
        [[
            ("hello world", (72, 72)),                  # prose 区域(y 0..200)
            ("formula placeholder text", (72, 250)),    # formula 区域(y 200..400)
            ("A 1", (72, 450)),                          # table 区域(y 400..600)
        ]],
    )
    _write_manifest(layout.work_dir, page_count=1)
    _write_page_res(
        layout.work_dir, 1,
        [
            {"block_label": "text", "block_content": "hello world",
             "block_bbox": [0, 0, 600, 200]},
            {"block_label": "display_formula", "block_content": "$x^2$",
             "block_bbox": [0, 200, 600, 400]},
            {"block_label": "table",
             "block_content": "<table><tr><th>A</th></tr><tr><td>1</td></tr></table>",
             "block_bbox": [0, 400, 600, 600]},
        ],
    )

    report = audit_document(
        pdf_path, layout, ROUTE_B_V1_UNCALIBRATED_THRESHOLDS, decisions_by_page=None
    )

    page = report["pages"][0]
    assert len(page["blocks"]) == 3
    assert page["blocks"][0]["content_source"] == "source_text"  # prose 采信
    assert page["blocks"][1]["reasons"] == ["label_not_adoptable"]  # 公式永不采信
    assert page["blocks"][2]["reasons"] == ["label_not_adoptable"]  # 表格永不采信

    assert len(page["formula_audit"]) == 1
    formula = page["formula_audit"][0]
    assert formula["block_id"] == 1
    assert formula["pua_count"] == 0
    assert formula["control_char_count"] == 0
    assert formula["source_char_count"] > 0
    assert formula["text_layer_has_no_formula_chars"] is True
    assert formula["source_unreliable_for_formula"] is False

    assert len(page["table_audit"]) == 1
    table = page["table_audit"][0]
    assert table["block_id"] == 2
    assert isinstance(table["header_fingerprint"], str)
    assert len(table["header_fingerprint"]) == 64  # sha256 hex
    assert "n_rows" in table["metrics"]


# ---- 6. write_audit_report 原子写:替换已存在文件、内容可 round-trip -------


def test_write_audit_report_atomic_replace_and_roundtrip(tmp_path):
    path = str(tmp_path / "nested" / "Book_source_audit.json")
    os.makedirs(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        f.write("not even json")  # 预先存在的(半截/旧)内容

    write_audit_report({"schema_version": 2, "n": 1}, path)
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == {"schema_version": 2, "n": 1}
    assert not os.path.exists(path + ".tmp")

    # 再写一次,证明可重复替换(不是只在目标不存在时才成功)。
    write_audit_report({"schema_version": 2, "n": 2}, path)
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == {"schema_version": 2, "n": 2}
    assert not os.path.exists(path + ".tmp")


def test_write_audit_report_creates_parent_dir(tmp_path):
    path = str(tmp_path / "brand_new_dir" / "Book_source_audit.json")
    write_audit_report({"ok": True}, path)
    with open(path, encoding="utf-8") as f:
        assert json.load(f) == {"ok": True}


# ---- 7. decisions_by_page=None → dry_run;提供决策 → recorded --------------


def test_audit_document_decisions_by_page_none_is_dry_run(tmp_path):
    layout = _doc_layout(tmp_path)
    pdf_path = str(tmp_path / "book.pdf")
    _make_pdf(pdf_path, [[("hello world", (72, 72))]])
    _write_manifest(layout.work_dir, page_count=1)
    _write_page_res(
        layout.work_dir, 1,
        [{"block_label": "text", "block_content": "hello world",
          "block_bbox": [0, 0, 600, 800]}],
    )

    report = audit_document(
        pdf_path, layout, ROUTE_B_V1_UNCALIBRATED_THRESHOLDS, decisions_by_page=None
    )
    assert report["adoption_source"] == "dry_run"


def test_audit_document_decisions_by_page_provided_is_recorded(tmp_path):
    layout = _doc_layout(tmp_path)
    pdf_path = str(tmp_path / "book.pdf")
    _make_pdf(pdf_path, [[("hello world", (72, 72))]])
    _write_manifest(layout.work_dir, page_count=1)
    _write_page_res(
        layout.work_dir, 1,
        [{"block_label": "text", "block_content": "totally different ocr text",
          "block_bbox": [0, 0, 600, 800]}],
    )
    # 显式给定"已记录"的决策(与现场 dry-run 推演出的结果不同,证明 recorded
    # 分支真的消费调用方传入的决策,而不是自己重新跑了一遍现场推演)。
    recorded = [
        AdoptionDecision(
            block_id=0, content_source="source_text", reasons=[],
            block_ned=0.0, adopted_text="hello world",
        )
    ]

    report = audit_document(
        pdf_path, layout, ROUTE_B_V1_UNCALIBRATED_THRESHOLDS,
        decisions_by_page={1: recorded},
    )
    assert report["adoption_source"] == "recorded"
    block = report["pages"][0]["blocks"][0]
    assert block["content_source"] == "source_text"
    assert block["reasons"] == []


# ---- 7b. born_digital_mode:显式参数透传,不从 decisions_by_page 反推 --------
# (review 裁定 Important 1:决不允许从 decisions_by_page 是否为 None 推断路由
# 模式——两者是独立维度。调用方(Task 9 编排/独立 CLI)必须显式传入。)


def test_audit_document_born_digital_mode_defaults_to_unknown_when_not_given(tmp_path):
    layout = _doc_layout(tmp_path)
    pdf_path = str(tmp_path / "book.pdf")
    _make_pdf(pdf_path, [[("hello world", (72, 72))]])
    _write_manifest(layout.work_dir, page_count=1)
    _write_page_res(
        layout.work_dir, 1,
        [{"block_label": "text", "block_content": "hello world",
          "block_bbox": [0, 0, 600, 800]}],
    )

    report = audit_document(
        pdf_path, layout, ROUTE_B_V1_UNCALIBRATED_THRESHOLDS, decisions_by_page=None
    )
    assert report["born_digital_mode"] == "unknown"


def test_audit_document_born_digital_mode_is_explicit_passthrough_not_inferred(tmp_path):
    layout = _doc_layout(tmp_path)
    pdf_path = str(tmp_path / "book.pdf")
    _make_pdf(pdf_path, [[("hello world", (72, 72))]])
    _write_manifest(layout.work_dir, page_count=1)
    _write_page_res(
        layout.work_dir, 1,
        [{"block_label": "text", "block_content": "hello world",
          "block_bbox": [0, 0, 600, 800]}],
    )

    # 关键反例:decisions_by_page=None(通常对应 dry_run)却显式传
    # born_digital_mode="hybrid"——如果实现还在偷偷从 decisions_by_page 反推,
    # 这里就会被打脸成 "ocr" 而不是调用方给的 "hybrid"。
    report = audit_document(
        pdf_path, layout, ROUTE_B_V1_UNCALIBRATED_THRESHOLDS,
        decisions_by_page=None, born_digital_mode="hybrid",
    )
    assert report["born_digital_mode"] == "hybrid"
    assert report["adoption_source"] == "dry_run"  # 两个维度互不影响

    # 反过来:decisions_by_page 提供了(通常对应 recorded)也不代表 born_digital_mode
    # 会被覆盖成 "hybrid"——同样必须原样透传调用方给的值。
    recorded = [
        AdoptionDecision(
            block_id=0, content_source="source_text", reasons=[],
            block_ned=0.0, adopted_text="hello world",
        )
    ]
    report2 = audit_document(
        pdf_path, layout, ROUTE_B_V1_UNCALIBRATED_THRESHOLDS,
        decisions_by_page={1: recorded}, born_digital_mode="ocr",
    )
    assert report2["born_digital_mode"] == "ocr"
    assert report2["adoption_source"] == "recorded"


def test_audit_document_recorded_missing_page_key_marks_no_decision(tmp_path):
    layout = _doc_layout(tmp_path)
    pdf_path = str(tmp_path / "book.pdf")
    _make_pdf(pdf_path, [[("hello world", (72, 72))]])
    _write_manifest(layout.work_dir, page_count=1)
    _write_page_res(
        layout.work_dir, 1,
        [{"block_label": "text", "block_content": "hello world",
          "block_bbox": [0, 0, 600, 800]}],
    )

    # decisions_by_page 非 None,但这一页没有对应条目——honest 兜底,不得
    # 编造一个 content_source。
    report = audit_document(
        pdf_path, layout, ROUTE_B_V1_UNCALIBRATED_THRESHOLDS, decisions_by_page={},
    )
    block = report["pages"][0]["blocks"][0]
    assert block["content_source"] == "ocr"
    assert block["reasons"] == ["no_decision"]


# ---- 8. summary 不含 pages 明细的复制(结构断言) ---------------------------


def test_audit_document_summary_has_no_page_detail_duplication(tmp_path):
    layout = _doc_layout(tmp_path)
    pdf_path = str(tmp_path / "book.pdf")
    _make_pdf(pdf_path, [[("hello world", (72, 72))]])
    _write_manifest(layout.work_dir, page_count=1)
    _write_page_res(
        layout.work_dir, 1,
        [{"block_label": "text", "block_content": "hello world",
          "block_bbox": [0, 0, 600, 800]}],
    )

    report = audit_document(
        pdf_path, layout, ROUTE_B_V1_UNCALIBRATED_THRESHOLDS, decisions_by_page=None
    )
    assert set(report["summary"].keys()) == {
        "status", "pages", "scorable_pages", "suspect_pages",
        "adoption", "issue_counts",
    }
    # 顶层 report["pages"] 是逐页明细列表;summary["pages"] 只是计数(int),
    # 两者同名不同义——summary 里绝不应嵌入页级明细。
    assert isinstance(report["pages"], list)
    assert isinstance(report["summary"]["pages"], int)
    assert report["schema_version"] == 2
    assert report["route"] == "B"
    assert report["threshold_profile"] == THRESHOLD_PROFILE_UNCALIBRATED
    assert report["pdf_fingerprint"]["page_count"] == 1
    assert isinstance(report["pdf_fingerprint"]["sha256"], str)
    assert len(report["pdf_fingerprint"]["sha256"]) == 64
    assert report["pdf_fingerprint"]["size_bytes"] == os.path.getsize(pdf_path)
    assert report["ocr_fingerprint"] == {"dpi": 150, "page_count": 1}


# ---- 9. CLI:必需参数缺失报错;给全参数产出报告文件 --------------------------


def test_cli_missing_required_arg_raises_systemexit():
    import pytest
    with pytest.raises(SystemExit):
        source_audit_main(["--src", "x.pdf", "--out", "y"])  # 缺 --work-dir/--stem


def test_cli_full_args_produces_report_file(tmp_path):
    out_dir = tmp_path / "out"
    work_dir_root = tmp_path / "work"
    pdf_path = str(tmp_path / "book.pdf")
    _make_pdf(pdf_path, [[("hello world", (72, 72))]])

    layout = resolve_layout("Book", str(out_dir), str(work_dir_root))
    _write_manifest(layout.work_dir, page_count=1)
    _write_page_res(
        layout.work_dir, 1,
        [{"block_label": "text", "block_content": "hello world",
          "block_bbox": [0, 0, 600, 800]}],
    )

    rc = source_audit_main([
        "--src", pdf_path,
        "--out", str(out_dir),
        "--work-dir", str(work_dir_root),
        "--stem", "Book",
    ])
    assert rc == 0
    assert os.path.exists(layout.source_audit_path)
    with open(layout.source_audit_path, encoding="utf-8") as f:
        report = json.load(f)
    assert report["schema_version"] == 2
    assert report["stem"] == "Book"
    assert report["adoption_source"] == "dry_run"
    # CLI 不知道自己跑在哪种路由下(那是 Task 9 编排的状态),born_digital_mode
    # 必须诚实写 unknown,不得编造。
    assert report["born_digital_mode"] == "unknown"


def test_cli_rejects_dry_run_adoption_flag_it_no_longer_has(tmp_path):
    # review 裁定 Important 2:--dry-run-adoption 是死 flag(本 CLI 唯一语义
    # 就是 dry-run,没有另一种模式可切换)——已删除。argparse 对未知参数报
    # SystemExit(2),证明这个开关真的不存在了,不是文档说了但代码没删。
    import pytest
    with pytest.raises(SystemExit):
        source_audit_main([
            "--src", "x.pdf", "--out", str(tmp_path / "out"),
            "--work-dir", str(tmp_path / "work"), "--stem", "Book",
            "--dry-run-adoption",
        ])


def test_route_b_v1_profile_frozen_by_owner_20260717():
    """Task 13 标定冻结锁(所有者 2026-07-17 批准)——改动任一值必须重走标定评审。"""
    from scripts.pipelines.textbooks.source_audit import (
        ROUTE_B_V1_THRESHOLDS, THRESHOLD_PROFILE_V1)
    t = ROUTE_B_V1_THRESHOLDS
    assert THRESHOLD_PROFILE_V1 == "route_b_v1"
    assert t.minimum_reliable_chars == 30
    assert t.maximum_bad_char_ratio == 0.02
    assert t.maximum_block_ned == 0.3
    assert t.minimum_char_recall == 0.8
    assert t.minimum_token_recall == 0.8
    assert t.minimum_numeric_token_recall == 0.9
    assert t.maximum_addition_ratio == 0.25
    assert t.maximum_repetition_score == 0.5
    assert t.minimum_single_column_sequence_ratio == 0.45
    assert t.maximum_samples_per_block == 3


def test_convert_production_profile_is_v1():
    from scripts.pipelines.textbooks import convert
    from scripts.pipelines.textbooks.source_audit import ROUTE_B_V1_THRESHOLDS
    assert convert.ROUTE_B_AUDIT_THRESHOLDS is ROUTE_B_V1_THRESHOLDS
