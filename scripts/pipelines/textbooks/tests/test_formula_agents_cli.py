from scripts.pipelines.textbooks.formula_agents import cli


def test_crops_only_collect_keeps_crop_bearing_or_hard_errors(monkeypatch):
    """--crops-only 留"带裁图 或 硬错";无裁图的纯警告被滤掉。"""
    fake = {"candidates": [
        {"candidate_id": "p0001-b0001", "crop_path": "/x/a.png",
         "reasons": ["worklist:bare_op"]},                                # 带裁图 → 留
        {"candidate_id": "p0002-b0002", "crop_path": None,
         "reasons": ["katex_warning:unicodeTextInMathMode"]},             # 无裁图纯警告 → 滤
        {"candidate_id": "p0003-b0003", "crop_path": "",
         "reasons": ["katex_error:KaTeX parse error"]},                   # 无裁图硬错 → 留
        {"candidate_id": "p0004-b0004", "crop_path": "/x/b.png",
         "reasons": ["katex_warning:unknownSymbol"]},                     # 带裁图 → 留
    ], "summary": {"stem": "X"}}
    monkeypatch.setattr(cli, "collect_formula_candidates", lambda layout: fake)

    out = cli.crops_only_collect(layout=object())

    ids = [c["candidate_id"] for c in out["candidates"]]
    assert ids == ["p0001-b0001", "p0003-b0003", "p0004-b0004"]   # b0002(无图纯警告)被滤
    assert out["summary"] == {"stem": "X"}
