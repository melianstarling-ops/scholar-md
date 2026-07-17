"""Task 5:块级采信门 prose adoption 的红→绿测试。

全部 synthetic dict/字符串构造,无需 fitz/GPU。消费 source_audit.SourceWord
与归一化函数;构造 assign_source_words 的返回结构(assignments/block_labels/
geometry_unscorable)喂给 adopt_prose_blocks。
"""
import pytest

from scripts.pipelines.textbooks.source_audit import SourceWord
from scripts.pipelines.textbooks.prose_adoption import (
    AdoptionDecision,
    AdoptionThresholds,
    adopt_prose_blocks,
    apply_adoption,
    block_ned,
    build_adopted_text,
)


# ---------------------------------------------------------------------------
# 构造辅助
# ---------------------------------------------------------------------------


def _sw(text, line_no=0, word_no=0, block_no=0):
    return SourceWord(
        text=text, bbox=(0, 0, 1, 1), block_no=block_no, line_no=line_no, word_no=word_no
    )


def _words(texts, line_no=0):
    return [_sw(t, line_no=line_no, word_no=i) for i, t in enumerate(texts)]


def _block(label, content, bbox=(0, 0, 100, 100)):
    return {"block_label": label, "block_content": content, "block_bbox": list(bbox)}


def _assignment(assignments, block_labels, *, geometry_unscorable=False):
    return {
        "geometry_unscorable": geometry_unscorable,
        "adoption_forbidden": geometry_unscorable,
        "assignments": assignments,
        "block_labels": block_labels,
        "unassigned": [],
    }


def _thresholds(min_ratio=0.5, max_ratio=2.0, max_ned=0.3):
    return AdoptionThresholds(
        adoption_min_char_ratio=min_ratio,
        adoption_max_char_ratio=max_ratio,
        adoption_max_ned=max_ned,
    )


def _one(decisions, block_id):
    for d in decisions:
        if d.block_id == block_id:
            return d
    raise AssertionError(f"no decision for block {block_id}")


# ===========================================================================
# build_adopted_text
# ===========================================================================


def test_build_adopted_text_basic_single_space_join():
    words = _words(["Photosynthesis", "converts", "light", "energy"])
    assert build_adopted_text(words) == "Photosynthesis converts light energy"


def test_hyphen_continuation_merges_only_when_both_sides_lowercase_latin():
    # determin- + istic → deterministic(两侧均小写拉丁,合并去连字符)
    merged = build_adopted_text(
        [_sw("determin-", line_no=0, word_no=0), _sw("istic", line_no=1, word_no=0)]
    )
    assert merged == "deterministic"

    # X-ray 类:断字左侧大写 → 保留连字符,不合并、不插空格
    kept = build_adopted_text(
        [_sw("X-", line_no=0, word_no=0), _sw("ray", line_no=1, word_no=0)]
    )
    assert kept == "X-ray"

    # 右侧大写也不合并
    kept2 = build_adopted_text(
        [_sw("anti-", line_no=0, word_no=0), _sw("Communist", line_no=1, word_no=0)]
    )
    assert kept2 == "anti-Communist"


def test_build_adopted_text_uses_nfc_not_nfkc_keeps_superscript():
    # 10² 采信后仍是 10²(NFC),绝不折叠成 102(NFKC)
    words = _words(["Area", "is", "10²", "units"])
    out = build_adopted_text(words)
    assert out == "Area is 10² units"
    assert "10²" in out
    assert "102" not in out


def test_build_adopted_text_orders_by_line_then_word_regardless_of_input_order():
    ordered = [
        _sw("first", line_no=0, word_no=0),
        _sw("second", line_no=0, word_no=1),
        _sw("third", line_no=1, word_no=0),
        _sw("fourth", line_no=1, word_no=1),
    ]
    expected = "first second third fourth"
    # 打乱输入顺序,结果不变
    shuffled = [ordered[2], ordered[0], ordered[3], ordered[1]]
    assert build_adopted_text(ordered) == expected
    assert build_adopted_text(shuffled) == expected


# ===========================================================================
# block_ned
# ===========================================================================


def test_block_ned_empty_vs_empty_is_zero():
    assert block_ned("", "") == 0.0


def test_block_ned_completely_different_near_one():
    assert block_ned("abcdef", "zyxwvu") == pytest.approx(1.0)


