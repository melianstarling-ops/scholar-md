/* textbooks debug view — 前端交互。数据来自 window.DEBUG_DATA。 */
(function () {
  const DATA = window.DEBUG_DATA;
  const SERVE = !!window.SERVE_MODE;
  const pages = DATA.pages;
  const stem = DATA.stem;
  const $ = (id) => document.getElementById(id);

  let idx = 0;
  let scale = 1;
  let annotMode = false;
  const annotations = (DATA.annotations || []).slice();   // {page,bbox,category,note}
  const hiddenLabels = new Set();

  const CAT = { 1: "渲染报错(红)", 2: "公式排版错", 3: "漏内容/漏识别", 4: "错误归类", 5: "图片位置/裁切错" };

  // 数学交给 markdown-it-katex 插件:$…$/$$…$$ 在 inline 解析阶段被 tokenize,
  // 先于 markdown 强调规则,故 LaTeX 里的 _ / * 不会被误当强调污染(与 VS Code 一致)。
  // throwOnError:false → 硬报错渲染成红色 .katex-error 节点,复现预览的红。
  const mdit = window.markdownit({ html: false, linkify: true, breaks: false })
    .use(window.mdKatexPlugin, { throwOnError: false, errorColor: "#ef4444" });

  // ---------- 报错索引:离屏渲染每页,数 .katex-error ----------
  function computeErrorIndex() {
    const scratch = document.createElement("div");
    scratch.style.cssText = "position:absolute;left:-9999px;top:0;visibility:hidden;width:800px";
    document.body.appendChild(scratch);
    const rows = [];
    pages.forEach((p, i) => {
      scratch.innerHTML = mdit.render(p.md || "");
      const cnt = scratch.querySelectorAll(".katex-error").length;
      if (cnt > 0) rows.push({ i, page: p.page, cnt });
    });
    document.body.removeChild(scratch);
    return rows;
  }

  function fillErrorIndex() {
    const sel = $("errorIndex");
    const rows = computeErrorIndex();
    sel.innerHTML =
      `<option value="">报错索引 (${rows.length} 页有红)…</option>` +
      rows.map((r) => `<option value="${r.i}">p${r.page} — ${r.cnt} 处红</option>`).join("");
    sel.onchange = () => { if (sel.value !== "") go(parseInt(sel.value, 10)); };
    // 页码集合供徽章判定
    window.__errPages = new Set(rows.map((r) => r.page));
  }

  // ---------- 图层开关 ----------
  function buildLayerToggles() {
    const labels = [...new Set(pages.flatMap((p) => p.blocks.map((b) => b.label)))].sort();
    const wrap = $("layerToggles");
    wrap.innerHTML = "";
    labels.forEach((lab) => {
      const color = (pages.flatMap((p) => p.blocks).find((b) => b.label === lab) || {}).color || "#999";
      const el = document.createElement("label");
      el.innerHTML = `<input type="checkbox" checked data-lab="${lab}"><span class="swatch" style="background:${color}"></span>${lab}`;
      el.querySelector("input").onchange = (e) => {
        if (e.target.checked) hiddenLabels.delete(lab); else hiddenLabels.add(lab);
        drawOverlays();
      };
      wrap.appendChild(el);
    });
  }

  // ---------- 叠框 ----------
  function recomputeScale() {
    const img = $("pageImg");
    const p = pages[idx];
    scale = p.width ? img.clientWidth / p.width : 1;
  }

  function drawOverlays() {
    const layer = $("overlayLayer");
    layer.innerHTML = "";
    const p = pages[idx];
    p.blocks.forEach((b) => {
      if (hiddenLabels.has(b.label)) return;
      const [x0, y0, x1, y1] = b.bbox;
      const d = document.createElement("div");
      d.className = "ov" + (b.is_noise ? " noise" : "");
      d.style.borderColor = b.color;
      d.style.left = x0 * scale + "px";
      d.style.top = y0 * scale + "px";
      d.style.width = (x1 - x0) * scale + "px";
      d.style.height = (y1 - y0) * scale + "px";
      d.onmouseenter = (e) => showTip(e, b);
      d.onmousemove = moveTip;
      d.onmouseleave = hideTip;
      layer.appendChild(d);
    });
  }

  let tipEl = null;
  function showTip(e, b) {
    hideTip();
    tipEl = document.createElement("div");
    tipEl.className = "ov-tip";
    tipEl.textContent = `#${b.block_id} ${b.label}` +
      (b.order === null ? " (order=None)" : ` (order=${b.order})`) +
      (b.content_head ? "\n" + b.content_head : "");
    document.body.appendChild(tipEl);
    moveTip(e);
  }
  function moveTip(e) { if (tipEl) { tipEl.style.left = e.pageX + 12 + "px"; tipEl.style.top = e.pageY + 12 + "px"; } }
  function hideTip() { if (tipEl) { tipEl.remove(); tipEl = null; } }

  // ---------- 右栏 ----------
  function renderRight() {
    const p = pages[idx];
    // 信号徽章
    const s = p.signals || {};
    const badges = [];
    const errN = (p.render_errors || []).length;
    if (errN) badges.push(`<span class="badge err">KaTeX 报错 ${errN}</span>`);
    if (s.column_suspected) badges.push(`<span class="badge col">双栏嫌疑</span>`);
    (s.unhandled_labels || []).forEach((l) => badges.push(`<span class="badge warn">未知 label: ${l}</span>`));
    (s.visual_warnings || []).forEach((w) => badges.push(`<span class="badge warn">${w.kind}</span>`));
    if (!badges.length) badges.push(`<span class="badge ok">无信号</span>`);
    $("signals").innerHTML = badges.join("");
    // md 渲染(含 katex,插件已判红)
    const out = $("mdOut");
    out.innerHTML = mdit.render(p.md || "");
    out.querySelectorAll(".katex-error").forEach((e) => {
      const host = e.closest(".katex-display") || e.closest(".katex") || e;
      host.classList.add("err-formula");
    });
  }

  // ---------- 翻页 ----------
  function go(i) {
    idx = Math.max(0, Math.min(pages.length - 1, i));
    const p = pages[idx];
    $("pageInput").value = p.page;
    const img = $("pageImg");
    if (p.image_b64) {
      $("noImg").hidden = true; img.hidden = false;
      img.onload = () => { recomputeScale(); drawOverlays(); drawAnnots(); };
      img.src = "data:image/jpeg;base64," + p.image_b64;
    } else {
      img.hidden = true; $("noImg").hidden = false;
      $("overlayLayer").innerHTML = ""; $("annotLayer").innerHTML = "";
    }
    renderRight();
  }

  // ---------- 标注模式 ----------
  function toggleAnnot() {
    annotMode = !annotMode;
    document.body.classList.toggle("annot-mode", annotMode);
    $("annotBtn").classList.toggle("active", annotMode);
    $("annotHint").hidden = !annotMode;
  }

  function drawAnnots() {
    const layer = $("annotLayer");
    layer.innerHTML = "";
    annotations.filter((a) => a.page === pages[idx].page).forEach((a) => {
      const [x0, y0, x1, y1] = a.bbox;
      const d = document.createElement("div");
      d.className = "annot";
      d.style.left = x0 * scale + "px"; d.style.top = y0 * scale + "px";
      d.style.width = (x1 - x0) * scale + "px"; d.style.height = (y1 - y0) * scale + "px";
      d.innerHTML = `<span class="tag">${a.category}·${CAT[a.category] || ""}${a.note ? " — " + a.note : ""}</span>`;
      layer.appendChild(d);
    });
  }

  function initDrag() {
    const stage = $("pageStage");
    let start = null, box = null;
    stage.addEventListener("mousedown", (e) => {
      if (!annotMode) return;
      const r = stage.getBoundingClientRect();
      start = { x: e.clientX - r.left, y: e.clientY - r.top };
      box = document.createElement("div"); box.className = "annot";
      box.style.left = start.x + "px"; box.style.top = start.y + "px";
      $("annotLayer").appendChild(box);
      e.preventDefault();
    });
    window.addEventListener("mousemove", (e) => {
      if (!start) return;
      const r = stage.getBoundingClientRect();
      const x = e.clientX - r.left, y = e.clientY - r.top;
      box.style.left = Math.min(x, start.x) + "px"; box.style.top = Math.min(y, start.y) + "px";
      box.style.width = Math.abs(x - start.x) + "px"; box.style.height = Math.abs(y - start.y) + "px";
    });
    window.addEventListener("mouseup", (e) => {
      if (!start) return;
      const r = stage.getBoundingClientRect();
      const x = e.clientX - r.left, y = e.clientY - r.top;
      const bbox = [Math.min(start.x, x), Math.min(start.y, y), Math.max(start.x, x), Math.max(start.y, y)]
        .map((v) => Math.round(v / scale));
      start = null; if (box) { box.remove(); box = null; }
      if (bbox[2] - bbox[0] < 5 || bbox[3] - bbox[1] < 5) return;
      const cat = prompt("分类  1渲染报错  2排版错  3漏内容  4错归类  5图片位置", "1");
      if (!cat || !CAT[cat]) return;
      const note = prompt("备注(可空)", "") || "";
      annotations.push({ stem, page: pages[idx].page, bbox, category: parseInt(cat, 10), note });
      drawAnnots();
    });
  }

  async function exportAnnots() {
    const payload = { stem, generated: DATA.generated, annotations };
    if (SERVE) {
      await fetch("/annotations", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
      alert("已回写工作区(serve 模式)");
    } else {
      const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
      const a = document.createElement("a");
      a.href = URL.createObjectURL(blob); a.download = stem + "_annotations.json"; a.click();
    }
  }

  // ---------- 绑定 ----------
  function init() {
    $("pageTotal").textContent = pages.length;
    $("prev").onclick = () => go(idx - 1);
    $("next").onclick = () => go(idx + 1);
    $("pageInput").onchange = (e) => {
      const pg = parseInt(e.target.value, 10);
      const i = pages.findIndex((p) => p.page === pg);
      go(i >= 0 ? i : idx);
    };
    $("annotBtn").onclick = toggleAnnot;
    $("exportBtn").onclick = exportAnnots;
    document.addEventListener("keydown", (e) => {
      if (e.target.tagName === "INPUT") return;
      if (e.key === "ArrowLeft") go(idx - 1);
      else if (e.key === "ArrowRight") go(idx + 1);
      else if (e.key.toLowerCase() === "m") toggleAnnot();
    });
    window.addEventListener("resize", () => { recomputeScale(); drawOverlays(); drawAnnots(); });
    buildLayerToggles();
    initDrag();
    go(0);
    fillErrorIndex();     // 最后跑(离屏渲染 100 页,稍慢)
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
