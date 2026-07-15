"""公式修正叠加层(§2 设计):读 `<stem>_corrections.json`,按 (page, block_id) +
content_fingerprint 覆盖 block_content。res.json 原始内容永不改动——修正只是一层
可回滚/可审计的覆盖,convert.py assemble() 在 reconstruct 之前调用。这是可选后处理
阶段:没有 corrections.json(未跑视觉修复/该文档无疑似块)时行为与不存在这一层完全一致。
"""
from __future__ import annotations

import json
import os

from scripts.pipelines.textbooks.vision_repair import content_fingerprint


def load_corrections(doc_dir: str) -> list[dict]:
    """读 `<stem>_corrections.json`;文件不存在时返回 []。"""
    stem = os.path.basename(os.path.normpath(doc_dir))
    path = os.path.join(doc_dir, f"{stem}_corrections.json")
    if not os.path.exists(path):
        return []
    with open(path, encoding="utf-8") as f:
        return json.load(f).get("corrections", [])


_VALID_STATUSES = {"pending", "accepted", "rejected"}


def set_correction_status(doc_dir: str, page: int, block_id, status: str) -> bool:
    """把匹配 (page, block_id) 的修正记录状态改为 status,写回 `<stem>_corrections.json`。
    这是人工确认门的写入端(debug 视图的采纳/驳回按钮走这里)。找不到匹配项返回 False、
    不改文件;status 非法直接抛 ValueError(拒绝带病写)。"""
    if status not in _VALID_STATUSES:
        raise ValueError(f"非法 status {status!r},须为 {_VALID_STATUSES}")
    stem = os.path.basename(os.path.normpath(doc_dir))
    path = os.path.join(doc_dir, f"{stem}_corrections.json")
    if not os.path.exists(path):
        return False
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    found = False
    for c in data.get("corrections", []):
        if c.get("page") == page and c.get("block_id") == block_id:
            c["status"] = status
            found = True
    if found:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    return found


def apply_corrections(blocks: list[dict], page: int, corrections: list[dict]) -> list[dict]:
    """返回新 blocks 列表(不原地改动入参);(page, block_id) 命中、fingerprint 与当前
    block_content 一致、且 status=="accepted" 才替换 block_content。

    status=="accepted" 的两个来源(2026-07-14 起):
      1. 人工在 debug 视图采纳(propose / 熔断模式);
      2. formula_agents 编排层通过五道准入闸后自动置为 accepted(默认全自动模式)。

    fingerprint 不匹配(res.json 漂移/重跑变了内容)或未采纳(pending/rejected/缺 status)
    则不应用 —— 宁可不修,不错配。这道指纹门在全自动模式下依然是红线:它防的是
    "md 中途漂移导致改错位置",与谁拍板无关。
    """
    by_block_id = {c["block_id"]: c for c in corrections
                   if c.get("page") == page and c.get("status") == "accepted"}
    out = []
    for b in blocks:
        c = by_block_id.get(b.get("block_id"))
        if c and content_fingerprint(b.get("block_content") or "") == c.get("content_fingerprint"):
            b = {**b, "block_content": c["corrected_latex"]}
        out.append(b)
    return out
