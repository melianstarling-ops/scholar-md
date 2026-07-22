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


def test_paragraph_title_sanitizes_inline_math_symbols():
    blocks = [{
        "block_label": "paragraph_title",
        "block_content": "12.4 CE Analyst $ ^{™} $",
        "block_order": 1,
    }]
    md, _ = reconstruct_markdown(blocks)
    assert r"## 12.4 CE Analyst $^{\text{TM}}$" in md


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


def test_display_formula_sanitizes_non_ascii_formula_number_symbols():
    blocks = [
        {"block_label": "display_formula", "block_content": r"$$ x=1 $$", "block_order": 1},
        {"block_label": "formula_number", "block_content": "(6-31)⊖", "block_order": 2},
    ]
    md, _ = reconstruct_markdown(blocks)
    assert r"\tag{(6-31)\text{\textcircled{-}}}" in md


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


def test_restore_emphasis_dots_does_not_merge_adjacent_unrelated_inline_spans():
    # 根因回归:_EMPH_WRAP_RE 内层用 *(零次或多次),允许捕获组为空,导致正则从
    # 第一个公式的收尾 $ 出发、越过空白、匹配到第二个公式的起始 $,把中间的
    # "$ $"(收尾$ + 空格 + 起始$)当成"内容为空的 underset 包裹"整体删掉,焊死
    # 两个本该独立的行内公式。真实语料:Jackson p237,"$J \Delta\sigma$ $d\mathbf{l}$"
    # 被吃成 "$J \Delta\sigmad\mathbf{l}$"(\sigma 和 d 被强行焊在一起)。
    s = r"but $J \Delta\sigma$ $d\mathbf{l}$ is equal to X."
    assert restore_emphasis_dots(s) == s


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


def test_sanitize_latex_collapses_bare_then_braced_double_subscript():
    # V_g_{\max}:裸首下标 _g 后紧跟 _{\max} → 合并(消 KaTeX double subscript 硬错)
    assert sanitize_latex(r"V_g_{\max} = 0.2\ V") == r"V_{g\ \max} = 0.2\ V"


def test_sanitize_latex_collapses_bare_bare_double_subscript():
    assert sanitize_latex(r"x_a_b") == r"x_{a\ b}"


def test_sanitize_latex_leaves_single_bare_subscript_untouched():
    assert sanitize_latex(r"x_i") == r"x_i"
    assert sanitize_latex(r"\int_a^b") == r"\int_a^b"


def test_sanitize_latex_bare_subscript_then_superscript_untouched():
    assert sanitize_latex(r"x_a^b") == r"x_a^b"


def test_sanitize_latex_collapses_bare_then_braced_double_superscript():
    assert sanitize_latex(r"x^a^{b}") == r"x^{a\ b}"


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


# --- 命令映射(_KATEX_CMD_MAP / _unwrap_ensuremath):KaTeX 不认的命令 → 语义等价替换 ---

def test_sanitize_latex_maps_upmu_to_mu():
    # upgreek 包的直立 μ(单位前缀 μA/μV/μF),KaTeX 无 → \mu
    assert sanitize_latex(r"0.016\;\upmu\mathrm{F}") == r"0.016\;\mu\mathrm{F}"


def test_sanitize_latex_upmu_does_not_touch_longer_command():
    # 字母边界:\upmuX(假想更长命令)不被误伤;实际语料里 \upmu 恒后接非字母
    s = r"\upmuped"
    assert sanitize_latex(s) == s


def test_sanitize_latex_preserves_leftarrow_command():
    # 根因回归:\left 定界符清洗曾把 \leftarrow 里的 \left 当定界符,啃成未定义的
    # \omegaarrow(频率定标 ω←ω/ωc)。字母边界守卫后 \leftarrow 必须逐字保留。
    s = r"\omega\leftarrow\frac{\omega}{\omega_{c}}"
    assert sanitize_latex(s) == s


def test_sanitize_latex_preserves_rightarrow_command():
    # 同根因:\sigma\rightarrow\infty 曾被啃成 \sigmaarrow(理想导体 σ→∞)
    assert sanitize_latex(r"(\sigma\rightarrow\infty)") == r"(\sigma\rightarrow\infty)"


def test_sanitize_latex_preserves_leftrightarrow_command():
    # 更长的箭头命令同样不能被 \left 定界符清洗误伤
    s = r"a\leftrightarrow b"
    assert sanitize_latex(s) == s


