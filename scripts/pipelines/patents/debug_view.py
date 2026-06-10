#!/usr/bin/env python3
"""debug_view.py — 可视化调试工具（自包含单 HTML，浏览器打开即用）。

仿 MinerU 左右对照：
  左 = PDF 页渲染图 + 引擎判定叠加层（HTML 绝对定位，可逐层显隐、hover 看词），
  右 = 该页 reading_order 的中间产物（段落卡片、剔除词清单、页统计）。

叠加层颜色：
  橙 = 剔除的中央行号        紫 = 剔除的页眉/页脚/栏号
  蓝 = 保留词（默认隐藏）    绿 = 段落区域（右栏 hover 联动高亮）
  红 = crosscheck 未解释删除（若产物目录有 *_crosscheck.json 则自动叠加）
  竖虚线 = 实测 gutter

不进主管线、不改产物；所有判定数据与主转换同源（同一批函数现算），
所见即引擎所判。键盘 ←/→ 翻页。

用法（H.5 自适应 I/O）:
    python scripts/pipelines/patents/debug_view.py                 # 默认源目录全量
    python scripts/pipelines/patents/debug_view.py --src <pdf|dir> [--zoom 2.0]
输出: <md-root>/<stem>/<stem>_debug.html（目录不存在则建）
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

_KIND_NOTE = {
    "COVER": "封面页：由 bib_parse 解析 → YAML 元数据 + Abstract（不走几何重排）",
    "FIGURE": "附图页：整页渲染 PNG → ## Figures（文字层词不进正文）",
    "FRONT_MATTER": "前置页：线性重排（剔页眉页脚，不剔行号）→ References 附录",
    "SPEC_BODY": "",
}


def _box(w: Word, cls: str, wid: str = "") -> dict:
    d = {"c": cls, "b": [round(w.x0, 1), round(w.y0, 1), round(w.x1, 1), round(w.y1, 1)], "t": w.text}
    if wid:
        d["id"] = wid
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
        boxes += [_box(w, "header_footer") for w in rm_bands]
        payload["removed"]["header_footer"] = [w.text for w in rm_bands]

        if info.kind == PageKind.SPEC_BODY:
            body, rm_nums = strip_line_numbers(body, info.gutter_x, profile)
            boxes += [_box(w, "line_number") for w in rm_nums]
            payload["removed"]["line_number"] = [w.text for w in rm_nums]

        boxes += [_box(w, "kept") for w in body]
        payload["n_kept"] = len(body)

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
                        boxes.append({"c": "para", "b": _union(all_w), "id": pid})
                        payload["paras"].append(
                            {"id": pid, "text": p["text"], "new_by": p["new_by"], "n_lines": len(p["lines"])}
                        )
            else:  # FRONT_MATTER：线性行，整页一卡
                lines = group_lines(body, y_tol)
                txt = "\n".join(join_line(ln, punct_thr) for ln in lines)
                boxes.append({"c": "para", "b": _union(body), "id": "F1"})
                payload["paras"].append({"id": "F1", "text": txt, "new_by": "linear", "n_lines": len(lines)})

    for u in unexplained:
        boxes.append({"c": "unexplained", "b": u["bbox"], "t": u["text"]})
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
  :root{
    --ln:#f59e0b; --hf:#8b5cf6; --kept:#3b82f6; --para:#10b981; --bad:#ef4444;
    --ink:#1d1d1f; --sub:#86868b; --line:#e8e8ed; --bg:#f5f5f7;
  }
  *{box-sizing:border-box;margin:0}
  body{font-family:-apple-system,"SF Pro Text","Segoe UI","Microsoft YaHei",sans-serif;
       background:var(--bg);color:var(--ink);height:100vh;display:flex;flex-direction:column;overflow:hidden}
  header{display:flex;align-items:center;gap:14px;padding:10px 18px;background:#fff;
         border-bottom:1px solid var(--line);flex-wrap:wrap}
  header h1{font-size:14px;font-weight:600;letter-spacing:-.01em}
  .pgnav{display:flex;align-items:center;gap:8px;font-size:13px;color:var(--sub)}
  .pgnav button{border:1px solid var(--line);background:#fff;border-radius:8px;width:28px;height:28px;
                cursor:pointer;font-size:14px;color:var(--ink)}
  .pgnav button:hover{background:var(--bg)}
  #kind{font-size:11px;font-weight:600;padding:3px 10px;border-radius:999px;color:#fff}
  .k-SPEC_BODY{background:#0a84ff}.k-COVER{background:#5e5ce6}.k-FIGURE{background:#98989d}.k-FRONT_MATTER{background:#ac8e68}
  .toggles{display:flex;gap:6px;margin-left:auto;flex-wrap:wrap}
  .tg{display:flex;align-items:center;gap:6px;font-size:12px;color:var(--ink);border:1px solid var(--line);
      border-radius:999px;padding:4px 10px;cursor:pointer;background:#fff;user-select:none}
  .tg input{display:none}
  .tg .dot{width:9px;height:9px;border-radius:50%}
  .tg.off{color:var(--sub);background:var(--bg)}
  .tg.off .dot{opacity:.25}
  main{flex:1;display:flex;min-height:0}
  #leftpane{flex:11;overflow:auto;padding:16px;display:flex;justify-content:center;align-items:flex-start}
  #stage{position:relative;width:min(100%,860px);box-shadow:0 2px 14px rgba(0,0,0,.10);border-radius:6px;overflow:hidden;background:#fff}
  #stage img{display:block;width:100%}
  #overlay{position:absolute;inset:0}
  .box{position:absolute;border-radius:2px}
  .box.kept{border:1px solid color-mix(in srgb,var(--kept) 45%,transparent);background:color-mix(in srgb,var(--kept) 7%,transparent)}
  .box.line_number{border:1.5px solid var(--ln);background:color-mix(in srgb,var(--ln) 22%,transparent)}
  .box.header_footer{border:1.5px solid var(--hf);background:color-mix(in srgb,var(--hf) 18%,transparent)}
  .box.unexplained{border:2.5px solid var(--bad);background:color-mix(in srgb,var(--bad) 18%,transparent);z-index:5}
  .box.para{border:1px dashed color-mix(in srgb,var(--para) 55%,transparent);border-left:3px solid var(--para);
            background:transparent;pointer-events:none}
  .box.para.hot{background:color-mix(in srgb,var(--para) 13%,transparent);border-color:var(--para)}
  #gutter{position:absolute;top:0;bottom:0;width:0;border-left:2px dashed rgba(255,69,58,.55);z-index:4}
  .hide-kept .box.kept,.hide-line_number .box.line_number,.hide-header_footer .box.header_footer,
  .hide-para .box.para,.hide-unexplained .box.unexplained,.hide-gutter #gutter{display:none}
  #rightpane{flex:9;overflow:auto;padding:16px 18px;border-left:1px solid var(--line);background:#fff}
  .sec{margin-bottom:18px}
  .sec h2{font-size:11px;font-weight:600;color:var(--sub);text-transform:uppercase;letter-spacing:.06em;margin-bottom:8px}
  #stats{display:flex;gap:14px;flex-wrap:wrap;font-size:12px;color:var(--sub)}
  #stats b{color:var(--ink);font-weight:600}
  .pcard{border:1px solid var(--line);border-left:3px solid var(--para);border-radius:10px;
         padding:9px 12px;margin-bottom:8px;font-size:12.5px;line-height:1.55;cursor:default;background:#fff}
  .pcard:hover{background:color-mix(in srgb,var(--para) 6%,#fff);border-color:var(--para)}
  .pcard .meta{font-size:10.5px;color:var(--sub);margin-bottom:3px;display:flex;gap:8px}
  .pcard .meta .pid{font-weight:700;color:var(--para)}
  .pcard.linear{white-space:pre-wrap}
  .chips{display:flex;flex-wrap:wrap;gap:5px}
  .chip{font-size:11px;padding:2px 8px;border-radius:6px;font-variant-numeric:tabular-nums}
  .chip.ln{background:color-mix(in srgb,var(--ln) 16%,#fff);color:#92600a;border:1px solid color-mix(in srgb,var(--ln) 40%,#fff)}
  .chip.hf{background:color-mix(in srgb,var(--hf) 12%,#fff);color:#5b3fb8;border:1px solid color-mix(in srgb,var(--hf) 35%,#fff)}
  .chip.bad{background:color-mix(in srgb,var(--bad) 12%,#fff);color:#b32018;border:1px solid color-mix(in srgb,var(--bad) 40%,#fff);font-weight:600}
  .note{font-size:12.5px;color:var(--sub);background:var(--bg);border-radius:10px;padding:10px 12px;line-height:1.6}
  .empty{font-size:12px;color:var(--sub)}
  footer{padding:6px 18px;font-size:11px;color:var(--sub);background:#fff;border-top:1px solid var(--line)}
  kbd{border:1px solid var(--line);border-radius:4px;padding:0 4px;background:var(--bg);font-family:inherit}
</style>
</head>
<body>
<header>
  <h1>__TITLE__</h1>
  <span id="kind"></span>
  <div class="pgnav">
    <button id="prev" title="上一页 (←)">‹</button>
    <span id="pg"></span>
    <button id="next" title="下一页 (→)">›</button>
  </div>
  <div class="toggles" id="toggles"></div>
</header>
<main>
  <section id="leftpane"><div id="stage"><img id="img" alt=""><div id="overlay"></div></div></section>
  <section id="rightpane">
    <div class="sec"><h2>页统计</h2><div id="stats"></div></div>
    <div class="sec" id="noteSec" hidden><h2>说明</h2><div class="note" id="note"></div></div>
    <div class="sec" id="badSec" hidden><h2 style="color:var(--bad)">crosscheck 未解释删除</h2><div class="chips" id="bad"></div></div>
    <div class="sec"><h2>重排段落（中间产物）</h2><div id="paras"></div></div>
    <div class="sec"><h2>剔除词</h2><div id="removed"></div></div>
  </section>
</main>
<footer><kbd>←</kbd> <kbd>→</kbd> 翻页 · 顶部圆点开关叠加层 · hover 词框/段落卡可联动 · 生成于 __STAMP__</footer>
<script>
const DATA = __DATA__;
const LAYERS = [
  ["line_number","行号(剔)","var(--ln)",true],
  ["header_footer","页眉/页脚(剔)","var(--hf)",true],
  ["kept","保留词","var(--kept)",false],
  ["para","段落","var(--para)",true],
  ["unexplained","未解释","var(--bad)",true],
  ["gutter","gutter","rgba(255,69,58,.8)",true],
];
const $=id=>document.getElementById(id);
const ov=$("overlay"), st=$("stage");
let cur=0;

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

const pct=(v,t)=>(v/t*100).toFixed(3)+"%";
function render(i){
  cur=i; const d=DATA[i];
  $("img").src="data:image/png;base64,"+d.img;
  $("pg").textContent=(i+1)+" / "+DATA.length;
  const k=$("kind"); k.textContent=d.kind; k.className="k-"+d.kind;
  $("prev").disabled=i===0; $("next").disabled=i===DATA.length-1;

  ov.innerHTML="";
  if(d.kind==="SPEC_BODY"&&d.gutter>0){
    const g=document.createElement("div"); g.id="gutter"; g.style.left=pct(d.gutter,d.w); ov.appendChild(g);
  }
  for(const b of d.boxes){
    const el=document.createElement("div");
    el.className="box "+b.c;
    if(b.id) el.dataset.id=b.id;
    el.style.left=pct(b.b[0],d.w); el.style.top=pct(b.b[1],d.h);
    el.style.width=pct(b.b[2]-b.b[0],d.w); el.style.height=pct(b.b[3]-b.b[1],d.h);
    if(b.t) el.title=b.t+"  ["+b.b.map(Math.round)+"]";
    ov.appendChild(el);
  }

  const kept=("n_kept" in d)?` · 保留 <b>${d.n_kept}</b>`:"";
  const rm=d.removed, nrm=(rm.line_number.length+rm.header_footer.length);
  $("stats").innerHTML=
    `<span>词 <b>${d.n_words}</b>${kept} · 剔除 <b>${nrm}</b></span>`+
    (d.kind==="SPEC_BODY"?`<span>gutter <b>${d.gutter}</b> · 行号阶梯 <b>${d.ladder_n}</b></span>`:"")+
    `<span>段落 <b>${d.paras.length}</b></span>`;

  $("noteSec").hidden=!d.note; $("note").textContent=d.note;

  $("badSec").hidden=!d.unexplained.length;
  $("bad").innerHTML=d.unexplained.map(u=>`<span class="chip bad" title="[${u.bbox}]">${esc(u.text)}</span>`).join("");

  const NEWBY={indent:"缩进起段",gap:"行距起段",first:"栏首",linear:"线性"};
  $("paras").innerHTML=d.paras.length?d.paras.map(p=>
    `<div class="pcard${p.new_by==="linear"?" linear":""}" data-for="${p.id}">
       <div class="meta"><span class="pid">${p.id}</span><span>${NEWBY[p.new_by]} · ${p.n_lines} 行</span></div>${esc(p.text)}</div>`
  ).join(""):`<div class="empty">本页无重排段落</div>`;
  for(const card of $("paras").querySelectorAll(".pcard")){
    card.addEventListener("mouseenter",()=>hot(card.dataset.for,true));
    card.addEventListener("mouseleave",()=>hot(card.dataset.for,false));
  }

  const sec=(cls,name,arr)=>arr.length?`<h2 style="margin:8px 0 6px;font-size:10.5px;color:var(--sub)">${name} × ${arr.length}</h2>
     <div class="chips">${arr.map(t=>`<span class="chip ${cls}">${esc(t)}</span>`).join("")}</div>`:"";
  const html=sec("ln","中央行号",rm.line_number)+sec("hf","页眉/页脚/栏号",rm.header_footer);
  $("removed").innerHTML=html||`<div class="empty">本页无剔除</div>`;
}
function hot(id,on){const el=ov.querySelector(`[data-id="${id}"]`); if(el) el.classList.toggle("hot",on);}
function esc(s){return s.replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));}
$("prev").onclick=()=>cur>0&&render(cur-1);
$("next").onclick=()=>cur<DATA.length-1&&render(cur+1);
document.addEventListener("keydown",e=>{
  if(e.key==="ArrowRight"&&cur<DATA.length-1)render(cur+1);
  if(e.key==="ArrowLeft"&&cur>0)render(cur-1);
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
                    help="产物根目录（找 crosscheck 报告 + 默认输出位置，默认 03_Output/patents）")
    ap.add_argument("--out", default=None, help="HTML 输出目录（默认 <md-root>/<stem>/）")
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
            out_dir = Path(args.out) if args.out else md_root / pdf.stem
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
