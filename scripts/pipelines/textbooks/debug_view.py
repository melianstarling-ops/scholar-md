"""textbooks 调试可视化工具:左=页面图+block_bbox 叠框(按 label 分色),
右=逐页 reconstruct 的 md 经 markdown-it+KaTeX **渲染后**显示(复现"红色报错")。

  python -m scripts.pipelines.textbooks.debug_view --doc <stem-dir>            # 静态落 <stem>_debug.html
  python -m scripts.pipelines.textbooks.debug_view --doc <stem-dir> --serve    # 服务模式(改代码刷新即见)
  python -m scripts.pipelines.textbooks.debug_view --doc <stem-dir> --collect  # 归位浏览器导出的标注
    可选:--src <pdf 覆盖>  --dpi  --port(默认8078)  --no-images(不嵌页图,快而小)

数据源:_work/page_NNNN_res.json(引擎输出,无 GPU) + 源 PDF 现场栅格化页图。
静态 HTML 把页图 base64 内嵌,自包含离线可用(vendored katex/markdown-it,零外部请求)。
"""
from __future__ import annotations

import argparse
import base64
import datetime
import importlib
import json
import os
import threading

import fitz

from scripts.pipelines.textbooks import checkpoint as cp
from scripts.pipelines.textbooks import debug_payload as dp
from scripts.pipelines.textbooks import images
from scripts.pipelines.textbooks.convert import reassemble_md
from scripts.pipelines.textbooks.corrections import (
    load_corrections, apply_corrections, set_correction_status,
)

ASSETS = os.path.join(os.path.dirname(os.path.abspath(__file__)), "debug_assets")
VENDOR = os.path.join(ASSETS, "vendor")


