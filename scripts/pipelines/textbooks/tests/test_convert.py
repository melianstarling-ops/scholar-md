import json
import os
import fitz
import pytest
from scripts.pipelines.textbooks import convert as cv
from scripts.pipelines.textbooks import checkpoint as cp


def _make_scan_pdf(tmp_path, n_pages):
    """无文本层 PDF(空白页) → triage 判 A。"""
    doc = fitz.open()
    for _ in range(n_pages):
        doc.new_page()
    p = tmp_path / "scan.pdf"
    doc.save(str(p))
    return str(p)


def _stub_engine(monkeypatch, behavior):
    """behavior: page(1-indexed)->blocks 或抛异常的可调用。桩掉 predict_page,
    并模拟 engine 对非空结果落 res.json 的副作用(空结果不落,复刻真 engine)。"""
    def fake_predict(png_path, work_dir):
        stem = os.path.splitext(os.path.basename(png_path))[0]  # page_0002
        page = int(stem.split("_")[1])
        blocks = behavior(page)                                 # 可能抛异常
        if blocks:                                              # 复刻 engine:非空才落盘
            os.makedirs(work_dir, exist_ok=True)
            with open(os.path.join(work_dir, f"{stem}_res.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"parsing_res_list": blocks}, f)
        return blocks
    monkeypatch.setattr(cv, "predict_page", fake_predict)


def _one_text_block(page):
    return [{"block_order": 0, "block_label": "text",
             "block_content": f"page {page} content"}]


def test_convert_full_run_A(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 3)
    _stub_engine(monkeypatch, _one_text_block)
    res = cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    assert res["route"] == "A"
    assert os.path.exists(res["md_path"])
    md = open(res["md_path"], encoding="utf-8").read()
    assert "page 1 content" in md and "page 3 content" in md
    assert res["failed_pages"] == []


def test_convert_disk_bounded(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 4)
    seen_png_counts = []

    def behavior(page):
        # predict 时快照 _work 里 png 数量,应 ≤1
        work = os.path.join(str(tmp_path / "out"), "scan", "_work")
        seen_png_counts.append(len([f for f in os.listdir(work) if f.endswith(".png")]))
        return _one_text_block(page)
    _stub_engine(monkeypatch, behavior)
    cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    assert max(seen_png_counts) <= 1
    # 结束后无残留 png
    work = os.path.join(str(tmp_path / "out"), "scan", "_work")
    assert [f for f in os.listdir(work) if f.endswith(".png")] == []


def test_convert_resume_skips_done(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 3)
    predicted = []
    def behavior(page):
        predicted.append(page)
        return _one_text_block(page)
    _stub_engine(monkeypatch, behavior)
    # 预置第 1、2 页检查点 + 匹配 manifest
    work = os.path.join(str(tmp_path / "out"), "scan", "_work")
    os.makedirs(work, exist_ok=True)
    for pg in (1, 2):
        with open(cp.page_res_path(work, pg), "w", encoding="utf-8") as f:
            json.dump({"parsing_res_list": _one_text_block(pg)}, f)
    m = cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 100, "A")
    cp.save_manifest(work, m)
    cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    assert predicted == [3]                       # 只跑缺失页


def test_convert_bad_page_isolated(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 3)
    def behavior(page):
        if page == 2:
            raise RuntimeError("boom on page 2")
        return _one_text_block(page)
    _stub_engine(monkeypatch, behavior)
    res = cv.convert_pdf(pdf, str(tmp_path / "out"), dpi=100)
    md = open(res["md_path"], encoding="utf-8").read()
    assert "page 1 content" in md and "page 3 content" in md
    assert [f["page"] for f in res["failed_pages"]] == [2]
    assert res["failed_pages"][0]["kind"] == "page-exception"


def test_convert_empty_page_checkpointed(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 2)
    calls = []
    def behavior(page):
        calls.append(page)
        return [] if page == 1 else _one_text_block(page)  # 第1页空白
    _stub_engine(monkeypatch, behavior)
    out = str(tmp_path / "out")
    cv.convert_pdf(pdf, out, dpi=100)
    work = os.path.join(out, "scan", "_work")
    assert cp.is_page_done(work, 1) is True            # 空白页也落了检查点
    # 再跑一次:空白页不应被重跑
    calls.clear()
    cv.convert_pdf(pdf, out, dpi=100)
    assert calls == []


def test_convert_fingerprint_mismatch_wipes(tmp_path, monkeypatch):
    pdf = _make_scan_pdf(tmp_path, 2)
    _stub_engine(monkeypatch, _one_text_block)
    out = str(tmp_path / "out")
    work = os.path.join(out, "scan", "_work")
    os.makedirs(work, exist_ok=True)
    # 预置一个 DPI 不同的旧 manifest + 一页旧检查点
    with open(cp.page_res_path(work, 1), "w", encoding="utf-8") as f:
        json.dump({"parsing_res_list": [{"block_order": 0, "block_label": "text",
                                         "block_content": "STALE 150dpi"}]}, f)
    cp.save_manifest(work, cp.new_manifest(pdf, cp.pdf_fingerprint(pdf), 150, "A"))
    cv.convert_pdf(pdf, out, dpi=100)                   # 请求 100 ≠ 记录 150
    md = open(os.path.join(out, "scan", "scan.md"), encoding="utf-8").read()
    assert "STALE" not in md                            # 旧检查点被清空重跑


def test_convert_route_B_registers(tmp_path, monkeypatch):
    # 有优质文本层 → triage 判 B,登记不转
    doc = fitz.open()
    for _ in range(3):
        pg = doc.new_page()
        pg.insert_text((72, 72), "the quick brown fox jumps over the lazy dog " * 8)
    pdf = tmp_path / "born.pdf"
    doc.save(str(pdf))
    res = cv.convert_pdf(str(pdf), str(tmp_path / "out"))
    assert res["route"] == "B"
    assert res["md_path"] is None