def test_block_ned_small_difference_small_value():
    # hello vs hallo:1 处替换 / 5 = 0.2
    assert block_ned("hello", "hallo") == pytest.approx(0.2)


def test_block_ned_one_side_empty_is_one():
    assert block_ned("abc", "") == pytest.approx(1.0)
    assert block_ned("", "abc") == pytest.approx(1.0)


def test_block_ned_unicode_by_codepoint():
    # 单个 astral / CJK 码点当作一个单位比较
    assert block_ned("北京", "北京") == 0.0
    assert block_ned("北京", "南京") == pytest.approx(0.5)


# ===========================================================================
# adopt_prose_blocks:六道采信门
# ===========================================================================


def test_clean_prose_block_is_adopted():
    """门 1-6 全过:干净正文块 + 健康源 words → 采信 source_text。"""
    src = _words(["Photosynthesis", "converts", "light", "energy"])
    blocks = [_block("text", "Photosynthesis converts light energy")]
    assignment = _assignment({0: src}, {0: "text"})
    decisions = adopt_prose_blocks(
        blocks, assignment, {}, geometry_ok=True, thresholds=_thresholds()
    )
    d = _one(decisions, 0)
    assert d.content_source == "source_text"
    assert d.reasons == []
    assert d.adopted_text == "Photosynthesis converts light energy"
    assert d.block_ned is not None and d.block_ned <= 0.3


def test_math_delimiter_in_ocr_forces_ocr_fallback():
    """门 4:OCR block_content 含 $...$ → 整块回退 math_in_prose_block。"""
    src = _words(["The", "total", "energy", "equals", "this"])
    blocks = [_block("text", "The total energy $E$ equals this")]
    assignment = _assignment({0: src}, {0: "text"})
    d = _one(
        adopt_prose_blocks(blocks, assignment, {}, geometry_ok=True, thresholds=_thresholds()),
        0,
    )
    assert d.content_source == "ocr"
    assert d.reasons == ["math_in_prose_block"]
    assert d.adopted_text is None


def test_latex_command_trace_in_ocr_forces_ocr_fallback():
    src = _words(["the", "value", "alpha", "here", "indeed"])
    blocks = [_block("text", r"the value \alpha here indeed")]
    assignment = _assignment({0: src}, {0: "text"})
    d = _one(
        adopt_prose_blocks(blocks, assignment, {}, geometry_ok=True, thresholds=_thresholds()),
        0,
    )
    assert d.content_source == "ocr"
    assert d.reasons == ["math_in_prose_block"]


def test_math_symbol_codepoint_in_source_words_forces_fallback():
    # 源 words 含数学运算符区码点(∑ U+2211)→ math_in_prose_block
    src = [_sw("The", word_no=0), _sw("∑", word_no=1), _sw("terms", word_no=2)]
    blocks = [_block("text", "The sum terms")]
    assignment = _assignment({0: src}, {0: "text"})
    d = _one(
        adopt_prose_blocks(blocks, assignment, {}, geometry_ok=True, thresholds=_thresholds()),
        0,
    )
    assert d.content_source == "ocr"
    assert d.reasons == ["math_in_prose_block"]


def test_pua_source_chars_force_fallback():
    """门 3:归属源 words 含 PUA → bad_source_chars。"""
    src = [_sw("clean", word_no=0), _sw("word", word_no=1), _sw("here", word_no=2)]
    blocks = [_block("text", "clean word here")]
    assignment = _assignment({0: src}, {0: "text"})
    d = _one(
        adopt_prose_blocks(blocks, assignment, {}, geometry_ok=True, thresholds=_thresholds()),
        0,
    )
    assert d.content_source == "ocr"
    assert d.reasons == ["bad_source_chars"]


def test_control_char_in_source_forces_fallback():
    src = [_sw("clean", word_no=0), _sw("wo\x07rd", word_no=1), _sw("here", word_no=2)]
    blocks = [_block("text", "clean word here")]
    assignment = _assignment({0: src}, {0: "text"})
    d = _one(
        adopt_prose_blocks(blocks, assignment, {}, geometry_ok=True, thresholds=_thresholds()),
        0,
    )
    assert d.content_source == "ocr"
    assert d.reasons == ["bad_source_chars"]


