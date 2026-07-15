import pytest

from scripts.pipelines.textbooks.formula_agents.gates import (
    KatexUnavailable, build_katex_probe_md, circuit_breaker, degenerate_gate,
    is_pure_token_reorder, katex_gate, regression_guard, rollback_md,
    similarity_gate, snapshot_md,
)
from scripts.pipelines.textbooks.formula_agents.protocol import AgentResult


def _r(latex, verdict="correct", cid="p0001-b0001"):
    return AgentResult(candidate_id=cid, verdict=verdict, latex=latex,
                       confidence=0.9, note="")


@pytest.mark.parametrize("junk", [
    "   ",                          # 空
    "Error: rate limit exceeded",   # 额度耗尽
    "quota exhausted",
    "I cannot read the image",      # 拒答
    "抱歉,我无法处理",
])
def test_degenerate_gate_rejects_non_formula_text(junk):
    assert degenerate_gate(_r(junk)) is not None


def test_degenerate_gate_passes_real_latex():
    assert degenerate_gate(_r("\\int_0^1 x^2 \\, dx")) is None


@pytest.mark.parametrize("verdict", ["accept", "uncertain", "not_formula_error"])
def test_gates_skip_verdicts_that_never_change_md(verdict):
    """只有 correct 会改 md,其余 verdict 不该被闸门误杀。"""
    assert degenerate_gate(_r("", verdict=verdict)) is None
    assert similarity_gate(_r("anything", verdict=verdict), "x + 1") is None


def test_similarity_gate_passes_small_repair():
    """典型修正:改一个下标字母。"""
    assert similarity_gate(_r("r_{hf} + 1"), "r_{nf} + 1") is None


@pytest.mark.parametrize("latex,engine,why", [
    ("x", "\\int_0^1 f(x) \\, dx + \\sum_n a_n", "太短"),
    ("\\int_0^1 f(x)\\,dx + \\sum_n a_n + \\prod_k b_k", "x + 1", "太长"),
    ("\\alpha\\beta\\gamma\\delta", "w + x + y + z", "符号不重合"),
])
def test_similarity_gate_rejects_hallucination(latex, engine, why):
    rej = similarity_gate(_r(latex), engine)
    assert rej is not None, why
    assert rej.gate == "similarity"


@pytest.mark.parametrize("old,new", [
    ("x", "x_i"),          # 补下标
    ("x", "x_1"),
    ("v", "\\vec{v}"),     # 补重音
    ("a", "\\hat a"),
])
def test_similarity_gate_passes_short_formula_augmentation(old, new):
    """短公式的合法增补(补下标/补重音)不该被长度比闸死锁。"""
    assert similarity_gate(_r(new), old) is None


def test_similarity_gate_rejects_short_formula_turned_into_giant_unrelated():
    """短公式仍不能被换成一个绝对长度差远超阈值的不相干庞然大物。"""
    rej = similarity_gate(
        _r("\\int_0^1 f(x) \\, dx + \\sum_n a_n + \\prod_k b_k \\cdot c_k"), "x")
    assert rej is not None
    assert rej.gate == "similarity"


def test_similarity_gate_rejects_short_formula_pure_symbol_swap():
    """短公式的纯符号替换(长度比越界规则不拦,但重合度为 0)仍须拒收。"""
    rej = similarity_gate(_r("y"), "x")
    assert rej is not None
    assert rej.gate == "similarity"


def test_similarity_gate_rejects_degenerate_repetition():
    """退化复读:长度比 1.0、旧阈值 0.3 下重合度也能骗过,新阈值 0.5 必须拦下。"""
    rej = similarity_gate(_r("x = x = x"), "x = y + z")
    assert rej is not None
    assert rej.gate == "similarity"


@pytest.mark.parametrize("engine,new", [
    ("r_{nf} + 1", "r_{hf} + 1"),   # 改下标
    ("a+b+c", "a-b-c"),             # 符号反转
    ("f(x)", "f'(x)"),              # 补撇号
])
def test_similarity_gate_real_repairs_unaffected_by_new_threshold(engine, new):
    """min_overlap 从 0.3 提到 0.5 后,真实修复(重合度普遍 >=0.75)不受影响。"""
    assert similarity_gate(_r(new), engine) is None


def test_similarity_gate_passes_when_engine_latex_empty():
    """原文归一化后为空 → 无从比较,直接放行,交给后续闸门。"""
    assert similarity_gate(_r("x_i"), "") is None


