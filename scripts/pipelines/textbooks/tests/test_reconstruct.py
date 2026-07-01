import json
from pathlib import Path

from scripts.pipelines.textbooks.reconstruct import (
    reconstruct_markdown,
    restore_emphasis_dots,
    sanitize_latex,
)

FIX = Path(__file__).parent / "fixtures"


def test_drops_order_none_blocks():
    # header/number(page) 的 block_order 为 None,应被剔除
    blocks = [
        {"block_label": "header", "block_content": "PAGE HEADER", "block_order": None},
        {"block_label": "number", "block_content": "186", "block_order": None},
        {"block_label": "text", "block_content": "Body text.", "block_order": 1},
    ]
    md = reconstruct_markdown(blocks)
    assert "PAGE HEADER" not in md
    assert "186" not in md
    assert "Body text." in md


def test_sorts_by_order():
    blocks = [
        {"block_label": "text", "block_content": "second", "block_order": 2},
        {"block_label": "text", "block_content": "first", "block_order": 1},
    ]
    md = reconstruct_markdown(blocks)
    assert md.index("first") < md.index("second")


def test_paragraph_title_becomes_heading():
    blocks = [{"block_label": "paragraph_title", "block_content": "第五章 静磁学", "block_order": 1}]
    md = reconstruct_markdown(blocks)
    assert md.strip().startswith("## 第五章 静磁学")


def test_display_formula_binds_adjacent_number():
    blocks = [
        {"block_label": "display_formula",
         "block_content": r" $$ \mathbf{N}=\boldsymbol{\mu}\times\mathbf{B} $$ ", "block_order": 4},
        {"block_label": "formula_number", "block_content": "(5.1)", "block_order": 5},
    ]
    md = reconstruct_markdown(blocks)
    assert r"\tag{5.1}" in md          # 编号并入公式
    assert md.count("$$") == 2         # 只一个公式块
    assert "\n(5.1)" not in md         # 编号不再单独成行


def test_formula_number_without_formula_kept_inline():
    # 落单的 formula_number(前面不是公式) 保留为文本,不丢
    blocks = [
        {"block_label": "text", "block_content": "see below", "block_order": 1},
        {"block_label": "formula_number", "block_content": "(9.9)", "block_order": 2},
    ]
    md = reconstruct_markdown(blocks)
    assert "(9.9)" in md


def test_restore_emphasis_dots():
    s = r"根本差别：$ \underset{\cdot}{没}\underset{\cdot}{有}\underset{\cdot}{自}\underset{\cdot}{由} $。"
    out = restore_emphasis_dots(s)
    assert out == "根本差别：没有自由。"


def test_golden_jackson_chinese():
    blocks = json.loads((FIX / "jackson_p200_res.json").read_text(encoding="utf-8"))["parsing_res_list"]
    md = reconstruct_markdown(blocks)
    assert md.strip().startswith("## 第五章")          # 标题
    assert r"\mathbf{N}=\boldsymbol{\mu}\times\mathbf{B}" in md  # 公式
    assert r"\tag{5.1}" in md                          # 编号绑定
    assert "186" not in md                             # 页码(order=None)剔除
    assert "underset" not in md                        # 着重号已还原


def test_golden_paul_english():
    blocks = json.loads((FIX / "paul_p200_res.json").read_text(encoding="utf-8"))["parsing_res_list"]
    md = reconstruct_markdown(blocks)
    assert r"\tag{5.30}" in md
    assert r"\tag{5.33}" in md                          # 编号全部绑回(md 端到端曾丢失的)
    assert "THE PER-UNIT-LENGTH" not in md              # 页眉(order=None)剔除
    assert "178" not in md                              # 页码剔除
    assert r"\displaylimits" not in md                  # KaTeX 不兼容命令已清洗(L-T16)


def test_sanitize_latex_strips_displaylimits():
    # \displaylimits 在 $$ 里冗余,删除;KaTeX 才能渲染
    assert sanitize_latex(r"\int\displaylimits_{a}^{b}") == r"\int_{a}^{b}"


def test_sanitize_latex_preserves_displaystyle():
    # 负向边界:不得误伤前缀相近的 \displaystyle
    assert sanitize_latex(r"\displaystyle\int x") == r"\displaystyle\int x"


def test_reconstruct_cleans_displaylimits_in_formula():
    blocks = [{"block_label": "display_formula",
               "block_content": r"$$ \int\displaylimits_{S} \mathbf{B} $$", "block_order": 1}]
    md = reconstruct_markdown(blocks)
    assert r"\displaylimits" not in md
    assert r"\int_{S}" in md