def _read(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _now() -> str:
    return datetime.datetime.now().strftime("%Y-%m-%d %H:%M")


def _load_render_errors(doc_dir: str, stem: str) -> dict[int, list]:
    path = os.path.join(doc_dir, stem + "_render_errors.json")
    by_page: dict[int, list] = {}
    if os.path.exists(path):
        data = json.load(open(path, encoding="utf-8"))
        for e in data.get("errors", []):
            by_page.setdefault(e.get("page"), []).append(e)
    return by_page


def _page_image_b64(doc, page: int, dpi: int, cache: dict) -> str | None:
    """内存直出 JPEG(扫描件照片压缩率高,比 PNG 小一个量级)。叠框对齐靠 res 的
    width/height 坐标系换算,与本图片自身分辨率无关,故 DPI 可独立调低省体积。"""
    if page in cache:
        return cache[page]
    if doc is None:
        cache[page] = None
        return None
    try:
        pix = doc[page - 1].get_pixmap(dpi=dpi)
        try:
            jpg = pix.tobytes("jpeg", jpg_quality=78)
        except TypeError:                                      # 老版 PyMuPDF 无 jpg_quality
            jpg = pix.tobytes("jpeg")
        cache[page] = base64.b64encode(jpg).decode()
    except Exception:                                          # noqa: BLE001 页图缺失不掀翻
        cache[page] = None
    return cache[page]


def _attach_crop_images(corrections: list[dict], doc_dir: str, stem: str) -> list[dict]:
    """给每条修正挂上 debug_repair 裁出的原图裁切(base64),供审核卡片把"真实源图"跟
    AI 修正并排放在一起对照(不是重渲染引擎 LaTeX——那本身可能就是错的,起不到核对
    作用)。读不到裁图(未跑 debug_repair / 产物已清)就不挂,前端退回渲染引擎 LaTeX。
    返回新列表,不改传入的 dict。"""
    crops_dir = os.path.join(doc_dir, f"{stem}_repair", "crops")
    out = []
    for c in corrections:
        c = dict(c)
        page, block_id = c.get("page"), c.get("block_id")
        if page is not None and block_id is not None:
            crop_path = os.path.join(crops_dir, images.crop_filename(page, block_id))
            if os.path.exists(crop_path):
                with open(crop_path, "rb") as f:
                    c["crop_b64"] = base64.b64encode(f.read()).decode()
        out.append(c)
    return out


def build_payloads(doc_dir: str, pdf_path: str | None, dpi: int, img_dpi: int,
                   embed_images: bool, img_cache: dict) -> tuple[str, list[dict]]:
    """逐页 res.json → payload 列表。每次调用重新 reconstruct(serve 下反映代码改动)。"""
    importlib.reload(dp)                                        # 拾取 reconstruct/payload 代码改动
    work = os.path.join(doc_dir, "_work")
    stem = os.path.basename(os.path.normpath(doc_dir))
    manifest = cp.load_manifest(work)
    total = manifest["fingerprint"]["page_count"] if manifest else 0
    render_errors = _load_render_errors(doc_dir, stem)
    corrections = _attach_crop_images(load_corrections(doc_dir), doc_dir, stem)
    doc = None
    if embed_images and pdf_path and os.path.exists(pdf_path):
        try:
            doc = fitz.open(pdf_path)
        except Exception:                                      # noqa: BLE001
            doc = None
    try:
        pages: list[dict] = []
        for i in range(1, total + 1):
            res_path = os.path.join(work, f"page_{i:04d}_res.json")
            if os.path.exists(res_path):
                res = json.load(open(res_path, encoding="utf-8"))
            else:
                res = {"parsing_res_list": [], "width": None, "height": None}
            if corrections:
                res = {**res, "parsing_res_list":
                       apply_corrections(res.get("parsing_res_list", []), i, corrections)}
            img = _page_image_b64(doc, i, img_dpi, img_cache) if embed_images else None
            pages.append(dp.build_page_payload(res, page=i, stem=stem, image_b64=img,
                                               page_errors=render_errors.get(i, []),
                                               corrections=corrections))
        return stem, pages
    finally:
        if doc is not None:
            doc.close()


def render_html(stem: str, pages: list[dict], serve: bool, annotations=None) -> str:
    data = {"stem": stem, "pages": pages, "generated": _now(), "annotations": annotations or []}
    payload_json = json.dumps(data, ensure_ascii=False).replace("</", "<\\/")   # 防 </script> 逃逸
    tpl = _read(os.path.join(ASSETS, "template.html"))
    repl = {
        "{{TITLE}}": f"{stem} — debug view",
        "{{STEM}}": stem,
        "{{STAMP}}": _now(),
        "{{KATEX_CSS}}": _read(os.path.join(VENDOR, "katex.inline.css")),
        "{{APP_CSS}}": _read(os.path.join(ASSETS, "app.css")),
        "{{KATEX_JS}}": _read(os.path.join(VENDOR, "katex.min.js")),
        "{{MARKDOWNIT_JS}}": _read(os.path.join(VENDOR, "markdown-it.min.js")),
        "{{MDKATEX_JS}}": _read(os.path.join(VENDOR, "markdown-it-katex.js")),
        "{{APP_JS}}": _read(os.path.join(ASSETS, "app.js")),
        "{{PAYLOAD_JSON}}": payload_json,
        "{{SERVE_MODE}}": "true" if serve else "false",
    }
    for k, v in repl.items():
        tpl = tpl.replace(k, v)
    return tpl


def _resolve_pdf(doc_dir: str, src: str | None) -> tuple[str | None, int]:
    manifest = cp.load_manifest(os.path.join(doc_dir, "_work"))
    dpi = manifest.get("dpi", cp.DEFAULT_DPI) if manifest else cp.DEFAULT_DPI
    pdf = src or (manifest.get("pdf_path") if manifest else None)
    return pdf, dpi


def collect_annotations(doc_dir: str, stem: str) -> int:
    """把浏览器导出到默认下载/工作区的 <stem>_annotations.json 归位到 doc_dir(占位:已在则报数)。"""
    path = os.path.join(doc_dir, stem + "_annotations.json")
    if os.path.exists(path):
        data = json.load(open(path, encoding="utf-8"))
        return len(data.get("annotations", []))
    return 0


def handle_post(doc_dir: str, stem: str, path: str, body: str,
                state: dict | None = None, reassemble_fn=None) -> tuple[int, bytes]:
    """POST 路由(纯函数便于单测)。
    `/corrections`:采纳/驳回 → set_correction_status;成功则置 state["dirty"]。
    `/reassemble`:dirty 才调 reassemble_fn 落 md、清脏(否则秒回)——落盘幂等,门控只为省。
    其它路径:沿用标注流程落 <stem>_annotations.json。返回 (status_code, response_body)。"""
    if path == "/corrections":
        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            return 400, b"bad json"
        try:
            ok = set_correction_status(doc_dir, data["page"], data["block_id"], data["status"])
        except (KeyError, ValueError) as e:
            return 400, str(e).encode("utf-8")
        if ok and state is not None:
            state["dirty"] = True
        return (200, b"ok") if ok else (404, b"not found")
    if path == "/reassemble":
        if state is not None and state.get("dirty") and reassemble_fn is not None:
            reassemble_fn()
            state["dirty"] = False
        return 200, b"ok"
    with open(os.path.join(doc_dir, stem + "_annotations.json"), "w", encoding="utf-8") as f:
        f.write(body)
    return 200, b"ok"


def _safe_reassemble(doc_dir: str, pdf_path: str | None, dpi: int,
                     reassemble_fn=None) -> str | None:
    """调 reassemble 落 md,异常只告警不抛(启动对账/后台落盘不掀翻服务)。"""
    fn = reassemble_fn or reassemble_md
    try:
        return fn(doc_dir, pdf_path, dpi)
    except Exception as e:                                     # noqa: BLE001
        print(f"[debug_view] reassemble 失败(忽略,不影响审核):{e}", flush=True)
        return None


def serve(doc_dir: str, pdf_path: str | None, dpi: int, img_dpi: int, port: int) -> None:
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    stem = os.path.basename(os.path.normpath(doc_dir))
    img_cache: dict = {}
    state = {"dirty": False}
    lock = threading.Lock()

    _safe_reassemble(doc_dir, pdf_path, dpi)     # 启动即对账:打开审核界面就把 md 同步到 json

    def reassemble_fn():
        _safe_reassemble(doc_dir, pdf_path, dpi)

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):  # 静默
            pass

        def do_GET(self):
            s, pages = build_payloads(doc_dir, pdf_path, dpi, img_dpi, True, img_cache)
            html = render_html(s, pages, serve=True).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(html)))
            self.end_headers()
            self.wfile.write(html)

        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            body = self.rfile.read(n).decode("utf-8")
            with lock:                                # 串行化:杜绝并发写同一 stem.md 的竞态
                status, resp = handle_post(doc_dir, stem, self.path, body,
                                           state=state, reassemble_fn=reassemble_fn)
            self.send_response(status)
            self.end_headers()
            self.wfile.write(resp)

    print(f"[debug_view] serve http://127.0.0.1:{port}/  (Ctrl-C 停)")
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()


