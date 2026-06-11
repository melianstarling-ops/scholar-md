#!/usr/bin/env python3
"""garblecheck.py — 坏字形检测器【只标不改，根因不可知】。

坏字形 = 文本层与页面图像不一致的可疑 token。两类根因都见过，检测器对二者皆有效：
  ① OCR 误读：扫描图 + OCR 文本层的专利，OCR 把图上的词识别错（`reasons`→`CaSOS`、
     `rings`→`ringS`、公开号 `2002/0116029`→`2002fO116029`；US9216286 实证，glyph=Unicode
     排除映射问题，见 lessons L10）。Google/智慧芽渠道 OCR 质量差异显著。
  ② 字体 ToUnicode 映射损坏：字形渲染正常但 code→Unicode 映射错乱，抽取得乱码。
引擎忠实透传文本层，故产物里就是坏串。

这类"坏字形透传"是 **Tier0 + crosscheck 的共同盲区**：两者都读同一文本层，
源与 md 的字符多重集一致，对不出差异。本检测器从**字形外不可能、源缺陷常见**
的拼写/大小写模式入手，把坏串标为审查标记（红→黄叠加层）。

设计取向：**只标不改、高精度优先、宁漏勿误**——确定性纠正（CaSOS→reasons）属猜测，
破"忠于源"红线（与弃 SymSpell compound 同理，见 lessons L9），故只检测、不改产物。

三类高置信信号（任一命中即标，返回类别串；正常返回 None）：
  - 'casing'    : 词典词但大小写畸形（`ringS`←rings）。放过标准三形与 CamelCase 名。
  - 'mixedcase' : 非词典词 + 词内 ≥2 连续大写(非首位、非全大写缩写)（`CaSOS`）。
  - 'numjoin'   : ≥4 位数字紧跟字母（公开号 `/0`→`fO` 致 `2002fO116029`）。

误报豁免（关键）：CamelCase 人名 `([A-Z][a-z]+)+`（McCabe / MacDonald / DeVoe /
LeGrande / SuWito）、全大写缩写（MRI / RF / DBS）、缩写复数（LEDs / DNAs，大写串在首位）、
小写前缀+全大写缩写（eECAP / mRNA / pHEMA / dsDNA，领域常规写法）、常用单位符号白名单（MHz/dB…）。
已知残留误报：商标符号粘连（Teflon™→TeflonTM）、混合大小写化学式（NaOH 形态）——罕见，标记可人工驳回。
"""
from __future__ import annotations

import re

from wordfix import is_word

# 公开号坏样:4位年份 + 1-2 个杂字母(原为 '/' 或 '/0') + ≥4 位序号尾。
# 收紧到"年份…长数字尾"以避开专利号片段(...286B2)与元件标号(4012A)等合法 alnum。
_NUMJOIN_RE = re.compile(r"\d{4}[A-Za-z]{1,2}\d{4,}")
_UPPER_RUN_RE = re.compile(r"[A-Z]{2,}")          # 词内连续大写段
_CAMEL_RE = re.compile(r"(?:[A-Z][a-z]+)+")       # 合法 CamelCase 名/标识
_PREFIXED_ACRONYM_RE = re.compile(r"[a-z]{1,2}[A-Z]{2,}")  # 小写前缀+全大写缩写:eECAP/mRNA/pHEMA

# 合法但大小写非常规的单位/符号:小写形恰在频词典里会误触 'casing'。枚举豁免。
_UNIT_OK = frozenset({
    "Hz", "kHz", "MHz", "GHz", "THz",
    "dB", "dBm", "dBi", "mAh", "kWh", "mWh", "Wh",
    "mV", "kV", "mA", "kΩ", "MΩ", "mW", "kW", "MW", "pH", "mmHg", "rpm",
})


def _core(token: str) -> str:
    """剥去首尾非字母数字（标点）。'CaSOS.'→'CaSOS'；'61/174,'→'61/174'。"""
    return re.sub(r"^[^0-9A-Za-z]+|[^0-9A-Za-z]+$", "", token)


def classify_garble(token: str) -> str | None:
    """单 token → 坏字形类别（'casing'/'mixedcase'/'numjoin'）或 None（正常）。

    >>> classify_garble('ringS')          # rings 被坏成尾大写
    'casing'
    >>> classify_garble('CaSOS')          # reasons 被坏成混合大写
    'mixedcase'
    >>> classify_garble('CaSOS.')         # 尾随标点不影响判定
    'mixedcase'
    >>> classify_garble('2002fO116029')   # 公开号 2002/0116029 的 /0→fO
    'numjoin'
    >>> classify_garble('2004O162600')    # 同族:/0→O
    'numjoin'
    >>> [classify_garble(w) for w in ('rings', 'Rings', 'RINGS', 'reasons', 'Background')]
    [None, None, None, None, None]
    >>> [classify_garble(w) for w in ('McCabe', 'MacDonald', 'DeVoe', 'LeGrande', 'SuWito')]
    [None, None, None, None, None]
    >>> [classify_garble(w) for w in ('MRI', 'RF', 'DBS', 'LEDs', 'DNAs', 'PCT')]
    [None, None, None, None, None, None]
    >>> [classify_garble(w) for w in ('MHz', 'kHz', 'GHz', 'dB')]   # 单位符号豁免
    [None, None, None, None]
    >>> [classify_garble(w) for w in ('6286B2', '4012A', '5314.459', 'A1', '61/174', '2009', 'US2010')]
    [None, None, None, None, None, None, None]
    >>> [classify_garble(w) for w in ('eECAP', 'mRNA', 'pHEMA', 'dsDNA')]   # 小写前缀+缩写,豁免
    [None, None, None, None]
    """
    core = _core(token)
    if len(core) < 2 or core in _UNIT_OK:
        return None
    if _NUMJOIN_RE.search(core):
        return "numjoin"
    if not core.isalpha():
        return None
    is_camel = bool(_CAMEL_RE.fullmatch(core))
    low = core.lower()
    if is_word(low):
        # 词典词:大小写畸形即坏。放过标准三形(全小写/全大写/首字母大写)与 CamelCase 名。
        if is_camel or core in (low, core.upper(), low.capitalize()):
            return None
        return "casing"
    # 非词典词:词内 ≥2 连续大写(非首位)即坏(CaSOS,大写起头)。放过 CamelCase 名、
    # 全大写缩写(及首位大写串 LEDs)、"小写前缀+全大写缩写"(eECAP/mRNA/pHEMA 领域写法)。
    if is_camel or core.isupper() or _PREFIXED_ACRONYM_RE.fullmatch(core):
        return None
    return "mixedcase" if any(m.start() > 0 for m in _UPPER_RUN_RE.finditer(core)) else None


if __name__ == "__main__":
    import doctest

    fail, total = doctest.testmod(verbose=False)
    print(f"doctest: {total - fail}/{total} passed")
    raise SystemExit(1 if fail else 0)