def test_sanitize_latex_still_downgrades_real_left_delimiter():
    # 守卫不影响真定界符:\left. 后接非字母,仍按原逻辑降级(不回归既有行为)
    assert sanitize_latex(r"\left\{a\right.}\\ &{}&{\left.+b\right.}\\ &{}&{c\right\}") == \
        r"\{a\\ &{}&{+b}\\ &{}&{c\}"


def test_sanitize_latex_null_delimiter_deletion_does_not_glue_control_word():
    # 根因回归(Kong p460):\quad\left.k^{2} 中的 \left. 按既有降级判定确实该删
    # (与 test_sanitize_latex_still_downgrades_real_left_delimiter 是同一套判定,
    # 这里不改判定本身),但删成 "" 会把 \quad 焊死成未定义控制序列 \quadk。
    # 改删成单个空格(数学模式下空格恰是控制词的天然终止符,视觉上与"空定界符
    # 本不可见"等价),\quad 与 k 之间保住边界。
    s = r"\left[a\right.+\\&\quad\left.k^{2}\right]"
    out = sanitize_latex(s)
    assert r"\quadk" not in out
    assert r"\quad k^{2}" in out


def test_sanitize_latex_maps_pit_to_pi_t():
    # OCR 把 "\pi t"(频率 1/\pi t_r)黏一起
    assert sanitize_latex(r"1/\pit_{r}") == r"1/\pi t_{r}"


def test_sanitize_latex_pit_does_not_touch_pitchfork():
    # 字母边界:真命令 \pitchfork 不被误拆
    s = r"a\pitchfork b"
    assert sanitize_latex(s) == s


def test_sanitize_latex_unwraps_ensuremath_simple():
    # 已在数学模式,\ensuremath{X} 冗余 → 解包为 X
    assert sanitize_latex(r"\frac{1}{2}\ln 2\ensuremath{\mathrm{N p}}") == \
        r"\frac{1}{2}\ln 2\mathrm{N p}"


def test_sanitize_latex_unwraps_ensuremath_with_nested_braces():
    # 内容含嵌套花括号,靠括号配对而非贪婪正则
    assert sanitize_latex(r"\ensuremath{20\cdot\pi^{2}}\cdot(L/\lambda)^{2}") == \
        r"20\cdot\pi^{2}\cdot(L/\lambda)^{2}"


def test_sanitize_latex_unwraps_multiple_ensuremath():
    assert sanitize_latex(r"\ensuremath{a}+\ensuremath{b}") == r"a+b"


def test_sanitize_latex_ensuremath_unbalanced_braces_left_intact():
    # 花括号不配对时放弃改写,绝不破坏(最坏=原样)
    s = r"\ensuremath{a+b"
    assert sanitize_latex(s) == s


def test_sanitize_latex_drops_stray_display_open_delimiter():
    # 数学内容已被 $$ 包裹,残留的 display 定界符 \[ 恒非法 → 删
    assert sanitize_latex(r"\[\begin{array}{l}x\end{array}") == \
        r"\begin{array}{l}x\end{array}"


# --- _repair_math_in_text:OCR 把数学(\ln/^/_)错包进 \text{},KaTeX text 模式硬报错 ---

def test_sanitize_latex_leaves_plain_text_command_untouched():
    # 合法 \text{}(纯文字/单位,无 ^ _ 或数学算符)绝不改写
    for s in (r"\text{ N p}", r"\text{d B}", r"\text{已包}", r"\mathrm{ G }"):
        assert sanitize_latex(s) == s


def test_sanitize_latex_splits_math_operator_out_of_text():
    # 数学算符 \ln 被错包进 \text{}:算符移出数学模式,尾随空格防与后文 Z 黏成 \lnZ
    assert sanitize_latex(r"n=0\text{:\ \ln}Z_{1}") == r"n=0\text{:\ }\ln Z_{1}"


def test_sanitize_latex_splits_superscript_out_of_text():
    # ^{\circ} 被错包 → 上标移出,基座数字随之入数学;— 留在 \text{}(text 模式合法,无警告)
    assert sanitize_latex(r"\underline{\text{—172^{\circ}}}") == r"\underline{\text{—}172^{\circ}}"


