from scripts.pipelines.textbooks.katex_triage import classify_error


def test_undefined_command_bucket_and_signature():
    b, sig = classify_error(r"KaTeX parse error: Undefined control sequence: \upmu at position 5",
                            r"0.016\;\upmu\mathrm{F}")
    assert b == "undefined_command" and sig == r"\upmu"


def test_math_in_text_bucket():
    b, _ = classify_error(r"KaTeX parse error: Can't use function '\ln' in text mode at position 2",
                          r"n=0\text{:\ \ln}Z_{1}")
    assert b == "math_in_text"


def test_double_subscript_bucket():
    b, _ = classify_error(r"KaTeX parse error: Double subscript at position 4", r"V_g_{\max}")
    assert b == "double_script"


def test_tag_garble_from_dot_beats_text_mode():
    # \dot 落在 \tag 里报 "text mode",但本质是编号乱码,应归 tag_garble(需读原页)
    b, _ = classify_error(
        r"KaTeX parse error: Can't use function '\dot' in text mode at position 96: …ht] \tag{(2.1  \dot{5}6 )}",
        r"f_{L}=f_{0}\left[...\right]")
    assert b == "tag_garble"


def test_tag_garble_from_underscore():
    b, _ = classify_error(
        r"KaTeX parse error: Expected '}', got '_' at position 109: …x<\infty \tag{3_1606}",
        r"\left(\frac{\partial^{2}}{\partial x^{2}}...\right)e_{z}(x,y)=0")
    assert b == "tag_garble"


def test_clean_tag_is_not_garble():
    # 干净编号不误触 tag_garble;这里落到结构桶(有 \end{array})
    b, _ = classify_error(
        r"KaTeX parse error: Expected 'EOF', got '\end' at position 472: …}\end{array} \tag{2.46b}",
        r"\begin{array}{c}{I(z)=...}\end{array}")
    assert b == "structural_env"


def test_structural_env_bucket_bmatrix():
    b, _ = classify_error(
        r"KaTeX parse error: Unexpected end of input in a macro argument, expected '}'",
        r"\begin{bmatrix A & B \\ C & D \end{bmatrix}_{o}=...")
    assert b == "structural_env"


def test_brace_mismatch_bucket_when_no_env():
    b, _ = classify_error(
        r"KaTeX parse error: Unexpected end of input in a macro argument, expected '}'",
        r"Z_{i1}=\frac{\sqrt{(Z_{0e}Z_{0o}\sqrt{(Z_{0e}-Z_{0o})^{2}")
    assert b == "brace_mismatch"


def test_other_bucket_default():
    b, _ = classify_error(r"KaTeX parse error: 某种没见过的错误", r"x=1")
    assert b == "other"


def test_needs_vision_partition():
    # 确定性桶 needs_vision=False;视觉桶=True。抽查两类。
    from scripts.pipelines.textbooks.katex_triage import _BUCKETS
    assert _BUCKETS["undefined_command"][1] is False
    assert _BUCKETS["double_script"][1] is False
    assert _BUCKETS["structural_env"][1] is True
    assert _BUCKETS["tag_garble"][1] is True


def test_triage_assembles_buckets_and_vision_worklist(tmp_path):
    from scripts.pipelines.textbooks import katex_triage as kt
    from scripts.pipelines.textbooks.paths import resolve_layout

    layout = resolve_layout("Book", str(tmp_path / "d"), str(tmp_path / "w"))
    import os
    os.makedirs(layout.doc_deliverable_dir, exist_ok=True)
    open(layout.md_path, "w", encoding="utf-8").write("md\n")

    fake = {"errors": [
        {"error": r"Undefined control sequence: \upmu", "latex_head": r"1\upmu\mathrm{F}"},
        {"error": r"Undefined control sequence: \upmu", "latex_head": r"2\upmu\mathrm{V}"},
        {"error": r"Unexpected end of input in a macro argument", "latex_head": r"\begin{bmatrix A & B"},
        {"error": r"Expected '}', got '_' at position 5: \tag{3_1606}", "latex_head": r"x \tag{3_1606}"},
    ], "warnings": [1, 2, 3]}

    rep = kt.triage(layout, scan_fn=lambda md, out: fake, attribute=False)

    assert rep["hard_errors"] == 4 and rep["warnings"] == 3
    assert rep["buckets"] == {"undefined_command": 2, "structural_env": 1, "tag_garble": 1}
    assert rep["undefined_commands"] == {r"\upmu": 2}
    # 视觉工单只含 needs_vision 桶(structural_env + tag_garble),不含确定性的 undefined
    wl_buckets = sorted(e["bucket"] for e in rep["worklist"])
    assert wl_buckets == ["structural_env", "tag_garble"]
