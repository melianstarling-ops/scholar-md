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
