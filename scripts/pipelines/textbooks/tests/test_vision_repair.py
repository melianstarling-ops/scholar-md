import json
import os

import pytest

from scripts.pipelines.textbooks.paths import resolve_layout
import scripts.pipelines.textbooks.vision_repair as vision_repair
from scripts.pipelines.textbooks.vision_repair import content_fingerprint, parse_vision_response


def test_content_fingerprint_deterministic():
    a = content_fingerprint(r"$$ c\Delta z=\frac{a}{c^{\prime}} $$")
    b = content_fingerprint(r"$$ c\Delta z=\frac{a}{c^{\prime}} $$")
    assert a == b


def test_content_fingerprint_differs_for_different_content():
    a = content_fingerprint(r"$$ c\Delta z=\frac{a}{c^{\prime}} $$")
    b = content_fingerprint(r"$$ E = mc^2 $$")
    assert a != b


def _envelope(inner_json_text):
    return json.dumps({
        "type": "result", "subtype": "success", "is_error": False,
        "result": inner_json_text,
        "total_cost_usd": 0.0527,
    })


def test_parse_vision_response_extracts_latex_and_confidence():
    stdout = _envelope(json.dumps({"latex": r"\frac{a}{b}", "confidence": "high"}))
    out = parse_vision_response(stdout)
    assert out["latex"] == r"\frac{a}{b}"
    assert out["confidence"] == "high"
    assert out["cost_usd"] == 0.0527


def test_parse_vision_response_strips_markdown_fence_in_result():
    fenced = "```json\n" + json.dumps({"latex": "x", "confidence": "low"}) + "\n```"
    stdout = _envelope(fenced)
    out = parse_vision_response(stdout)
    assert out["latex"] == "x"


def test_parse_vision_response_raises_on_malformed_outer_json():
    with pytest.raises(ValueError):
        parse_vision_response("not json at all")


def test_parse_vision_response_raises_on_malformed_inner_json():
    stdout = _envelope("not json either")
    with pytest.raises(ValueError):
        parse_vision_response(stdout)


def test_call_claude_vision_invokes_subprocess_with_crop_path(monkeypatch):
    captured = {}

    class FakeResult:
        stdout = _envelope(json.dumps({"latex": "x", "confidence": "high"}))

    def fake_run(argv, input=None, **kwargs):
        captured["argv"] = argv
        captured["input"] = input
        return FakeResult()

    monkeypatch.setattr(vision_repair.subprocess, "run", fake_run)
    out = vision_repair.call_claude_vision("C:/crops/eq_1.58.png")

    assert "-p" in captured["argv"]
    assert "--strict-mcp-config" in captured["argv"]
    assert "--output-format" in captured["argv"]
    assert "json" in captured["argv"]
    assert "C:/crops/eq_1.58.png" in captured["input"]
    assert out["latex"] == "x"
    assert out["confidence"] == "high"


def _write_worklist(layout, items):
    os.makedirs(os.path.dirname(layout.worklist_path), exist_ok=True)
    path = layout.worklist_path
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"stem": layout.stem, "count": len(items), "items": items}, f)
    return path


def _item(page, block_id, crop_path, engine_latex="$$ a $$", kinds=("bare_op",)):
    return {"page": page, "block_id": block_id, "bbox": [0, 0, 1, 1], "kinds": list(kinds),
            "ops": [r"\oint"], "engine_latex": engine_latex, "crop_path": crop_path}


def test_run_vision_repair_uses_batch_fn_once_for_all_items(tmp_path):
    layout = resolve_layout("book", str(tmp_path / "out"))
    _write_worklist(layout, [
        _item(49, 3, "eq3.png", r"$$ c\Delta z=\frac{a}{c^{\prime}} $$",
              ("bare_op", "frac_primed_denom")),
        _item(49, 6, "eq6.png"),
    ])
    calls = []

    def fake_batch(entries, timeout=300):
        calls.append(entries)
        return {"49_3": {"latex": r"c\Delta z=\oint_{c'} a", "confidence": "high"},
                "49_6": {"latex": "fixed6", "confidence": "medium"}}

    result = vision_repair.run_vision_repair(layout, batch_fn=fake_batch, batch_size=10)

    assert len(calls) == 1                      # 一次调用打包了两项,不是两次单图调用
    assert result["count"] == 2
    assert result["failed"] == []
    assert result["corrections_path"] == layout.corrections_path
    with open(result["corrections_path"], encoding="utf-8") as f:
        data = json.load(f)
    by_id = {c["block_id"]: c for c in data["corrections"]}
    assert by_id[3]["kind"] == "bare_op+frac_primed_denom"
    assert by_id[3]["corrected_latex"] == r"$$ c\Delta z=\oint_{c'} a $$"
    assert by_id[3]["content_fingerprint"] == \
        content_fingerprint(r"$$ c\Delta z=\frac{a}{c^{\prime}} $$")
    assert by_id[6]["corrected_latex"] == "$$ fixed6 $$"


