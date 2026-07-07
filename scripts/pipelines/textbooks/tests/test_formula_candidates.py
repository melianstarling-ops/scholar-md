import json
import os

from PIL import Image

from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks.formula_candidates import (
    candidate_id,
    collect_formula_candidates,
    estimate_candidate_tokens,
)
from scripts.pipelines.textbooks.paths import resolve_layout


def _write_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _write_page(layout, page, blocks):
    os.makedirs(layout.work_dir, exist_ok=True)
    _write_json(cp.page_res_path(layout.work_dir, page), {
        "width": 200,
        "height": 300,
        "parsing_res_list": blocks,
    })


def _block(block_id=3, content=r"$$ \oint E \cdot dl $$"):
    return {
        "block_label": "display_formula",
        "block_id": block_id,
        "block_order": 1,
        "block_bbox": [10, 20, 110, 70],
        "block_content": content,
    }


def _write_worklist(layout, items):
    _write_json(layout.worklist_path, {
        "stem": layout.stem,
        "count": len(items),
        "items": items,
    })


def _worklist_item(page=1, block_id=3, kinds=("bare_op",), crop_path=None):
    return {
        "page": page,
        "block_id": block_id,
        "bbox": [10, 20, 110, 70],
        "kinds": list(kinds),
        "ops": [r"\oint"],
        "engine_latex": r"$$ \oint E \cdot dl $$",
        "crop_path": crop_path,
    }


def test_candidate_id_is_display_only_and_stable():
    assert candidate_id(49, 3) == "p0049-b0003"


def test_collects_worklist_item_as_candidate(tmp_path):
    layout = resolve_layout("Book", str(tmp_path / "out"))
    _write_page(layout, 1, [_block()])
    _write_worklist(layout, [_worklist_item()])

    result = collect_formula_candidates(layout)

    candidates = result["candidates"]
    assert len(candidates) == 1
    assert candidates[0]["candidate_id"] == "p0001-b0003"
    assert candidates[0]["reasons"] == ["worklist:bare_op"]
    assert candidates[0]["block_label"] == "display_formula"
    assert candidates[0]["estimate_basis"] == "bbox_proxy"
    assert result["summary"]["by_reason"] == {"worklist": 1}


def test_collects_katex_warning_from_warnings_array(tmp_path):
    layout = resolve_layout("Book", str(tmp_path / "out"))
    _write_page(layout, 2, [_block(block_id=7, content=r"$$ \Omega $$")])
    _write_json(layout.render_errors_path, {
        "total": 1,
        "errors": [],
        "warnings": [{
            "page": 2,
            "mode": "display",
            "code": "unicodeTextInMathMode",
            "block_ids": [7],
            "message": "Unicode text character",
            "latex_head": r"\Omega",
        }],
    })

    result = collect_formula_candidates(layout)

    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["reasons"] == ["katex_warning:unicodeTextInMathMode"]
    assert result["summary"]["by_reason"] == {"katex_warning": 1}


def test_merges_sources_and_does_not_duplicate_worklist_render_error(tmp_path):
    layout = resolve_layout("Book", str(tmp_path / "out"))
    _write_page(layout, 3, [_block(block_id=9, content=r"$$ \frac{x}{y} $$")])
    _write_worklist(layout, [_worklist_item(page=3, block_id=9, kinds=("render_error", "bare_op"))])
    _write_json(layout.render_errors_path, {
        "total": 1,
        "errors": [{
            "page": 3,
            "mode": "display",
            "block_ids": [9],
            "error": "ParseError: Expected '}'",
            "latex_head": r"\frac{x}{y",
        }],
        "warnings": [],
    })

    result = collect_formula_candidates(layout)

    assert len(result["candidates"]) == 1
    assert result["candidates"][0]["reasons"] == [
        "katex_error:ParseError",
        "worklist:bare_op",
    ]
    assert result["summary"]["raw_reason_hits"] == 2
    assert result["summary"]["deduped_count"] == 1
    assert result["summary"]["by_reason"] == {"katex_error": 1, "worklist": 1}


def test_existing_crop_uses_crop_estimate_basis(tmp_path):
    layout = resolve_layout("Book", str(tmp_path / "out"))
    crop = tmp_path / "crop.png"
    Image.new("RGB", (40, 30), color="white").save(crop)
    _write_page(layout, 1, [_block()])
    _write_worklist(layout, [_worklist_item(crop_path=str(crop))])

    result = collect_formula_candidates(layout)

    candidate = result["candidates"][0]
    assert candidate["estimate_basis"] == "crop"
    assert candidate["estimated_pixels"] == 1200
    assert result["summary"]["estimate_basis_counts"] == {"crop": 1}
    assert result["summary"]["crop_pixels"]["max_single"] == {
        "width": 40,
        "height": 30,
        "pixels": 1200,
    }


def test_selfcheck_missing_is_unresolved_not_candidate(tmp_path):
    layout = resolve_layout("Book", str(tmp_path / "out"))
    _write_json(layout.selfcheck_path, {
        "missing": ["lost formula fragment"],
    })

    result = collect_formula_candidates(layout)

    assert result["candidates"] == []
    assert result["summary"]["unresolved_inputs"]["selfcheck_missing"]["count"] == 1


def test_writes_jsonl_and_summary(tmp_path):
    layout = resolve_layout("Book", str(tmp_path / "out"))
    _write_page(layout, 1, [_block()])
    _write_worklist(layout, [_worklist_item()])

    result = collect_formula_candidates(layout, write=True)

    assert result["summary"]["outputs"] == {
        "jsonl": layout.formula_candidates_path,
        "summary": layout.formula_candidates_summary_path,
    }
    with open(layout.formula_candidates_path, encoding="utf-8") as f:
        lines = [json.loads(line) for line in f if line.strip()]
    assert len(lines) == result["summary"]["deduped_count"] == 1
    with open(layout.formula_candidates_summary_path, encoding="utf-8") as f:
        summary = json.load(f)
    assert summary["deduped_count"] == 1


def test_token_estimator_is_deterministic():
    estimate = estimate_candidate_tokens([{"estimated_pixels": 1200}, {"estimated_pixels": 800}])
    assert estimate == {
        "image_tokens_low": 1,
        "image_tokens_high": 2,
        "output_tokens_low": 180,
        "output_tokens_high": 420,
        "total_tokens_low": 181,
        "total_tokens_high": 422,
    }


# TODO: add a vision_repair adapter compatibility test when candidates become
# the direct input to model execution.
