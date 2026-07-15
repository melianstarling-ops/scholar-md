import pytest

from scripts.pipelines.textbooks.formula_agents.latex_equiv import latex_equiv


def _fake_render(pairs):
    """把预设的 latex->mathml 映射做成可注入的 render_fn。"""
    def render_fn(latex_list, node_bin=None):
        return [pairs[x] for x in latex_list]
    return render_fn


def test_string_equal_after_normalize_short_circuits_without_render():
    called = {"n": 0}

    def render_fn(latex_list, node_bin=None):
        called["n"] += 1
        return ["x"] * len(latex_list)

    assert latex_equiv("$$ x+1 $$", "x+1", render_fn=render_fn) is True
    assert called["n"] == 0                       # 字符串就相等,免渲染


def test_format_variants_equal_via_mathml():
    """写法不同、MathML 相同 -> 等价。"""
    render = _fake_render({r"\dfrac{a}{b}": "<mfrac><mi>a</mi><mi>b</mi></mfrac>",
                           r"\frac{a}{b}":  "<mfrac><mi>a</mi><mi>b</mi></mfrac>"})
    assert latex_equiv(r"\dfrac{a}{b}", r"\frac{a}{b}", render_fn=render) is True


def test_genuinely_different_formulas_not_equal():
    render = _fake_render({"a+b": "<mi>a</mi><mo>+</mo><mi>b</mi>",
                           "a-b": "<mi>a</mi><mo>-</mo><mi>b</mi>"})
    assert latex_equiv("a+b", "a-b", render_fn=render) is False


def test_reordering_is_not_equivalent():
    """MathML 保留顺序:a+b != b+a,绝不把重排当等价。"""
    render = _fake_render({"a+b": "<mi>a</mi><mo>+</mo><mi>b</mi>",
                           "b+a": "<mi>b</mi><mo>+</mo><mi>a</mi>"})
    assert latex_equiv("a+b", "b+a", render_fn=render) is False


def test_node_unavailable_returns_none():
    """render 返回 None(node 缺失) -> latex_equiv 返回 None,调用方保守当不等价。"""
    assert latex_equiv("a+b", "c+d", render_fn=lambda *a, **k: None) is None


# --- Critical #1 回归: mathvariant 是语义属性,不能被剥离属性时一并丢掉 ---

@pytest.mark.parametrize("bare,decorated", [
    (r"\mathbf{v}", "v"),          # 粗体向量 vs 标量
    (r"\mathbb{R}", "R"),          # 实数集 vs 变量 R
    (r"\mathcal{A}", "A"),         # 花体 vs 变量 A
    (r"\boldsymbol{x}", "x"),      # 粗斜体 vs 标量
])
def test_mathvariant_semantic_forms_not_equivalent_injected(bare, decorated):
    """注入 render_fn:mathvariant 不同的 MathML 必须判不等价。"""
    render = _fake_render({
        bare: f'<mi mathvariant="bold">x</mi>',
        decorated: '<mi>x</mi>',
    })
    assert latex_equiv(bare, decorated, render_fn=render) is False


@pytest.mark.parametrize("a,b", [
    (r"\mathbf{v}", "v"),
    (r"\mathbb{R}", "R"),
    (r"\mathcal{A}", "A"),
    (r"\boldsymbol{x}", "x"),
])
def test_mathvariant_semantic_forms_not_equivalent_real_node(a, b):
    """真机 node + 真 KaTeX:同上,不注入,验证 latex_to_mathml.mjs 真实保留 mathvariant。"""
    import shutil
    if not shutil.which("node"):
        pytest.skip("node 不可用,跳过真机验证")
    assert latex_equiv(a, b) is False


@pytest.mark.parametrize("a,b", [
    (r"\dfrac{a}{b}", r"\frac{a}{b}"),
    ("x^2", "x^{2}"),
    ("{a}", "a"),
])
def test_format_variants_still_equivalent_real_node(a, b):
    """真机验证:剥离外观属性/折叠 mstyle 与冗余 mrow 的既有行为不受影响。"""
    import shutil
    if not shutil.which("node"):
        pytest.skip("node 不可用,跳过真机验证")
    assert latex_equiv(a, b) is True


# --- Critical #2 回归: 任一侧渲染为空 = 不可判定,不是"等价" ---

@pytest.mark.parametrize("mathmls", [
    ["", ""],                    # 两侧都渲染失败(不同的乱码)
    ["<mi>a</mi>", ""],          # 只有一侧渲染失败
    ["", "<mi>a</mi>"],
    ["   ", "<mi>a</mi>"],       # 纯空白也算空
])
def test_empty_render_is_undecidable_not_equal(mathmls):
    def render_fn(latex_list, node_bin=None):
        return mathmls
    assert latex_equiv("junk-a", "junk-b", render_fn=render_fn) is None


def test_two_different_garbled_latex_real_node():
    """真机验证:两条不同的不可渲染 latex 不能因为都变空串而被判等价。"""
    import shutil
    if not shutil.which("node"):
        pytest.skip("node 不可用,跳过真机验证")
    assert latex_equiv(r"\frac{1", r"\left(") is None
    assert latex_equiv(r"\frac{1", r"\frac{2") is None
