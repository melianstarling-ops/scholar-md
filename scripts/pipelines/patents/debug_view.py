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
  * 平移：普通模式长按左键 250ms 进入拖动平移（标记模式下拖拽仍是画框）；
    横向滚动条在左栏**顶缘**（代理条，仅页面宽于视口时出现）——原生底部横条
    会被 Dock 弹出遮挡，已收口禁用；触控板横扫/Shift+滚轮照常横移。
  * 暗色页面：默认对 PDF 页做反色渲染（仅暗色主题下生效），◑ 按钮可关。
  * 双向联动：右栏段落卡 hover → 左侧高亮；左侧点击词框/段落框 → 右栏定位；
    标记框 ↔ 右栏"本页标记"行互相联动（hover 高亮、点击定位）。
  * 标记（M 进入标记模式）：
      - 点击词框 / 拖拽画区域框 → 弹出色点气泡直接选语义（误删红/漏删橙/
        转换错蓝/漏识别主题色），选定即自动收起；点已有标记框可重选/删除；
      - 区域框按住左键可整框拖动微调位置；
      - 右栏标记行内嵌色点，可直接改语义；Delete 键删除选中标记；
      - localStorage 实时自动保存；导出有两条路：
        · 服务模式（`--serve`，推荐）：「导出标记」直接 POST 回写
          03_Output/patents/<名>/<名>_annotations.json，无弹窗、无 Downloads、无 --collect；
        · 静态模式：「导出标记」下载到 Downloads + 复制剪贴板，随后跑
          `debug_view.py --collect` 归位。POST 失败自动退回下载。
  * 主题：默认暗色（对齐 .vscode/md-theme.css 的 claude-dark 配色），☀/☾ 可切换。

不进主管线、不改产物；判定数据与主转换同源（同一批函数现算），所见即引擎所判。

用法（H.5 自适应 I/O）:
    python scripts/pipelines/patents/debug_view.py                 # 静态:生成 <stem>_debug.html
    python scripts/pipelines/patents/debug_view.py --src <pdf|dir> [--zoom 2.0]
    python scripts/pipelines/patents/debug_view.py --serve         # 服务模式:导出直写工作区(推荐)
    python scripts/pipelines/patents/debug_view.py --collect       # 归位下载目录的标记文件(静态模式善后)
输出: 静态模式落本脚本同目录 <stem>_debug.html（已全局 gitignore）；服务模式不落静态文件。
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

from bib_parse import _inid_map, parse_cover
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
    reconstruct,
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
                 unexplained: list[dict], garbles: list[dict], zoom: float,
                 img: str | None = None, stem: str = "") -> dict:
    if img is None:   # serve 热刷新时由 _cached_imgs 复用,静态模式现渲染
        pix = doc[info.index].get_pixmap(matrix=fitz.Matrix(zoom, zoom))
        img = base64.b64encode(pix.tobytes("png")).decode()
    payload: dict = {
        "page": info.index + 1,
        "kind": info.kind.value,
        "w": info.width,
        "h": info.height,
        "img": img,
        "gutter": round(info.gutter_x, 1),
        "ladder_n": len(info.ladder),
        "n_words": len(info.words),
        "boxes": [],
        "paras": [],
        "removed": {"line_number": [], "header_footer": []},
        "unexplained": unexplained,
        "garbles": garbles,
        "note": _KIND_NOTE[info.kind.value],
    }
    boxes: list[dict] = payload["boxes"]

    if info.kind == PageKind.COVER:
        # 封面走 bib_parse(INID 切片)而非几何重排:画全部 OCR 词框(cword 层,默认可见),
        # 右栏出 bib_parse 抽取诊断——6 个 YAML 字段抽到没抽到、切到哪些 INID 码、缺哪些关键码。
        boxes += [_box(w, "cword") for w in info.words]
        meta, abstract = parse_cover(info, profile, stem)
        text, _, _, _ = reconstruct(info.words, info.height, info.width / 2, profile)
        inid = _inid_map(text)
        ab = abstract.strip()
        fields = [
            {"label": "标题 (54)", "val": meta.get("title", ""), "ok": bool(meta.get("title"))},
            {"label": "发明人 (72)", "val": "; ".join(meta.get("inventors") or []),
             "ok": bool(meta.get("inventors"))},
            {"label": "受让人 (73)", "val": meta.get("assignee", ""), "ok": bool(meta.get("assignee"))},
            {"label": "公告日 (45)", "val": meta.get("date_granted", ""),
             "ok": bool(meta.get("date_granted"))},
            {"label": "分类号 (51/52)", "val": ", ".join(meta.get("classifications") or []),
             "ok": bool(meta.get("classifications"))},
            {"label": "摘要 (57)", "val": (ab[:160] + "…") if len(ab) > 160 else ab, "ok": bool(ab)},
        ]
        key_codes = ["54", "72", "73", "45", "57"]
        payload["cover"] = {
            "patent_number": meta.get("patent_number", ""),   # 取自文件名,不靠 OCR
            "fields": fields,
            "inid_found": sorted(inid.keys(), key=int),
            "missing_key": [c for c in key_codes if c not in inid],
        }
        payload["n_kept"] = len(info.words)

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
    for i, gb in enumerate(garbles):
        boxes.append({"c": "garble", "b": gb["bbox"], "t": f"{gb['text']} ⟨{gb['reason']}⟩", "i": i})
    return payload


