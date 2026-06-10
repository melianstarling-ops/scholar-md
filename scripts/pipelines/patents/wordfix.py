"""wordfix.py — 词内空格断裂重连的【词典成员判定】数据源（SOP：可读性修，确定性）。

只提供 `is_word(w)`：w（大小写不敏感）是否英语常用词。供 `reading_order.rejoin_split_words`
的 narrow 判据用。**刻意只用词典做"是不是词"的判定**——不调用 SymSpell 的 `lookup_compound`
/ `word_segmentation` / bigram：实测它们在专利语料上对技术术语幻觉（`neurostimu lation`→
`euro time nation`）、对源缺陷强行纠正（`APPLICATONS`→`applications`），破"忠于源"红线（见 lessons L9）。

词源：symspellpy 自带 `frequency_dictionary_en_82_765.txt`（MIT，~82k 常用英语词 + 频率）。
本模块只读其【词形】列做成员集合，不构造 SymSpell 对象（省去用不到的编辑距离索引）。
故 narrow 规则给定该词表后**完全确定性、不造词**。
"""
from __future__ import annotations

import importlib.resources as ir
from functools import lru_cache

_DICT_RESOURCE = ("symspellpy", "frequency_dictionary_en_82_765.txt")


@lru_cache(maxsize=1)
def _wordset() -> frozenset[str]:
    """读 symspellpy 自带常用词表的词形列 → 小写词集合（懒加载，进程内缓存一次）。"""
    pkg, fname = _DICT_RESOURCE
    words: set[str] = set()
    with (ir.files(pkg) / fname).open(encoding="utf-8") as fh:
        for line in fh:
            parts = line.split()
            if parts:
                words.add(parts[0].lower())
    return frozenset(words)


def is_word(w: str) -> bool:
    """w 是否英语常用词（大小写不敏感）。空/含非字母一律 False（调用方只该传纯字母 token）。"""
    return bool(w) and w.isalpha() and w.lower() in _wordset()
