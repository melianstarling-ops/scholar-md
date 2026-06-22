#!/usr/bin/env python3
"""ocr_layer.py — 无文本层/坏字形页的 OCR 夹层 PDF 产线（前置于 convert_patent）。

对源 PDF 产出派生的**夹层 PDF**：原页面图像 + 不可见文本层(词级坐标)，
喂既有确定性管线（page_classify.page_words 经 get_text("words") 直读，零改下游）。
源 PDF 永不修改（AGENTS C.1）。

引擎：PyMuPDF **内置** Tesseract（C++ 编译进 MuPDF，非外部程序；版本随 PyMuPDF
锁定，PyMuPDF 1.27.2 → MuPDF 1.27.2 → Tesseract 5.5.0），零新增依赖。
语言数据 tessdata 为纯数据文件，路径取 `SCHOLARMD_TESSDATA` 环境变量，
默认仓内 `.tessdata/`（gitignore；放官方 tessdata_best 的 eng.traineddata）。

模式（2026-06-12 所有者拍板：混合策略）：
  gapfill(默认)  只 OCR 无文本层页，有层页原样保留。无层页 OCR 后若按管线页型
                 判定非 SPEC_BODY（图纸页等）则**弃层保原页**——"识别到的是图片
                 就不写入正文"（所有者政策）；图纸照旧走 FIGURE 渲染。
  force          全部页栅格化重 OCR（弃源文本层）。
  --extra-pages  gapfill 基础上对指定页(0 起)定点重 OCR——给 garblecheck 标过
                 正文坏字形的页用（如 US9216286B2 的 idx89 CaSOS / idx99 ringS）；
                 文献页(FRONT_MATTER)按决策不重 OCR，坏字形维持标记交付。

重影质量门（确定性，只判 OCR 产层、不改字符）：
  Tesseract 对"稀疏、左右栏长短悬殊"页的版面行切分会失败——同一物理行被切出
  双行高的重叠"行"，识别成乱码重影（实证：US9216286B2 idx121，claim 7 头部
  `hy mplanta…`；DPI 300/400/600、灰度、裁空白均无效，单栏喂图则全对）。
  检测信号（二信号正交，呼应 lessons 总则 2，阈值由 7 候选页实测分布定标）：
  ① 页级双峰 p50/p10 词高比 ≥ DIPLOPIA_BIMODAL（整页行切分崩坏指纹，灾难页
  1.50 vs 正常密排页 1.00–1.12）；② 词框高 ≥ DIPLOPIA_H_RATIO×p10 且字母核
  非词典词的数量 ≥ DIPLOPIA_MIN。同时满足才触发（密排页的粘词假阳性只满足②）。
  触发后**分栏二次 OCR**（等价于 Tesseract PSM 单栏，内置接口不暴露 PSM，
  以裁剪实现）：按首轮词框定中央行号带，左栏/右栏分别裁剪重 OCR，
  中央带沿用首轮结果（行号阶梯首轮本就读对，且 page_classify 靠它判 SPEC_BODY），
  三条竖带拼回原尺寸页。

用法（H.5 自适应 I/O）：
    python ocr_layer.py --src <pdf|目录> [...] [--mode gapfill|force]
                        [--extra-pages auto|89,99] [--dpi 300] [--jobs N] [--out 目录]
  --extra-pages auto = 按混合策略自动选定点页（SPEC_BODY 且 garblecheck 有坏字形）。
  --out 省略时落 `03_Output/patents/<stem>/_ocr/<stem>.pdf`（不写源目录，保 02_Source
  纯净；派生件与源同 stem → crosscheck/debug_view 的 stem 配对零改），同目录写
  `<stem>.provenance.json` 留痕。

一键集成：`batch_patents.py --ocr` 经 `prepare_sandwich()` 调本模块——需要才产
夹层（有无层页或自动定点页），否则直接喂原件。

doctest:  python -m doctest ocr_layer.py
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import fitz

from wordfix import is_word

PROJECT_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_TESSDATA = PROJECT_ROOT / ".tessdata"
OUTPUT_ROOT = PROJECT_ROOT / "03_Output" / "patents"
# 并行默认上限 8:实测 30 进程比 8 慢 52%(争用),且留核防前台卡顿(L12 教训 6)
DEFAULT_JOBS = max(1, min(8, (os.cpu_count() or 2) - 2))

LANG = "eng"
DIPLOPIA_H_RATIO = 1.6   # 词框高/基线 ≥ 此值 → 候选重影(正常词框约 1 行高)
DIPLOPIA_MIN = 2         # 触发下限:非词典高框词数
DIPLOPIA_BIMODAL = 1.3   # 触发下限:页级 p50/p10 词高比(整页行切分崩坏的指纹)
BAND_MARGIN = 3.0        # 中央行号带左右各放余量(pt)


# ---------------------------------------------------------------- 纯函数(可测)

def _alpha_core(text: str) -> str:
    """剥首尾非字母后的核。'mplanta,'→'mplanta'；'©'→''。

    >>> _alpha_core('mplanta,'), _alpha_core('©'), _alpha_core('(hy)')
    ('mplanta', '', 'hy')
    """
    s = text.strip()
    while s and not s[0].isalpha():
        s = s[1:]
    while s and not s[-1].isalpha():
        s = s[:-1]
    return s


def diplopia_suspects(words: list[tuple]) -> list[str]:
    """重影候选：词框高 ≥ DIPLOPIA_H_RATIO×基线 且 字母核非词典词。

    words: (x0,y0,x1,y1,text,...) 序列(get_text("words") 原样)。
    返回候选词文本列表（len ≥ DIPLOPIA_MIN 时调用方触发分栏二次 OCR）。

    基线取**第 10 分位**而非中位：US9216286B2 idx121 实测整页约半数词框被
    重影抬到 13–17pt(p50=13.0 被污染)，但干净单行词仍在 p10=8.6——
    中位基线会被污染拉高而漏检(实跑踩过)，低分位对污染免疫(lessons 总则 1)。

    实测 idx121 重影词高 16–18.5pt、单行词 ≈8.2–8.6pt：
    >>> normal = [(0, i*10, 30, i*10+8, t) for i, t in enumerate(
    ...     ['The', 'implantable', 'medical', 'lead', 'of', 'claim', 'wherein'])]
    >>> bad = [(340, 215, 378, 231.6, 'mplanta'), (404, 203, 421, 221.5, 'dical')]
    >>> diplopia_suspects(normal + bad)
    ['mplanta', 'dical']

    污染中位数反例(半页词框被抬高,p50≈16 但 p10=8)——中位基线漏检,p10 不漏：
    >>> half_tall = normal + [(0, 200+i*20, 40, 200+i*20+16, t) for i, t in
    ...     enumerate(['portion', 'shield', 'lead', 'claim', 'dical', 'mplanta', 'hy'])]
    >>> diplopia_suspects(half_tall)
    ['dical', 'mplanta', 'hy']

    合法词即便框高(行合并但读对)也不算；全页正常时返回空：
    >>> diplopia_suspects(normal + [(0, 80, 30, 96, 'shield')])
    []
    >>> diplopia_suspects(normal)
    []

    词数过少(无统计意义)不判：
    >>> diplopia_suspects([(0, 0, 30, 16, 'xq')])
    []
    """
    if len(words) < 5:
        return []
    heights = sorted(w[3] - w[1] for w in words)
    h_base = heights[len(heights) // 10]      # p10:干净单行词高,对重影污染免疫
    if h_base <= 0:
        return []
    out = []
    for w in words:
        if (w[3] - w[1]) >= DIPLOPIA_H_RATIO * h_base:
            core = _alpha_core(w[4])
            if core and not is_word(core.lower()):
                out.append(w[4])
    return out


def needs_column_retry(words: list[tuple]) -> bool:
    """重影质量门：页级双峰(p50/p10 ≥ DIPLOPIA_BIMODAL) **且** 非词高框 ≥ MIN。

    两信号正交(lessons 总则 2)，2026-06-12 用 7 个候选页实测分布定标(总则 1)：
      - 整页行切分崩坏(US9216286B2 idx121):约半数词框被抬到双行高,
        p50/p10 = 13.0/8.6 = **1.50**,非词高框 13 个 → 触发。
      - 密排正常页(idx89/99/100/101/102/115):p50/p10 = **1.00–1.12**,
        其"可疑词"实为粘词(ipsojacket/thatmay)与连字符尾,非重影 → 不触发。
        对这些页整页重来会把"已良好"的首轮文本换成条带重识别版,
        还引入条带边缘伪迹(实跑:crosscheck 未解释 +23) → 维持首轮+标记,
        宁缺勿错(总则 4)。

    >>> collapse = ([(0, i*10, 30, i*10+8.6, 'claim') for i in range(150)]
    ...     + [(0, i*10, 40, i*10+14, t) for i, t in enumerate(
    ...         ['portion', 'shield']*73 + ['dical', 'mplanta', 'hy', 'th'])])
    >>> needs_column_retry(collapse)          # p50/p10≈1.6, 非词高框 4
    True
    >>> dense = ([(0, i, 30, i+8.2, 'claim') for i in range(1300)]
    ...          + [(0, 0, 30, 16, w) for w in ('ipsojacket', 'thatmay', 'ofan')])
    >>> needs_column_retry(dense)             # 粘词高框 3 个,但 p50/p10=1.0
    False
    >>> needs_column_retry([(0, 0, 30, 8, 'a')])
    False
    """
    if len(words) < 5:
        return False
    heights = sorted(w[3] - w[1] for w in words)
    n = len(heights)
    p10, p50 = heights[n // 10], heights[n // 2]
    if p10 <= 0 or p50 / p10 < DIPLOPIA_BIMODAL:
        return False
    return len(diplopia_suspects(words)) >= DIPLOPIA_MIN


def ladder_band(words: list[tuple], page_w: float) -> tuple[float, float]:
    """由首轮词框定中央行号带 (bandL, bandR)。

    取页面中部 1/3 内的"5 的倍数"纯数字词(中央行号阶梯，与 page_classify
    同一特征)，带 = 其 x 范围 ± BAND_MARGIN。找不到阶梯时退化为页中线两侧
    BAND_MARGIN(零宽带，纯对半分栏)。

    >>> ws = [(300, 100, 312, 108, '5'), (299, 300, 313, 308, '10'),
    ...       (50, 50, 90, 58, 'left'), (500, 50, 540, 58, 'right')]
    >>> ladder_band(ws, 614.4)
    (296.0, 316.0)
    >>> ladder_band([(50, 50, 90, 58, 'left')], 614.4)
    (304.2, 310.2)
    """
    lo, hi = page_w / 3, page_w * 2 / 3
    xs = [
        (w[0], w[2])
        for w in words
        if w[4].isdecimal() and int(w[4]) % 5 == 0 and 0 < int(w[4]) <= 80  # isdecimal:挡上标 ²³(isdigit()=True 但 int() 报错)
        and lo <= (w[0] + w[2]) / 2 <= hi
    ]
    if not xs:
        c = page_w / 2
        return (round(c - BAND_MARGIN, 1), round(c + BAND_MARGIN, 1))
    return (round(min(x0 for x0, _ in xs) - BAND_MARGIN, 1),
            round(max(x1 for _, x1 in xs) + BAND_MARGIN, 1))


# ------------------------------------------------------------------ OCR 产层

def _ocr_clip(page: "fitz.Page", clip, dpi: int, tessdata: str) -> "fitz.Document":
    """页面区域 → 栅格化 → 内置 Tesseract → 1 页夹层 PDF(尺寸=clip,pt)。"""
    pix = page.get_pixmap(dpi=dpi, clip=clip)
    return fitz.open("pdf", pix.pdfocr_tobytes(language=LANG, tessdata=tessdata))


def ocr_page_sandwich(page: "fitz.Page", dpi: int, tessdata: str) -> tuple[bytes, bool]:
    """单页 → 夹层 PDF 字节。返回 (pdf_bytes, retried)。

    首轮整页 OCR；命中重影质量门则分栏二次 OCR：
    左栏/右栏裁剪各自重 OCR(等价 PSM 单栏)，中央行号带沿用首轮，拼回原尺寸。
    """
    w, h = page.rect.width, page.rect.height
    first = _ocr_clip(page, None, dpi, tessdata)
    words = first[0].get_text("words")
    if not needs_column_retry(words):
        data = first.tobytes()
        first.close()
        return data, False

    band_l, band_r = ladder_band(words, w)
    left = _ocr_clip(page, fitz.Rect(0, 0, band_l, h), dpi, tessdata)
    right = _ocr_clip(page, fitz.Rect(band_r, 0, w, h), dpi, tessdata)
    # 中央带(行号阶梯)沿用首轮:阶梯首轮读对,page_classify 靠它判页型。
    # ⚠ 必须 redaction 真删带外文本——show_pdf_page 的 clip 只裁视觉显示,
    #   整页文本层会原样进 XObject,与条带文本叠成双份(实跑踩过:crosscheck
    #   未解释 16→68,碎片漏进 md)。
    fp = first[0]
    fp.add_redact_annot(fitz.Rect(0, 0, band_l, h))
    fp.add_redact_annot(fitz.Rect(band_r, 0, w, h))
    fp.apply_redactions()
    out = fitz.open()
    np = out.new_page(width=w, height=h)
    np.show_pdf_page(fitz.Rect(0, 0, w, h), first, 0)   # 带内文本+图(带外已白)
    np.show_pdf_page(fitz.Rect(0, 0, band_l, h), left, 0)    # 左栏图+文盖白区
    np.show_pdf_page(fitz.Rect(band_r, 0, w, h), right, 0)   # 右栏图+文盖白区
    data = out.tobytes(garbage=3, deflate=True)
    for d in (first, left, right, out):
        d.close()
    return data, True


def _worker_init() -> None:
    """工人进程降为 BELOW_NORMAL 优先级(Windows)：即使占满核,前台应用优先拿
    CPU,不卡所有者操作(2026-06-12 所有者要求的卡顿保险)。"""
    if sys.platform == "win32":
        import ctypes
        kern = ctypes.windll.kernel32
        kern.SetPriorityClass(kern.GetCurrentProcess(), 0x00004000)  # BELOW_NORMAL


def _worker(args: tuple) -> tuple[int, bytes, bool]:
    """进程池工人：每进程自行开源件(跨进程不传 Document)。"""
    src_path, index, dpi, tessdata = args
    doc = fitz.open(src_path)
    data, retried = ocr_page_sandwich(doc[index], dpi, tessdata)
    doc.close()
    return index, data, retried


def build_sandwich(src: Path, out_pdf: Path, mode: str, extra_pages: set[int],
                   dpi: int, tessdata: str, jobs: int) -> dict:
    """产夹层 PDF + provenance。返回 provenance dict。"""
    from page_classify import PageKind, classify_page
    from profiles import get_profile

    t0 = time.time()
    doc = fitz.open(src)
    textless = {i for i in range(doc.page_count) if not doc[i].get_text("words")}
    targets = [
        i for i in range(doc.page_count)
        if mode == "force" or i in extra_pages or i in textless
    ]
    results: dict[int, tuple[bytes, bool]] = {}
    if targets:
        if jobs > 1 and len(targets) > 1:
            with ProcessPoolExecutor(max_workers=jobs, initializer=_worker_init) as ex:
                for i, data, retried in ex.map(
                        _worker, [(str(src), i, dpi, tessdata) for i in targets]):
                    results[i] = (data, retried)
        else:
            for i in targets:
                results[i] = ocr_page_sandwich(doc[i], dpi, tessdata)

    # 无层页弃层规则(2026-06-12 所有者政策:识别到的是图片就不写入正文)：
    # 无层页 OCR 后按管线同一套页型判定——非 SPEC_BODY(图纸/无法判为正文)则
    # **不写入文本层**,保留原无层页(下游照旧按 FIGURE 渲染图,图签不进 md)。
    # 正文无层页(US9216286 的 22 页,有行号阶梯)判 SPEC_BODY → 保留 OCR 层。
    # 例外:COVER(封面,恒第一页)同样保留 OCR 层——封面是书目文字而非图纸,
    # 须喂 bib_parse 出 YAML 元数据(2026-06-15 所有者:扫描件封面要喂进去)。
    # 封面文字只被 convert 的 parse_cover 读取,不进 bodies/fronts/figs → 不泄漏正文。
    profile = get_profile()
    discarded: list[int] = []
    for i in sorted(textless & set(results)):
        one = fitz.open("pdf", results[i][0])
        kind = classify_page(i, one[0], profile).kind
        one.close()
        if kind not in (PageKind.SPEC_BODY, PageKind.COVER):
            del results[i]
            discarded.append(i)

    out = fitz.open()
    for i in range(doc.page_count):
        if i in results:
            one = fitz.open("pdf", results[i][0])
            out.insert_pdf(one)
            one.close()
        else:
            out.insert_pdf(doc, from_page=i, to_page=i)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    out.save(out_pdf, garbage=3, deflate=True)
    out.close()

    eng_data = Path(tessdata) / f"{LANG}.traineddata"
    prov = {
        "source": str(src),
        "source_sha256": hashlib.sha256(src.read_bytes()).hexdigest(),
        "output": str(out_pdf),
        "engine": f"Tesseract (built into MuPDF {fitz.VersionFitz} via PyMuPDF {fitz.VersionBind})",
        "tessdata": f"{LANG}.traineddata sha256:"
                    + (hashlib.sha256(eng_data.read_bytes()).hexdigest()[:16]
                       if eng_data.exists() else "?"),
        "mode": mode, "dpi": dpi,
        "ocr_pages": sorted(results),
        "extra_pages": sorted(extra_pages),
        "ocr_discarded_nonbody_pages": discarded,
        "column_retry_pages": sorted(i for i, (_, r) in results.items() if r),
        "elapsed_sec": round(time.time() - t0, 1),
        "generated": time.strftime("%Y-%m-%d %H:%M:%S"),
    }
    doc.close()
    prov_path = out_pdf.with_name(out_pdf.stem + ".provenance.json")
    prov_path.write_text(json.dumps(prov, ensure_ascii=False, indent=2), encoding="utf-8")
    return prov


# ------------------------------------------------------------------ 一键集成

def auto_extra_pages(doc: "fitz.Document", profile) -> set[int]:
    """混合策略(2026-06-12 所有者拍板)的自动定点页：
    SPEC_BODY 且 garblecheck 有坏字形 → 重 OCR 根治；
    文献页(FRONT_MATTER)等不入选 → 坏字形维持标记交付。"""
    from garblecheck import classify_garble
    from page_classify import PageKind, classify_page
    extra = set()
    for i in range(doc.page_count):
        words = doc[i].get_text("words")
        if not words:
            continue                      # 无层页归 gapfill,不算定点
        if (any(classify_garble(w[4]) for w in words)
                and classify_page(i, doc[i], profile).kind == PageKind.SPEC_BODY):
            extra.add(i)
    return extra


def prepare_sandwich(src: Path, out_dir: Path | None = None, dpi: int = 300,
                     tessdata: str | None = None, jobs: int | None = None) -> Path | None:
    """一键入口(batch_patents --ocr 调用)：需要才产夹层。

    返回夹层 PDF 路径；无无层页且无自动定点页时返回 None(直接喂原件即可)。
    """
    from profiles import get_profile
    tessdata = tessdata or os.environ.get("SCHOLARMD_TESSDATA", str(DEFAULT_TESSDATA))
    doc = fitz.open(src)
    textless = any(not doc[i].get_text("words") for i in range(doc.page_count))
    extra = auto_extra_pages(doc, get_profile())
    doc.close()
    if not textless and not extra:
        return None
    out_dir = out_dir or OUTPUT_ROOT / src.stem / "_ocr"
    prov = build_sandwich(src, out_dir / f"{src.stem}.pdf", "gapfill", extra,
                          dpi, tessdata, jobs or DEFAULT_JOBS)
    return Path(prov["output"])


# ----------------------------------------------------------------------- CLI

def collect_pdfs(srcs: list[str]) -> list[Path]:
    """H.5:--src 吃 文件/目录/多个,目录扫 *.pdf,去重保序。"""
    seen, out = set(), []
    for s in srcs:
        p = Path(s)
        for f in (sorted(p.glob("*.pdf")) if p.is_dir() else [p]):
            r = f.resolve()
            if r not in seen:
                seen.add(r)
                out.append(f)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--src", nargs="+", required=True, help="PDF 文件/目录,可多个")
    ap.add_argument("--out", default=None,
                    help="输出目录(默认 03_Output/patents/<stem>/,不写源目录)")
    ap.add_argument("--mode", choices=("gapfill", "force"), default="gapfill")
    ap.add_argument("--extra-pages", default="",
                    help="gapfill 基础上定点重 OCR 的页号(0 起,逗号分隔,如 89,99);"
                         "或 'auto'=自动选(SPEC_BODY 且有坏字形)")
    ap.add_argument("--dpi", type=int, default=300)
    ap.add_argument("--jobs", type=int, default=DEFAULT_JOBS,
                    help="并行进程数(默认 min(8, CPU-2),工人低优先级;1=串行)")
    ap.add_argument("--tessdata", default=os.environ.get("SCHOLARMD_TESSDATA",
                                                         str(DEFAULT_TESSDATA)))
    args = ap.parse_args()

    if not (Path(args.tessdata) / f"{LANG}.traineddata").exists():
        print(f"[ERROR] 缺语言数据 {args.tessdata}\\{LANG}.traineddata —— 从 "
              "github.com/tesseract-ocr/tessdata_best 下载后放入该目录,"
              "或设 SCHOLARMD_TESSDATA。")
        return 1
    auto = args.extra_pages.strip().lower() == "auto"
    extra = (set() if auto
             else {int(x) for x in args.extra_pages.split(",") if x.strip()})

    pdfs = collect_pdfs(args.src)
    print(f"OCR 夹层产线: {len(pdfs)} 份 · mode={args.mode}"
          + (" extra=auto" if auto else (f" extra={sorted(extra)}" if extra else ""))
          + f" dpi={args.dpi} jobs={args.jobs}")
    failed = 0
    for p in pdfs:
        out_dir = Path(args.out) if args.out else OUTPUT_ROOT / p.stem / "_ocr"
        if auto:
            from profiles import get_profile
            d = fitz.open(p)
            extra = auto_extra_pages(d, get_profile())
            d.close()
        try:
            prov = build_sandwich(p, out_dir / f"{p.stem}.pdf", args.mode,
                                  extra, args.dpi, args.tessdata, args.jobs)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"  [ERROR] {p.stem}: {e}")
            continue
        retry = prov["column_retry_pages"]
        print(f"  [OK] {p.stem} — OCR {len(prov['ocr_pages'])} 页"
              + (f" (分栏二次: {retry})" if retry else "")
              + f" {prov['elapsed_sec']}s → {prov['output']}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
