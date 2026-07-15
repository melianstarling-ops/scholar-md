"""逐批持久化 attempt/模型/状态/耗时/错误/结果;中断后只续未完成批次。

原始 stdout/stderr 一并入 ledger —— 不丢现场(F8)。
"""
from __future__ import annotations

import hashlib
import json
import os

_TERMINAL = frozenset({"done", "blocked"})


def batch_id(candidate_ids: list[str]) -> str:
    """由 candidate_ids 确定性派生(顺序敏感),用于续跑时识别同一批。"""
    return hashlib.sha256("|".join(candidate_ids).encode("utf-8")).hexdigest()[:12]


def append_ledger(ledger_path: str, record: dict) -> None:
    os.makedirs(os.path.dirname(os.path.abspath(ledger_path)), exist_ok=True)
    with open(ledger_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_ledger(ledger_path: str) -> list[dict]:
    """文件不存在返回 [];损坏行跳过(不因半行崩掉整个续跑)。"""
    if not os.path.exists(ledger_path):
        return []
    rows: list[dict] = []
    with open(ledger_path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                obj = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(obj, dict):
                rows.append(obj)
    return rows


def _chunk(items: list, size: int) -> list[list]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def resume_pending(ledger: list[dict], candidates: list[dict], *,
                   batch_size: int = 10) -> list[dict]:
    """只返回没有终态(done/blocked)记录的批次里的候选。

    成功批次不重跑;中断批次可续跑。批次划分必须与主流程一致(同 batch_size、同顺序),
    否则 batch_id 对不上。
    """
    terminal = {row.get("batch_id") for row in ledger
                if row.get("status") in _TERMINAL and row.get("batch_id")}
    out: list[dict] = []
    for batch in _chunk(list(candidates), batch_size):
        if batch_id([c["candidate_id"] for c in batch]) not in terminal:
            out.extend(batch)
    return out
