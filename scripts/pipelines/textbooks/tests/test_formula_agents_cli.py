from scripts.pipelines.textbooks.formula_agents import cli


def test_crops_only_collect_keeps_only_crop_bearing(monkeypatch):
    """--crops-only 只留带裁图候选;无裁图的(多为 KaTeX 警告)被滤掉。"""
    fake = {"candidates": [
        {"candidate_id": "p0001-b0001", "crop_path": "/x/a.png"},
        {"candidate_id": "p0002-b0002", "crop_path": None},          # 无裁图 → 滤
        {"candidate_id": "p0003-b0003", "crop_path": ""},            # 空 → 滤
        {"candidate_id": "p0004-b0004", "crop_path": "/x/b.png"},
    ], "summary": {"stem": "X"}}
    monkeypatch.setattr(cli, "collect_formula_candidates", lambda layout: fake)

    out = cli.crops_only_collect(layout=object())

    ids = [c["candidate_id"] for c in out["candidates"]]
    assert ids == ["p0001-b0001", "p0004-b0004"]
    assert out["summary"] == {"stem": "X"}