def main() -> None:
    ap = argparse.ArgumentParser(description="textbooks 调试可视化", formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--doc", required=True, help="转换产物目录(含 _work/ 的 <stem> 目录)")
    ap.add_argument("--src", default=None, help="源 PDF 覆盖(默认取 manifest.pdf_path)")
    ap.add_argument("--dpi", type=int, default=None, help="bbox 坐标系 DPI(默认取 manifest,一般不用改)")
    ap.add_argument("--img-dpi", type=int, default=110, help="左栏页图栅格化 DPI(默认110,越低 HTML 越小)")
    ap.add_argument("--serve", action="store_true", help="服务模式(改代码刷新即见)")
    ap.add_argument("--port", type=int, default=8078, help="服务端口(默认8078)")
    ap.add_argument("--collect", action="store_true", help="归位浏览器导出的标注")
    ap.add_argument("--no-images", action="store_true", help="不内嵌页图(快而小,只看右栏渲染)")
    ap.add_argument("--reassemble", action="store_true",
                    help="幂等重组:应用已采纳修正,覆盖写 <stem>.md 后退出(无 UI 收尾/回填)")
    args = ap.parse_args()

    doc_dir = os.path.abspath(args.doc)
    stem = os.path.basename(os.path.normpath(doc_dir))
    pdf_path, mdpi = _resolve_pdf(doc_dir, args.src)
    dpi = args.dpi or mdpi

    if args.reassemble:
        md_path = _safe_reassemble(doc_dir, pdf_path, dpi)
        print(f"[debug_view] reassemble → {md_path}")
        return

    if args.collect:
        print(f"[debug_view] 标注 {collect_annotations(doc_dir, stem)} 条 @ {stem}_annotations.json")
        return

    if args.serve:
        serve(doc_dir, pdf_path, dpi, args.img_dpi, args.port)
        return

    s, pages = build_payloads(doc_dir, pdf_path, dpi, args.img_dpi, not args.no_images, {})
    html = render_html(s, pages, serve=False)
    out = os.path.join(doc_dir, stem + "_debug.html")
    with open(out, "w", encoding="utf-8") as f:
        f.write(html)
    n_img = sum(1 for p in pages if p["image_b64"])
    n_err = sum(len(p["render_errors"]) for p in pages)
    print(f"[debug_view] {out}")
    print(f"  页 {len(pages)} | 内嵌页图 {n_img} | 标记 KaTeX 报错 {n_err} | 大小 {os.path.getsize(out)/1024/1024:.1f} MB")
    if not pdf_path or not os.path.exists(pdf_path or ""):
        print(f"  ⚠ 源 PDF 不可用({pdf_path});左栏页图缺省,右栏渲染不受影响")


if __name__ == "__main__":
    main()