def test_run_vision_repair_splits_into_multiple_batches_by_batch_size(tmp_path):
    layout = resolve_layout("book", str(tmp_path / "out"))
    _write_worklist(layout, [_item(1, 1, "a.png"), _item(1, 2, "b.png")])
    calls = []

    def fake_batch(entries, timeout=300):
        calls.append(entries)
        return {e["key"]: {"latex": "x", "confidence": "high"} for e in entries}

    vision_repair.run_vision_repair(layout, batch_fn=fake_batch, batch_size=1)
    assert len(calls) == 2                      # batch_size=1 → 每项各成一批


def test_run_vision_repair_falls_back_to_single_call_for_key_missing_from_batch(tmp_path):
    layout = resolve_layout("book", str(tmp_path / "out"))
    _write_worklist(layout, [_item(1, 1, "a.png"), _item(1, 2, "b.png")])

    def partial_batch(entries, timeout=300):
        return {"1_1": {"latex": "x1", "confidence": "high"}}   # 漏了 1_2

    def fake_single(crop_path, timeout=120):
        assert crop_path == "b.png"
        return {"latex": "x2", "confidence": "medium"}

    result = vision_repair.run_vision_repair(layout, batch_fn=partial_batch,
                                             vision_fn=fake_single, batch_size=10)
    assert result["count"] == 2
    assert result["failed"] == []
    assert result["corrections_path"] == layout.corrections_path
    with open(result["corrections_path"], encoding="utf-8") as f:
        data = json.load(f)
    by_id = {c["block_id"]: c["corrected_latex"] for c in data["corrections"]}
    assert by_id == {1: "$$ x1 $$", 2: "$$ x2 $$"}


def test_run_vision_repair_falls_back_to_single_call_when_whole_batch_raises(tmp_path):
    layout = resolve_layout("book", str(tmp_path / "out"))
    _write_worklist(layout, [_item(1, 1, "a.png"), _item(1, 2, "b.png")])

    def raising_batch(entries, timeout=300):
        raise RuntimeError("batch boom")

    def fake_single(crop_path, timeout=120):
        return {"latex": f"fixed:{crop_path}", "confidence": "low"}

    result = vision_repair.run_vision_repair(layout, batch_fn=raising_batch,
                                             vision_fn=fake_single, batch_size=10)
    assert result["count"] == 2
    assert result["failed"] == []


def test_run_vision_repair_records_failed_when_single_fallback_also_fails(tmp_path):
    layout = resolve_layout("book", str(tmp_path / "out"))
    _write_worklist(layout, [_item(1, 1, "bad.png"), _item(1, 2, "good.png")])

    def partial_batch(entries, timeout=300):
        return {"1_2": {"latex": "good", "confidence": "high"}}   # 漏了 1_1

    def flaky_single(crop_path, timeout=120):
        if crop_path == "bad.png":
            raise ValueError("boom")
        return {"latex": "unused", "confidence": "high"}

    result = vision_repair.run_vision_repair(layout, batch_fn=partial_batch,
                                             vision_fn=flaky_single, batch_size=10)
    assert result["count"] == 1
    assert len(result["failed"]) == 1
    assert result["failed"][0]["block_id"] == 1
    assert result["corrections_path"] == layout.corrections_path
    with open(result["corrections_path"], encoding="utf-8") as f:
        data = json.load(f)
    assert [c["block_id"] for c in data["corrections"]] == [2]


def test_resolve_claude_bin_prefers_node_entry_when_present(tmp_path, monkeypatch):
    node_modules = tmp_path / "node_modules" / "@anthropic-ai" / "claude-code"
    node_modules.mkdir(parents=True)
    entry = node_modules / "cli-wrapper.cjs"
    entry.write_text("", encoding="utf-8")
    shim = tmp_path / "claude.cmd"
    shim.write_text("", encoding="utf-8")

    def fake_which(name):
        return str(shim) if "claude" in name else "C:/node/node.exe"

    monkeypatch.setattr(vision_repair.shutil, "which", fake_which)
    argv = vision_repair._resolve_claude_bin()
    assert argv == ["C:/node/node.exe", str(entry)]


