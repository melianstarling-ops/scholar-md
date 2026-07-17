"""route_b_fixture 轻量 schema 测试(零 GPU;GPU 采样走私有 smoke,不进 pytest)。"""
import json
import os

import pytest

from scripts.pipelines.textbooks import route_b_fixture as rf


def test_fixture_dir_name_ascii_only():
    assert rf.fixture_dir_name("steensma", 55) == "steensma_p55"
    with pytest.raises(ValueError):
        rf.fixture_dir_name("Steensma", 55)
    with pytest.raises(ValueError):
        rf.fixture_dir_name("样本", 1)


def test_manifest_entry_fields():
    e = rf.manifest_entry(label="ngx_clean_prose", page=65, source="a.pdf",
                          dpi=150, fingerprint={"size_bytes": 1, "sha256": "x"},
                          title="干净正文", mode="gpu_sample", runtime_s=12.34,
                          paddle_version="3.3.0")
    assert e["dir"] == "ngx_clean_prose_p65"
    assert e["mode"] == "gpu_sample"
    assert e["paddle_pipeline_version"] == "v1.6"
    assert e["runtime_s"] == 12.3
    assert e["expected_frozen"] is False


def test_manifest_roundtrip(tmp_path):
    root = str(tmp_path)
    m = rf.load_manifest(root)
    assert m == {"schema_version": 1, "fixtures": {}}
    m["fixtures"]["x_p1"] = {"dir": "x_p1"}
    rf.save_manifest(root, m)
    assert rf.load_manifest(root)["fixtures"]["x_p1"]["dir"] == "x_p1"
    assert not os.path.exists(os.path.join(root, "manifest.json.tmp"))


def test_reuse_fixture_copies_res_and_keeps_expected(tmp_path):
    res = tmp_path / "page_0112_res.json"
    res.write_text(json.dumps({"parsing_res_list": [{"block_label": "table"}]}),
                   encoding="utf-8")
    root = str(tmp_path / "fixtures")
    e = rf.build_reuse_fixture(str(res), 112, "table_pozar", root, title=None)
    d = os.path.join(root, "table_pozar_p112")
    assert json.load(open(os.path.join(d, "ocr_res.json"), encoding="utf-8"))[
        "parsing_res_list"][0]["block_label"] == "table"
    assert e["mode"] == "reuse_res" and e["dpi"] is None
    # expected_audit.json 骨架已建且重复构建不覆盖
    exp = os.path.join(d, "expected_audit.json")
    with open(exp, "w", encoding="utf-8") as f:
        json.dump({"status": "OK", "frozen_by_owner": True}, f)
    rf.build_reuse_fixture(str(res), 112, "table_pozar", root, title=None)
    assert json.load(open(exp, encoding="utf-8"))["status"] == "OK"
