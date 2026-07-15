"""LaTeX 等价判定:两家模型写法不同但数学一致时,交叉验证仍应判"一致"。

判据是"渲染成同一个 MathML 结构树",不是字符串相等。MathML 保留顺序,
故 a+b != b+a —— 绝不把重排当等价。node 缺失、或任一侧渲染为空(无法判定)
时返回 None,调用方保守当不等价。
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile

from scripts.pipelines.textbooks.formula_agents.protocol import normalize_latex

_MJS = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                    "debug_assets", "latex_to_mathml.mjs")


def mathml_of(latex_list: list[str], *, node_bin: str | None = None) -> list[str] | None:
    """调 latex_to_mathml.mjs,把每条 latex 渲染为规范化 MathML 串。node 缺失返回 None。"""
    node = node_bin or shutil.which("node")
    if not node:
        return None
    payload = json.dumps(latex_list, ensure_ascii=False)
    try:
        proc = subprocess.run([node, _MJS], input=payload, capture_output=True,
                              text=True, encoding="utf-8", errors="replace", timeout=60)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    try:
        out = json.loads(proc.stdout)
    except json.JSONDecodeError:
        return None
    return out if isinstance(out, list) and len(out) == len(latex_list) else None


def latex_equiv(a: str, b: str, *, node_bin: str | None = None,
                render_fn=None) -> bool | None:
    """分层判等价:字符串归一相等 -> True(免渲染);否则比 MathML;node 缺失 -> None。"""
    if normalize_latex(a) == normalize_latex(b):
        return True
    render = render_fn or (lambda latex_list, node_bin=None: mathml_of(latex_list, node_bin=node_bin))
    rendered = render([a, b], node_bin=node_bin)
    if rendered is None or len(rendered) != 2:
        return None
    # 任一侧规范化 MathML 为空(渲染失败/找不到 <math> 结构)= 无法判定,
    # 不是"等价"。绝不能让两条不同的不可渲染 latex 因为都变成空串而被判
    # True——那等于把"渲染失败"当"结构相同",会把错公式当验证通过写进书。
    if not rendered[0].strip() or not rendered[1].strip():
        return None
    return rendered[0] == rendered[1]
