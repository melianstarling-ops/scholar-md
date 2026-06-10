#!/usr/bin/env python3
"""debug_view.py — 可视化调试工具（自包含单 HTML，VS Code 内置预览/浏览器均可）。

仿 MinerU 左右对照：
  左 = PDF 页渲染图 + 引擎判定叠加层（HTML 绝对定位，可逐层显隐、可缩放），
  右 = 该页 reading_order 的中间产物（段落卡片、剔除词清单、页统计、标记清单）。

叠加层颜色：
  橙 = 剔除的中央行号        紫 = 剔除的页眉/页脚/栏号
  蓝 = 保留词（默认隐藏）    绿 = 段落区域
  红 = crosscheck 未解释删除（若产物目录有 *_crosscheck.json 则自动叠加）
  竖虚线 = 实测 gutter

交互：
  * 翻页：‹ › / ←→ 键 / 页码输入框直跳 / 底部缩略图条（Dock 式自动隐藏，
    鼠标移到底缘弹出，移开收起；横滑+居中吸附+active 放大）。
  * 缩放：− / + 按钮（中间显示倍率，点击恢复适宽）；Ctrl+滚轮以鼠标位置为锚点缩放。
  * 暗色页面：默认对 PDF 页做反色渲染（仅暗色主题下生效），◑ 按钮可关。
  * 双向联动：右栏段落卡 hover → 左侧高亮；左侧点击词框/段落框 → 右栏定位；
    标记框 ↔ 右栏"本页标记"行互相联动（hover 高亮、点击定位）。
  * 标记（M 进入标记模式）：
      - 点击词框 / 拖拽画区域框 → 弹出色点气泡直接选语义（误删红/漏删橙/
        转换错蓝/漏识别主题色），选定即自动收起；点已有标记框可重选/删除；
      - 区域框按住左键可整框拖动微调位置；
      - 右栏标记行内嵌色点，可直接改语义；Delete 键删除选中标记；
      - localStorage 实时自动保存；「导出标记」下载 <名>_annotations.json
        （浏览器限制只能落"下载"目录）+ 复制剪贴板；随后跑
        `debug_view.py --collect` 把下载目录的标记文件归位到
        03_Output/patents/<名>/ 供 agent 读取。
  * 主题：默认暗色（对齐 .vscode/md-theme.css 的 claude-dark 配色），☀/☾ 可切换。

不进主管线、不改产物；判定数据与主转换同源（同一批函数现算），所见即引擎所判。

用法（H.5 自适应 I/O）:
    python scripts/pipelines/patents/debug_view.py                 # 默认源目录全量
    python scripts/pipelines/patents/debug_view.py --src <pdf|dir> [--zoom 2.0]
    python scripts/pipelines/patents/debug_view.py --collect       # 归位下载目录的标记文件
输出: 本脚本同目录 <stem>_debug.html（所有者指定；已全局 gitignore，不入公开仓）
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import datetime
from pathlib import Path

import fitz

from page_classify import PageKind, classify_document
from profiles import LayoutProfile, get_profile
from reading_order import (
    Word,
    Y_TOL_RATIO,
    _column_paragraph_infos,
    group_lines,
    join_line,
    median_char_width,
    median_height,
    split_columns,
    strip_bands,
    strip_line_numbers,
)

PROJECT_ROOT = Path(__file__).resolve().parents[3]
SOURCE_ROOT = Path(
    os.environ.get("SCHOLARMD_PATENTS_SRC", str(PROJECT_ROOT / "02_Source" / "patents"))
)
OUTPUT_ROOT = PROJECT_ROOT / "03_Output" / "patents"
DEFAULT_HTML_DIR = Path(__file__).resolve().parent   # 所有者指定:HTML 落脚本同目录

_KIND_NOTE = {
    "COVER": "封面页：由 bib_parse 解析 → YAML 元数据 + Abstract（不走几何重排）",
    "FIGURE": "附图页：整页渲染 PNG → ## Figures（文字层词不进正文）",
    "FRONT_MATTER": "前置页：线性重排（剔页眉页脚，不剔行号）→ References 附录",
    "SPEC_BODY": "",
}


def _box(w: Word, cls: str, **extra) -> dict:
    d = {"c": cls, "b": [round(w.x0, 1), round(w.y0, 1), round(w.x1, 1), round(w.y1, 1)], "t": w.text}
    d.update(extra)
    return d


def _union(words: list[Word]) -> list[float]:
    return [round(min(w.x0 for w in words), 1), round(min(w.y0 for w in words), 1),
            round(max(w.x1 for w in words), 1), round(max(w.y1 for w in words), 1)]


def page_payload(doc: "fitz.Document", info, profile: LayoutProfile,
                 unexplained: list[dict], zoom: float) -> dict:
    pix = doc[info.index].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
    payload: dict = {
        "page": info.index + 1,
        "kind": info.kind.value,
        "w": info.width,
        "h": info.height,
        "img": base64.b64encode(pix.tobytes("png")).decode(),
        "gutter": round(info.gutter_x, 1),
        "ladder_n": len(info.ladder),
        "n_words": len(info.words),
        "boxes": [],
        "paras": [],
        "removed": {"line_number": [], "header_footer": []},
        "unexplained": unexplained,
        "note": _KIND_NOTE[info.kind.value],
    }
    boxes: list[dict] = payload["boxes"]

    if info.kind in (PageKind.SPEC_BODY, PageKind.FRONT_MATTER):
        body, rm_bands = strip_bands(info.words, info.height, profile)
        boxes += [_box(w, "header_footer", i=i) for i, w in enumerate(rm_bands)]
        payload["removed"]["header_footer"] = [w.text for w in rm_bands]

        if info.kind == PageKind.SPEC_BODY:
            body, rm_nums = strip_line_numbers(body, info.gutter_x, profile)
            boxes += [_box(w, "line_number", i=i) for i, w in enumerate(rm_nums)]
            payload["removed"]["line_number"] = [w.text for w in rm_nums]

        payload["n_kept"] = len(body)
        word_para: dict[int, str] = {}   # id(Word) -> 段落 id（保留词点击联动用）

        if body:
            punct_thr = max(profile.space_gap_abs, profile.space_gap_ratio * median_char_width(body))
            line_h = median_height(body)
            y_tol = Y_TOL_RATIO * line_h
            if info.kind == PageKind.SPEC_BODY:
                left, right = split_columns(body, info.gutter_x)
                for col, ws in (("L", left), ("R", right)):
                    n = 0
                    for p in _column_paragraph_infos(ws, punct_thr, y_tol, line_h):
                        n += 1
                        pid = f"{col}{n}"
                        all_w = [w for ln in p["lines"] for w in ln]
                        for w in all_w:
                            word_para[id(w)] = pid
                        boxes.append({"c": "para", "b": _union(all_w), "id": pid})
                        payload["paras"].append(
                            {"id": pid, "text": p["text"], "new_by": p["new_by"], "n_lines": len(p["lines"])}
                        )
            else:  # FRONT_MATTER：线性行，整页一卡
                lines = group_lines(body, y_tol)
                txt = "\n".join(join_line(ln, punct_thr) for ln in lines)
                boxes.append({"c": "para", "b": _union(body), "id": "F1"})
                payload["paras"].append({"id": "F1", "text": txt, "new_by": "linear", "n_lines": len(lines)})
                for w in body:
                    word_para[id(w)] = "F1"

        boxes += [_box(w, "kept", p=word_para.get(id(w), "")) for w in body]

    for i, u in enumerate(unexplained):
        boxes.append({"c": "unexplained", "b": u["bbox"], "t": u["text"], "i": i})
    return payload


def build_html(name: str, pages: list[dict], resolved: list[dict]) -> str:
    data = json.dumps(pages, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    res = json.dumps(resolved, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return (
        _TEMPLATE.replace("__TITLE__", name)
        .replace("__STAMP__", f"{datetime.now():%Y-%m-%d %H:%M}")
        .replace("__RESOLVED__", res)
        .replace("__DATA__", data)
    )


def _load_resolved(md_root: Path, stem: str) -> list[dict]:
    """扫产物目录全部归档件,取已处理标记的 (page,bbox) 键集 → 嵌入 HTML 供多轮调和(SOP-07 §3)。"""
    out: list[dict] = []
    for f in sorted((md_root / stem).glob(f"{stem}_annotations_resolved*.json")):
        try:
            rep = json.loads(f.read_text(encoding="utf-8"))
            for a in rep.get("annotations", []):
                if "page" in a and "bbox" in a:
                    out.append({"page": a["page"], "bbox": a["bbox"]})
        except (json.JSONDecodeError, OSError) as e:  # 坏档不挡生成
            print(f"  [WARN] 跳过损坏归档 {f.name}: {e}")
    return out


_TEMPLATE = r"""<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<title>__TITLE__ · debug</title>
<style>
  /* 主题：默认暗色,palette 对齐 .vscode/md-theme.css(claude-dark) */
  :root{
    --ln:#f59e0b; --hf:#a78bfa; --kept:#6a9bcc; --para:#5fb87a; --bad:#ef4444;
    --bg:#191A1B; --panel:#1F1F1E; --card:#262625; --ink:#F9F9F7; --sub:#9c9b94;
    --line:rgba(222,220,209,.18); --accent:#D97757; --shadow:rgba(0,0,0,.45);
  }
  [data-theme="light"]{
    --ln:#f59e0b; --hf:#8b5cf6; --kept:#3b82f6; --para:#10b981; --bad:#ef4444;
    --bg:#f5f5f7; --panel:#ffffff; --card:#ffffff; --ink:#1d1d1f; --sub:#86868b;
    --line:#e8e8ed; --accent:#D97757; --shadow:rgba(0,0,0,.10);
  }
  *{box-sizing:border-box;margin:0}
  body{font-family:"Inter",-apple-system,BlinkMacSystemFont,"Segoe UI","Noto Sans SC","Microsoft YaHei",sans-serif;
       background:var(--bg);color:var(--ink);height:100vh;display:flex;flex-direction:column;overflow:hidden}

  /* ---- 顶栏：两行,统一左对齐,组间分隔线 ---- */
  header{display:flex;flex-direction:column;gap:8px;padding:10px 16px 8px;background:var(--panel);
         border-bottom:1px solid var(--line)}
  .row{display:flex;align-items:center;gap:10px;flex-wrap:wrap}
  header h1{font-size:13.5px;font-weight:600;letter-spacing:-.01em}
  .sep{width:1px;height:18px;background:var(--line);flex:0 0 auto}
  .grp{display:flex;align-items:center;gap:6px;font-size:12.5px;color:var(--sub)}
  .grp button,.btn{border:1px solid var(--line);background:var(--panel);border-radius:8px;min-width:28px;height:28px;
                cursor:pointer;font-size:13px;color:var(--ink);padding:0 9px;font-family:inherit}
  .grp button:hover,.btn:hover{background:var(--card);border-color:var(--accent)}
  .btn{position:relative}
  .btn.on{background:var(--accent);border-color:var(--accent);color:#fff}
  .btn.dirty::after{content:"";position:absolute;top:-3px;right:-3px;width:8px;height:8px;
                    border-radius:50%;background:var(--accent);border:1.5px solid var(--panel)}
  #pgin{width:3.2em;height:28px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);
        text-align:center;font-size:12.5px;font-family:inherit;appearance:textfield;-moz-appearance:textfield}
  #pgin::-webkit-outer-spin-button,#pgin::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
  #pgin:focus{outline:none;border-color:var(--accent)}
  #kind{font-size:11px;font-weight:600;padding:3px 10px;border-radius:999px;color:#fff;cursor:default}
  .k-SPEC_BODY{background:#0a84ff}.k-COVER{background:#5e5ce6}.k-FIGURE{background:#98989d}.k-FRONT_MATTER{background:#ac8e68}
  .tg{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--ink);border:1px solid var(--line);
      border-radius:999px;padding:4px 10px;cursor:pointer;background:var(--panel);user-select:none}
  .tg input{display:none}
  .tg .dot{width:9px;height:9px;border-radius:50%}
  .tg.off{color:var(--sub);background:var(--bg)}
  .tg.off .dot{opacity:.25}

  main{flex:1;display:flex;min-height:0}
  #leftcol{flex:11;position:relative;display:flex;flex-direction:column;min-width:0;min-height:0;overflow:hidden}
  #leftpane{flex:1;position:relative;overflow:auto;padding:16px;min-height:0}
  #stage{position:relative;width:min(100%,860px);margin:0 auto;box-shadow:0 2px 14px var(--shadow);
         border-radius:6px;overflow:hidden;background:#fff;touch-action:pan-x pan-y}
  #stage img{display:block;width:100%;pointer-events:none;user-select:none}
  [data-theme="dark"] #stage.inv img{filter:invert(.93) hue-rotate(180deg)}
  #overlay{position:absolute;inset:0;user-select:none}
  .box{position:absolute;border-radius:2px;opacity:.8}
  .box:hover{opacity:1}
  .box.kept{border:1px solid color-mix(in srgb,var(--kept) 50%,transparent);background:color-mix(in srgb,var(--kept) 9%,transparent);
            z-index:2;cursor:pointer}
  .box.line_number{border:1.5px solid var(--ln);background:color-mix(in srgb,var(--ln) 24%,transparent);z-index:3;cursor:pointer}
  .box.header_footer{border:1.5px solid var(--hf);background:color-mix(in srgb,var(--hf) 20%,transparent);z-index:3;cursor:pointer}
  .box.unexplained{border:2.5px solid var(--bad);background:color-mix(in srgb,var(--bad) 20%,transparent);z-index:5;cursor:pointer}
  .box.para{border:1px dashed color-mix(in srgb,var(--para) 60%,transparent);border-left:3px solid var(--para);
            background:transparent;z-index:1;cursor:pointer}
  .box.para.hot{background:color-mix(in srgb,var(--para) 15%,transparent);border-color:var(--para)}
  .annbox{position:absolute;z-index:6;cursor:grab;border-radius:3px;opacity:.8;touch-action:none}
  .annbox:hover,.annbox.sel,.annbox.moving{opacity:1}
  .annbox.moving{cursor:grabbing}
  .annbox.wrong_del{border:2.5px double var(--bad);box-shadow:0 0 0 2px color-mix(in srgb,var(--bad) 35%,transparent)}
  .annbox.missed_del{border:2.5px double var(--ln);box-shadow:0 0 0 2px color-mix(in srgb,var(--ln) 35%,transparent)}
  .annbox.conv_err{border:2.5px double var(--kept);box-shadow:0 0 0 2px color-mix(in srgb,var(--kept) 40%,transparent)}
  .annbox.missed_rec{border:2.5px double var(--accent);box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 35%,transparent)}
  .annbox.sel{outline:2.5px solid var(--ink);outline-offset:3px}
  .annbox.hot{filter:saturate(1.5) brightness(1.3)}
  .annbox.resolved{opacity:.3;border-style:dashed;box-shadow:none}
  .drawrect{position:absolute;z-index:7;border:1.5px dashed var(--accent);background:color-mix(in srgb,var(--accent) 12%,transparent);
            pointer-events:none;border-radius:3px}
  #gutter{position:absolute;top:0;bottom:0;width:0;border-left:2px dashed rgba(255,69,58,.55);z-index:4}
  .hide-kept .box.kept,.hide-line_number .box.line_number,.hide-header_footer .box.header_footer,
  .hide-para .box.para,.hide-unexplained .box.unexplained,.hide-gutter #gutter,.hide-ann .annbox{display:none}
  .annmode .box,.annmode #overlay{cursor:crosshair}

  /* ---- 语义气泡 ---- */
  /* 紧凑气泡:高度与顶栏控件(28px)一致,锚定标记框、留间距不遮挡 */
  #pop{position:fixed;display:flex;align-items:center;gap:7px;background:var(--card);border:1px solid var(--line);
       border-radius:999px;padding:0 10px;height:28px;z-index:60;box-shadow:0 6px 22px var(--shadow)}
  #pop[hidden]{display:none}   /* 显式覆盖:#pop 的 display:flex 优先级高于 UA 的 [hidden] */
  #pop .pdot{width:13px;height:13px;border-radius:50%;cursor:pointer;border:2px solid transparent;transition:transform .12s}
  #pop .pdot:hover{transform:scale(1.2)}
  #pop .pdot.on{border-color:var(--ink)}
  #pop .pdel{border:0;background:none;color:var(--sub);cursor:pointer;font-size:12.5px;padding:0 1px;line-height:1}
  #pop .pdel:hover{color:var(--bad)}

  /* ---- 底部缩略图条：Dock 式自动隐藏 ---- */
  #filmzone{position:absolute;left:0;right:0;bottom:0;height:16px;z-index:19}
  #film{position:absolute;left:0;right:0;bottom:0;z-index:20;padding:10px 16px;overflow-x:auto;
        scroll-snap-type:x proximity;scrollbar-width:thin;border-top:1px solid var(--line);
        background:color-mix(in srgb,var(--panel) 88%,transparent);backdrop-filter:blur(10px);
        transform:translateY(calc(100% - 7px));opacity:.6;     /* 静止态:贴底,露 7px 把手(恢复原始形态) */
        transition:transform .7s cubic-bezier(.22,1,.36,1),opacity .55s ease}
  #film.show{transform:translateY(0);opacity:1}
  #filmtrack{display:flex;gap:10px;width:max-content;padding:2px}
  .thumb{flex:0 0 auto;height:104px;border-radius:8px;overflow:hidden;position:relative;cursor:pointer;
         border:2px solid var(--line);transform:scale(.93);transition:transform .22s,border-color .18s;
         scroll-snap-align:center;background:#fff;box-shadow:0 1px 6px var(--shadow)}
  .thumb img{height:100%;width:auto;display:block}
  [data-theme="dark"] .inv-thumbs .thumb img{filter:invert(.93) hue-rotate(180deg)}
  .thumb:hover{transform:scale(1)}
  .thumb.active{transform:scale(1);border-color:var(--accent)}
  .thumb .tno{position:absolute;right:4px;bottom:4px;font-size:10px;font-weight:600;color:#fff;
              background:rgba(0,0,0,.55);border-radius:5px;padding:1px 5px}
  .thumb .tdot{position:absolute;left:5px;top:5px;width:8px;height:8px;border-radius:50%}

  #rightpane{flex:9;overflow:auto;padding:16px 18px;border-left:1px solid var(--line);background:var(--panel)}
  .sec{margin-bottom:18px}
  .sec h2{font-size:11px;font-weight:600;color:var(--sub);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}
  #stats{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--sub)}
  #stats b{color:var(--ink);font-weight:600}
  .pcard{border:1px solid var(--line);border-left:3px solid var(--para);border-radius:10px;background:var(--card);
         padding:9px 12px;margin-bottom:8px;font-size:12.5px;line-height:1.55;cursor:default}
  .pcard:hover{border-color:var(--para)}
  .pcard .meta{font-size:10.5px;color:var(--sub);margin-bottom:3px;display:flex;gap:8px}
  .pcard .meta .pid{font-weight:700;color:var(--para)}
  .pcard.linear{white-space:pre-wrap}
  .chips{display:flex;flex-wrap:wrap;gap:5px}
  .chip{font-size:11px;padding:2px 8px;border-radius:6px;font-variant-numeric:tabular-nums;border:1px solid transparent}
  .chip.ln{background:color-mix(in srgb,var(--ln) 16%,var(--card));color:var(--ln);border-color:color-mix(in srgb,var(--ln) 40%,transparent)}
  .chip.hf{background:color-mix(in srgb,var(--hf) 14%,var(--card));color:var(--hf);border-color:color-mix(in srgb,var(--hf) 38%,transparent)}
  .chip.bad{background:color-mix(in srgb,var(--bad) 14%,var(--card));color:var(--bad);border-color:color-mix(in srgb,var(--bad) 40%,transparent);font-weight:600}
  .flash{animation:flash 1.2s ease-out}
  @keyframes flash{0%,55%{outline:2px solid var(--accent);outline-offset:2px;background:color-mix(in srgb,var(--accent) 14%,transparent)}100%{outline:0 solid transparent}}
  .annitem{border:1px solid var(--line);background:var(--card);border-radius:8px;margin-bottom:6px}
  .annitem:hover{border-color:var(--accent)}
  .annitem.res{opacity:.45}
  .annrow{display:flex;align-items:center;gap:8px;font-size:12px;padding:6px 10px;cursor:default}
  .annrow .cat{font-size:10.5px;font-weight:700;padding:1px 7px;border-radius:999px;color:#fff;flex:0 0 auto}
  .cat.wrong_del{background:var(--bad)}.cat.missed_del{background:var(--ln)}.cat.conv_err{background:var(--kept)}.cat.missed_rec{background:var(--accent)}
  .annrow .atext{flex:1;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .annrow .dots{display:flex;gap:5px;flex:0 0 auto}
  .annrow .setcat{width:13px;height:13px;border-radius:50%;cursor:pointer;border:2px solid transparent;opacity:.5}
  .annrow .setcat:hover{opacity:1}
  .annrow .setcat.on{opacity:1;border-color:var(--ink)}
  .annrow .del,.annrow .nbtn{flex:0 0 auto;cursor:pointer;color:var(--sub);border:0;background:none;font-size:13px}
  .annrow .del:hover{color:var(--bad)}
  .annrow .nbtn:hover,.annrow .nbtn.has{color:var(--accent)}
  .annnote{display:block;width:calc(100% - 16px);margin:0 8px 8px;border:1px solid var(--line);border-radius:6px;
           background:var(--bg);color:var(--ink);font-family:inherit;font-size:12px;line-height:1.5;
           padding:6px 8px;resize:vertical;min-height:46px}
  .annnote:focus{outline:none;border-color:var(--accent)}
  .nsnip{font-size:10.5px;color:var(--sub);padding:0 10px 6px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
  .mini{border:1px solid var(--line);background:var(--panel);color:var(--sub);border-radius:999px;
        font-size:10.5px;padding:2px 9px;cursor:pointer;font-family:inherit;margin-left:8px;vertical-align:1px}
  .mini:hover{color:var(--accent);border-color:var(--accent)}
  .note{font-size:12.5px;color:var(--sub);background:var(--bg);border-radius:10px;padding:10px 12px;line-height:1.6}
  .empty{font-size:12px;color:var(--sub)}
  footer{padding:6px 16px;font-size:11px;color:var(--sub);background:var(--panel);border-top:1px solid var(--line)}
  kbd{border:1px solid var(--line);border-radius:4px;padding:0 4px;background:var(--bg);font-family:inherit}
  #toast{position:fixed;left:50%;bottom:42px;transform:translateX(-50%);background:var(--ink);color:var(--bg);
         font-size:12.5px;padding:8px 16px;border-radius:999px;opacity:0;transition:opacity .25s;pointer-events:none;z-index:99}
</style>
</head>
<body data-theme="dark">
<header>
  <div class="row">
    <h1>__TITLE__</h1>
    <span id="kind"></span>
    <span class="sep"></span>
    <div class="grp">
      <button id="prev" title="上一页 (←)">‹</button>
      <input id="pgin" type="number" min="1" value="1" title="输入页码直跳">
      <span id="pgtotal"></span>
      <button id="next" title="下一页 (→)">›</button>
    </div>
    <span class="sep"></span>
    <div class="grp">
      <button id="zout" title="缩小">−</button>
      <button id="zl" title="点击恢复适宽">适宽</button>
      <button id="zin" title="放大">+</button>
      <button id="invBtn" title="PDF 页面反色（仅暗色主题下生效）">◑</button>
    </div>
    <span class="sep"></span>
    <div class="grp">
      <button class="btn" id="annBtn" title="标记模式 (M)：点词框/拖拽画框 → 气泡选语义">✎ 标记</button>
      <button class="btn" id="expBtn" title="下载+复制 *_annotations.json;之后跑 debug_view.py --collect 归位到 md 产物文件夹">导出标记</button>
      <button class="btn" id="themeBtn" title="亮/暗切换">☀</button>
    </div>
  </div>
  <div class="row" id="toggles"></div>
</header>
<main>
  <section id="leftcol">
    <div id="leftpane"><div id="stage"><img id="img" alt=""><div id="overlay"></div></div></div>
    <div id="filmzone"></div>
    <div id="film"><div id="filmtrack"></div></div>
  </section>
  <section id="rightpane">
    <div class="sec"><h2>页统计</h2><div id="stats"></div></div>
    <div class="sec" id="noteSec" hidden><h2>说明</h2><div class="note" id="note"></div></div>
    <div class="sec" id="annSec" hidden><h2 style="color:var(--accent)">本页标记<button id="clrRes" class="mini" hidden>清除已处理</button></h2><div id="annList"></div></div>
    <div class="sec" id="badSec" hidden><h2 style="color:var(--bad)">crosscheck 未解释删除</h2><div class="chips" id="bad"></div></div>
    <div class="sec"><h2>重排段落（中间产物）</h2><div id="paras"></div></div>
    <div class="sec"><h2>剔除词</h2><div id="removed"></div></div>
  </section>
</main>
<footer><kbd>←</kbd><kbd>→</kbd> 翻页 · <kbd>M</kbd> 标记模式（点词框/拖框 → 气泡选语义） · <kbd>Delete</kbd> 删选中标记 · <kbd>Ctrl</kbd>+滚轮 指针锚点缩放 · 底缘悬停出缩略图 · 生成于 __STAMP__</footer>
<div id="pop" hidden></div>
<div id="toast"></div>
<script>
const DATA = __DATA__;
const RESOLVED = __RESOLVED__;   // agent 归档件中已处理标记的 (page,bbox) 键集(生成时嵌入,SOP-07 §3)
const TITLE = document.title.replace(" · debug","");
const LAYERS = [
  ["line_number","行号(剔)","var(--ln)",true],
  ["header_footer","页眉/页脚(剔)","var(--hf)",true],
  ["kept","保留词","var(--kept)",false],
  ["para","段落","var(--para)",true],
  ["unexplained","未解释","var(--bad)",true],
  ["ann","标记","var(--accent)",true],
  ["gutter","gutter","rgba(255,69,58,.8)",true],
];
const WORD_CATS = ["wrong_del","missed_del","conv_err"];
const REGION_CATS = ["missed_rec","wrong_del","missed_del","conv_err"];
const ANN_NAMES = {wrong_del:"误删", missed_del:"漏删", conv_err:"转换错", missed_rec:"漏识别"};
const CATC = {wrong_del:"var(--bad)", missed_del:"var(--ln)", conv_err:"var(--kept)", missed_rec:"var(--accent)"};
const KIND_LABEL = {SPEC_BODY:"正文·双栏", COVER:"封面", FIGURE:"附图", FRONT_MATTER:"前置/引用"};
const $=id=>document.getElementById(id);
const ov=$("overlay"), st=$("stage"), lp=$("leftpane");
let cur=0, zoom=0, annMode=false, suppressClick=false, selKey=null;   // zoom 0 = 适宽
const BASE=860, ANN_KEY="dbgann:"+TITLE;
let ann = new Map(Object.entries(JSON.parse(localStorage.getItem(ANN_KEY)||"{}")));

/* ---- 图层开关(第二行,与第一行同左对齐) ---- */
const tgBox=$("toggles");
for(const [key,label,color,on] of LAYERS){
  const lab=document.createElement("label");
  lab.className="tg"+(on?"":" off");
  lab.innerHTML=`<input type="checkbox" ${on?"checked":""}><span class="dot" style="background:${color}"></span>${label}`;
  if(!on) st.classList.add("hide-"+key);
  lab.querySelector("input").addEventListener("change",e=>{
    st.classList.toggle("hide-"+key,!e.target.checked);
    lab.classList.toggle("off",!e.target.checked);
  });
  tgBox.appendChild(lab);
}

/* ---- 工具 ---- */
const pct=(v,t)=>(v/t*100).toFixed(3)+"%";
const esc=s=>s.replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function toast(msg){const t=$("toast");t.textContent=msg;t.style.opacity=1;clearTimeout(t._h);t._h=setTimeout(()=>t.style.opacity=0,2200);}
function flashEl(el){if(!el)return;el.scrollIntoView({block:"center",behavior:"smooth"});el.classList.remove("flash");void el.offsetWidth;el.classList.add("flash");}

/* ---- 缩放(Ctrl+滚轮以指针为锚点) ---- */
function applyZoom(){
  st.style.width = zoom===0 ? "min(100%,860px)" : Math.round(BASE*zoom)+"px";
  $("zl").textContent = zoom===0 ? "适宽" : Math.round(zoom*100)+"%";
}
$("zin").onclick=()=>{zoom=Math.min((zoom||st.offsetWidth/BASE)*1.25,5);applyZoom();};
$("zout").onclick=()=>{zoom=Math.max((zoom||st.offsetWidth/BASE)/1.25,.4);applyZoom();};
$("zl").onclick=()=>{zoom=0;applyZoom();};
lp.addEventListener("wheel",e=>{
  if(!e.ctrlKey)return;
  e.preventDefault();
  const r=lp.getBoundingClientRect();
  const px=e.clientX-r.left, py=e.clientY-r.top;          // 指针在视口内位置
  const oldW=st.offsetWidth, oldH=st.offsetHeight;
  const fx=(lp.scrollLeft+px-st.offsetLeft)/oldW;         // 指针落点在页面内的比例
  const fy=(lp.scrollTop+py-st.offsetTop)/oldH;
  if(zoom===0)zoom=oldW/BASE;
  zoom = e.deltaY<0 ? Math.min(zoom*1.12,5) : Math.max(zoom/1.12,.4);
  applyZoom();
  const newW=st.offsetWidth, newH=oldH*newW/oldW;         // 读取强制 reflow
  lp.scrollLeft=st.offsetLeft+fx*newW-px;
  lp.scrollTop =st.offsetTop +fy*newH-py;
},{passive:false});

/* ---- PDF 页面反色(暗色主题) ---- */
function setInv(on){st.classList.toggle("inv",on);$("filmtrack").parentElement.classList.toggle("inv-thumbs",on);
  $("invBtn").classList.toggle("on",on);localStorage.setItem("dbginv",on?"1":"0");}
$("invBtn").onclick=()=>setInv(!st.classList.contains("inv"));
setInv(localStorage.getItem("dbginv")!=="0");

/* ---- 缩略图条(Dock 式自动隐藏) ---- */
const film=$("film"), track=$("filmtrack");
const KINDC={SPEC_BODY:"#0a84ff",COVER:"#5e5ce6",FIGURE:"#98989d",FRONT_MATTER:"#ac8e68"};
DATA.forEach((d,i)=>{
  const t=document.createElement("div");
  t.className="thumb"; t.dataset.i=i;
  t.innerHTML=`<img loading="lazy" src="data:image/png;base64,${d.img}" alt="">
    <span class="tdot" style="background:${KINDC[d.kind]}"></span><span class="tno">${i+1}</span>`;
  t.onclick=()=>render(i);
  track.appendChild(t);
});
function syncFilm(){
  track.querySelectorAll(".thumb").forEach(t=>t.classList.toggle("active",+t.dataset.i===cur));
  const a=track.querySelector(".thumb.active");
  if(a&&film.classList.contains("show"))a.scrollIntoView({inline:"center",block:"nearest",behavior:"smooth"});
}
let filmT=null;   // hover 意图延迟,避免鼠标路过底缘时惊跳
$("filmzone").addEventListener("mouseenter",()=>{
  clearTimeout(filmT);
  filmT=setTimeout(()=>{film.classList.add("show");syncFilm();},160);
});
$("filmzone").addEventListener("mouseleave",()=>clearTimeout(filmT));
film.addEventListener("mouseleave",()=>{clearTimeout(filmT);film.classList.remove("show");});
film.addEventListener("wheel",e=>{           // 竖滚轮 → 横滑(相册式)
  if(e.ctrlKey)return;
  if(Math.abs(e.deltaY)>Math.abs(e.deltaX)){e.preventDefault();film.scrollLeft+=e.deltaY;}
},{passive:false});

/* ---- 标记存取 ---- */
const annKey=(page,bstr)=>page+"|"+bstr;
/* 已处理判定:与归档键集按 页+坐标(容差0.6pt) 匹配,浮点格式差异免疫 */
function isResolved(v){
  return RESOLVED.some(r=>r.page===v.page&&v.bbox.every((c,i)=>Math.abs(c-r.bbox[i])<0.6));
}
/* 未导出变更角标:当前标记序列化 vs 上次导出快照 */
const EXP_KEY="dbgexp:"+TITLE;
function annSerial(){return JSON.stringify([...ann.entries()].sort((a,b)=>a[0]<b[0]?-1:1));}
function updateDirty(){
  $("expBtn").classList.toggle("dirty",ann.size>0&&annSerial()!==localStorage.getItem(EXP_KEY));
}
function exportJson(){
  const arr=[...ann.values()].sort((a,b)=>a.page-b.page);
  return JSON.stringify({doc:TITLE,exported:new Date().toISOString(),n:arr.length,
    legend:{wrong_del:"误删(内容被错误剔除)",missed_del:"漏删(噪声未被剔除)",conv_err:"转换错误(空格/段落/标题等)",missed_rec:"漏识别(引擎完全没框到的区域)"},
    annotations:arr},null,2);
}
function saveAnn(){localStorage.setItem(ANN_KEY,JSON.stringify(Object.fromEntries(ann)));updateDirty();}
/* 导出:浏览器只能落"下载"目录(无法直写工作区路径)。归位到 md 产物文件夹用
   `debug_view.py --collect`(把下载目录的 *_annotations.json 移到 03_Output/patents/<名>/)。 */
$("expBtn").onclick=()=>{
  const json=exportJson();
  navigator.clipboard&&navigator.clipboard.writeText(json).then(()=>{},()=>{});
  const a=document.createElement("a");
  a.href=URL.createObjectURL(new Blob([json],{type:"application/json"}));
  a.download=TITLE+"_annotations.json";a.click();URL.revokeObjectURL(a.href);
  localStorage.setItem(EXP_KEY,annSerial());updateDirty();
  toast(`已导出 ${ann.size} 条(下载目录+剪贴板);跑 --collect 归位到 md 文件夹`);
};

/* ---- 标记绘制 ---- */
function drawAnn(){
  ov.querySelectorAll(".annbox").forEach(e=>e.remove());
  const d=DATA[cur];
  for(const [k,v] of ann){
    if(v.page!==d.page)continue;
    const res=isResolved(v);
    const el=document.createElement("div");
    el.className="annbox "+v.cat+(v.kind==="region"?" region":"")+(k===selKey?" sel":"")+(res?" resolved":"");
    el.dataset.k=k;
    el.style.left=pct(v.bbox[0],d.w);el.style.top=pct(v.bbox[1],d.h);
    el.style.width=pct(v.bbox[2]-v.bbox[0],d.w);el.style.height=pct(v.bbox[3]-v.bbox[1],d.h);
    el.title=(res?"✓已处理 ":"")+(v.kind==="region"?"区域: ":"")+ANN_NAMES[v.cat]
      +(v.text?" — "+v.text:"")+(v.note?"\n备注: "+v.note:"")+"\n(按住拖动 / Delete 删除)";
    el.addEventListener("pointerdown",ev=>startMove(ev,el,k));
    ov.appendChild(el);
  }
  renderAnnList();
}

/* 标记框拖动微调:逐框 pointer capture(不走委托,webview 下可靠);4px 阈值区分点击 */
function startMove(ev,el,k){
  if(ev.button!==0)return;
  const v=ann.get(k);
  if(!v)return;
  ev.preventDefault();
  const d=DATA[cur], mv={x0:ev.clientX,y0:ev.clientY,bbox:[...v.bbox],moved:false,nb:null};
  el.setPointerCapture(ev.pointerId);
  const onMove=em=>{
    if(!mv.moved&&Math.hypot(em.clientX-mv.x0,em.clientY-mv.y0)<4)return;
    mv.moved=true;el.classList.add("moving");
    const r=st.getBoundingClientRect();
    const dx=(em.clientX-mv.x0)*d.w/r.width, dy=(em.clientY-mv.y0)*d.h/r.height;
    const [x0,y0,x1,y1]=mv.bbox, w=x1-x0, h=y1-y0;
    const nx=Math.min(Math.max(0,x0+dx),d.w-w), ny=Math.min(Math.max(0,y0+dy),d.h-h);
    mv.nb=[nx,ny,nx+w,ny+h].map(n=>Math.round(n*10)/10);
    el.style.left=pct(nx,d.w);el.style.top=pct(ny,d.h);
  };
  const onUp=()=>{
    el.removeEventListener("pointermove",onMove);
    el.removeEventListener("pointerup",onUp);
    el.classList.remove("moving");
    if(mv.moved&&mv.nb){
      suppressClick=true;setTimeout(()=>suppressClick=false,250);   // 拖后click可能因元素重建而不触发,定时清旗
      ann.delete(k);                                  // key 含坐标,移动后重建
      const nk=annKey(d.page,mv.nb.join(","));
      ann.set(nk,{...v,bbox:mv.nb});
      selKey=nk;saveAnn();drawAnn();
    }
  };
  el.addEventListener("pointermove",onMove);
  el.addEventListener("pointerup",onUp);
}
function delAnn(k){ann.delete(k);if(selKey===k)selKey=null;saveAnn();drawAnn();toast("已删除标记");}

/* ---- 语义气泡:点中标记/词框 → 色点直选 ---- */
const pop={el:$("pop"),key:null,pending:null,justOpened:false};
function openPop(anchor,opts){   // anchor: 标记框/词框的 DOMRect,气泡悬于框上方 10px(放不下翻到下方)
  pop.key=opts.key||null; pop.pending=opts.pending||null;
  pop.justOpened=true; setTimeout(()=>pop.justOpened=false,50);
  const kind=pop.key?(ann.get(pop.key).kind||"word"):pop.pending.kind;
  const cats=kind==="region"?REGION_CATS:WORD_CATS;
  const curCat=pop.key?ann.get(pop.key).cat:null;
  pop.el.innerHTML=cats.map(c=>
    `<span class="pdot${c===curCat?" on":""}" data-c="${c}" title="${ANN_NAMES[c]}" style="background:${CATC[c]}"></span>`).join("")
    +(pop.key?`<button class="pdel" title="删除 (Delete)">✕</button>`:"");
  pop.el.hidden=false;
  const pw=pop.el.offsetWidth, ph=pop.el.offsetHeight;
  pop.el.style.left=Math.min(Math.max(8,(anchor.left+anchor.right)/2-pw/2),innerWidth-pw-8)+"px";
  pop.el.style.top=(anchor.top-ph-10<8 ? anchor.bottom+10 : anchor.top-ph-10)+"px";
  pop.el.querySelectorAll(".pdot").forEach(p=>p.onclick=ev=>{ev.stopPropagation();applyCat(p.dataset.c);});
  const del=pop.el.querySelector(".pdel");
  if(del)del.onclick=ev=>{ev.stopPropagation();delAnn(pop.key);closePop();};
}
function applyCat(c){
  const key=pop.key, pending=pop.pending;
  closePop();                                       // 选定即关,再落数据
  if(key){const v=ann.get(key);if(v){v.cat=c;ann.set(key,v);toast("标记: "+ANN_NAMES[c]);}}
  else if(pending){
    const k=annKey(pending.page,pending.bbox.join(","));
    ann.set(k,{page:pending.page,text:pending.text,bbox:pending.bbox,cat:c,kind:pending.kind});
    selKey=k;toast("标记: "+ANN_NAMES[c]+(pending.text?" — "+pending.text:""));
  }
  saveAnn();drawAnn();
}
function closePop(){pop.el.hidden=true;pop.key=null;pop.pending=null;}
document.addEventListener("click",e=>{
  if(!pop.el.hidden&&!pop.justOpened&&!pop.el.contains(e.target))closePop();
});

/* ---- 标记模式：空白处拖拽画区域框 ---- */
let drawing=null;
st.addEventListener("pointerdown",e=>{
  if(!annMode||e.button!==0)return;
  if(e.target.closest(".annbox"))return;            // 点已有标记 → 移动/气泡,不画新框
  drawing={x0:e.clientX,y0:e.clientY,moved:false,el:null};
});
st.addEventListener("pointermove",e=>{
  if(!drawing)return;
  if(!drawing.moved&&Math.hypot(e.clientX-drawing.x0,e.clientY-drawing.y0)<5)return;
  drawing.moved=true;
  if(!drawing.el){drawing.el=document.createElement("div");drawing.el.className="drawrect";ov.appendChild(drawing.el);}
  const r=st.getBoundingClientRect();
  drawing.el.style.left=(Math.min(e.clientX,drawing.x0)-r.left)+"px";
  drawing.el.style.top=(Math.min(e.clientY,drawing.y0)-r.top)+"px";
  drawing.el.style.width=Math.abs(e.clientX-drawing.x0)+"px";
  drawing.el.style.height=Math.abs(e.clientY-drawing.y0)+"px";
});
st.addEventListener("pointerup",e=>{
  if(!drawing)return;
  if(drawing.moved){
    suppressClick=true;
    drawing.el.remove();
    const d=DATA[cur], r=st.getBoundingClientRect();
    const sx=d.w/r.width, sy=d.h/r.height;
    const x0=Math.max(0,(Math.min(e.clientX,drawing.x0)-r.left)*sx), y0=Math.max(0,(Math.min(e.clientY,drawing.y0)-r.top)*sy);
    const x1=Math.min(d.w,(Math.max(e.clientX,drawing.x0)-r.left)*sx), y1=Math.min(d.h,(Math.max(e.clientY,drawing.y0)-r.top)*sy);
    if(x1-x0>3&&y1-y0>3){
      const b=[x0,y0,x1,y1].map(v=>Math.round(v*10)/10);
      const k=annKey(d.page,b.join(","));
      ann.set(k,{page:d.page,text:"",bbox:b,cat:"missed_rec",kind:"region"});
      selKey=k;saveAnn();drawAnn();
      const nb=ov.querySelector(`.annbox[data-k="${k}"]`);   // 落框即默认漏识别,气泡锚框可改
      if(nb)openPop(nb.getBoundingClientRect(),{key:k});
    }
  }
  drawing=null;
});

/* ---- 左侧点击：标记气泡 或 双向联动 ---- */
st.addEventListener("click",e=>{
  if(suppressClick){suppressClick=false;return;}
  const d=DATA[cur];
  const ab=e.target.closest(".annbox");
  if(ab){                                            // 任意模式:点标记框 → 选中+气泡
    const k0=ab.dataset.k, r0=ab.getBoundingClientRect();
    selKey=k0;drawAnn();                             // drawAnn 重建元素,先取 rect
    openPop(r0,{key:k0});
    flashEl($("annList")&&$("annList").querySelector(`[data-k="${k0}"]`));
    e.stopPropagation();return;
  }
  const el=e.target.closest(".box");
  if(!el)return;
  if(annMode){
    if(!el.dataset.t)return;                         // 词级框才可点标
    const k=annKey(d.page,el.dataset.b);
    const r0=el.getBoundingClientRect();
    if(ann.has(k)){selKey=k;drawAnn();openPop(r0,{key:k});}
    else openPop(r0,{pending:{kind:"word",page:d.page,
      bbox:el.dataset.b.split(",").map(Number),text:el.dataset.t}});
    e.stopPropagation();return;
  }
  if(el.dataset.p)      flashEl($("card-"+el.dataset.p));                       // 保留词 → 段落卡
  else if(el.dataset.id) flashEl($("card-"+el.dataset.id));                     // 段落框 → 段落卡
  else if(el.dataset.r)  flashEl($("chip-"+el.dataset.r+"-"+el.dataset.i));     // 剔除词 → chip
  else if(el.classList.contains("unexplained")) flashEl($("badchip-"+el.dataset.i));
});

/* ---- 右栏标记清单：改语义/note 输入/删除/已处理调和/双向联动 ---- */
const noteEdit=new Set();   // 正在编辑 note 的 key(不入 ann 值,避免污染导出)
function renderAnnList(){
  const d=DATA[cur];
  const rows=[...ann.entries()].filter(([,v])=>v.page===d.page);
  const nRes=[...ann.values()].filter(isResolved).length;        // 全文档已处理数
  const cb=$("clrRes");
  cb.hidden=!nRes; cb.textContent=`清除已处理 (${nRes})`;
  $("annSec").hidden=!rows.length;
  $("annList").innerHTML=rows.map(([k,v])=>{
    const cats=(v.kind==="region")?REGION_CATS:WORD_CATS;
    const res=isResolved(v), editing=noteEdit.has(k);
    return `<div class="annitem${res?" res":""}" data-k="${k}">
     <div class="annrow">
       <span class="cat ${v.cat}">${res?"✓ ":""}${ANN_NAMES[v.cat]}</span>
       <span class="atext">${v.kind==="region"?"▭ 区域 ["+v.bbox.map(Math.round)+"]":esc(v.text)}</span>
       <span class="dots">${cats.map(c=>`<span class="setcat${c===v.cat?" on":""}" data-c="${c}" title="${ANN_NAMES[c]}" style="background:${CATC[c]}"></span>`).join("")}</span>
       <button class="nbtn${v.note?" has":""}" title="文字说明(随导出 note 字段带给 agent)">✎</button>
       <button class="del" title="删除标记">✕</button>
     </div>
     ${!editing&&v.note?`<div class="nsnip" title="${esc(v.note)}">${esc(v.note)}</div>`:""}
     ${editing?`<textarea class="annnote" placeholder="描述具体问题…(自动保存)">${esc(v.note||"")}</textarea>`:""}
    </div>`;}).join("");
  $("annList").querySelectorAll(".annitem").forEach(row=>{
    const k=row.dataset.k;
    row.querySelectorAll(".setcat").forEach(p=>p.onclick=ev=>{ev.stopPropagation();
      const v=ann.get(k);v.cat=p.dataset.c;ann.set(k,v);saveAnn();drawAnn();});
    row.querySelector(".del").onclick=ev=>{ev.stopPropagation();noteEdit.delete(k);delAnn(k);};
    row.querySelector(".nbtn").onclick=ev=>{ev.stopPropagation();
      noteEdit.has(k)?noteEdit.delete(k):noteEdit.add(k);
      renderAnnList();
      const ta=$("annList").querySelector(`.annitem[data-k="${k}"] .annnote`);
      if(ta){ta.focus();ta.selectionStart=ta.value.length;}};
    const ta=row.querySelector(".annnote");
    if(ta){
      ta.addEventListener("click",ev=>ev.stopPropagation());
      ta.addEventListener("input",()=>{
        const v=ann.get(k);
        if(ta.value.trim())v.note=ta.value;else delete v.note;
        ann.set(k,v);saveAnn();});
    }
    row.addEventListener("mouseenter",()=>{const b=ov.querySelector(`.annbox[data-k="${k}"]`);if(b)b.classList.add("hot");});
    row.addEventListener("mouseleave",()=>{const b=ov.querySelector(`.annbox[data-k="${k}"]`);if(b)b.classList.remove("hot");});
    row.addEventListener("click",()=>{selKey=k;drawAnn();
      const b=ov.querySelector(`.annbox[data-k="${k}"]`);if(b){b.scrollIntoView({block:"center",behavior:"smooth"});flashEl(b);}});
  });
}
$("clrRes").onclick=ev=>{
  ev.stopPropagation();
  let n=0;
  for(const [k,v] of [...ann]) if(isResolved(v)){ann.delete(k);noteEdit.delete(k);n++;}
  saveAnn();drawAnn();toast(`已清除 ${n} 条已处理标记`);
};
$("annBtn").onclick=()=>{annMode=!annMode;$("annBtn").classList.toggle("on",annMode);st.classList.toggle("annmode",annMode);
  toast(annMode?"标记模式开：点词框打标 / 空白处拖拽画区域框":"标记模式关");};

/* ---- 主题 ---- */
function applyTheme(t){document.body.dataset.theme=t;$("themeBtn").textContent=t==="dark"?"☀":"☾";localStorage.setItem("dbgtheme",t);}
$("themeBtn").onclick=()=>applyTheme(document.body.dataset.theme==="dark"?"light":"dark");
applyTheme(localStorage.getItem("dbgtheme")||"dark");
updateDirty();

/* ---- 渲染页 ---- */
$("pgtotal").textContent="/ "+DATA.length;
$("pgin").max=DATA.length;
$("pgin").addEventListener("change",()=>{
  const v=Math.min(Math.max(1,+$("pgin").value||1),DATA.length);
  render(v-1);
});
$("pgin").addEventListener("keydown",e=>{if(e.key==="Enter")$("pgin").blur();});

function render(i){
  cur=i; const d=DATA[i];
  closePop(); selKey=null;
  $("img").src="data:image/png;base64,"+d.img;
  $("pgin").value=i+1;
  const k=$("kind"); k.textContent=KIND_LABEL[d.kind]; k.className="k-"+d.kind;
  k.title="页型判定(page_classify): "+d.kind;
  $("prev").disabled=i===0; $("next").disabled=i===DATA.length-1;
  syncFilm();

  ov.innerHTML="";
  if(d.kind==="SPEC_BODY"&&d.gutter>0){
    const g=document.createElement("div"); g.id="gutter"; g.style.left=pct(d.gutter,d.w); ov.appendChild(g);
  }
  for(const b of d.boxes){
    const el=document.createElement("div");
    el.className="box "+b.c;
    el.style.left=pct(b.b[0],d.w); el.style.top=pct(b.b[1],d.h);
    el.style.width=pct(b.b[2]-b.b[0],d.w); el.style.height=pct(b.b[3]-b.b[1],d.h);
    if(b.id){el.dataset.id=b.id;}
    if(b.t!==undefined){el.title=b.t+"  ["+b.b.map(Math.round)+"]";el.dataset.t=b.t;el.dataset.b=b.b.join(",");}
    if(b.p!==undefined&&b.p)el.dataset.p=b.p;
    if(b.i!==undefined&&(b.c==="line_number"||b.c==="header_footer")){el.dataset.r=b.c;el.dataset.i=b.i;}
    if(b.i!==undefined&&b.c==="unexplained")el.dataset.i=b.i;
    ov.appendChild(el);
  }
  drawAnn();

  const kept=("n_kept" in d)?` · 保留 <b>${d.n_kept}</b>`:"";
  const rm=d.removed, nrm=(rm.line_number.length+rm.header_footer.length);
  $("stats").innerHTML=
    `<span>词 <b>${d.n_words}</b>${kept} · 剔除 <b>${nrm}</b></span>`+
    (d.kind==="SPEC_BODY"?`<span>gutter <b>${d.gutter}</b> · 行号阶梯 <b>${d.ladder_n}</b></span>`:"")+
    `<span>段落 <b>${d.paras.length}</b></span>`;

  $("noteSec").hidden=!d.note; $("note").textContent=d.note;

  $("badSec").hidden=!d.unexplained.length;
  $("bad").innerHTML=d.unexplained.map((u,j)=>`<span class="chip bad" id="badchip-${j}" title="[${u.bbox}]">${esc(u.text)}</span>`).join("");

  const NEWBY={indent:"缩进起段",gap:"行距起段",first:"栏首",linear:"线性"};
  $("paras").innerHTML=d.paras.length?d.paras.map(p=>
    `<div class="pcard${p.new_by==="linear"?" linear":""}" id="card-${p.id}" data-for="${p.id}">
       <div class="meta"><span class="pid">${p.id}</span><span>${NEWBY[p.new_by]} · ${p.n_lines} 行</span></div>${esc(p.text)}</div>`
  ).join(""):`<div class="empty">本页无重排段落</div>`;
  for(const card of $("paras").querySelectorAll(".pcard")){
    card.addEventListener("mouseenter",()=>hot(card.dataset.for,true));
    card.addEventListener("mouseleave",()=>hot(card.dataset.for,false));
  }

  const sec=(cls,key,name,arr)=>arr.length?`<h2 style="margin:8px 0 6px;font-size:10.5px;color:var(--sub)">${name} × ${arr.length}</h2>
     <div class="chips">${arr.map((t,j)=>`<span class="chip ${cls}" id="chip-${key}-${j}">${esc(t)}</span>`).join("")}</div>`:"";
  const html=sec("ln","line_number","中央行号",rm.line_number)+sec("hf","header_footer","页眉/页脚/栏号",rm.header_footer);
  $("removed").innerHTML=html||`<div class="empty">本页无剔除</div>`;
}
function hot(id,on){const el=ov.querySelector(`[data-id="${id}"]`); if(el) el.classList.toggle("hot",on);}
$("prev").onclick=()=>cur>0&&render(cur-1);
$("next").onclick=()=>cur<DATA.length-1&&render(cur+1);
document.addEventListener("keydown",e=>{
  if(e.target.tagName==="INPUT")return;
  if(e.key==="ArrowRight"&&cur<DATA.length-1)render(cur+1);
  if(e.key==="ArrowLeft"&&cur>0)render(cur-1);
  if(e.key==="m"||e.key==="M")$("annBtn").click();
  if(e.key==="Delete"&&selKey){delAnn(selKey);closePop();}
  if(e.key==="Escape"){closePop();selKey=null;drawAnn();}
});
render(0);
</script>
</body>
</html>
"""


# ---------------- CLI（H.5 自适应 I/O） ----------------

def collect_pdfs(srcs: list[str]) -> list[Path]:
    seen: dict[Path, None] = {}
    for s in srcs:
        p = Path(s)
        if p.is_dir():
            for f in sorted(p.glob("*.pdf")):
                seen.setdefault(f.resolve())
        elif p.suffix.lower() == ".pdf" and p.exists():
            seen.setdefault(p.resolve())
        else:
            print(f"  [WARN] 跳过不存在/非 PDF: {s}")
    return list(seen)


def collect_annotations(pdfs: list[Path], md_root: Path) -> int:
    """把浏览器下载目录里导出的 <stem>_annotations*.json 归位到 md 产物文件夹。
    同一 stem 多份(浏览器重名加 (1) 等)取最新,旧副本留在原处不动。"""
    downloads = Path.home() / "Downloads"
    moved = 0
    for pdf in pdfs:
        cands = sorted(downloads.glob(f"{pdf.stem}_annotations*.json"),
                       key=lambda p: p.stat().st_mtime)
        if not cands:
            continue
        newest = cands[-1]
        target_dir = md_root / pdf.stem
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / f"{pdf.stem}_annotations.json"
        target.write_bytes(newest.read_bytes())
        newest.unlink()
        moved += 1
        print(f"  [MOVE] {newest.name} → {target}"
              + (f"（另有 {len(cands) - 1} 份旧副本留在下载目录）" if len(cands) > 1 else ""))
    if not moved:
        print(f"  下载目录({downloads})未找到匹配的 *_annotations.json")
    return moved


def _load_unexplained(md_root: Path, stem: str, n_pages: int) -> list[list[dict]]:
    """读 crosscheck 报告（如有），取每页未解释词。"""
    per_page: list[list[dict]] = [[] for _ in range(n_pages)]
    for cand in (md_root / stem / f"{stem}_crosscheck.json", md_root / f"{stem}_crosscheck.json"):
        if cand.exists():
            rep = json.loads(cand.read_text(encoding="utf-8"))
            for pr in rep.get("pages", []):
                idx = pr["page"] - 1
                if 0 <= idx < n_pages:
                    per_page[idx] = pr.get("unexplained", [])
            break
    return per_page


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", nargs="*", default=[str(SOURCE_ROOT)],
                    help="PDF 文件或目录，可多个（默认 SCHOLARMD_PATENTS_SRC / 02_Source/patents）")
    ap.add_argument("--md-root", default=str(OUTPUT_ROOT),
                    help="产物根目录（找 crosscheck 报告，默认 03_Output/patents）")
    ap.add_argument("--out", default=str(DEFAULT_HTML_DIR),
                    help="HTML 输出目录（默认本脚本同目录；已 gitignore）")
    ap.add_argument("--zoom", type=float, default=2.0, help="页面渲染倍率（默认 2.0）")
    ap.add_argument("--collect", action="store_true",
                    help="把浏览器下载目录的 *_annotations.json 归位到 md 产物文件夹后退出")
    args = ap.parse_args()

    pdfs = collect_pdfs(args.src)
    if not pdfs:
        print("未找到 PDF。")
        return 1
    md_root = Path(args.md_root)
    if args.collect:
        return 0 if collect_annotations(pdfs, md_root) >= 0 else 1
    profile = get_profile()

    print(f"[{datetime.now():%H:%M:%S}] 生成调试视图 {len(pdfs)} 份（zoom={args.zoom}）\n")
    failed = 0
    for pdf in pdfs:
        try:
            doc = fitz.open(str(pdf))
            infos = classify_document(doc, profile)
            unexpl = _load_unexplained(md_root, pdf.stem, doc.page_count)
            pages = [page_payload(doc, info, profile, unexpl[info.index], args.zoom) for info in infos]
            doc.close()
            html = build_html(pdf.stem, pages, _load_resolved(md_root, pdf.stem))
            out_dir = Path(args.out)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_html = out_dir / f"{pdf.stem}_debug.html"
            out_html.write_text(html, encoding="utf-8")
            mb = out_html.stat().st_size / 1e6
            print(f"  [OK] {pdf.stem} — {len(pages)} 页, {mb:.1f} MB → {out_html}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [ERROR] {pdf.stem}: {e}")
    print(f"\n{'=' * 56}\n完成: {len(pdfs) - failed} OK / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