def build_html(name: str, pages: list[dict], resolved: list[dict], serve: bool = False) -> str:
    data = json.dumps(pages, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    res = json.dumps(resolved, ensure_ascii=False, separators=(",", ":")).replace("</", "<\\/")
    save_url = f"/save/{name}" if serve else ""   # 服务模式:导出 POST 回写工作区;静态:空→走下载
    return (
        _TEMPLATE.replace("__TITLE__", name)
        .replace("__STAMP__", f"{datetime.now():%Y-%m-%d %H:%M}")
        .replace("__SAVE_URL__", save_url)
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
  /* 横向滚动收口到顶部代理条(#hscroll):原生底部横条会被 Dock 弹出遮挡 */
  #leftpane{flex:1;position:relative;overflow-y:auto;overflow-x:hidden;padding:16px;min-height:0}
  #leftpane.panning{cursor:grabbing}
  #leftpane.panning *{cursor:grabbing!important}
  #hscroll{height:0;overflow-x:auto;overflow-y:hidden;scrollbar-width:thin;background:var(--panel);
           border-bottom:1px solid transparent;transition:height .2s ease}
  #hscroll.on{height:12px;border-bottom-color:var(--line)}
  #hscroll>div{height:1px}
  #stage{position:relative;width:min(100%,860px);margin:0 auto;box-shadow:0 2px 14px var(--shadow);
         border-radius:6px;overflow:hidden;background:#fff;touch-action:pan-x pan-y}
  #stage img{display:block;width:100%;pointer-events:none;user-select:none}
  [data-theme="dark"] #stage.inv img{filter:invert(.93) hue-rotate(180deg)}
  #overlay{position:absolute;inset:0;user-select:none}
  .box{position:absolute;border-radius:2px;opacity:.8}
  .box:hover{opacity:1}
  .box.kept{border:1px solid color-mix(in srgb,var(--kept) 50%,transparent);background:color-mix(in srgb,var(--kept) 9%,transparent);
            z-index:2;cursor:pointer}
  .box.cword{border:1px solid color-mix(in srgb,var(--kept) 55%,transparent);background:color-mix(in srgb,var(--kept) 10%,transparent);
            z-index:2;cursor:default}
  .box.line_number{border:1.5px solid var(--ln);background:color-mix(in srgb,var(--ln) 24%,transparent);z-index:3;cursor:pointer}
  .box.header_footer{border:1.5px solid var(--hf);background:color-mix(in srgb,var(--hf) 20%,transparent);z-index:3;cursor:pointer}
  .box.unexplained{border:2.5px solid var(--bad);background:color-mix(in srgb,var(--bad) 20%,transparent);z-index:5;cursor:pointer}
  .box.garble{border:2.5px solid #d2a106;background:color-mix(in srgb,#d2a106 22%,transparent);z-index:5;cursor:pointer}
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
  .hide-kept .box.kept,.hide-cword .box.cword,.hide-line_number .box.line_number,.hide-header_footer .box.header_footer,
  .hide-para .box.para,.hide-unexplained .box.unexplained,.hide-garble .box.garble,.hide-gutter #gutter,.hide-ann .annbox{display:none}
  /* ---- 封面解析(bib_parse)诊断面板 ---- */
  .cfield{display:flex;gap:7px;align-items:baseline;padding:5px 0;border-bottom:1px solid var(--line);font-size:11px}
  .cfield:last-child{border-bottom:0}
  .cfield .ck{flex:none;font-weight:700;width:13px;text-align:center}
  .cfield .ck.ok{color:var(--para)}.cfield .ck.no{color:var(--bad)}
  .cfield .cl{flex:none;min-width:78px;color:var(--sub)}
  .cfield .cv{color:var(--ink);word-break:break-word}
  .cfield.miss .cv{color:var(--sub)}
  .chip.misscode{border-color:var(--bad);color:var(--bad)}
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
  /* 错误页筛选:⚠ 角标常显,开启时缩略图只剩错误页 */
  .thumb .terr{position:absolute;right:4px;top:3px;font-size:10px;color:var(--bad);
               text-shadow:0 0 3px rgba(0,0,0,.6)}
  #errBtn.on{color:var(--bad);border-color:var(--bad);background:color-mix(in srgb,var(--bad) 12%,transparent)}
  .erronly #filmtrack .thumb.noerr{display:none}

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
  .annrow .del{flex:0 0 auto;cursor:pointer;color:var(--sub);border:0;background:none;font-size:13px}
  .annrow .del:hover{color:var(--bad)}
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
      <button id="errBtn" title="只看有错误标记的页:未解释删除/坏字形 (E)">⚠</button>
      <button id="reloadBtn" title="重新载入最新转换结果(服务模式下重读最新 md/crosscheck) (R / F5)">↻</button>
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
      <button class="btn" id="expBtn" title="导出标记:服务模式直写 03_Output/;静态模式下载到 Downloads 后跑 --collect 归位">导出标记</button>
      <button class="btn" id="themeBtn" title="亮/暗切换">☀</button>
    </div>
  </div>
  <div class="row" id="toggles"></div>
</header>
<main>
  <section id="leftcol">
    <div id="hscroll" title="横向滚动(页面宽于视口时出现)"><div></div></div>
    <div id="leftpane"><div id="stage"><img id="img" alt=""><div id="overlay"></div></div></div>
    <div id="filmzone"></div>
    <div id="film"><div id="filmtrack"></div></div>
  </section>
  <section id="rightpane">
    <div class="sec"><h2>页统计</h2><div id="stats"></div></div>
    <div class="sec" id="coverSec" hidden><h2 style="color:var(--kept)">封面解析 (bib_parse)</h2><div id="coverBody"></div></div>
    <div class="sec" id="noteSec" hidden><h2>说明</h2><div class="note" id="note"></div></div>
    <div class="sec" id="annSec" hidden><h2 style="color:var(--accent)">本页标记<button id="clrRes" class="mini" hidden>清除已处理</button></h2><div id="annList"></div></div>
    <div class="sec" id="badSec" hidden><h2 style="color:var(--bad)">crosscheck 未解释删除</h2><div class="chips" id="bad"></div></div>
    <div class="sec" id="garbleSec" hidden><h2 style="color:#d2a106">坏字形(疑似源缺陷·只标不改)</h2><div class="chips" id="garble"></div></div>
    <div class="sec" id="parasSec"><h2>重排段落（中间产物）</h2><div id="paras"></div></div>
    <div class="sec" id="removedSec"><h2>剔除词</h2><div id="removed"></div></div>
  </section>
</main>
<footer><kbd>←</kbd><kbd>→</kbd> 翻页 · <kbd>M</kbd> 标记模式（点词框/拖框 → 气泡选语义） · <kbd>Delete</kbd> 删选中标记 · <kbd>Ctrl</kbd>+滚轮 指针锚点缩放 · 长按左键拖动平移 · 横向滚动条在左栏顶缘 · <kbd>E</kbd> 错误页筛选 · 底缘悬停出缩略图 · 生成于 __STAMP__</footer>
<div id="pop" hidden></div>
<div id="toast"></div>
<script>
const DATA = __DATA__;
const RESOLVED = __RESOLVED__;   // agent 归档件中已处理标记的 (page,bbox) 键集(生成时嵌入,SOP-07 §3)
const SAVE_URL = "__SAVE_URL__"; // 非空=服务模式:导出直接 POST 回写 03_Output;空=静态:走浏览器下载
const TITLE = document.title.replace(" · debug","");
const LAYERS = [
  ["line_number","行号(剔)","var(--ln)",true],
  ["header_footer","页眉/页脚(剔)","var(--hf)",true],
  ["kept","保留词","var(--kept)",false],
  ["cword","封面词(OCR)","var(--kept)",true],
  ["para","段落","var(--para)",true],
  ["unexplained","未解释","var(--bad)",true],
  ["garble","坏字形","#d2a106",true],
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

/* ---- 顶部横向滚动条(代理):#leftpane overflow-x:hidden,原生底横条让位给 Dock ---- */
const hs=$("hscroll"), hsi=hs.firstElementChild;
function syncH(){
  hsi.style.width=lp.scrollWidth+"px";
  hs.classList.toggle("on",lp.scrollWidth>lp.clientWidth+1);
}
/* 双向同步:scroll 事件仅在值实际变化时触发,赋同值即收敛,无需加锁 */
hs.addEventListener("scroll",()=>{lp.scrollLeft=hs.scrollLeft;});
lp.addEventListener("scroll",()=>{hs.scrollLeft=lp.scrollLeft;});
new ResizeObserver(syncH).observe(st);
new ResizeObserver(syncH).observe(lp);
/* overflow-x:hidden 杀掉了原生横向手势 → 触控板 deltaX / Shift+滚轮 改由 JS 横移 */
lp.addEventListener("wheel",e=>{
  if(e.ctrlKey)return;                                  // 缩放走上面的 ctrl 分支
  const dx=(e.shiftKey&&!e.deltaX)?e.deltaY:e.deltaX;
  if(dx){e.preventDefault();lp.scrollLeft+=dx;}
},{passive:false});

/* ---- 长按左键拖动平移(普通模式;标记模式拖拽仍是画区域框) ---- */
let pan=null,panTimer=null,panStart=null;
lp.addEventListener("pointerdown",e=>{
  if(e.button!==0||annMode||e.target.closest(".annbox"))return;
  panStart={x:e.clientX,y:e.clientY,id:e.pointerId};
  clearTimeout(panTimer);
  panTimer=setTimeout(()=>{                             // 按住 250ms 不动 → 进入平移
    if(!panStart)return;
    pan={x:panStart.x,y:panStart.y,sl:lp.scrollLeft,st:lp.scrollTop,moved:false};
    try{lp.setPointerCapture(panStart.id);}catch(_){}   // D2:指针捕获直挂容器
    lp.classList.add("panning");
  },250);
});
lp.addEventListener("pointermove",e=>{
  if(pan){
    e.preventDefault();
    if(!pan.moved&&Math.hypot(e.clientX-pan.x,e.clientY-pan.y)>2)pan.moved=true;
    lp.scrollLeft=pan.sl-(e.clientX-pan.x);
    lp.scrollTop =pan.st-(e.clientY-pan.y);
  }else if(panStart&&Math.hypot(e.clientX-panStart.x,e.clientY-panStart.y)>6){
    clearTimeout(panTimer);panStart=null;               // 先动后停 ≠ 长按,放行原有点击/画框
  }
});
function endPan(e){
  clearTimeout(panTimer);panStart=null;
  if(!pan)return;
  const moved=pan.moved;pan=null;
  lp.classList.remove("panning");
  try{lp.releasePointerCapture(e.pointerId);}catch(_){}
  if(moved){suppressClick=true;setTimeout(()=>suppressClick=false,250);}  // D2:旗标定时清除
}
lp.addEventListener("pointerup",endPan);
lp.addEventListener("pointercancel",endPan);

/* ---- PDF 页面反色(暗色主题) ---- */
function setInv(on){st.classList.toggle("inv",on);$("filmtrack").parentElement.classList.toggle("inv-thumbs",on);
  $("invBtn").classList.toggle("on",on);localStorage.setItem("dbginv",on?"1":"0");}
$("invBtn").onclick=()=>setInv(!st.classList.contains("inv"));
setInv(localStorage.getItem("dbginv")!=="0");

/* ---- 缩略图条(Dock 式自动隐藏) ---- */
const film=$("film"), track=$("filmtrack");
const KINDC={SPEC_BODY:"#0a84ff",COVER:"#5e5ce6",FIGURE:"#98989d",FRONT_MATTER:"#ac8e68"};
/* 错误页 = 有 crosscheck 未解释删除 或 坏字形标记的页(确定性标记,与右栏区块同源) */
const errPages=DATA.map((d,i)=>(d.unexplained.length||(d.garbles||[]).length)?i:-1).filter(i=>i>=0);
let errOnly=false;
DATA.forEach((d,i)=>{
  const t=document.createElement("div");
  const isErr=errPages.includes(i);
  t.className="thumb"+(isErr?"":" noerr"); t.dataset.i=i;
  t.innerHTML=`<img loading="lazy" src="data:image/png;base64,${d.img}" alt="">
    <span class="tdot" style="background:${KINDC[d.kind]}"></span>${isErr?'<span class="terr">⚠</span>':''}<span class="tno">${i+1}</span>`;
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
function markExported(){localStorage.setItem(EXP_KEY,annSerial());updateDirty();}
function downloadJson(json){
  navigator.clipboard&&navigator.clipboard.writeText(json).then(()=>{},()=>{});
  const a=document.createElement("a");
  a.href=URL.createObjectURL(new Blob([json],{type:"application/json"}));
  a.download=TITLE+"_annotations.json";a.click();URL.revokeObjectURL(a.href);
  markExported();
  toast(`已导出 ${ann.size} 条(下载目录+剪贴板);跑 --collect 归位到 md 文件夹`);
}
$("expBtn").onclick=async()=>{
  const json=exportJson();
  if(SAVE_URL){   // 服务模式:直写工作区,失败再退回下载
    try{
      const r=await fetch(SAVE_URL,{method:"POST",headers:{"Content-Type":"application/json"},body:json});
      if(!r.ok)throw new Error(r.status);
      markExported();
      toast(`已写入工作区 ${ann.size} 条 → 03_Output/（无需 --collect）`);
      return;
    }catch(e){toast("回写失败,改用下载…");}
  }
  downloadJson(json);
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
  else if(el.classList.contains("garble")) flashEl($("garblechip-"+el.dataset.i));
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
       <button class="del" title="删除标记">✕</button>
     </div>
     ${!editing&&v.note?`<div class="nsnip" title="${esc(v.note)}">${esc(v.note)}</div>`:""}
     ${editing?`<textarea class="annnote" placeholder="描述具体问题…(自动保存,失焦收起)">${esc(v.note||"")}</textarea>`:""}
    </div>`;}).join("");
  $("annList").querySelectorAll(".annitem").forEach(row=>{
    const k=row.dataset.k;
    row.querySelectorAll(".setcat").forEach(p=>p.onclick=ev=>{ev.stopPropagation();
      const v=ann.get(k);v.cat=p.dataset.c;ann.set(k,v);saveAnn();drawAnn();});
    row.querySelector(".del").onclick=ev=>{ev.stopPropagation();noteEdit.delete(k);delAnn(k);};
    const ta=row.querySelector(".annnote");
    if(ta){
      ta.addEventListener("click",ev=>ev.stopPropagation());
      ta.addEventListener("input",()=>{
        const v=ann.get(k);
        if(ta.value.trim())v.note=ta.value;else delete v.note;
        ann.set(k,v);saveAnn();});
      ta.addEventListener("keydown",ev=>{if(ev.key==="Escape")ta.blur();});
      ta.addEventListener("blur",()=>{noteEdit.delete(k);drawAnn();});   // 失焦收起,回摘要显示
    }
    row.addEventListener("mouseenter",()=>{const b=ov.querySelector(`.annbox[data-k="${k}"]`);if(b)b.classList.add("hot");});
    row.addEventListener("mouseleave",()=>{const b=ov.querySelector(`.annbox[data-k="${k}"]`);if(b)b.classList.remove("hot");});
    row.addEventListener("click",()=>{        // 点行 = 联动定位 + 直接展开 note 输入(无需按钮)
      selKey=k;noteEdit.add(k);drawAnn();
      const b=ov.querySelector(`.annbox[data-k="${k}"]`);if(b){b.scrollIntoView({block:"center",behavior:"smooth"});flashEl(b);}
      const t2=$("annList").querySelector(`.annitem[data-k="${k}"] .annnote`);
      if(t2){t2.focus();t2.selectionStart=t2.value.length;}});
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

/* ---- 错误页筛选(⚠/E):翻页与缩略图条都只在错误页间移动 ---- */
function updatePgTotal(){
  const pos=errPages.indexOf(cur);
  $("pgtotal").textContent="/ "+DATA.length+(errOnly?` · ⚠${pos>=0?pos+1:"-"}/${errPages.length}`:"");
}
function setErrOnly(on){
  if(on&&!errPages.length){toast("本档没有错误标记页(未解释/坏字形)");return;}
  errOnly=on;
  $("errBtn").classList.toggle("on",on);
  document.body.classList.toggle("erronly",on);
  if(on&&!errPages.includes(cur))render(errPages[0]); else render(cur);
  toast(on?`错误页筛选开:${errPages.length} 页(未解释/坏字形)`:"错误页筛选关");
}
$("errBtn").onclick=()=>setErrOnly(!errOnly);

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
  localStorage.setItem("dbgpage:"+TITLE,i);   // 记当前页,刷新后恢复(热刷新不弹回首页)
  closePop(); selKey=null;
  $("img").src="data:image/png;base64,"+d.img;
  $("pgin").value=i+1;
  const k=$("kind"); k.textContent=KIND_LABEL[d.kind]; k.className="k-"+d.kind;
  k.title="页型判定(page_classify): "+d.kind;
  const lo=errOnly&&errPages.length?errPages[0]:0,
        hi=errOnly&&errPages.length?errPages[errPages.length-1]:DATA.length-1;
  $("prev").disabled=i<=lo; $("next").disabled=i>=hi;
  updatePgTotal();
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
    if(b.i!==undefined&&(b.c==="unexplained"||b.c==="garble"))el.dataset.i=b.i;
    ov.appendChild(el);
  }
  drawAnn();

  const isCover=d.kind==="COVER";
  const okN=isCover&&d.cover?d.cover.fields.filter(f=>f.ok).length:0;
  const kept=("n_kept" in d&&!isCover)?` · 保留 <b>${d.n_kept}</b>`:"";
  const rm=d.removed, nrm=(rm.line_number.length+rm.header_footer.length);
  $("stats").innerHTML=
    `<span>词 <b>${d.n_words}</b>${kept}${isCover?"":` · 剔除 <b>${nrm}</b>`}</span>`+
    (d.kind==="SPEC_BODY"?`<span>gutter <b>${d.gutter}</b> · 行号阶梯 <b>${d.ladder_n}</b></span>`:"")+
    (isCover?`<span>字段抽取 <b>${okN}/6</b></span>`:`<span>段落 <b>${d.paras.length}</b></span>`);

  $("noteSec").hidden=!d.note; $("note").textContent=d.note;

  $("badSec").hidden=!d.unexplained.length;
  $("bad").innerHTML=d.unexplained.map((u,j)=>`<span class="chip bad" id="badchip-${j}" title="[${u.bbox}]">${esc(u.text)}</span>`).join("");

  const gb=d.garbles||[];
  $("garbleSec").hidden=!gb.length;
  $("garble").innerHTML=gb.map((u,j)=>`<span class="chip" id="garblechip-${j}" style="border-color:#d2a106;color:#d2a106" title="${u.reason} [${u.bbox}]">${esc(u.text)}</span>`).join("");

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

  // 封面页:用 bib_parse 诊断面板替代重排段落/剔除词区
  $("coverSec").hidden=!isCover; $("parasSec").hidden=isCover; $("removedSec").hidden=isCover;
  if(isCover&&d.cover){
    const cv=d.cover;
    const pn=`<div class="cfield"><span class="ck ok">✓</span><span class="cl">专利号</span>`+
      `<span class="cv">${esc(cv.patent_number)} <span style="color:var(--sub)">(取自文件名)</span></span></div>`;
    const fields=cv.fields.map(f=>
      `<div class="cfield${f.ok?"":" miss"}"><span class="ck ${f.ok?"ok":"no"}">${f.ok?"✓":"✗"}</span>`+
      `<span class="cl">${esc(f.label)}</span><span class="cv">${f.val?esc(f.val):"— 未解析 —"}</span></div>`).join("");
    const found=cv.inid_found.map(c=>`<span class="chip">(${c})</span>`).join("");
    const miss=cv.missing_key.map(c=>`<span class="chip misscode" title="OCR 未读对该 INID 码 → 对应字段抽空">(${c})</span>`).join("");
    $("coverBody").innerHTML=pn+fields+
      `<div style="margin-top:9px"><div style="color:var(--sub);font-size:10px;margin-bottom:4px">切到的 INID 码 × ${cv.inid_found.length}</div><div class="chips">${found}</div></div>`+
      (miss?`<div style="margin-top:8px"><div style="color:var(--bad);font-size:10px;margin-bottom:4px">缺失关键码 × ${cv.missing_key.length}(对应字段抽空)</div><div class="chips">${miss}</div></div>`:"");
  }
}
function hot(id,on){const el=ov.querySelector(`[data-id="${id}"]`); if(el) el.classList.toggle("hot",on);}
function step(dir){
  if(errOnly&&errPages.length){
    const nxt=dir>0?errPages.find(p=>p>cur):[...errPages].reverse().find(p=>p<cur);
    if(nxt!==undefined)render(nxt);
  }else{
    const t=cur+dir;
    if(t>=0&&t<DATA.length)render(t);
  }
}
$("prev").onclick=()=>step(-1);
$("next").onclick=()=>step(1);
$("reloadBtn").onclick=()=>location.reload();
document.addEventListener("keydown",e=>{
  if(e.target.tagName==="INPUT")return;
  if(e.key==="ArrowRight")step(1);
  if(e.key==="ArrowLeft")step(-1);
  if(e.key==="m"||e.key==="M")$("annBtn").click();
  if(e.key==="e"||e.key==="E")$("errBtn").click();
  if(e.key==="r"||e.key==="R")location.reload();
  if(e.key==="Delete"&&selKey){delAnn(selKey);closePop();}
  if(e.key==="Escape"){closePop();selKey=null;drawAnn();}
});
const _saved=+(localStorage.getItem("dbgpage:"+TITLE)||0);
render(_saved>=0&&_saved<DATA.length?_saved:0);   // 刷新后停在原页(热刷新工作流)
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


def _load_crosscheck(md_root: Path, stem: str, n_pages: int) -> tuple[list[list[dict]], list[list[dict]]]:
    """读 crosscheck 报告（如有），取每页未解释词与坏字形（ToUnicode 缺陷,只标不改）。"""
    unexpl: list[list[dict]] = [[] for _ in range(n_pages)]
    garble: list[list[dict]] = [[] for _ in range(n_pages)]
    for cand in (md_root / stem / f"{stem}_crosscheck.json", md_root / f"{stem}_crosscheck.json"):
        if cand.exists():
            rep = json.loads(cand.read_text(encoding="utf-8"))
            for pr in rep.get("pages", []):
                idx = pr["page"] - 1
                if 0 <= idx < n_pages:
                    unexpl[idx] = pr.get("unexplained", [])
                    garble[idx] = pr.get("garbles", [])
            break
    return unexpl, garble


# serve 热刷新:页面图(PDF 渲染)按 PDF mtime 缓存。改转换代码重转 md 后浏览器刷新,
# 只要源/夹层 PDF 未变就复用图,刷新只重算 overlay/md。仅 serve 模式用(静态生成不缓存,免内存涨)。
_IMG_CACHE: dict[tuple, list[str]] = {}


def _cached_imgs(pdf: Path, doc: "fitz.Document", zoom: float) -> list[str]:
    key = (str(pdf), pdf.stat().st_mtime, zoom)
    if key not in _IMG_CACHE:
        for k in [k for k in _IMG_CACHE if k[0] == str(pdf)]:   # PDF 重转 → 旧版本图作废
            del _IMG_CACHE[k]
        _IMG_CACHE[key] = [
            base64.b64encode(doc[i].get_pixmap(matrix=fitz.Matrix(zoom, zoom)).tobytes("png")).decode()
            for i in range(doc.page_count)
        ]
    return _IMG_CACHE[key]


# serve 热刷新需 reload 的转换模块,顺序=被依赖者在前(拓扑):wordfix/profiles 无内部依赖,
# reading_order 依赖 wordfix,page_classify 依赖 profiles+reading_order。新增管线模块时补这里。
_PIPELINE_MODULES = ("wordfix", "profiles", "reading_order", "claims",
                     "figures", "bib_parse", "page_classify")


def _reload_pipeline() -> None:
    """reload 转换模块并重绑定本模块 from-import 的符号 —— 改转换代码后浏览器刷新即见最新
    (改代码有语法错时,reload 在此抛异常,do_GET 捕获并显示在错误页,正是开发期想要的)。"""
    import importlib
    for name in _PIPELINE_MODULES:
        mod = sys.modules.get(name)
        if mod is not None:
            importlib.reload(mod)
    import bib_parse as _bp
    import page_classify as _pc
    import profiles as _pf
    import reading_order as _ro
    g = globals()
    g.update(classify_document=_pc.classify_document, PageKind=_pc.PageKind,
             get_profile=_pf.get_profile, LayoutProfile=_pf.LayoutProfile,
             parse_cover=_bp.parse_cover, _inid_map=_bp._inid_map)
    for fn in ("Word", "Y_TOL_RATIO", "_column_paragraph_infos", "group_lines", "join_line",
               "median_char_width", "median_height", "reconstruct", "split_columns", "strip_bands",
               "strip_line_numbers"):
        g[fn] = getattr(_ro, fn)


def render_doc(pdf: Path, md_root: Path, profile: LayoutProfile, zoom: float, serve: bool) -> tuple[str, int]:
    """单篇 → (html, 页数)。静态/服务两模式共用，仅 SAVE_URL 注入不同。
    serve 模式每次请求实时重读最新 md/crosscheck 渲染(热刷新),页面图按 PDF mtime 缓存复用。"""
    doc = fitz.open(str(pdf))
    infos = classify_document(doc, profile)
    unexpl, garble = _load_crosscheck(md_root, pdf.stem, doc.page_count)
    imgs = _cached_imgs(pdf, doc, zoom) if serve else None
    pages = [page_payload(doc, info, profile, unexpl[info.index], garble[info.index], zoom,
                          imgs[info.index] if imgs else None, pdf.stem) for info in infos]
    doc.close()
    return build_html(pdf.stem, pages, _load_resolved(md_root, pdf.stem), serve), len(pages)


def serve_docs(pdfs: list[Path], md_root: Path, zoom: float, port: int) -> int:
    """本地服务模式（热刷新）：每次 GET **实时重读最新 md/crosscheck 渲染** —— 改转换代码、
    终端重转后,浏览器刷新即见最新结果。POST /save/<stem> 直接回写
    03_Output/<stem>/<stem>_annotations.json（无需 --collect）。仅绑 127.0.0.1。"""
    import urllib.parse
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    by_stem = {p.stem: p for p in pdfs}

    def index_html() -> str:
        links = "".join(f'<li><a href="/{s}">{s}</a></li>' for s in sorted(by_stem))
        return f"<!doctype html><meta charset=utf-8><title>debug_view</title><h2>调试视图</h2><ul>{links}</ul>"

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静默默认日志
            pass

        def _html(self, html: str, code: int = 200):
            body = html.encode("utf-8")
            self.send_response(code)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")   # 刷新必拿最新,不走浏览器缓存
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            key = urllib.parse.unquote(self.path.split("?")[0]).strip("/")
            if key == "" and len(by_stem) > 1:
                self._html(index_html())
                return
            pdf = by_stem.get(key) or (next(iter(by_stem.values())) if key == "" else None)
            if pdf is None:
                self.send_error(404)
                return
            try:
                _reload_pipeline()   # 改转换代码 → 刷新即用最新逻辑(语法错在此抛,显错误页)
                html, _ = render_doc(pdf, md_root, get_profile(), zoom, serve=True)  # 实时重读最新产物
            except Exception:   # noqa: BLE001  单次渲染/reload 失败回错误页,不崩服务
                import traceback
                self._html("<!doctype html><meta charset=utf-8>"
                           f"<pre style='padding:16px;color:#c33'>渲染失败:\n\n{traceback.format_exc()}</pre>", 500)
                return
            self._html(html)

        def do_POST(self):
            path = urllib.parse.unquote(self.path)
            if not path.startswith("/save/"):
                self.send_error(404)
                return
            stem = path[len("/save/"):]
            if stem not in by_stem:   # 只接受已知文档,只写固定文件名
                self.send_error(403, "unknown doc")
                return
            body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
            try:
                json.loads(body)
            except (json.JSONDecodeError, ValueError):
                self.send_error(400, "bad json")
                return
            target = md_root / stem / f"{stem}_annotations.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(body)
            print(f"  [SAVE] {stem} → {target}")
            self.send_response(200)
            self.send_header("Content-Length", "2")
            self.end_headers()
            self.wfile.write(b"ok")

    srv = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    base = f"http://127.0.0.1:{port}"
    print(f"\n服务已启动 → 在 VS Code Simple Browser 或浏览器打开：")
    if len(by_stem) == 1:
        print(f"  {base}/")
    else:
        for s in sorted(by_stem):
            print(f"  {base}/{s}")
    print("  改代码→终端重转→浏览器刷新即见最新；导出标记直接回写 03_Output/。Ctrl+C 停止。\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        srv.server_close()
    return 0


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
    ap.add_argument("--serve", action="store_true",
                    help="本地服务模式：导出标记直接回写 03_Output/（无需 --collect），经 localhost 打开页面")
    ap.add_argument("--port", type=int, default=8077, help="服务端口（默认 8077）")
    args = ap.parse_args()

    pdfs = collect_pdfs(args.src)
    if not pdfs:
        print("未找到 PDF。")
        return 1
    md_root = Path(args.md_root)
    if args.collect:
        return 0 if collect_annotations(pdfs, md_root) >= 0 else 1
    profile = get_profile()

    if args.serve:   # 本地服务(热刷新):每次刷新实时重读最新产物渲染
        print(f"[{datetime.now():%H:%M:%S}] 服务模式(热刷新) {len(pdfs)} 份（zoom={args.zoom}）")
        print("  工作流:改转换代码 → 浏览器刷新(↻/R/F5)即见最新(转换逻辑自动 reload);")
        print("  crosscheck/标记类需先在终端重跑 crosscheck_words.py 再刷新。")
        return serve_docs(pdfs, md_root, args.zoom, args.port)

    print(f"[{datetime.now():%H:%M:%S}] 生成调试视图 {len(pdfs)} 份（zoom={args.zoom}）\n")
    failed = 0
    for pdf in pdfs:
        try:
            html, n = render_doc(pdf, md_root, profile, args.zoom, serve=False)
            out_dir = Path(args.out)
            out_dir.mkdir(parents=True, exist_ok=True)
            out_html = out_dir / f"{pdf.stem}_debug.html"
            out_html.write_text(html, encoding="utf-8")
            mb = out_html.stat().st_size / 1e6
            print(f"  [OK] {pdf.stem} — {n} 页, {mb:.1f} MB → {out_html}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [ERROR] {pdf.stem}: {e}")
    print(f"\n{'=' * 56}\n完成: {len(pdfs) - failed} OK / {failed} 失败")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