def test_sanitize_latex_splits_cjk_text_from_trailing_math_subscript():
    # 有 CJK:文字留在 \text{},数学(带基座的下标 C_1)拆出到数学模式
    assert sanitize_latex(r"\text{并联电容C_1}") == r"\text{并联电容}C_1"


def test_sanitize_latex_splits_cjk_text_around_embedded_math():
    # 数学夹在两段 CJK 中间:两侧文字各自保留 \text{},中间 L_f 拆出
    assert sanitize_latex(r"\text{扼流电感L_f组成}") == r"\text{扼流电感}L_f\text{组成}"


def test_sanitize_latex_math_in_text_pops_whole_number_as_base():
    # 上标基座取最长 alnum 串:172 整体入数学,不把数字拆成 17+2
    assert sanitize_latex(r"\text{—172^{\circ}}") == r"\text{—}172^{\circ}"


def test_table_pass_through_sanitizes_inline_math_entities():
    blocks = [{
        "block_label": "table",
        "block_content": r"<table><tr><td>$ R_{S}&lt;Z_{C} $</td><td>$ \varepsilon&#x27;_{r} $</td></tr></table>",
        "block_order": None,
        "block_bbox": [0, 0, 10, 10],
        "block_id": 3,
    }]
    md, _ = reconstruct_markdown(blocks)
    assert r"$R_{S}<Z_{C}$" in md
    assert r"$\varepsilon'_{r}$" in md


def test_inline_formula_block_sanitizes_math_body():
    # inline_formula 块内容已自带 $$...$$ 包裹,历史上落未知分支被当纯文本直落,
    # 绕过 sanitize_latex → 命令映射等触不到。现应路由过 math-span 清洗。
    blocks = [{
        "block_label": "inline_formula",
        "block_content": r"$$ 0.016\;\upmu\mathrm{F} $$",
        "block_order": 1, "block_id": 1, "block_bbox": [0, 0, 10, 10],
    }]
    md, _ = reconstruct_markdown(blocks)
    assert r"\upmu" not in md            # sanitize_latex 已触达(否则会原样直落)
    assert r"\mu\mathrm{F}" in md


def test_inline_formula_block_without_dollar_wrap_passes_through_unchanged():
    # 无 $ 包裹的裸内容:math-span 清洗找不到 span,原样穿过(与旧直落行为等价,无回归)。
    blocks = [{
        "block_label": "inline_formula", "block_content": "see figure 3",
        "block_order": 1, "block_id": 1, "block_bbox": [0, 0, 10, 10],
    }]
    md, _ = reconstruct_markdown(blocks)
    assert "see figure 3" in md


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
    assert wrap_cjk_in_text(r"\mathrm{已包}") == r"\mathrm{\text{已包}}"


def test_wrap_cjk_in_text_does_not_double_wrap_existing_mathbf_command():
    assert wrap_cjk_in_text(r"\mathbf{已包}") == r"\mathbf{\text{已包}}"


def test_sanitize_latex_moves_cjk_out_of_math_font_mode():
    assert sanitize_latex(r"\mathrm{ 波 }") == r"\mathrm{ \text{波} }"
    assert sanitize_latex(r"\mathbf{ 甲 }") == r"\mathbf{ \text{甲} }"


def test_sanitize_latex_normalizes_circled_digits_for_katex_text_mode():
    assert sanitize_latex("x^{①}") == r"x^{\text{\textcircled{1}}}"
    assert sanitize_latex(r"x^{\text{①}}") == r"x^{\text{\textcircled{1}}}"
    assert sanitize_latex("x^{⓪}") == r"x^{\text{\textcircled{0}}}"


def test_sanitize_latex_normalizes_text_only_symbols_and_commands():
    assert sanitize_latex("x^{®}") == r"x^{\text{\textregistered}}"
    assert sanitize_latex(r"x^{\textregistered}") == r"x^{\text{\textregistered}}"
    assert sanitize_latex(r"x^{\text{®}a}") == r"x^{\text{\text{\textregistered}}a}"
    assert sanitize_latex("x^{™}") == r"x^{\text{TM}}"
    assert sanitize_latex("1–100") == r"1\text{--}100"
    assert sanitize_latex("x^{†}+R_{Ω}") == r"x^{\dagger{}}+R_{\Omega{}}"
    assert sanitize_latex("○+⊖") == r"\text{\textcircled{ }}+\text{\textcircled{-}}"


