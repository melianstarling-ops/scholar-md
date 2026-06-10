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
  * 翻页：‹ › 按钮 / ←→ 键 / 页码输入框直跳 / 底部缩略图条（▦ 开关，横滑点选）。
  * 缩放：− / + 按钮（中间按钮显示倍率，点击恢复适宽），或 Ctrl+滚轮。
  * 双向联动：右栏段落卡 hover → 左侧高亮；左侧点击词框/段落框 → 右栏定位
    到对应段落卡 / 剔除词 chip / 告警 chip（看出"保留/删除/转错"去向）。
  * 标记模式（按钮或 M 键）：
      - 点击词框循环打标 误删(红)→漏删(橙)→转换错(蓝)→取消；
      - 空白处拖拽画框 = 区域标记，默认"漏识别"(主题色)，点击该框可换类别/删除；
      - 自动存 localStorage，"导出标记"复制/下载 <名>_annotations.json 供 agent 修引擎。
  * 主题：默认暗色（对齐 .vscode/md-theme.css 的 claude-dark 配色），☀/☾ 可切换。

不进主管线、不改产物；判定数据与主转换同源（同一批函数现算），所见即引擎所判。

用法（H.5 自适应 I/O）:
    python scripts/pipelines/patents/debug_view.py                 # 默认源目录全量
    python scripts/pipelines/patents/debug_view.py --src <pdf|dir> [--zoom 2.0]
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