def test_resolve_claude_bin_falls_back_to_cmd_wrap_without_node_entry(tmp_path, monkeypatch):
    shim = tmp_path / "claude.cmd"
    shim.write_text("", encoding="utf-8")

    def fake_which(name):
        return str(shim) if "claude" in name else None

    monkeypatch.setattr(vision_repair.shutil, "which", fake_which)
    argv = vision_repair._resolve_claude_bin()
    assert argv == ["cmd", "/c", str(shim)]


def test_resolve_claude_bin_direct_exe_when_no_shim_or_node(tmp_path, monkeypatch):
    exe = tmp_path / "claude.exe"
    exe.write_text("", encoding="utf-8")

    def fake_which(name):
        return str(exe) if "claude" in name else None

    monkeypatch.setattr(vision_repair.shutil, "which", fake_which)
    argv = vision_repair._resolve_claude_bin()
    assert argv == [str(exe)]


def test_build_vision_prompt_asks_for_mathcal_over_mathscr():
    prompt = vision_repair.build_vision_prompt("x.png")
    assert r"\mathcal" in prompt


def test_extract_json_array_parses_clean_array():
    arr = vision_repair._extract_json_array('[{"key": "1", "latex": "x"}]')
    assert arr == [{"key": "1", "latex": "x"}]


def test_extract_json_array_strips_markdown_fence():
    text = "```json\n" + json.dumps([{"key": "1", "latex": "x"}]) + "\n```"
    arr = vision_repair._extract_json_array(text)
    assert arr == [{"key": "1", "latex": "x"}]


def test_extract_json_array_finds_array_amid_noise():
    text = "Sure, here you go:\n" + json.dumps([{"key": "1", "latex": "x"}]) + "\nDone."
    arr = vision_repair._extract_json_array(text)
    assert arr == [{"key": "1", "latex": "x"}]


def test_extract_json_array_raises_on_no_array():
    with pytest.raises(ValueError):
        vision_repair._extract_json_array("no array here")


def test_build_batch_vision_prompt_lists_all_keys_and_paths():
    entries = [{"key": "49_3", "crop_path": "C:/a/eq3.png"},
               {"key": "49_6", "crop_path": "C:/a/eq6.png"}]
    prompt = vision_repair.build_batch_vision_prompt(entries)
    assert "49_3" in prompt and "C:/a/eq3.png" in prompt
    assert "49_6" in prompt and "C:/a/eq6.png" in prompt
    assert r"\mathcal" in prompt


def test_parse_batch_vision_response_keys_results_by_key():
    inner = json.dumps([
        {"key": "49_3", "latex": "x3", "confidence": "high"},
        {"key": "49_6", "latex": "x6", "confidence": "medium"},
    ])
    stdout = _envelope(inner)
    out = vision_repair.parse_batch_vision_response(stdout)
    assert out["49_3"] == {"latex": "x3", "confidence": "high"}
    assert out["49_6"] == {"latex": "x6", "confidence": "medium"}


def test_parse_batch_vision_response_raises_on_malformed_outer_json():
    with pytest.raises(ValueError):
        vision_repair.parse_batch_vision_response("not json")


def test_call_claude_vision_batch_invokes_subprocess_once_for_all_entries(monkeypatch):
    captured = {}

    class FakeResult:
        stdout = _envelope(json.dumps([
            {"key": "49_3", "latex": "x3", "confidence": "high"},
            {"key": "49_6", "latex": "x6", "confidence": "high"},
        ]))

    def fake_run(argv, input=None, **kwargs):
        captured["argv"] = argv
        captured["input"] = input
        return FakeResult()

    monkeypatch.setattr(vision_repair.subprocess, "run", fake_run)
    entries = [{"key": "49_3", "crop_path": "C:/a/eq3.png"},
               {"key": "49_6", "crop_path": "C:/a/eq6.png"}]
    out = vision_repair.call_claude_vision_batch(entries)

    assert "-p" in captured["argv"]
    assert "--strict-mcp-config" in captured["argv"]
    assert "C:/a/eq3.png" in captured["input"] and "C:/a/eq6.png" in captured["input"]
    assert out["49_3"]["latex"] == "x3"
    assert out["49_6"]["latex"] == "x6"


def test_correction_record_defaults_status_to_pending():
    item = {"page": 49, "block_id": 3, "kinds": ["bare_op"],
            "engine_latex": "$$ a $$"}
    rec = vision_repair._correction_record(item, {"latex": "x", "confidence": "high"}, "2026-07-04")
    assert rec["status"] == "pending"
