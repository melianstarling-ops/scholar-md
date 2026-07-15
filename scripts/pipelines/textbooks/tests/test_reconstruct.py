import json
from pathlib import Path

from scripts.pipelines.textbooks.reconstruct import (
    reconstruct_fragments,
    reconstruct_markdown,
    restore_emphasis_dots,
    sanitize_latex,
    wrap_cjk_in_text,
)

FIX = Path(__file__).parent / "fixtures"


def test_drops_order_none_blocks():
    # header/number(page) 的 block_order 为 None,应被剔除
    blocks = [
        {"block_label": "header", "block_content": "PAGE HEADER", "block_order": None},
        {"block_label": "number", "block_content": "186", "block_order": None},
        {"block_label": "text", "block_content": "Body text.", "block_order": 1},
    ]
    md, _ = reconstruct_markdown(blocks)
    assert "PAGE HEADER" not in md
    assert "186" not in md
    assert "Body text." in md


def test_sorts_by_order():
    blocks = [
        {"block_label": "text", "block_content": "second", "block_order": 2},
        {"block_label": "text", "block_content": "first", "block_order": 1},
    ]
    md, _ = reconstruct_markdown(blocks)
    assert md.index("first") < md.index("second")


def test_paragraph_title_becomes_heading():
    blocks = [{"block_label": "paragraph_title", "block_content": "第五章 静磁学", "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert md.strip().startswith("## 第五章 静磁学")


def test_display_formula_binds_adjacent_number():
    blocks = [
        {"block_label": "display_formula",
         "block_content": r" $$ \mathbf{N}=\boldsymbol{\mu}\times\mathbf{B} $$ ", "block_order": 4},
        {"block_label": "formula_number", "block_content": "(5.1)", "block_order": 5},
    ]
    md, _ = reconstruct_markdown(blocks)
    assert r"\tag{5.1}" in md          # 编号并入公式
    assert md.count("$$") == 2         # 只一个公式块
    assert "\n(5.1)" not in md         # 编号不再单独成行


def test_formula_number_without_formula_kept_inline():
    # 落单的 formula_number(前面不是公式) 保留为文本,不丢
    blocks = [
        {"block_label": "text", "block_content": "see below", "block_order": 1},
        {"block_label": "formula_number", "block_content": "(9.9)", "block_order": 2},
    ]
    md, _ = reconstruct_markdown(blocks)
    assert "(9.9)" in md


def test_restore_emphasis_dots():
    s = r"根本差别：$ \underset{\cdot}{没}\underset{\cdot}{有}\underset{\cdot}{自}\underset{\cdot}{由} $。"
    out = restore_emphasis_dots(s)
    assert out == "根本差别：没有自由。"


def test_golden_jackson_chinese():
    blocks = json.loads((FIX / "jackson_p200_res.json").read_text(encoding="utf-8"))["parsing_res_list"]
    md, _ = reconstruct_markdown(blocks)
    assert md.strip().startswith("## 第五章")          # 标题
    assert r"\mathbf{N}=\boldsymbol{\mu}\times\mathbf{B}" in md  # 公式
    assert r"\tag{5.1}" in md                          # 编号绑定
    assert "186" not in md                             # 页码(order=None)剔除
    assert "underset" not in md                        # 着重号已还原


def test_golden_paul_english():
    blocks = json.loads((FIX / "paul_p200_res.json").read_text(encoding="utf-8"))["parsing_res_list"]
    md, _ = reconstruct_markdown(blocks)
    assert r"\tag{5.30}" in md
    assert r"\tag{5.33}" in md                          # 编号全部绑回(md 端到端曾丢失的)
    assert "THE PER-UNIT-LENGTH" not in md              # 页眉(order=None)剔除
    assert "178" not in md                              # 页码剔除
    assert r"\displaylimits" not in md                  # KaTeX 不兼容命令已清洗(L-T16)


def test_reference_content_kept():
    blocks = [{"block_label": "reference_content",
               "block_content": "[1] S. Ramo, Fields and Waves, Wiley, 1984.", "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert "[1] S. Ramo, Fields and Waves, Wiley, 1984." in md


def test_abstract_kept():
    blocks = [{"block_label": "abstract", "block_content": "This is the second edition.", "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert "This is the second edition." in md


def test_content_preserves_line_breaks():
    # content(目录/前言页码列表)逐行有意义,须保留换行,不能挤成一段
    blocks = [{"block_label": "content", "block_content": "Preface xvii\nIntroduction 1", "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert "Preface xvii  \nIntroduction 1" in md


def test_algorithm_wrapped_in_code_fence():
    blocks = [{"block_label": "algorithm",
               "block_content": "EXAMPLE\nVS 1 0 PULSE(0 5 0 1N 1N 4N 10N)\n.END", "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert "```\nEXAMPLE\nVS 1 0 PULSE(0 5 0 1N 1N 4N 10N)\n.END\n```" in md


def test_doc_title_without_paragraph_title_sibling_is_cover_metadata():
    # 无 paragraph_title 兄弟块 → 封面元信息,不当标题
    blocks = [{"block_label": "doc_title", "block_content": "SECOND EDITION\nCLAYTON R. PAUL", "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert "## SECOND EDITION" not in md
    assert "SECOND EDITION" in md
    assert "CLAYTON R. PAUL" in md


def test_doc_title_with_paragraph_title_sibling_becomes_heading():
    # 同页有 paragraph_title 兄弟块(章节序号) → doc_title 是被误标的正文章节标题
    blocks = [
        {"block_label": "paragraph_title", "block_content": "1", "block_order": 1},
        {"block_label": "doc_title", "block_content": "INTRODUCTION", "block_order": 2},
    ]
    md, _ = reconstruct_markdown(blocks)
    assert "## INTRODUCTION" in md


def test_unknown_label_falls_back_to_plain_content():
    # 兜底 else:未来出现新 label 时也不能静默丢失
    blocks = [{"block_label": "some_future_label", "block_content": "future content", "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert "future content" in md


def test_unknown_label_warns_to_stderr(capsys):
    # 兜底分支产出的内容不受 selfcheck 校验(反正会出现在 md 里),唯一能暴露给人看的
    # 信号只有这条告警——必须点名 label,方便定位是哪种新 label 冒出来了
    blocks = [{"block_label": "some_future_label", "block_content": "future content", "block_order": 1}]
    reconstruct_markdown(blocks)
    err = capsys.readouterr().err
    assert "some_future_label" in err


def test_algorithm_fence_escapes_embedded_triple_backticks():
    # content 内部若已含三个反引号(未来其他学科教材代码块可能出现),三反引号围栏
    # 会被提前截断;围栏长度要比 content 内最长的连续反引号串多一个
    content = "before\n```\nnested\n```\nafter"
    blocks = [{"block_label": "algorithm", "block_content": content, "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert f"````\n{content}\n````" in md


def test_sanitize_latex_strips_displaylimits():
    # \displaylimits 在 $$ 里冗余,删除;KaTeX 才能渲染
    assert sanitize_latex(r"\int\displaylimits_{a}^{b}") == r"\int_{a}^{b}"


def test_sanitize_latex_preserves_displaystyle():
    # 负向边界:不得误伤前缀相近的 \displaystyle
    assert sanitize_latex(r"\displaystyle\int x") == r"\displaystyle\int x"


def test_sanitize_latex_collapses_adjacent_double_subscript():
    # 同一节点连续两个下标 _{A}_{B} 是 KaTeX 硬报错(double subscript);兜底:单行合并
    assert sanitize_latex(r"x_{A}_{B}") == r"x_{A\ B}"


def test_sanitize_latex_collapses_triple_subscript():
    # 连续三下标也应全部合并进一个下标(循环直到稳定)
    assert sanitize_latex(r"x_{A}_{B}_{C}") == r"x_{A\ B\ C}"


def test_sanitize_latex_leaves_nested_single_subscript_untouched():
    # §3 假阳性陷阱:多层嵌套花括号后接一个正常单下标,不是双下标,绝不能动
    s = r"\overrightarrow{\mathcal{H}}_{\mathrm{t}}"
    assert sanitize_latex(s) == s


def test_sanitize_latex_leaves_sub_then_super_untouched():
    # 下标后接上标是合法的(同一 base 的 sub+super),不得误合并
    s = r"x_{A}^{B}"
    assert sanitize_latex(s) == s


def test_sanitize_latex_collapses_adjacent_double_superscript():
    # 同一节点连续两个上标 ^{A}^{B} 是 KaTeX 硬报错(double superscript);兜底:单行合并
    assert sanitize_latex(r"\omega^{\prime}^{2}") == r"\omega^{\prime 2}"


def test_sanitize_latex_collapses_triple_superscript():
    # 连续三上标也应全部合并进一个上标(循环直到稳定)
    assert sanitize_latex(r"x^{A}^{B}^{C}") == r"x^{A\ B\ C}"


def test_sanitize_latex_leaves_prime_then_super_untouched():
    # x'^{2} 本身可被 KaTeX 渲染,不是相邻 braced double superscript
    s = r"x'^{2}"
    assert sanitize_latex(s) == s


def test_sanitize_latex_fixes_real_1_3a_double_subscript():
    # 真实语料 1.3a 的畸形片段:underbrace 收尾 } 后被连下标两次
    frag = (r"\underbrace{\nabla_{z}\times\overrightarrow{\mathcal{E}}_{\mathrm{t}}}"
            r"_{in\text{the}}_{\substack{\text{transverse}\\ \text{plane}}}")
    out = sanitize_latex(frag)
    # 非法的相邻双下标 junction 已消除
    assert r"}_{in\text{the}}_{\substack" not in out
    # 两段下标内容都完整保留(零丢失),合并进同一个 _{...}
    assert r"_{in\text{the}\ \substack{\text{transverse}\\ \text{plane}}}" in out
    # underbrace 主体不受影响
    assert r"\underbrace{\nabla_{z}\times\overrightarrow{\mathcal{E}}_{\mathrm{t}}}" in out


def test_sanitize_latex_collapses_chained_atop_to_substack():
    # 一个 group 里 2+ 个 \atop 是 KaTeX 硬报错(only one infix per group);合并进 \substack
    assert sanitize_latex(r"_{in the\atop transverse\atop plane}") == \
        r"_{\substack{in the\\transverse\\plane}}"


def test_sanitize_latex_leaves_single_atop_untouched():
    # 单个 \atop 是合法的 KaTeX 堆叠(一个 infix),不得误改
    assert sanitize_latex(r"{a\atop b}") == r"{a\atop b}"


def test_sanitize_latex_atop_does_not_touch_atopwithdelims():
    # \atopwithdelims 是另一个命令,\atop 前缀匹配不得误伤
    s = r"{a\atopwithdelims() b}"
    assert sanitize_latex(s) == s


def test_sanitize_latex_fixes_cdotd_glue():
    # OCR 把 \cdot d(点积+微分 d) 粘成未定义控制序列 \cdotd
    assert sanitize_latex(r"\mathcal{H}\cdotd\vec{l}") == r"\mathcal{H}\cdot d\vec{l}"


def test_sanitize_latex_cdotd_does_not_touch_cdot():
    # 负向边界:合法的 \cdot 不得被 \cdotd 规则误伤
    assert sanitize_latex(r"a\cdot b") == r"a\cdot b"


def test_sanitize_latex_decodes_known_entities_inside_math():
    assert sanitize_latex(r"\varepsilon&#x27;_{r}") == r"\varepsilon'_{r}"
    assert sanitize_latex(r"R_{S}&lt;Z_{C}") == r"R_{S}<Z_{C}"
    assert sanitize_latex(r"R_{L}&gt;Z_{C}") == r"R_{L}>Z_{C}"


def test_sanitize_latex_strips_boldmath_declaration():
    # PaddleOCR-VL emits LaTeX's text-mode \boldmath declaration inside math.
    # KaTeX does not implement it; dropping the declaration preserves payload text.
    assert sanitize_latex(r"\mathrm{\boldmath~G~}") == r"\mathrm{~G~}"


def test_sanitize_latex_drops_unmatched_closing_brace():
    # OCR sometimes appends one impossible top-level "}" after an otherwise
    # balanced expression; removing only the unmatched closer preserves payload.
    assert sanitize_latex(r"\mathrm{\frac{\partial}{\partial t}}}\Leftrightarrow j\omega") == \
        r"\mathrm{\frac{\partial}{\partial t}}\Leftrightarrow j\omega"


def test_sanitize_latex_keeps_balanced_nested_braces():
    s = r"\hat{\mathrm{~\bf~P~}}(\hat{\mathrm{~\bf~Z~}})"
    assert sanitize_latex(s) == s


def test_sanitize_latex_removes_literal_dollar_markers_inside_formula():
    # Once we are inside $$...$$, literal dollar markers from figure labels make
    # KaTeX hard-fail. Drop the marker only, keeping the visible label text.
    assert sanitize_latex(r"\boldsymbol{h}(t)\ $ a)") == r"\boldsymbol{h}(t)\  a)"


def test_sanitize_latex_drops_empty_left_right_delimiters():
    # OCR sometimes wraps an ordinary factor as \left.f(t)dt-\cdots\right.,
    # which leaves KaTeX in a delimiter state across an array row break.
    assert sanitize_latex(r"\frac{t^{2}}{2!}\left.f(t)d t-\cdots\right.") == \
        r"\frac{t^{2}}{2!}f(t)d t-\cdots"


def test_sanitize_latex_collapses_redundant_right_delimiter_before_bracket_close():
    # OCR can split one visible bracket pair across array rows by closing with
    # \right. and reopening with \left.; KaTeX then sees the later \right\} as
    # unmatched. Preserve the visible delimiters and remove only that row split.
    assert sanitize_latex(
        r"\left[E(t)\right.}\\ &{}&{\left.-E(t)\right]\right\}"
    ) == r"[E(t)\\ &{}&{-E(t)]\}"


def test_sanitize_latex_closes_unbalanced_array_row_before_separator():
    assert sanitize_latex(
        r"\begin{array}{r c l}{}&{=}&{\displaystyle\int\limits_{0}^{\infty}{f(t)d t-s\int\limits_{0}^{\infty}{t f(t)d t}+s^{2}\int\limits_{0}^{\infty}{\frac{t^{2}}{2!}f(t)d t-\cdots}}\\ {}&{=}&{k_{0}}\end{array}"
    ) == r"\begin{array}{r c l}{}&{=}&{\displaystyle\int\limits_{0}^{\infty}{f(t)d t-s\int\limits_{0}^{\infty}{t f(t)d t}+s^{2}\int\limits_{0}^{\infty}{\frac{t^{2}}{2!}f(t)d t-\cdots}}}\\ {}&{=}&{k_{0}}\end{array}"


def test_sanitize_latex_downgrades_split_array_braces():
    assert sanitize_latex(
        r"\left\{a\right.}\\ &{}&{\left.+b\right.}\\ &{}&{c\right\}"
    ) == r"\{a\\ &{}&{+b}\\ &{}&{c\}"


def test_sanitize_latex_downgrades_orphan_right_delimiter_after_invisible_left():
    assert sanitize_latex(
        r"\left\{a\right.\\&\left.\frac{b}{c}\right\}"
    ) == r"\left\{a\right.\\&\frac{b}{c}\}"


def test_sanitize_latex_removes_subscript_from_spacing_command():
    assert sanitize_latex(r"x\\&\quad_{(12)}\end{aligned}") == \
        r"x\\&\quad{}_{(12)}\end{aligned}"


def test_sanitize_latex_fixes_malformed_bmatrix_command():
    assert sanitize_latex(
        r"\mathrm{~\bmatrix{~\widehat{\mathbf{Z}}_{C}(s)\mathbf{~}}}\\ \end{bmatrix}_{i k}"
    ) == r"\left[\widehat{\mathbf{Z}}_{C}(s)\right]_{i k}"


def test_sanitize_latex_moves_orphan_aprime_into_integral_limit():
    assert sanitize_latex(
        r"a^{\prime}\\ \int\limits_{a}^{\overrightarrow{\hat{E}}}_{\mathrm{t}}^{\mathrm{inc}}\cdot d\overrightarrow{l}"
    ) == \
        r"\int\limits_{a}^{a^{\prime}}\overrightarrow{\hat{E}}_{\mathrm{t}}^{\mathrm{inc}}\cdot d\overrightarrow{l}"


def test_sanitize_latex_unwraps_malformed_integral_frac_run():
    assert sanitize_latex(
        r"\frac{\displaystyle\int\displaystyle\int}\vec{\mathcal{H}}_{\mathrm{t}}"
    ) == r"{\displaystyle\int\displaystyle\int}\vec{\mathcal{H}}_{\mathrm{t}}"


def test_sanitize_latex_inserts_missing_endarray_before_right_bracket():
    assert sanitize_latex(
        r"\left[\begin{array}{cc}\vdots&\vdots\\a&b\right]_{T}"
    ) == r"\left[\begin{array}{cc}\vdots&\vdots\\a&b\end{array}\right]_{T}"


def test_sanitize_latex_expands_array_colspec_to_seen_columns():
    assert sanitize_latex(
        r"\begin{array}{cc}a&b&c\\d&e&f\end{array}"
    ) == r"\begin{array}{ccc}a&b&c\\d&e&f\end{array}"


def test_table_pass_through_sanitizes_inline_math_entities():
    blocks = [{
        "block_label": "table",
        "block_content": r"<table><tr><td>$ R_{S}&lt;Z_{C} $</td><td>$ \varepsilon&#x27;_{r} $</td></tr></table>",
        "block_order": None,
        "block_bbox": [0, 0, 10, 10],
        "block_id": 3,
    }]
    md, _ = reconstruct_markdown(blocks)
    assert r"$ R_{S}<Z_{C} $" in md
    assert r"$ \varepsilon'_{r} $" in md


def test_reconstruct_prefers_adjacent_formula_number_over_embedded_tag():
    # OCR sometimes includes a damaged \tag in the formula body and also emits a
    # cleaner adjacent formula_number block. The final display math must have
    # exactly one tag, otherwise KaTeX raises "Multiple \tag".
    blocks = [
        {"block_label": "display_formula", "block_content": r"$$ x=1\tag{8.1698} $$", "block_order": 1},
        {"block_label": "formula_number", "block_content": "(8.169a)", "block_order": 2},
    ]
    md, _ = reconstruct_markdown(blocks)
    assert r"\tag{8.1698}" not in md
    assert md.count(r"\tag{") == 1
    assert r"\tag{8.169a}" in md


def test_reconstruct_fixes_1_3b_chained_atop_end_to_end():
    # 真实 1.3b:下标里链式双 \atop
    body = (r"$$ \left(\nabla_{\mathrm{t}}+\nabla_{z}\right)\times\overrightarrow{\mathcal{H}}_{\mathrm{t}}"
            r"=\underbrace{\nabla_{\mathrm{t}}\times\overrightarrow{\mathcal{H}}_{\mathrm{t}}}_{z\text{ directed}}"
            r"+\underbrace{\nabla_{z}\times\overrightarrow{\mathcal{H}}_{\mathrm{t}}}"
            r"_{in the\atop transverse\atop plane}"
            r"=\sigma\overrightarrow{\mathcal{E}}_{\mathrm{t}}+\varepsilon\frac{\partial\overrightarrow{\mathcal{E}}_{\mathrm{t}}}{\partial t} $$")
    blocks = [{"block_label": "display_formula", "block_content": body, "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert r"\atop" not in md                       # 链式 atop 已消除
    assert r"\substack{in the\\transverse\\plane}" in md


def test_reconstruct_fixes_1_3a_double_subscript_end_to_end():
    # 真实 1.3a display_formula 块经 reconstruct 后不应残留相邻双下标
    body = (r"$$ \left(\nabla_{\mathrm{t}}+\nabla_{z}\right)\times\overrightarrow{\mathcal{E}}_{\mathrm{t}}"
            r"=\underbrace{\nabla_{\mathrm{t}}\times\overrightarrow{\mathcal{E}}_{\mathrm{t}}}_{z\text{ directed}}"
            r"+\underbrace{\nabla_{z}\times\overrightarrow{\mathcal{E}}_{\mathrm{t}}}"
            r"_{in\text{the}}_{\substack{\text{transverse}\\ \text{plane}}}"
            r"=-\mu\frac{\partial\overrightarrow{\mathcal{H}}_{\mathrm{t}}}{\partial t} $$")
    blocks = [{"block_label": "display_formula", "block_content": body, "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert r"}_{in\text{the}}_{\substack" not in md
    assert r"\substack{\text{transverse}\\ \text{plane}}" in md   # 内容仍在


def test_reconstruct_cleans_displaylimits_in_formula():
    blocks = [{"block_label": "display_formula",
               "block_content": r"$$ \int\displaylimits_{S} \mathbf{B} $$", "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert r"\displaylimits" not in md
    assert r"\int_{S}" in md


def test_returns_tuple_of_md_and_warnings():
    blocks = [{"block_label": "text", "block_content": "hi", "block_order": 1}]
    result = reconstruct_markdown(blocks)
    assert isinstance(result, tuple) and len(result) == 2
    md, warnings = result
    assert "hi" in md
    assert warnings == []


def test_passthrough_label_inserted_by_y0():
    # footnote(order=None,y0=200)应插在 y0=100 的正文和 y0=300 的正文之间
    blocks = [
        {"block_label": "text", "block_content": "first", "block_order": 1,
         "block_bbox": [0, 100, 10, 110]},
        {"block_label": "footnote", "block_content": "note here", "block_order": None,
         "block_bbox": [0, 200, 10, 210]},
        {"block_label": "text", "block_content": "second", "block_order": 2,
         "block_bbox": [0, 300, 10, 310]},
    ]
    md, warnings = reconstruct_markdown(blocks)
    assert md.index("first") < md.index("note here") < md.index("second")
    assert warnings == []


def test_tie_y0_extra_goes_after_ordered_fragment():
    # spec §3 反例:extra y0=300、ordered y0 序列 [100,300,500]
    # 权威语义"插在第一个 y0>300 的片段之前" → extra 排在 300 之后、500 之前
    blocks = [
        {"block_label": "text", "block_content": "at100", "block_order": 1,
         "block_bbox": [0, 100, 10, 110]},
        {"block_label": "text", "block_content": "at300", "block_order": 2,
         "block_bbox": [0, 300, 10, 310]},
        {"block_label": "text", "block_content": "at500", "block_order": 3,
         "block_bbox": [0, 500, 10, 510]},
        {"block_label": "footnote", "block_content": "tied_extra", "block_order": None,
         "block_bbox": [0, 300, 10, 305]},
    ]
    md, _ = reconstruct_markdown(blocks)
    assert md.index("at300") < md.index("tied_extra") < md.index("at500")


def test_pure_extras_page_degenerates_to_extras_only():
    # 零 ordered 片段(纯图/纯脚注页):归并退化为全部 extra 按 y0 输出
    blocks = [
        {"block_label": "footnote", "block_content": "only content", "block_order": None,
         "block_bbox": [0, 50, 10, 60]},
    ]
    md, warnings = reconstruct_markdown(blocks)
    assert "only content" in md
    assert md.strip() != ""
    assert warnings == []


def test_extras_without_bbox_appended_at_page_tail_in_original_order():
    blocks = [
        {"block_label": "text", "block_content": "body", "block_order": 1,
         "block_bbox": [0, 100, 10, 110]},
        {"block_label": "footnote", "block_content": "no_bbox_a", "block_order": None,
         "block_bbox": None},
        {"block_label": "footnote", "block_content": "no_bbox_b", "block_order": None,
         "block_bbox": None},
    ]
    md, _ = reconstruct_markdown(blocks)
    assert md.index("body") < md.index("no_bbox_a") < md.index("no_bbox_b")


def test_passthrough_empty_content_silently_skipped():
    blocks = [
        {"block_label": "text", "block_content": "body", "block_order": 1,
         "block_bbox": [0, 100, 10, 110]},
        {"block_label": "figure_title", "block_content": "", "block_order": None,
         "block_bbox": [0, 50, 10, 60]},
    ]
    md, warnings = reconstruct_markdown(blocks)
    assert md.strip() == "body"
    assert warnings == []


def test_known_noise_labels_silently_dropped():
    blocks = [
        {"block_label": "text", "block_content": "body", "block_order": 1,
         "block_bbox": [0, 100, 10, 110]},
        {"block_label": "header", "block_content": "RUNNING HEADER", "block_order": None,
         "block_bbox": [0, 0, 10, 10]},
        {"block_label": "number", "block_content": "42", "block_order": None,
         "block_bbox": [0, 900, 10, 910]},
        {"block_label": "header_image", "block_content": "", "block_order": None,
         "block_bbox": [0, 0, 10, 10]},
    ]
    md, warnings = reconstruct_markdown(blocks)
    assert "RUNNING HEADER" not in md
    assert "42" not in md
    assert warnings == []


def test_unknown_unordered_label_with_content_warns_and_drops():
    blocks = [
        {"block_label": "text", "block_content": "body", "block_order": 1,
         "block_bbox": [0, 100, 10, 110]},
        {"block_label": "mystery_label", "block_content": "surprise content",
         "block_order": None, "block_bbox": [0, 50, 10, 60]},
    ]
    md, warnings = reconstruct_markdown(blocks)
    assert "surprise content" not in md
    assert len(warnings) == 1
    assert warnings[0] == {"kind": "unhandled_label", "label": "mystery_label",
                            "page": None, "block_id": None, "sample": "surprise content"}


def test_visual_block_missing_bbox_warns_and_drops():
    blocks = [
        {"block_label": "text", "block_content": "body", "block_order": 1,
         "block_bbox": [0, 100, 10, 110]},
        {"block_label": "image", "block_content": "", "block_order": None,
         "block_bbox": None, "block_id": 7},
    ]
    md, warnings = reconstruct_markdown(blocks, stem="doc", page=3)
    assert ".png" not in md
    assert warnings == [{"kind": "visual_missing_bbox", "label": "image",
                          "page": 3, "block_id": 7, "sample": ""}]


def test_visual_block_emits_image_link_with_stem_and_page():
    blocks = [
        {"block_label": "image", "block_content": "", "block_order": None,
         "block_bbox": [0, 50, 10, 60], "block_id": 4},
    ]
    md, warnings = reconstruct_markdown(blocks, stem="mybook", page=6)
    assert "![](mybook.assets/page_0006_block_4.png)" in md
    assert warnings == []


def test_visual_block_without_stem_page_raises():
    blocks = [
        {"block_label": "image", "block_content": "", "block_order": None,
         "block_bbox": [0, 50, 10, 60], "block_id": 4},
    ]
    import pytest
    with pytest.raises(ValueError):
        reconstruct_markdown(blocks)


def test_visual_block_unexpected_content_keeps_both_and_warns():
    blocks = [
        {"block_label": "chart", "block_content": "unexpected data label",
         "block_order": None, "block_bbox": [0, 50, 10, 60], "block_id": 2},
    ]
    md, warnings = reconstruct_markdown(blocks, stem="doc", page=1)
    assert "![](doc.assets/page_0001_block_2.png)" in md
    assert "unexpected data label" in md
    assert warnings == [{"kind": "visual_unexpected_content", "label": "chart",
                          "page": 1, "block_id": 2, "sample": "unexpected data label"}]


def test_golden_p28_image_inserted_between_text_and_captions():
    blocks = json.loads((FIX / "paul_p28_res.json").read_text(encoding="utf-8"))["parsing_res_list"]
    md, warnings = reconstruct_markdown(blocks, stem="paul", page=28)
    assert "paul.assets/page_0028_block_" in md
    # image 在正文 text 之后、figure_title 说明文字之前(该页真实版面顺序)
    text_pos = md.index("cables used to interconnect")
    image_pos = md.index("paul.assets/")
    caption_pos = md.index("FIGURE 1.1")
    assert text_pos < image_pos < caption_pos
    assert warnings == []


def test_ordered_block_malformed_bbox_does_not_raise():
    # block_bbox 存在但非 4 元素(单元素 [5],access bbox[1] 会 IndexError)应像
    # 缺失一样降级处理(y0 默认 0),不应崩溃;内容仍须正常出现在输出里。
    blocks = [{"block_label": "text", "block_content": "malformed bbox body",
               "block_order": 1, "block_bbox": [5]}]
    md, _ = reconstruct_markdown(blocks)
    assert "malformed bbox body" in md


def test_visual_block_malformed_bbox_warns_and_drops():
    # 畸形 bbox([1, 2] 非 4 元素)应与缺失 bbox 一视同仁:告警 + 丢弃,不崩溃
    blocks = [
        {"block_label": "text", "block_content": "body", "block_order": 1,
         "block_bbox": [0, 100, 10, 110]},
        {"block_label": "image", "block_content": "", "block_order": None,
         "block_bbox": [1, 2], "block_id": 7},
    ]
    md, warnings = reconstruct_markdown(blocks, stem="doc", page=3)
    assert ".png" not in md
    assert warnings == [{"kind": "visual_missing_bbox", "label": "image",
                          "page": 3, "block_id": 7, "sample": ""}]


def test_fragments_joined_equal_markdown():
    # 片段拼回的 md 必须与 reconstruct_markdown 逐字节一致(防重构漂移)
    blocks = json.loads((FIX / "paul_p200_res.json").read_text(encoding="utf-8"))["parsing_res_list"]
    frags, w1 = reconstruct_fragments(blocks, stem="paul", page=200)
    md, w2 = reconstruct_markdown(blocks, stem="paul", page=200)
    assert "\n\n".join(f["md"] for f in frags) + "\n" == md
    assert w1 == w2


def test_fragments_carry_block_ids():
    blocks = [
        {"block_label": "text", "block_content": "body", "block_order": 1,
         "block_bbox": [0, 10, 10, 20], "block_id": 7},
    ]
    frags, _ = reconstruct_fragments(blocks)
    assert frags[0]["bids"] == [7]
    assert frags[0]["md"] == "body"


def test_fragments_formula_absorbs_number_carries_both_bids():
    # 公式吸收编号 → 同一片段归属两个块(display_formula + formula_number)
    blocks = [
        {"block_label": "display_formula", "block_content": r"$$ x=1 $$", "block_order": 4,
         "block_bbox": [0, 10, 10, 20], "block_id": 11},
        {"block_label": "formula_number", "block_content": "(5.1)", "block_order": 5,
         "block_bbox": [0, 10, 10, 20], "block_id": 12},
    ]
    frags, _ = reconstruct_fragments(blocks)
    assert len(frags) == 1
    assert sorted(frags[0]["bids"]) == [11, 12]
    assert r"\tag{5.1}" in frags[0]["md"]


def test_fragments_visual_block_carries_id():
    blocks = [
        {"block_label": "image", "block_content": "", "block_order": None,
         "block_bbox": [0, 50, 10, 60], "block_id": 4},
    ]
    frags, _ = reconstruct_fragments(blocks, stem="b", page=6)
    assert frags[0]["bids"] == [4]
    assert "page_0006_block_4.png" in frags[0]["md"]


def test_golden_p6_column_suspect_output_is_deterministic():
    blocks = json.loads((FIX / "paul_p6_res.json").read_text(encoding="utf-8"))["parsing_res_list"]
    from scripts.pipelines.textbooks.selfcheck import detect_column_layout
    assert detect_column_layout(blocks) is True          # 该页已知是双栏嫌疑页(spec §1/§3)
    md1, _ = reconstruct_markdown(blocks, stem="paul", page=6)
    md2, _ = reconstruct_markdown(blocks, stem="paul", page=6)
    assert md1 == md2                                     # 锁"确定性",不锁"正确性"


# --- wrap_cjk_in_text(CJK sanitizer,清 unicodeTextInMathMode/unknownSymbol 警告) ---

def test_wrap_cjk_in_text_wraps_leading_word():
    assert wrap_cjk_in_text("峰值 MPE=x") == r"\text{峰值} MPE=x"


def test_wrap_cjk_in_text_wraps_trailing_punctuation():
    assert wrap_cjk_in_text("a=b。") == "a=b\\text{。}"


def test_wrap_cjk_in_text_wraps_multiple_runs_in_one_formula():
    src = r"峰值 MPE=\frac{MPE\times 平均时间 (s)}{5\times 脉冲宽度 (s)}"
    expected = r"\text{峰值} MPE=\frac{MPE\times \text{平均时间} (s)}{5\times \text{脉冲宽度} (s)}"
    assert wrap_cjk_in_text(src) == expected


def test_wrap_cjk_in_text_leaves_pure_ascii_formula_untouched():
    s = r"Z_{0}=\left(\frac{L}{C}\right)^{1/2}"
    assert wrap_cjk_in_text(s) == s


def test_wrap_cjk_in_text_leaves_greek_and_operators_untouched():
    # 希腊字母/数学算符不在 _TEXTISH 区段内,天然不受影响
    s = r"\alpha\beta\sum\int\times\le\Gamma\Omega"
    assert wrap_cjk_in_text(s) == s


def test_wrap_cjk_in_text_is_idempotent():
    s = "峰值 MPE=x。"
    once = wrap_cjk_in_text(s)
    assert wrap_cjk_in_text(once) == once


def test_wrap_cjk_in_text_does_not_double_wrap_existing_text_command():
    assert wrap_cjk_in_text(r"\text{已包}") == r"\text{已包}"


def test_wrap_cjk_in_text_does_not_double_wrap_existing_mathrm_command():
    assert wrap_cjk_in_text(r"\mathrm{已包}") == r"\mathrm{已包}"


def test_wrap_cjk_in_text_does_not_double_wrap_existing_mathbf_command():
    assert wrap_cjk_in_text(r"\mathbf{已包}") == r"\mathbf{已包}"


def test_wrap_cjk_in_text_wraps_circled_digit_and_geometric_shape():
    # ①-⓿(带圈数字)与 ■-◿(几何图形,含○)是 KaTeX unknownSymbol 警告的常见来源
    assert wrap_cjk_in_text("x^{①}") == r"x^{\text{①}}"
    assert wrap_cjk_in_text("^{○}") == r"^{\text{○}}"


def test_wrap_cjk_in_text_wraps_fullwidth_punctuation():
    # 全角括号/顿号等落在全角/半角形式区段("＀-￯")
    assert wrap_cjk_in_text("a（b）") == r"a\text{（}b\text{）}"


def test_sanitize_latex_wraps_cjk_via_full_pipeline():
    # sanitize_latex 是 wrap_cjk_in_text 的唯一集成点,确认串联生效
    assert sanitize_latex("峰值 MPE=x") == r"\text{峰值} MPE=x"


def test_sanitize_latex_ascii_formula_unaffected_by_cjk_wrap():
    s = r"\int\displaylimits_{a}^{b}"
    # 冗余命令仍被清洗,但纯 ASCII 结果不因新增的 CJK 包裹而改变
    assert sanitize_latex(s) == r"\int_{a}^{b}"


def test_reconstruct_wraps_cjk_in_display_formula():
    blocks = [{"block_label": "display_formula",
               "block_content": "$$ 峰值 MPE=x $$", "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert r"\text{峰值}" in md
    assert "峰值 MPE" not in md            # 裸露 CJK 已被包裹,不再原样出现


def test_reconstruct_leaves_pure_formula_display_untouched():
    blocks = [{"block_label": "display_formula",
               "block_content": r"$$ Z_{0}=\left(\frac{L}{C}\right)^{1/2} $$", "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert r"$$ Z_{0}=\left(\frac{L}{C}\right)^{1/2} $$" in md
    assert r"\text{" not in md


def test_reconstruct_wraps_cjk_inside_inline_math_span_in_text_block():
    blocks = [{"block_label": "text",
               "block_content": "见 $峰值 P=1$ 一节。", "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert r"$\text{峰值} P=1$" in md
    # 数学定界符外的正文中文本来就是文字,不应被 \text{} 包裹
    assert md.startswith("见 ")
    assert md.rstrip("\n").endswith("一节。")


def test_reconstruct_wraps_cjk_inside_table_math_span():
    blocks = [{
        "block_label": "table",
        "block_content": r"<table><tr><td>$ 电压 U=1 $</td></tr></table>",
        "block_order": None,
        "block_bbox": [0, 0, 10, 10],
        "block_id": 3,
    }]
    md, _ = reconstruct_markdown(blocks)
    assert r"$ \text{电压} U=1 $" in md