def test_char_ratio_too_low_half_block_mapping():
    """门 2:源字符量/OCR 远低于下限(半块映射)→ char_ratio_out_of_range。"""
    src = _words(["Hi"])
    blocks = [
        _block("text", "This is a very long paragraph with many words and characters here")
    ]
    assignment = _assignment({0: src}, {0: "text"})
    d = _one(
        adopt_prose_blocks(blocks, assignment, {}, geometry_ok=True, thresholds=_thresholds()),
        0,
    )
    assert d.content_source == "ocr"
    assert d.reasons == ["char_ratio_out_of_range"]


def test_char_ratio_too_high_smeared_block_mapping():
    src = _words(
        ["This", "is", "a", "very", "long", "source", "paragraph", "with", "many", "words"]
    )
    blocks = [_block("text", "x")]
    assignment = _assignment({0: src}, {0: "text"})
    d = _one(
        adopt_prose_blocks(blocks, assignment, {}, geometry_ok=True, thresholds=_thresholds()),
        0,
    )
    assert d.content_source == "ocr"
    assert d.reasons == ["char_ratio_out_of_range"]


def test_empty_assigned_words_is_char_ratio_out_of_range():
    blocks = [_block("text", "some ocr content here")]
    assignment = _assignment({}, {0: "text"})  # 无归属 words
    d = _one(
        adopt_prose_blocks(blocks, assignment, {}, geometry_ok=True, thresholds=_thresholds()),
        0,
    )
    assert d.content_source == "ocr"
    assert d.reasons == ["char_ratio_out_of_range"]


def test_reverse_reconciliation_disagreement_forces_fallback():
    """门 6:采信文本与 OCR 文本 NED 超上限 → adoption_disagreement。"""
    src = _words(["alpha", "beta", "gamma", "delta"])
    blocks = [_block("text", "zulu yankee xray whiskey")]
    assignment = _assignment({0: src}, {0: "text"})
    d = _one(
        adopt_prose_blocks(
            blocks, assignment, {}, geometry_ok=True, thresholds=_thresholds(max_ned=0.3)
        ),
        0,
    )
    assert d.content_source == "ocr"
    assert d.reasons == ["adoption_disagreement"]
    assert d.block_ned is not None and d.block_ned > 0.3


def test_unreconstructable_when_content_normalizes_empty():
    """门 5:归属 words 非空但内容归一化后为空 → unreconstructable。

    源 word 仅含空白,ocr 非空;放宽下限阈值到 0.0 让门 2 通过,门 5 才可达。
    """
    src = [_sw("   ", word_no=0)]
    blocks = [_block("text", "real ocr content")]
    assignment = _assignment({0: src}, {0: "text"})
    d = _one(
        adopt_prose_blocks(
            blocks, assignment, {}, geometry_ok=True,
            thresholds=_thresholds(min_ratio=0.0, max_ratio=2.0),
        ),
        0,
    )
    assert d.content_source == "ocr"
    assert d.reasons == ["unreconstructable"]


def test_geometry_not_ok_forces_whole_page_fallback():
    """门 1:geometry_ok=False → 全页回退 geometry_unscorable。"""
    src = _words(["perfectly", "healthy", "source", "text"])
    blocks = [_block("text", "perfectly healthy source text")]
    assignment = _assignment({0: src}, {0: "text"})
    d = _one(
        adopt_prose_blocks(blocks, assignment, {}, geometry_ok=False, thresholds=_thresholds()),
        0,
    )
    assert d.content_source == "ocr"
    assert d.reasons == ["geometry_unscorable"]


def test_geometry_unscorable_in_assignment_forces_fallback():
    src = _words(["perfectly", "healthy", "source", "text"])
    blocks = [_block("text", "perfectly healthy source text")]
    assignment = _assignment({0: src}, {0: "text"}, geometry_unscorable=True)
    d = _one(
        adopt_prose_blocks(blocks, assignment, {}, geometry_ok=True, thresholds=_thresholds()),
        0,
    )
    assert d.content_source == "ocr"
    assert d.reasons == ["geometry_unscorable"]


def test_non_whitelist_label_never_adopted_however_healthy():
    """非白名单 label(table/formula/header 等)绝不采信 label_not_adoptable。"""
    src = _words(["perfectly", "healthy", "matching", "text"])
    for label in ("table", "display_formula", "header", "image", "number", "footer_image"):
        blocks = [_block(label, "perfectly healthy matching text")]
        assignment = _assignment({0: src}, {0: label})
        d = _one(
            adopt_prose_blocks(
                blocks, assignment, {}, geometry_ok=True, thresholds=_thresholds()
            ),
            0,
        )
        assert d.content_source == "ocr", label
        assert d.reasons == ["label_not_adoptable"], label
        assert d.block_ned is None
        assert d.adopted_text is None