@pytest.mark.parametrize("engine,new,why", [
    ("x", "x^2+x-y+1", "凭空捏造多项式,变量全变"),
    ("x+1", "x+y+z+w+2", "凭空捏造,引入 y/z/w 三个全新变量"),
    ("n!", "n!=n*(n-1)!*2", "凭空捏造递推式"),
])
def test_similarity_gate_new_token_budget_rejects_fabrication(engine, new, why):
    """回归:_MAX_ABS_DELTA 豁免 + 短公式分母小 → 重合度虚高,曾让这些捏造
    滑过闸门。新符号预算必须拦下。"""
    rej = similarity_gate(_r(new), engine)
    assert rej is not None, why
    assert rej.gate == "similarity"


@pytest.mark.parametrize("engine,new", [
    ("x", "x_i"),                 # 补下标
    ("v", "\\vec{v}"),            # 补重音
    ("a", "\\hat a"),             # 补重音
    ("r_{nf} + 1", "r_{hf} + 1"), # 改下标
    ("a+b+c", "a-b-c"),           # 符号反转
    ("f(x)", "f'(x)"),            # 补撇号
])
def test_similarity_gate_new_token_budget_does_not_regress_legit_repairs(engine, new):
    """新符号预算不该误伤真实的合法修补(补下标/补重音/改符号)。"""
    assert similarity_gate(_r(new), engine) is None


def test_similarity_gate_accepted_tradeoff_rejects_garbled_engine_rescue():
    """已知且接受的代价(非 bug):引擎输出严重乱码、模型给出真值救回的场景,
    也会被新符号预算拒收。字符串层面无法区分"乱码被救回"与"凭空捏造",
    按"绝不改坏"优先于"尽量多修"的第一原则,这里选择拒收——该条目会
    进 uncertain 报告让所有者人工确认,而不是被自动悄悄写进教材。不要为
    了放行这类大改写而放宽预算。"""
    rej = similarity_gate(_r("\\int_{0}^{\\infty} e^{-x^2}\\,dx"), "0 infty e x2 dx")
    assert rej is not None
    assert rej.gate == "similarity"


# --- 重排判据(similarity_gate 盲区的加固前置判据)---

def test_is_pure_token_reorder_detects_identity_swap():
    """a+b=c -> c+b=a:token 集合相同,序列不同 = 纯重排。"""
    assert is_pure_token_reorder("c+b=a", "a+b=c") is True


def test_is_pure_token_reorder_false_when_unchanged():
    """完全相同、无改动 —— 不是重排。"""
    assert is_pure_token_reorder("a+b=c", "a+b=c") is False


def test_is_pure_token_reorder_false_when_new_symbol_introduced():
    """r_{hf} 相对 r_{nf} 引入了新符号 h(非集合相同),不是纯重排。"""
    assert is_pure_token_reorder("r_{hf}", "r_{nf}") is False


def test_is_pure_token_reorder_false_when_new_latex_empty():
    assert is_pure_token_reorder("", "x") is False


@pytest.mark.parametrize("n,total,tripped", [
    (7, 10, True),     # 70% > 60%
    (6, 10, False),    # 60% 不算超
    (2, 10, False),
    (0, 0, False),     # 空候选不熔断
])
def test_circuit_breaker(n, total, tripped):
    assert (circuit_breaker(n, total, ratio=0.6) is not None) is tripped


CANDS = {
    "p0001-b0001": {"candidate_id": "p0001-b0001", "page": 1, "block_id": 1,
                    "engine_latex": "x^2 + 1"},
    "p0002-b0002": {"candidate_id": "p0002-b0002", "page": 2, "block_id": 2,
                    "engine_latex": "y^2 + 1"},
}


# --- 闸 1: KaTeX 可渲染门 ---

def test_probe_md_tags_each_formula_and_skips_non_mutating():
    results = [_r("x", cid="p0001-b0001", verdict="accept"),      # 不改 md,不入探针
               _r("y^{2}", cid="p0002-b0002")]                     # correct,入探针
    md = build_katex_probe_md(results, CANDS)
    assert "<!-- page: 2 block_ids: 2 -->" in md
    assert "y^{2}" in md
    assert "<!-- page: 1 block_ids: 1 -->" not in md
    assert md.count("$$") == 2                                     # 一条 display 公式