def build_html(name: str, pages: list[dict]) -> str:
    data = json.dumps(pages, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    return (
        _TEMPLATE.replace("__TITLE__", name)
        .replace("__STAMP__", f"{datetime.now():%Y-%m-%d %H:%M}")
        .replace("__DATA__", data)
    )


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
  .btn.on{background:var(--accent);border-color:var(--accent);color:#fff}
  #pgin{width:3.2em;height:28px;border:1px solid var(--line);border-radius:8px;background:var(--bg);color:var(--ink);
        text-align:center;font-size:12.5px;font-family:inherit;appearance:textfield;-moz-appearance:textfield}
  #pgin::-webkit-outer-spin-button,#pgin::-webkit-inner-spin-button{-webkit-appearance:none;margin:0}
  #pgin:focus{outline:none;border-color:var(--accent)}
  #kind{font-size:11px;font-weight:600;padding:3px 10px;border-radius:999px;color:#fff}
  .k-SPEC_BODY{background:#0a84ff}.k-COVER{background:#5e5ce6}.k-FIGURE{background:#98989d}.k-FRONT_MATTER{background:#ac8e68}
  .tg{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--ink);border:1px solid var(--line);
      border-radius:999px;padding:4px 10px;cursor:pointer;background:var(--panel);user-select:none}
  .tg input{display:none}
  .tg .dot{width:9px;height:9px;border-radius:50%}
  .tg.off{color:var(--sub);background:var(--bg)}
  .tg.off .dot{opacity:.25}

  main{flex:1;display:flex;min-height:0}
  #leftcol{flex:11;display:flex;flex-direction:column;min-width:0;min-height:0}
  #leftpane{flex:1;overflow:auto;padding:16px;min-height:0}
  #stage{position:relative;width:min(100%,860px);margin:0 auto;box-shadow:0 2px 14px var(--shadow);
         border-radius:6px;overflow:hidden;background:#fff;touch-action:pan-x pan-y}
  #stage img{display:block;width:100%;pointer-events:none;user-select:none}
  #overlay{position:absolute;inset:0}
  .box{position:absolute;border-radius:2px}
  .box.kept{border:1px solid color-mix(in srgb,var(--kept) 50%,transparent);background:color-mix(in srgb,var(--kept) 9%,transparent);
            z-index:2;cursor:pointer}
  .box.line_number{border:1.5px solid var(--ln);background:color-mix(in srgb,var(--ln) 24%,transparent);z-index:3;cursor:pointer}
  .box.header_footer{border:1.5px solid var(--hf);background:color-mix(in srgb,var(--hf) 20%,transparent);z-index:3;cursor:pointer}
  .box.unexplained{border:2.5px solid var(--bad);background:color-mix(in srgb,var(--bad) 20%,transparent);z-index:5;cursor:pointer}
  .box.para{border:1px dashed color-mix(in srgb,var(--para) 60%,transparent);border-left:3px solid var(--para);
            background:transparent;z-index:1;cursor:pointer}
  .box.para.hot{background:color-mix(in srgb,var(--para) 15%,transparent);border-color:var(--para)}
  .annbox{position:absolute;z-index:6;pointer-events:none;border-radius:3px}
  .annbox.region{pointer-events:auto;cursor:pointer}
  .annbox.wrong_del{border:2.5px double var(--bad);box-shadow:0 0 0 2px color-mix(in srgb,var(--bad) 35%,transparent)}
  .annbox.missed_del{border:2.5px double var(--ln);box-shadow:0 0 0 2px color-mix(in srgb,var(--ln) 35%,transparent)}
  .annbox.conv_err{border:2.5px double var(--kept);box-shadow:0 0 0 2px color-mix(in srgb,var(--kept) 40%,transparent)}
  .annbox.missed_rec{border:2.5px double var(--accent);box-shadow:0 0 0 2px color-mix(in srgb,var(--accent) 35%,transparent)}
  .drawrect{position:absolute;z-index:7;border:1.5px dashed var(--accent);background:color-mix(in srgb,var(--accent) 12%,transparent);
            pointer-events:none;border-radius:3px}
  #gutter{position:absolute;top:0;bottom:0;width:0;border-left:2px dashed rgba(255,69,58,.55);z-index:4}
  .hide-kept .box.kept,.hide-line_number .box.line_number,.hide-header_footer .box.header_footer,
  .hide-para .box.para,.hide-unexplained .box.unexplained,.hide-gutter #gutter,.hide-ann .annbox{display:none}
  .annmode .box,.annmode #overlay{cursor:crosshair}

  /* ---- 底部缩略图条(Adobe 式;横向滑动,居中吸附,active 放大) ---- */
  #film{flex:0 0 auto;border-top:1px solid var(--line);background:var(--panel);padding:10px 16px;overflow-x:auto;
        scroll-snap-type:x proximity;scrollbar-width:thin}
  #filmtrack{display:flex;gap:10px;width:max-content;padding:2px}
  .thumb{flex:0 0 auto;height:104px;border-radius:8px;overflow:hidden;position:relative;cursor:pointer;
         border:2px solid var(--line);transform:scale(.93);transition:transform .22s,border-color .18s;
         scroll-snap-align:center;background:#fff;box-shadow:0 1px 6px var(--shadow)}
  .thumb img{height:100%;width:auto;display:block}
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
  .annrow{display:flex;align-items:center;gap:8px;font-size:12px;border:1px solid var(--line);background:var(--card);
          border-radius:8px;padding:6px 10px;margin-bottom:6px}
  .annrow .cat{font-size:10.5px;font-weight:700;padding:1px 7px;border-radius:999px;color:#fff;flex:0 0 auto}
  .cat.wrong_del{background:var(--bad)}.cat.missed_del{background:var(--ln)}.cat.conv_err{background:var(--kept)}.cat.missed_rec{background:var(--accent)}
  .annrow .del{margin-left:auto;cursor:pointer;color:var(--sub);border:0;background:none;font-size:13px}
  .annrow .del:hover{color:var(--bad)}
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
      <button id="filmBtn" title="缩略图条开关">▦</button>
    </div>
    <span class="sep"></span>
    <div class="grp">
      <button id="zout" title="缩小">−</button>
      <button id="zl" title="点击恢复适宽">适宽</button>
      <button id="zin" title="放大">+</button>
    </div>
    <span class="sep"></span>
    <div class="grp">
      <button class="btn" id="annBtn" title="标记模式 (M)：点词框循环打标;空白处拖拽画区域框">✎ 标记</button>
      <button class="btn" id="expBtn" title="复制并下载 *_annotations.json">导出标记</button>
      <button class="btn" id="themeBtn" title="亮/暗切换">☀</button>
    </div>
  </div>
  <div class="row" id="toggles"></div>
</header>
<main>
  <section id="leftcol">
    <div id="leftpane"><div id="stage"><img id="img" alt=""><div id="overlay"></div></div></div>
    <div id="film"><div id="filmtrack"></div></div>
  </section>
  <section id="rightpane">
    <div class="sec"><h2>页统计</h2><div id="stats"></div></div>
    <div class="sec" id="noteSec" hidden><h2>说明</h2><div class="note" id="note"></div></div>
    <div class="sec" id="annSec" hidden><h2 style="color:var(--accent)">本页标记</h2><div id="annList"></div></div>
    <div class="sec" id="badSec" hidden><h2 style="color:var(--bad)">crosscheck 未解释删除</h2><div class="chips" id="bad"></div></div>
    <div class="sec"><h2>重排段落（中间产物）</h2><div id="paras"></div></div>
    <div class="sec"><h2>剔除词</h2><div id="removed"></div></div>
  </section>
</main>
<footer><kbd>←</kbd><kbd>→</kbd> 翻页 · <kbd>M</kbd> 标记模式（点词框打标 / 拖拽画区域框） · <kbd>Ctrl</kbd>+滚轮缩放 · 左击词框/段落框 → 右栏定位 · 生成于 __STAMP__</footer>
<div id="toast"></div>
<script>
const DATA = __DATA__;
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
const $=id=>document.getElementById(id);
const ov=$("overlay"), st=$("stage"), lp=$("leftpane");
let cur=0, zoom=0, annMode=false, suppressClick=false;   // zoom 0 = 适宽
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

/* ---- 缩放 ---- */
function applyZoom(){
  st.style.width = zoom===0 ? "min(100%,860px)" : Math.round(BASE*zoom)+"px";
  $("zl").textContent = zoom===0 ? "适宽" : Math.round(zoom*100)+"%";
}
$("zin").onclick=()=>{zoom=Math.min((zoom||1)*1.25,5);applyZoom();};
$("zout").onclick=()=>{zoom=Math.max((zoom||1)/1.25,.4);applyZoom();};
$("zl").onclick=()=>{zoom=0;applyZoom();};
lp.addEventListener("wheel",e=>{
  if(!e.ctrlKey)return;
  e.preventDefault();
  zoom = e.deltaY<0 ? Math.min((zoom||1)*1.12,5) : Math.max((zoom||1)/1.12,.4);
  applyZoom();
},{passive:false});

/* ---- 缩略图条 ---- */
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
  if(a&&!film.hidden)a.scrollIntoView({inline:"center",block:"nearest",behavior:"smooth"});
}
film.addEventListener("wheel",e=>{           // 竖滚轮 → 横滑(相册式)
  if(e.ctrlKey)return;
  if(Math.abs(e.deltaY)>Math.abs(e.deltaX)){e.preventDefault();film.scrollLeft+=e.deltaY;}
},{passive:false});
function setFilm(show){film.hidden=!show;localStorage.setItem("dbgfilm",show?"1":"0");if(show)syncFilm();}
$("filmBtn").onclick=()=>setFilm(film.hidden);
setFilm(localStorage.getItem("dbgfilm")!=="0");

/* ---- 标记 ---- */
const annKey=(page,bstr)=>page+"|"+bstr;
function saveAnn(){localStorage.setItem(ANN_KEY,JSON.stringify(Object.fromEntries(ann)));}
function drawAnn(){
  ov.querySelectorAll(".annbox").forEach(e=>e.remove());
  const d=DATA[cur];
  for(const [k,v] of ann){
    if(v.page!==d.page)continue;
    const el=document.createElement("div");
    el.className="annbox "+v.cat+(v.kind==="region"?" region":"");
    el.dataset.k=k;
    el.style.left=pct(v.bbox[0],d.w);el.style.top=pct(v.bbox[1],d.h);
    el.style.width=pct(v.bbox[2]-v.bbox[0],d.w);el.style.height=pct(v.bbox[3]-v.bbox[1],d.h);
    el.title=(v.kind==="region"?"区域: ":"")+ANN_NAMES[v.cat]+(v.text?" — "+v.text:"");
    ov.appendChild(el);
  }
  renderAnnList();
}
function renderAnnList(){
  const d=DATA[cur];
  const rows=[...ann.entries()].filter(([,v])=>v.page===d.page);
  $("annSec").hidden=!rows.length;
  $("annList").innerHTML=rows.map(([k,v])=>
    `<div class="annrow"><span class="cat ${v.cat}">${ANN_NAMES[v.cat]}</span>
     <span>${v.kind==="region"?"▭ 区域 ["+v.bbox.map(Math.round)+"]":esc(v.text)}</span>
     <button class="del" data-k="${esc(k)}" title="删除标记">✕</button></div>`).join("");
  $("annList").querySelectorAll(".del").forEach(b=>b.onclick=()=>{ann.delete(b.dataset.k);saveAnn();drawAnn();});
}
$("annBtn").onclick=()=>{annMode=!annMode;$("annBtn").classList.toggle("on",annMode);st.classList.toggle("annmode",annMode);
  toast(annMode?"标记模式开：点词框打标 / 空白处拖拽画区域框":"标记模式关");};
$("expBtn").onclick=()=>{
  const arr=[...ann.values()].sort((a,b)=>a.page-b.page);
  const json=JSON.stringify({doc:TITLE,exported:new Date().toISOString(),n:arr.length,
    legend:{wrong_del:"误删(内容被错误剔除)",missed_del:"漏删(噪声未被剔除)",conv_err:"转换错误(空格/段落/标题等)",missed_rec:"漏识别(引擎完全没框到的区域)"},
    annotations:arr},null,2);
  navigator.clipboard&&navigator.clipboard.writeText(json).then(()=>toast(`已复制 ${arr.length} 条标记 JSON 到剪贴板`),()=>{});
  const a=document.createElement("a");
  a.href=URL.createObjectURL(new Blob([json],{type:"application/json"}));
  a.download=TITLE+"_annotations.json";a.click();URL.revokeObjectURL(a.href);
};

/* ---- 标记模式：空白处拖拽画区域框 ---- */
let drawing=null;
st.addEventListener("pointerdown",e=>{
  if(!annMode||e.button!==0)return;
  if(e.target.closest(".annbox.region"))return;     // 点已有区域框 → 走 click 换类别
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
    const cl=v=>Math.max(0,v);
    const x0=cl((Math.min(e.clientX,drawing.x0)-r.left)*sx), y0=cl((Math.min(e.clientY,drawing.y0)-r.top)*sy);
    const x1=Math.min(d.w,(Math.max(e.clientX,drawing.x0)-r.left)*sx), y1=Math.min(d.h,(Math.max(e.clientY,drawing.y0)-r.top)*sy);
    if(x1-x0>3&&y1-y0>3){
      const b=[x0,y0,x1,y1].map(v=>Math.round(v*10)/10);
      ann.set(annKey(d.page,b.join(",")),{page:d.page,text:"",bbox:b,cat:"missed_rec",kind:"region"});
      saveAnn();drawAnn();toast("已添加区域标记：漏识别（点击该框可换类别/删除）");
    }
  }
  drawing=null;
});

