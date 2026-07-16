import base64
import json
from pathlib import Path

from scripts.pipelines.textbooks.formula_benchmark_report import (
    load_report_data,
    normalize_latex,
    render_report,
)


def _write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload), encoding="utf-8")


def _result(candidate_id: str, latex: str) -> dict:
    return {
        "candidate_id": candidate_id,
        "classification": "formula",
        "latex": latex,
        "confidence": "high",
        "note": "",
    }


def _make_run(tmp_path: Path, name: str, jobs: list[dict], *, first: bool) -> Path:
    run = tmp_path / name
    images = run / "images"
    images.mkdir(parents=True)
    candidates = []
    for index, candidate_id in enumerate(("p0001-b0001", "p0002-b0002"), 1):
        image = images / f"{candidate_id}.png"
        image.write_bytes(b"\x89PNG\r\n\x1a\n" + bytes([index]))
        candidates.append({
            "candidate_id": candidate_id,
            "page": index,
            "block_id": index,
            "block_label": "display_formula",
            "reasons": ["fixture"],
            "engine_latex": "$$ bad $$",
            "image_path": str(image),
        })
    _write_json(run / "manifest.json", {
        "run_id": name,
        "candidate_count": 2,
        "candidates": candidates,
    })
    if first:
        _write_json(run / "root_baseline.json", {"results": [
            _result("p0001-b0001", "x"),
            _result("p0002-b0002", "y"),
        ]})
    statuses = []
    for job in jobs:
        status = {key: value for key, value in job.items() if key != "results"}
        statuses.append(status)
        if job["valid"]:
            _write_json(run / "jobs" / job["job_id"] / "result.json", {
                "results": job["results"],
            })
            _write_json(run / "jobs" / job["job_id"] / "status.json", status)
    summary_name = "validated_summary.json" if first else "run_summary.json"
    _write_json(run / summary_name, {"records" if first else "statuses": statuses})
    return run


def test_normalize_latex_only_collapses_wrappers_and_whitespace():
    assert normalize_latex(" $$  x + y  $$ ") == "x+y"
    assert normalize_latex(r"\text{two words} + x") == r"\text{two words}+x"
    assert normalize_latex(r"\vec{x}") != normalize_latex(r"\overrightarrow{x}")


def test_load_report_data_merges_two_rounds_and_keeps_failures(tmp_path):
    run1 = _make_run(tmp_path, "round-one", [
        {
            "job_id": "claude-one", "backend": "claude", "model": "sonnet",
            "effort": "high", "duration_seconds": 12.5, "valid": True, "error": "",
            "results": [_result("p0001-b0001", " $x$ "), _result("p0002-b0002", "y")],
        },
        {
            "job_id": "agy-failed", "backend": "agy", "model": "flash",
            "effort": "high", "duration_seconds": 2.0, "valid": False,
            "error": "missing results",
        },
    ], first=True)
    run2 = _make_run(tmp_path, "round-two", [
        {
            "job_id": "kimi-two", "backend": "kimi", "model": "kimi",
            "effort": "thinking", "duration_seconds": 9.0, "valid": True, "error": "",
            "results": [_result("p0001-b0001", "x"), _result("p0002-b0002", "z")],
        },
    ], first=False)
    stale = json.loads((run2 / "manifest.json").read_text(encoding="utf-8"))
    stale["run_id"] = "round-one"  # delta fixture copied the first manifest unchanged
    _write_json(run2 / "manifest.json", stale)

    data = load_report_data([run1, run2])

    assert [row["candidate_id"] for row in data["candidates"]] == [
        "p0001-b0001", "p0002-b0002",
    ]
    assert data["summary"] == {"candidate_count": 2, "valid_model_count": 2,
                               "failed_model_count": 1, "run_count": 2}
    assert len(data["candidates"][0]["groups"]) == 1
    assert data["candidates"][0]["groups"][0]["matches_root"] is True
    assert len(data["candidates"][1]["groups"]) == 2
    assert data["candidates"][1]["has_disagreement"] is True
    assert data["models"][1]["valid"] is False
    assert data["runs"][1]["run_id"] == "round-two"
    assert next(model for model in data["models"] if model["round_index"] == 2)["round_id"] == "round-two"
    assert base64.b64decode(data["candidates"][0]["image_b64"]).startswith(b"\x89PNG")


def test_load_report_data_can_keep_only_manually_confirmed_errors(tmp_path):
    run1 = _make_run(tmp_path, "round-one", [{
        "job_id": "claude-one", "backend": "claude", "model": "sonnet",
        "effort": "high", "duration_seconds": 12.5, "valid": True, "error": "",
        "results": [_result("p0001-b0001", "wrong-x"), _result("p0002-b0002", "wrong-y")],
    }], first=True)
    run2 = _make_run(tmp_path, "round-two", [{
        "job_id": "kimi-two", "backend": "kimi", "model": "kimi",
        "effort": "thinking", "duration_seconds": 9.0, "valid": True, "error": "",
        "results": [_result("p0001-b0001", "x"), _result("p0002-b0002", "wrong-z")],
    }], first=False)

    data = load_report_data([run1, run2], confirmed_errors={
        "r1:claude-one": {"p0002-b0002"},
        "r2:kimi-two": {"p0002-b0002"},
    })

    assert [row["candidate_id"] for row in data["candidates"]] == ["p0002-b0002"]
    candidate = data["candidates"][0]
    assert candidate["root"]["latex"] == "y"
    assert {row["model_key"] for row in candidate["all_models"]} == {
        "r1:claude-one", "r2:kimi-two",
    }
    assert all(not group["matches_root"] for group in candidate["groups"])
    assert data["summary"]["candidate_count"] == 1


def test_render_report_is_self_contained_and_exposes_review_controls(tmp_path):
    run1 = _make_run(tmp_path, "round-one", [{
        "job_id": "claude-one", "backend": "claude", "model": "sonnet",
        "effort": "high", "duration_seconds": 12.5, "valid": True, "error": "",
        "results": [_result("p0001-b0001", "x"), _result("p0002-b0002", "y")],
    }], first=True)
    run2 = _make_run(tmp_path, "round-two", [{
        "job_id": "kimi-two", "backend": "kimi", "model": "kimi",
        "effort": "thinking", "duration_seconds": 9.0, "valid": True, "error": "",
        "results": [_result("p0001-b0001", "x"), _result("p0002-b0002", "z")],
    }], first=False)

    data = load_report_data([run1, run2])
    data["candidates"][0]["root"]["note"] = "</script>"
    html = render_report(data)

    assert "data:image/png;base64," in html
    assert "katex.render" in html
    assert "id=\"filter-differences\"" in html
    assert "id=\"view-all-models\"" in html
    assert "round-one" in html and "round-two" in html
    assert 'src="http' not in html and 'href="http' not in html
    assert "<\\/script>" in html
    assert "render-failed" in html
    assert "KaTeX 无法渲染" in html