def test_sanitize_latex_unicode_warning_cleanup_is_idempotent():
    src = r"\mathrm{方向性}+x^{\text{①}}+y^{®}+z^{™}+†+Ω+○+⊖"
    once = sanitize_latex(src)
    assert sanitize_latex(once) == once


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
    assert r"$\text{电压} U=1$" in md


# ---------------------------------------------------------------------------
# Task A:行内公式定界符空格归一。PaddleOCR-VL 输出 `$ X $`(定界符内侧带空格),
# 主流 Markdown 渲染器(VSCode 预览/pandoc)要求开 $ 后、闭 $ 前非空格,否则按字面
# 文本显示;KaTeX 门只验编译不验定界符,故 0 硬错也漏过此类问题。归一化只在识别出
# 公式包裹的 math-span 路径生效(_sanitize_markdown_math_spans),不做全文盲目正则,
# 防止误伤正文里的孤立 $(货币场景)。display $$...$$ 无此渲染规则问题,不受影响。
# ---------------------------------------------------------------------------

def test_inline_math_delimiter_ws_stripped_in_text_block():
    blocks = [{"block_label": "text", "block_content": "场强 $ B_0 $ 恒定。", "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert "$B_0$" in md
    assert "$ B_0 $" not in md


def test_inline_math_delimiter_multi_space_stripped():
    blocks = [{"block_label": "text",
               "block_content": r"场强 $  \mathbf{B}_0  $ 恒定。", "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert r"$\mathbf{B}_0$" in md


def test_inline_math_delimiter_single_sided_space_stripped():
    blocks = [{"block_label": "text", "block_content": "场强 $B_0 $ 恒定。", "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert "$B_0$" in md
    blocks2 = [{"block_label": "text", "block_content": "场强 $ B_0$ 恒定。", "block_order": 1}]
    md2, _ = reconstruct_markdown(blocks2)
    assert "$B_0$" in md2


def test_inline_math_delimiter_ws_stripped_in_heading():
    blocks = [{
        "block_label": "paragraph_title",
        "block_content": "1.3 磁感应强度 $ B_{0} $ 的定义",
        "block_order": 1,
    }]
    md, _ = reconstruct_markdown(blocks)
    assert "## 1.3 磁感应强度 $B_{0}$ 的定义" in md


def test_display_formula_delimiter_spacing_untouched_by_inline_normalization():
    # inline_formula 块内容本身可自带 $$...$$(见 test_inline_formula_block_sanitizes_math_body);
    # 走 math-span 清洗时 display 分支不受行内归一化影响。
    blocks = [{
        "block_label": "inline_formula", "block_content": r"$$ B_0 = 1 $$",
        "block_order": 1, "block_id": 1, "block_bbox": [0, 0, 10, 10],
    }]
    md, _ = reconstruct_markdown(blocks)
    assert r"$$ B_0 = 1 $$" in md


def test_inline_math_delimiter_already_normalized_is_idempotent():
    blocks = [{"block_label": "text", "block_content": "$B_0$ 不变。", "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert "$B_0$" in md


def test_inline_math_delimiter_normalization_preserves_internal_spacing():
    # 只去定界符内侧首尾空白;公式内容中间的空格(词间距)不动
    blocks = [{"block_label": "text", "block_content": "$a + b$ 保持。", "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert "$a + b$" in md


def test_inline_math_delimiter_all_whitespace_body_kept_as_is():
    # `$   $`(内体全是空白,不是真公式)strip 后会变空,拼出 "$" + "" + "$" = "$$"
    # ——这会被后续渲染器当成 display 定界符,可能一路吞掉后文直到下一个 $$。
    # 全空白内体判定不是真公式,保持原样,不触发这个更危险的 "$$" 拼接。
    # 用 inline_formula 标签(直接走 _sanitize_markdown_math_spans,不经
    # restore_emphasis_dots)隔离验证,避免与该函数另一个预存在缺陷
    # (_EMPH_WRAP_RE 会把无 \underset 的裸 "$...$" 全文吞掉)混在一起。
    blocks = [{
        "block_label": "inline_formula", "block_content": "a $   $ b",
        "block_order": 1, "block_id": 1, "block_bbox": [0, 0, 10, 10],
    }]
    md, _ = reconstruct_markdown(blocks)
    assert "$$" not in md
    assert "a $   $ b" in md
    assert "$a+b$" not in md


# ---------------------------------------------------------------------------
# Task C:表格降级导出——无合并单元格的简单表改发 Markdown 管道表格(数学立即
# 全渲染);复杂表(rowspan/colspan、多 <table> 根、嵌套表、判不准)一律保守
# 保留现状 HTML 直出,逐字节不变。C3(顺手项):上下标双花括号归一,范围严格
# 限定在 _/^ 紧跟的双花括号。
# ---------------------------------------------------------------------------


def _table_block(content, block_id=1):
    return [{
        "block_label": "table", "block_content": content,
        "block_order": None, "block_bbox": [0, 0, 10, 10], "block_id": block_id,
    }]


def test_simple_table_with_formula_cell_becomes_pipe_table():
    content = (
        r'<table><tr><th>方程</th><th>说明</th></tr>'
        r'<tr><td>$\nabla \cdot \mathbf{B} = 0$</td><td>无源</td></tr></table>'
    )
    md, _ = reconstruct_markdown(_table_block(content))
    assert "<table>" not in md
    assert "| 方程 | 说明 |" in md
    assert "| --- | --- |" in md
    assert r"| $\nabla \cdot \mathbf{B} = 0$ | 无源 |" in md


def test_table_with_rowspan_falls_back_to_html_unchanged():
    # 复杂表(rowspan≥2)回归锁定:与改动前逐字节一致(无 $/无 \underset,
    # _sanitize_markdown_math_spans/restore_emphasis_dots 均为恒等变换)。
    content = ('<table><tr><td rowspan="2">A</td><td>B</td></tr>'
               '<tr><td>C</td></tr></table>')
    md, _ = reconstruct_markdown(_table_block(content))
    assert md == content + "\n"


def test_table_with_colspan_falls_back_to_html_unchanged():
    content = '<table><tr><td colspan="2">A</td></tr><tr><td>B</td><td>C</td></tr></table>'
    md, _ = reconstruct_markdown(_table_block(content))
    assert md == content + "\n"


def test_table_with_multiple_roots_falls_back_to_html_unchanged():
    content = '<table><tr><td>A</td></tr></table><table><tr><td>B</td></tr></table>'
    md, _ = reconstruct_markdown(_table_block(content))
    assert md == content + "\n"


def test_table_with_nested_table_falls_back_to_html_unchanged():
    content = '<table><tr><td><table><tr><td>inner</td></tr></table></td></tr></table>'
    md, _ = reconstruct_markdown(_table_block(content))
    assert md == content + "\n"


def test_table_cell_pipe_escaped_and_newline_folded():
    content = '<table><tr><th>H</th></tr><tr><td>a|b\nc</td></tr></table>'
    md, _ = reconstruct_markdown(_table_block(content))
    assert r"a\|b c" in md
    assert "a|b" not in md          # 未转义的裸 | 不应残留(会被误读成列分隔符)


def test_table_cell_math_span_with_literal_pipe_falls_back_to_html():
    # review 裁定 Critical:数学区间内的字面 | (绝对值 |x|、条件概率 P(A|B))
    # 转义会被 KaTeX 读成 \| 范数记号(语义改变),不转义又会被管道表格切列引擎
    # 错误切分(块级按未转义 | 切列,先于行内 math 规则,超额列被渲染器丢弃)。
    # 两难之下按判不准原则整表回落 HTML,不做半吊子转义。
    content = '<table><tr><th>H</th></tr><tr><td>$P(A|B)$</td></tr></table>'
    md, _ = reconstruct_markdown(_table_block(content))
    assert md == content + "\n"


def test_table_cell_math_span_with_literal_pipe_display_falls_back_to_html():
    # display $$...$$ 内的字面 | 同样触发回落(不止行内 $...$)。这里只断言"没
    # 被错误切列成管道表格"(仍是 <table> 结构、无 "| --- |" 分隔行),不断言
    # 逐字节相等——该 content 恰好命中 restore_emphasis_dots 一个与本任务无关
    # 的既有缺陷:_EMPH_WRAP_RE 对无 \underset 内容的相邻裸 "$$" 也会当成空
    # 着重号包裹整体吞掉(与表格/Task C 无关,任意 "$$ x $$" 单独调用
    # restore_emphasis_dots 同样复现,已记入报告疑虑,不在本任务范围内修)。
    content = '<table><tr><th>H</th></tr><tr><td>$$|x|$$</td></tr></table>'
    md, _ = reconstruct_markdown(_table_block(content))
    assert "<table>" in md
    assert "| --- |" not in md


def test_table_with_non_structural_tag_falls_back_to_html():
    # review 裁定 Important:parse_table_html 只留纯文本,5<br>10 会拼成 510
    # (数值损坏)。表结构标签(table/thead/tbody/tfoot/tr/td/th/caption)以外的
    # 任何标签一律触发 HTML 回落,不猜语义。
    content = '<table><tr><th>H</th></tr><tr><td>5<br>10</td></tr></table>'
    md, _ = reconstruct_markdown(_table_block(content))
    assert md == content + "\n"


def test_table_with_sup_tag_falls_back_to_html():
    content = '<table><tr><th>H</th></tr><tr><td>x<sup>2</sup></td></tr></table>'
    md, _ = reconstruct_markdown(_table_block(content))
    assert md == content + "\n"


def test_table_pure_text_and_formula_still_degrades_to_pipe_table():
    # 回归确认:纯文本 + 公式(无非结构标签、无数学区间内 |)的简单表仍降级
    # 为管道表格——上面两个新回落条件不应误伤主路径。
    content = (
        r'<table><tr><th>方程</th><th>说明</th></tr>'
        r'<tr><td>$\nabla \cdot \mathbf{B} = 0$</td><td>无源</td></tr></table>'
    )
    md, _ = reconstruct_markdown(_table_block(content))
    assert "<table>" not in md
    assert r"| $\nabla \cdot \mathbf{B} = 0$ | 无源 |" in md


def test_table_short_row_padded_no_column_misalignment():
    content = ('<table><tr><th>A</th><th>B</th><th>C</th></tr>'
               '<tr><td>1</td></tr></table>')
    md, _ = reconstruct_markdown(_table_block(content))
    lines = [l for l in md.splitlines() if l.strip()]
    header_line = next(l for l in lines if l.startswith("| A"))
    sep_line = next(l for l in lines if l.startswith("| ---"))
    data_line = next(l for l in lines if l.startswith("| 1"))
    assert header_line.count("|") == sep_line.count("|") == data_line.count("|") == 4


def test_table_caption_emitted_above_pipe_table():
    content = '<table><caption>表 1 参数</caption><tr><th>A</th></tr><tr><td>1</td></tr></table>'
    md, _ = reconstruct_markdown(_table_block(content))
    assert "表 1 参数" in md
    assert md.index("表 1 参数") < md.index("| A |")


def test_empty_table_falls_back_to_html():
    content = "<table></table>"
    md, _ = reconstruct_markdown(_table_block(content))
    assert md == content + "\n"


def test_sanitize_latex_normalizes_double_braced_subscript():
    assert sanitize_latex(r"T_{{1}}") == r"T_{1}"


def test_sanitize_latex_normalizes_double_braced_superscript():
    assert sanitize_latex(r"x^{{2}}") == r"x^{2}"


def test_sanitize_latex_frac_double_brace_argument_untouched():
    # C3 只锚定在 _/^ 紧跟的双花括号;\frac 的花括号参数前面不是 _/^,不受影响
    assert sanitize_latex(r"\frac{{a}}{b}") == r"\frac{{a}}{b}"


def test_sanitize_latex_single_braced_group_untouched():
    # 单层花括号(非双花括号)不匹配 C3 模式,原样保留
    assert sanitize_latex(r"{\alpha}") == r"{\alpha}"


def test_inline_formula_double_braced_subscript_normalized_end_to_end():
    blocks = [{"block_label": "text", "block_content": "设 $T_{{1}}$ 为初始温度。", "block_order": 1}]
    md, _ = reconstruct_markdown(blocks)
    assert r"$T_{1}$" in md
    assert r"$T_{{1}}$" not in md


def test_display_formula_double_braced_subscript_normalized():
    blocks = [{
        "block_label": "display_formula", "block_content": r"$$ x_{{n}} = 1 $$",
        "block_order": 1, "block_id": 1, "block_bbox": [0, 0, 10, 10],
    }]
    md, _ = reconstruct_markdown(blocks)
    assert r"x_{n}" in md
    assert r"x_{{n}}" not in md