def test_all_whitelist_labels_are_adoptable():
    whitelist = [
        "text", "abstract", "reference_content", "content",
        "paragraph_title", "doc_title", "figure_title", "footnote",
    ]
    for label in whitelist:
        src = _words(["healthy", "matching", "prose", "content"])
        blocks = [_block(label, "healthy matching prose content")]
        assignment = _assignment({0: src}, {0: label})
        d = _one(
            adopt_prose_blocks(
                blocks, assignment, {}, geometry_ok=True, thresholds=_thresholds()
            ),
            0,
        )
        assert d.content_source == "source_text", label
        assert d.reasons == [], label


def test_ocr_error_is_corrected_by_adopted_source_text():
    """OCR 正文数字错 + 源 words 正确 + NED 仍低于上限 → 采信后内容正确。"""
    src = _words(["The", "value", "is", "24", "units"])
    blocks = [_block("text", "The value is 42 units")]  # OCR 把 24 认成 42
    assignment = _assignment({0: src}, {0: "text"})
    decisions = adopt_prose_blocks(
        blocks, assignment, {}, geometry_ok=True, thresholds=_thresholds()
    )
    d = _one(decisions, 0)
    assert d.content_source == "source_text"
    assert d.adopted_text == "The value is 24 units"
    applied = apply_adoption(blocks, decisions)
    assert applied[0]["block_content"] == "The value is 24 units"


def test_full_provenance_every_block_gets_a_decision():
    src_ok = _words(["clean", "adopted", "prose", "here"])
    blocks = [
        _block("text", "clean adopted prose here"),
        _block("table", "| a | b |"),
        _block("display_formula", "E = mc^2"),
    ]
    assignment = _assignment(
        {0: src_ok}, {0: "text", 1: "table", 2: "display_formula"}
    )
    decisions = adopt_prose_blocks(
        blocks, assignment, {}, geometry_ok=True, thresholds=_thresholds()
    )
    assert len(decisions) == 3
    assert _one(decisions, 0).content_source == "source_text"
    assert _one(decisions, 1).reasons == ["label_not_adoptable"]
    assert _one(decisions, 2).reasons == ["label_not_adoptable"]


# ===========================================================================
# apply_adoption:原子替换 / 幂等 / 不 mutate
# ===========================================================================


def test_apply_adoption_replaces_only_adopted_blocks_and_keeps_others():
    blocks = [
        _block("text", "old ocr one"),
        _block("table", "| keep | me |"),
    ]
    decisions = [
        AdoptionDecision(
            block_id=0, content_source="source_text", reasons=[],
            block_ned=0.0, adopted_text="new adopted one",
        ),
        AdoptionDecision(
            block_id=1, content_source="ocr", reasons=["label_not_adoptable"],
            block_ned=None, adopted_text=None,
        ),
    ]
    applied = apply_adoption(blocks, decisions)
    assert applied[0]["block_content"] == "new adopted one"
    assert applied[1]["block_content"] == "| keep | me |"
    # 其余字段原样
    assert applied[0]["block_label"] == "text"
    assert applied[0]["block_bbox"] == blocks[0]["block_bbox"]


def test_apply_adoption_does_not_mutate_input():
    blocks = [_block("text", "original content")]
    original_snapshot = dict(blocks[0])
    decisions = [
        AdoptionDecision(
            block_id=0, content_source="source_text", reasons=[],
            block_ned=0.0, adopted_text="replaced content",
        )
    ]
    applied = apply_adoption(blocks, decisions)
    # 原列表对象、原 dict 不变
    assert blocks[0] == original_snapshot
    assert blocks[0]["block_content"] == "original content"
    assert applied is not blocks
    assert applied[0] is not blocks[0]
    assert applied[0]["block_content"] == "replaced content"


def test_apply_adoption_is_idempotent():
    blocks = [_block("text", "old content")]
    decisions = [
        AdoptionDecision(
            block_id=0, content_source="source_text", reasons=[],
            block_ned=0.0, adopted_text="new content",
        )
    ]
    once = apply_adoption(blocks, decisions)
    twice = apply_adoption(once, decisions)
    assert once[0]["block_content"] == "new content"
    assert twice[0]["block_content"] == "new content"
    assert once == twice