def test_katex_gate_rejects_unparseable_and_keeps_the_rest():
    results = [_r("x^{2}+2", cid="p0001-b0001"),
               _r("\\frac{1", cid="p0002-b0002")]                  # 未闭合 = 硬错

    def fake_scan(md_path, out_path, **kw):
        return {"errors": [{"page": 2, "block_ids": [2],
                            "error": "KaTeX parse error: Expected '}'"}]}

    passed, rejected = katex_gate(results, CANDS, work_dir=".", scan_fn=fake_scan)
    assert [r.candidate_id for r in passed] == ["p0001-b0001"]
    assert len(rejected) == 1
    assert rejected[0].candidate_id == "p0002-b0002" and rejected[0].gate == "katex"


def test_katex_gate_passes_all_when_clean():
    passed, rejected = katex_gate([_r("x^{2}+2", cid="p0001-b0001")], CANDS,
                                  work_dir=".", scan_fn=lambda *a, **k: {"errors": []})
    assert len(passed) == 1 and rejected == []


def test_katex_gate_raises_when_node_missing():
    """node 不可用 → 没有校验能力 → 绝不放行,抛错让调用方整轮降级 propose。"""
    with pytest.raises(KatexUnavailable):
        katex_gate([_r("x^{2}", cid="p0001-b0001")], CANDS, work_dir=".",
                   scan_fn=lambda *a, **k: None)


def test_katex_gate_rejects_malformed_candidate_page_instead_of_crashing():
    """Minor 回归:候选 page/block_id 非数字须被保守拒收,不崩溃、不放行。"""
    bad_cands = {
        "p000x-b0001": {"candidate_id": "p000x-b0001", "page": "x", "block_id": 1,
                        "engine_latex": "x^2 + 1"},
    }
    results = [_r("x^{2}+2", cid="p000x-b0001")]
    passed, rejected = katex_gate(results, bad_cands, work_dir=".",
                                  scan_fn=lambda *a, **k: {"errors": []})
    assert passed == []
    assert len(rejected) == 1
    assert rejected[0].candidate_id == "p000x-b0001" and rejected[0].gate == "katex"


def test_katex_gate_skips_malformed_error_record_mapping_without_crashing():
    """Minor 回归:err 记录里的 page/block_id 非数字应被跳过映射,不崩溃。"""

    def fake_scan(md_path, out_path, **kw):
        return {"errors": [{"page": "not-a-number", "block_ids": [1],
                            "error": "irrelevant"}]}

    passed, rejected = katex_gate([_r("x^{2}+2", cid="p0001-b0001")], CANDS,
                                  work_dir=".", scan_fn=fake_scan)
    assert len(passed) == 1 and rejected == []


def test_katex_gate_skips_node_call_when_nothing_mutating():
    passed, rejected = katex_gate(
        [_r("x", cid="p0001-b0001", verdict="accept")], CANDS, work_dir=".",
        scan_fn=lambda *a, **k: pytest.fail("无 correct 时不该调 node"))
    assert len(passed) == 1 and rejected == []


# --- 闸 5: 快照 / 回滚 / 回归守卫 ---

def test_snapshot_and_rollback_roundtrip(tmp_path):
    md = tmp_path / "book.md"
    md.write_text("原始内容", encoding="utf-8")

    snap = snapshot_md(str(md))
    assert snap == str(md) + ".pre_agent.bak"

    md.write_text("被改坏了", encoding="utf-8")
    rollback_md(str(md), snap)
    assert md.read_text(encoding="utf-8") == "原始内容"
    assert snapshot_md(str(tmp_path / "nope.md")) is None      # md 不存在


@pytest.mark.parametrize("scan_result,should_trip", [
    ({"errors": [{"error": "boom"}]}, True),    # 硬错从 0 涨到 1 → 回滚
    ({"errors": []},                  False),   # 无变化 → 放行
    (None,                            True),    # node 不可用 → 无法验证 → 不敢放行
])
def test_regression_guard(tmp_path, scan_result, should_trip):
    md = tmp_path / "book.md"
    md.write_text("$$ x $$", encoding="utf-8")
    reason = regression_guard(str(md), work_dir=str(tmp_path),
                              baseline_hard_errors=0,
                              scan_fn=lambda *a, **k: scan_result)
    assert (reason is not None) is should_trip