/* ---- 左侧点击：标记(词/区域) 或 联动 ---- */
st.addEventListener("click",e=>{
  if(suppressClick){suppressClick=false;return;}
  const d=DATA[cur];
  const reg=e.target.closest(".annbox.region");
  if(reg&&annMode){                                   // 区域框：循环类别,末位删除
    const k=reg.dataset.k, v=ann.get(k);
    if(!v)return;
    const idx=REGION_CATS.indexOf(v.cat)+1;
    if(idx>=REGION_CATS.length){ann.delete(k);toast("已删除区域标记");}
    else{v.cat=REGION_CATS[idx];ann.set(k,v);toast("区域标记: "+ANN_NAMES[v.cat]);}
    saveAnn();drawAnn();return;
  }
  const el=e.target.closest(".box");
  if(!el)return;
  if(annMode){
    if(!el.dataset.t)return;                          // 词级框才可点标
    const k=annKey(d.page,el.dataset.b);
    const curCat=ann.get(k)?.cat, idx=curCat?WORD_CATS.indexOf(curCat)+1:0;
    if(idx>=WORD_CATS.length||idx<0){ann.delete(k);toast("已取消标记");}
    else{ann.set(k,{page:d.page,text:el.dataset.t,bbox:el.dataset.b.split(",").map(Number),cat:WORD_CATS[idx],kind:"word"});
         toast("标记: "+ANN_NAMES[WORD_CATS[idx]]+" — "+el.dataset.t);}
    saveAnn();drawAnn();return;
  }
  if(el.dataset.p)      flashEl($("card-"+el.dataset.p));                       // 保留词 → 段落卡
  else if(el.dataset.id) flashEl($("card-"+el.dataset.id));                     // 段落框 → 段落卡
  else if(el.dataset.r)  flashEl($("chip-"+el.dataset.r+"-"+el.dataset.i));     // 剔除词 → chip
  else if(el.classList.contains("unexplained")) flashEl($("badchip-"+el.dataset.i));
});

/* ---- 主题 ---- */
function applyTheme(t){document.body.dataset.theme=t;$("themeBtn").textContent=t==="dark"?"☀":"☾";localStorage.setItem("dbgtheme",t);}
$("themeBtn").onclick=()=>applyTheme(document.body.dataset.theme==="dark"?"light":"dark");
applyTheme(localStorage.getItem("dbgtheme")||"dark");

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
  $("img").src="data:image/png;base64,"+d.img;
  $("pgin").value=i+1;
  const k=$("kind"); k.textContent=d.kind; k.className="k-"+d.kind;
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
    args = ap.parse_args()

    pdfs = collect_pdfs(args.src)
    if not pdfs:
        print("未找到 PDF。")
        return 1
    md_root = Path(args.md_root)
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
            html = build_html(pdf.stem, pages)
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
