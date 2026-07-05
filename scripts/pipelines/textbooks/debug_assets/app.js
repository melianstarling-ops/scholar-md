/* textbooks debug view —— 前端交互(移植 patents debug_view 的外壳与手感)。
   左:页面图 + block_bbox 叠框(分组分色);右:逐页 reconstruct 的 md 经
   markdown-it + @vscode/markdown-it-katex 渲染,硬报错渲染成红,复现 VS Code 预览的红。 */
(function () {
  const DATA = window.DEBUG_DATA;
  const SERVE = !!window.SERVE_MODE;
  const pages = DATA.pages;
  const stem = DATA.stem;
  const $ = (id) => document.getElementById(id);
  const ov = $("overlay"), st = $("stage"), lp = $("leftpane");

  // ---- label → 分组(叠框分色/图层开关) ----
  const GROUP = {
    text: "text", abstract: "text", reference_content: "text", content: "text",
    paragraph_title: "title", doc_title: "title",
    display_formula: "formula", formula_number: "formula",
    image: "visual", chart: "visual",
    table: "pass", footnote: "pass", figure_title: "pass",
    algorithm: "code", header: "noise", number: "noise", header_image: "noise",
  };
  const groupOf = (lab) => GROUP[lab] || "text";
  const LAYERS = [
    ["text", "正文", "var(--text)", true], ["title", "标题", "var(--title)", true],
    ["formula", "公式", "var(--formula)", true], ["visual", "图表", "var(--visual)", true],
    ["pass", "表/脚注", "var(--pass)", true], ["code", "代码", "var(--code)", true],
    ["noise", "噪声(剔)", "var(--noise)", false], ["ann", "标记", "var(--accent)", true],
  ];
  // ---- 标注 5 类 ----
  const CATS = ["render_err", "layout_err", "missing", "miscat", "img_pos"];
  const CAT_NAMES = { render_err: "渲染报错", layout_err: "排版错", missing: "漏内容", miscat: "错归类", img_pos: "图片位置" };
  const CATC = { render_err: "var(--bad)", layout_err: "var(--warn)", missing: "var(--formula)", miscat: "var(--title)", img_pos: "var(--accent)" };
  const CAT_NUM = { render_err: 1, layout_err: 2, missing: 3, miscat: 4, img_pos: 5 };   // 导出映射(兼容 check_annotations)
  const NUM_CAT = { 1: "render_err", 2: "layout_err", 3: "missing", 4: "miscat", 5: "img_pos" };

  const BASE = 860;
  const ANN_KEY = "tbdbgann:" + stem, EXP_KEY = "tbdbgexp:" + stem;
  let cur = 0, zoom = 0, annMode = false, suppressClick = false, selKey = null;
  let probPages = [];   // 问题页:KaTeX 硬报错(红) 或 疑似漏识别(裸大算符,琥珀)
  let corrDirty = false;   // 本页有采纳/驳回写回但还没刷新(离开本页时才刷新,省得点一次刷一次)
  const ann = new Map(Object.entries(JSON.parse(localStorage.getItem(ANN_KEY) || "{}")));

  // 数学交给 markdown-it-katex 插件($…$ 在 inline 阶段 tokenize,先于 markdown 强调,LaTeX 不被污染)
  const mdit = window.markdownit({ html: false, linkify: true, breaks: false })
    .use(window.mdKatexPlugin, { throwOnError: false, errorColor: "#ef4444" });

  const pct = (v, t) => (v / t * 100).toFixed(3) + "%";
  const esc = (s) => (s || "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  function toast(m) { const t = $("toast"); t.textContent = m; t.style.opacity = 1; clearTimeout(t._h); t._h = setTimeout(() => (t.style.opacity = 0), 2200); }
  function flashEl(el) { if (!el) return; el.scrollIntoView({ block: "center", behavior: "smooth" }); el.classList.remove("flash"); void el.offsetWidth; el.classList.add("flash"); }

  // ---------- 图层开关 ----------
  const tgBox = $("toggles");
  for (const [key, label, color, on] of LAYERS) {
    const lab = document.createElement("label");
    lab.className = "tg" + (on ? "" : " off");
    lab.innerHTML = `<input type="checkbox" ${on ? "checked" : ""}><span class="dot" style="background:${color}"></span>${label}`;
    if (!on) st.classList.add("hide-" + key);
    lab.querySelector("input").addEventListener("change", (e) => {
      st.classList.toggle("hide-" + key, !e.target.checked);
      lab.classList.toggle("off", !e.target.checked);
    });
    tgBox.appendChild(lab);
  }

  // ---------- 缩放 ----------
  function applyZoom() {
    st.style.width = zoom === 0 ? "min(100%,860px)" : Math.round(BASE * zoom) + "px";
    $("zl").textContent = zoom === 0 ? "适宽" : Math.round(zoom * 100) + "%";
  }
  $("zin").onclick = () => { zoom = Math.min((zoom || st.offsetWidth / BASE) * 1.25, 5); applyZoom(); };
  $("zout").onclick = () => { zoom = Math.max((zoom || st.offsetWidth / BASE) / 1.25, 0.4); applyZoom(); };
  $("zl").onclick = () => { zoom = 0; applyZoom(); };
  lp.addEventListener("wheel", (e) => {
    if (!e.ctrlKey) return;
    e.preventDefault();
    const r = lp.getBoundingClientRect();
    const px = e.clientX - r.left, py = e.clientY - r.top;
    const oldW = st.offsetWidth, oldH = st.offsetHeight;
    const fx = (lp.scrollLeft + px - st.offsetLeft) / oldW, fy = (lp.scrollTop + py - st.offsetTop) / oldH;
    if (zoom === 0) zoom = oldW / BASE;
    zoom = e.deltaY < 0 ? Math.min(zoom * 1.12, 5) : Math.max(zoom / 1.12, 0.4);
    applyZoom();
    const newW = st.offsetWidth, newH = oldH * newW / oldW;
    lp.scrollLeft = st.offsetLeft + fx * newW - px;
    lp.scrollTop = st.offsetTop + fy * newH - py;
  }, { passive: false });

  // ---------- 顶部横向滚动条(代理) ----------
  const hs = $("hscroll"), hsi = hs.firstElementChild;
  function syncH() { hsi.style.width = lp.scrollWidth + "px"; hs.classList.toggle("on", lp.scrollWidth > lp.clientWidth + 1); }
  hs.addEventListener("scroll", () => { lp.scrollLeft = hs.scrollLeft; });
  lp.addEventListener("scroll", () => { hs.scrollLeft = lp.scrollLeft; });
  if (window.ResizeObserver) { new ResizeObserver(syncH).observe(st); new ResizeObserver(syncH).observe(lp); }
  lp.addEventListener("wheel", (e) => {
    if (e.ctrlKey) return;
    const dx = (e.shiftKey && !e.deltaX) ? e.deltaY : e.deltaX;
    if (dx) { e.preventDefault(); lp.scrollLeft += dx; }
  }, { passive: false });

  // ---------- 长按左键拖动平移 ----------
  let pan = null, panTimer = null, panStart = null;
  lp.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || annMode || e.target.closest(".annbox")) return;
    panStart = { x: e.clientX, y: e.clientY, id: e.pointerId };
    clearTimeout(panTimer);
    panTimer = setTimeout(() => {
      if (!panStart) return;
      pan = { x: panStart.x, y: panStart.y, sl: lp.scrollLeft, st: lp.scrollTop, moved: false };
      try { lp.setPointerCapture(panStart.id); } catch (_) { }
      lp.classList.add("panning");
    }, 250);
  });
  lp.addEventListener("pointermove", (e) => {
    if (pan) {
      e.preventDefault();
      if (!pan.moved && Math.hypot(e.clientX - pan.x, e.clientY - pan.y) > 2) pan.moved = true;
      lp.scrollLeft = pan.sl - (e.clientX - pan.x);
      lp.scrollTop = pan.st - (e.clientY - pan.y);
    } else if (panStart && Math.hypot(e.clientX - panStart.x, e.clientY - panStart.y) > 6) {
      clearTimeout(panTimer); panStart = null;
    }
  });
  function endPan(e) {
    clearTimeout(panTimer); panStart = null;
    if (!pan) return;
    const moved = pan.moved; pan = null;
    lp.classList.remove("panning");
    try { lp.releasePointerCapture(e.pointerId); } catch (_) { }
    if (moved) { suppressClick = true; setTimeout(() => (suppressClick = false), 250); }
  }
  lp.addEventListener("pointerup", endPan);
  lp.addEventListener("pointercancel", endPan);

  // ---------- 反色 ----------
  function setInv(on) {
    st.classList.toggle("inv", on);
    $("filmtrack").parentElement.classList.toggle("inv-thumbs", on);
    $("invBtn").classList.toggle("on", on);
    localStorage.setItem("tbdbginv", on ? "1" : "0");
  }
  $("invBtn").onclick = () => setInv(!st.classList.contains("inv"));

  // ---------- 主题 ----------
  function applyTheme(t) { document.body.dataset.theme = t; $("themeBtn").textContent = t === "dark" ? "☀" : "☾"; localStorage.setItem("tbdbgtheme", t); }
  $("themeBtn").onclick = () => applyTheme(document.body.dataset.theme === "dark" ? "light" : "dark");

  // ---------- 问题页(离屏渲染判红,与 headless 扫描器同源;叠加 payload 里的疑似/待审) ----------
  const nPendingReview = (p) => (p.blocks || []).filter((b) => b.correction && b.correction.status === "pending").length;
  function computeProblems() {
    const scratch = document.createElement("div");
    scratch.style.cssText = "position:absolute;left:-9999px;top:0;visibility:hidden;width:820px";
    document.body.appendChild(scratch);
    const rows = [];
    pages.forEach((p, i) => {
      scratch.innerHTML = mdit.render(p.md || "");
      const nErr = scratch.querySelectorAll(".katex-error").length;
      const nSusp = (p.suspicions || []).length;
      const nReview = nPendingReview(p);
      if (nErr > 0 || nSusp > 0) rows.push({ i, page: p.page, nErr, nSusp, nReview });
    });
    document.body.removeChild(scratch);
    return rows;
  }
  const probLabel = (r) => [r.nErr ? `${r.nErr} 红` : "", r.nSusp ? `疑似${r.nSusp}` : "",
    r.nReview ? `★待审${r.nReview}` : ""].filter(Boolean).join("·");

  // ---------- 缩略图条 ----------
  const film = $("film"), track = $("filmtrack");
  function buildFilm() {
    const probMap = new Map(probPages.map((r) => [r.i, r]));
    track.innerHTML = "";
    pages.forEach((p, i) => {
      const t = document.createElement("div");
      const pr = probMap.get(i);
      t.className = "thumb" + (pr ? "" : " noerr"); t.dataset.i = i;
      const img = p.image_b64 ? `<img loading="lazy" src="data:image/jpeg;base64,${p.image_b64}" alt="">` : "";
      const mark = pr ? (pr.nErr ? '<span class="terr">⚠</span>' : "") + (pr.nSusp ? '<span class="tsusp">◆</span>' : "")
        + (pr.nReview ? '<span class="trev">★</span>' : "") : "";
      t.innerHTML = img + mark +
        (p.signals && p.signals.column_suspected ? '<span class="tcol">▮▮</span>' : "") +
        `<span class="tno">${p.page}</span>`;
      t.onclick = () => gotoIndex(i);
      track.appendChild(t);
    });
  }
  function syncFilm() {
    track.querySelectorAll(".thumb").forEach((t) => t.classList.toggle("active", +t.dataset.i === cur));
    const a = track.querySelector(".thumb.active");
    if (a && film.classList.contains("show")) a.scrollIntoView({ inline: "center", block: "nearest", behavior: "smooth" });
  }
  let filmT = null;
  $("filmzone").addEventListener("mouseenter", () => { clearTimeout(filmT); filmT = setTimeout(() => { film.classList.add("show"); syncFilm(); }, 160); });
  $("filmzone").addEventListener("mouseleave", () => clearTimeout(filmT));
  film.addEventListener("mouseleave", () => { clearTimeout(filmT); film.classList.remove("show"); });
  film.addEventListener("wheel", (e) => { if (e.ctrlKey) return; if (Math.abs(e.deltaY) > Math.abs(e.deltaX)) { e.preventDefault(); film.scrollLeft += e.deltaY; } }, { passive: false });

  // ---------- 问题页筛选(红 + 疑似 / 待审修正) ----------
  // reviewOnly 是 errOnly 的严格子集(有待审提案的页必然还带着疑似,内容未生效前不会被清)。
  // 两个筛选互斥:开一个自动关另一个,activeFilter() 统一给 step/render/updatePgTotal 用。
  let errOnly = false, reviewOnly = false;
  let reviewPages = [];
  const activeFilter = () => (reviewOnly ? reviewPages : errOnly ? probPages : null);
  function updatePgTotal() {
    const af = activeFilter();
    const idx = af ? af.findIndex((r) => r.i === cur) : -1;
    const tag = reviewOnly ? "📝" : errOnly ? "⚠" : "";
    $("pgtotal").textContent = "/ " + pages.length + (af ? ` · ${tag}${idx >= 0 ? idx + 1 : "-"}/${af.length}` : "");
  }
  function setErrOnly(on) {
    if (on && !probPages.length) { toast("本档无问题页(红/疑似)"); return; }
    errOnly = on; if (on) reviewOnly = false;
    $("errBtn").classList.toggle("on", errOnly);
    $("reviewBtn").classList.toggle("on", reviewOnly);
    document.body.classList.toggle("erronly", errOnly || reviewOnly);
    const af = activeFilter();
    if (af && !af.some((r) => r.i === cur)) gotoIndex(af[0].i); else gotoIndex(cur);
    toast(on ? `问题页筛选开:${probPages.length} 页(红/疑似)` : "问题页筛选关");
  }
  function setReviewOnly(on) {
    if (on && !reviewPages.length) { toast("本档无待审修正(先跑 vision_repair 生成提案)"); return; }
    reviewOnly = on; if (on) errOnly = false;
    $("reviewBtn").classList.toggle("on", reviewOnly);
    $("errBtn").classList.toggle("on", errOnly);
    document.body.classList.toggle("erronly", errOnly || reviewOnly);
    const af = activeFilter();
    if (af && !af.some((r) => r.i === cur)) gotoIndex(af[0].i); else gotoIndex(cur);
    toast(on ? `待审修正筛选开:${reviewPages.length} 页,优先看这些` : "待审修正筛选关");
  }
  $("errBtn").onclick = () => setErrOnly(!errOnly);
  $("reviewBtn").onclick = () => setReviewOnly(!reviewOnly);

  // ---------- 标记存取 ----------
  const annKey = (page, bstr) => page + "|" + bstr;
  function annSerial() { return JSON.stringify([...ann.entries()].sort((a, b) => (a[0] < b[0] ? -1 : 1))); }
  function updateDirty() { $("expBtn").classList.toggle("dirty", ann.size > 0 && annSerial() !== localStorage.getItem(EXP_KEY)); }
  function saveAnn() { localStorage.setItem(ANN_KEY, JSON.stringify(Object.fromEntries(ann))); updateDirty(); }
  function markExported() { localStorage.setItem(EXP_KEY, annSerial()); updateDirty(); }
  function exportJson() {
    const arr = [...ann.values()].sort((a, b) => a.page - b.page).map((v) => ({
      stem, page: v.page, bbox: v.bbox, block_id: v.block_id, category: CAT_NUM[v.cat], note: v.note || "",
    }));
    return JSON.stringify({ stem, generated: DATA.generated, exported: new Date().toISOString(),
      legend: CAT_NAMES, annotations: arr }, null, 2);
  }
  $("expBtn").onclick = async () => {
    const json = exportJson();
    if (SERVE) {
      try {
        const r = await fetch("/annotations", { method: "POST", headers: { "Content-Type": "application/json" }, body: json });
        if (!r.ok) throw new Error(r.status);
        markExported(); toast(`已写入工作区 ${ann.size} 条 → 03_Output/`); return;
      } catch (e) { toast("回写失败,改用下载…"); }
    }
    navigator.clipboard && navigator.clipboard.writeText(json).then(() => { }, () => { });
    const a = document.createElement("a");
    a.href = URL.createObjectURL(new Blob([json], { type: "application/json" }));
    a.download = stem + "_annotations.json"; a.click(); URL.revokeObjectURL(a.href);
    markExported(); toast(`已导出 ${ann.size} 条(下载+剪贴板);跑 --collect 归位`);
  };

  // ---------- 标记绘制 ----------
  function drawAnn() {
    ov.querySelectorAll(".annbox").forEach((e) => e.remove());
    const d = pages[cur];
    if (!d.width || !d.height) { renderAnnList(); return; }
    for (const [k, v] of ann) {
      if (v.page !== d.page) continue;
      const el = document.createElement("div");
      el.className = "annbox " + v.cat + (k === selKey ? " sel" : "");
      el.dataset.k = k;
      el.style.left = pct(v.bbox[0], d.width); el.style.top = pct(v.bbox[1], d.height);
      el.style.width = pct(v.bbox[2] - v.bbox[0], d.width); el.style.height = pct(v.bbox[3] - v.bbox[1], d.height);
      el.title = CAT_NAMES[v.cat] + (v.note ? "\n备注: " + v.note : "") + "\n(按住拖动 / Delete 删除)";
      el.addEventListener("pointerdown", (ev) => startMove(ev, el, k));
      ov.appendChild(el);
    }
    renderAnnList();
  }
  function startMove(ev, el, k) {
    if (ev.button !== 0) return;
    const v = ann.get(k); if (!v) return;
    ev.preventDefault();
    const d = pages[cur], mv = { x0: ev.clientX, y0: ev.clientY, bbox: [...v.bbox], moved: false, nb: null };
    el.setPointerCapture(ev.pointerId);
    const onMove = (em) => {
      if (!mv.moved && Math.hypot(em.clientX - mv.x0, em.clientY - mv.y0) < 4) return;
      mv.moved = true; el.classList.add("moving");
      const r = st.getBoundingClientRect();
      const dx = (em.clientX - mv.x0) * d.width / r.width, dy = (em.clientY - mv.y0) * d.height / r.height;
      const [x0, y0, x1, y1] = mv.bbox, w = x1 - x0, h = y1 - y0;
      const nx = Math.min(Math.max(0, x0 + dx), d.width - w), ny = Math.min(Math.max(0, y0 + dy), d.height - h);
      mv.nb = [nx, ny, nx + w, ny + h].map((n) => Math.round(n * 10) / 10);
      el.style.left = pct(nx, d.width); el.style.top = pct(ny, d.height);
    };
    const onUp = () => {
      el.removeEventListener("pointermove", onMove); el.removeEventListener("pointerup", onUp);
      el.classList.remove("moving");
      if (mv.moved && mv.nb) {
        suppressClick = true; setTimeout(() => (suppressClick = false), 250);
        ann.delete(k);
        const nk = annKey(d.page, mv.nb.join(","));
        ann.set(nk, { ...v, bbox: mv.nb }); selKey = nk; saveAnn(); drawAnn();
      }
    };
    el.addEventListener("pointermove", onMove); el.addEventListener("pointerup", onUp);
  }
  function delAnn(k) { ann.delete(k); if (selKey === k) selKey = null; saveAnn(); drawAnn(); toast("已删除标记"); }

  // ---------- 语义气泡 ----------
  const pop = { el: $("pop"), key: null, pending: null, justOpened: false };
  function openPop(anchor, opts) {
    pop.key = opts.key || null; pop.pending = opts.pending || null;
    pop.justOpened = true; setTimeout(() => (pop.justOpened = false), 50);
    const curCat = pop.key ? ann.get(pop.key).cat : null;
    pop.el.innerHTML = CATS.map((c) =>
      `<span class="pdot${c === curCat ? " on" : ""}" data-c="${c}" title="${CAT_NAMES[c]}" style="background:${CATC[c]}"></span>`).join("")
      + (pop.key ? `<button class="pdel" title="删除 (Delete)">✕</button>` : "");
    pop.el.hidden = false;
    const pw = pop.el.offsetWidth, ph = pop.el.offsetHeight;
    pop.el.style.left = Math.min(Math.max(8, (anchor.left + anchor.right) / 2 - pw / 2), innerWidth - pw - 8) + "px";
    pop.el.style.top = (anchor.top - ph - 10 < 8 ? anchor.bottom + 10 : anchor.top - ph - 10) + "px";
    pop.el.querySelectorAll(".pdot").forEach((p) => (p.onclick = (ev) => { ev.stopPropagation(); applyCat(p.dataset.c); }));
    const del = pop.el.querySelector(".pdel");
    if (del) del.onclick = (ev) => { ev.stopPropagation(); delAnn(pop.key); closePop(); };
  }
  function applyCat(c) {
    const key = pop.key, pending = pop.pending;
    closePop();
    if (key) { const v = ann.get(key); if (v) { v.cat = c; ann.set(key, v); toast("标记: " + CAT_NAMES[c]); } }
    else if (pending) {
      const k = annKey(pending.page, pending.bbox.join(","));
      ann.set(k, { page: pending.page, bbox: pending.bbox, cat: c, block_id: pending.block_id });
      selKey = k; toast("标记: " + CAT_NAMES[c]);
    }
    saveAnn(); drawAnn();
  }
  function closePop() { pop.el.hidden = true; pop.key = null; pop.pending = null; }
  document.addEventListener("click", (e) => { if (!pop.el.hidden && !pop.justOpened && !pop.el.contains(e.target)) closePop(); });

  // ---------- 标记模式:拖拽画区域框 ----------
  let drawing = null;
  st.addEventListener("pointerdown", (e) => {
    if (!annMode || e.button !== 0 || e.target.closest(".annbox")) return;
    drawing = { x0: e.clientX, y0: e.clientY, moved: false, el: null };
  });
  st.addEventListener("pointermove", (e) => {
    if (!drawing) return;
    if (!drawing.moved && Math.hypot(e.clientX - drawing.x0, e.clientY - drawing.y0) < 5) return;
    drawing.moved = true;
    if (!drawing.el) { drawing.el = document.createElement("div"); drawing.el.className = "drawrect"; ov.appendChild(drawing.el); }
    const r = st.getBoundingClientRect();
    drawing.el.style.left = (Math.min(e.clientX, drawing.x0) - r.left) + "px";
    drawing.el.style.top = (Math.min(e.clientY, drawing.y0) - r.top) + "px";
    drawing.el.style.width = Math.abs(e.clientX - drawing.x0) + "px";
    drawing.el.style.height = Math.abs(e.clientY - drawing.y0) + "px";
  });
  st.addEventListener("pointerup", (e) => {
    if (!drawing) return;
    if (drawing.moved) {
      suppressClick = true; drawing.el.remove();
      const d = pages[cur], r = st.getBoundingClientRect();
      const sx = d.width / r.width, sy = d.height / r.height;
      const x0 = Math.max(0, (Math.min(e.clientX, drawing.x0) - r.left) * sx), y0 = Math.max(0, (Math.min(e.clientY, drawing.y0) - r.top) * sy);
      const x1 = Math.min(d.width, (Math.max(e.clientX, drawing.x0) - r.left) * sx), y1 = Math.min(d.height, (Math.max(e.clientY, drawing.y0) - r.top) * sy);
      if (x1 - x0 > 3 && y1 - y0 > 3) {
        const b = [x0, y0, x1, y1].map((v) => Math.round(v * 10) / 10);
        const k = annKey(d.page, b.join(","));
        ann.set(k, { page: d.page, bbox: b, cat: "render_err" }); selKey = k; saveAnn(); drawAnn();
        const nb = ov.querySelector(`.annbox[data-k="${k}"]`);
        if (nb) openPop(nb.getBoundingClientRect(), { key: k });
      }
    }
    drawing = null;
  });

  // ---------- 左栏点击:标记块框 / 联动 ----------
  st.addEventListener("click", (e) => {
    if (suppressClick) { suppressClick = false; return; }
    const d = pages[cur];
    const ab = e.target.closest(".annbox");
    if (ab) { const k0 = ab.dataset.k, r0 = ab.getBoundingClientRect(); selKey = k0; drawAnn(); openPop(r0, { key: k0 }); e.stopPropagation(); return; }
    const el = e.target.closest(".box");
    if (!el) return;
    if (annMode) {
      const bbox = el.dataset.b.split(",").map(Number);
      const k = annKey(d.page, bbox.join(","));
      const r0 = el.getBoundingClientRect();
      if (ann.has(k)) { selKey = k; drawAnn(); openPop(r0, { key: k }); }
      else openPop(r0, { pending: { page: d.page, bbox, block_id: el.dataset.bid ? +el.dataset.bid : null } });
      e.stopPropagation(); return;
    }
    const blk = $("mdOut").querySelector(`.mdblk[data-bids~="${el.dataset.bid}"]`);   // 点左框 → 定位右栏对应转换结果
    if (blk) flashEl(blk);
    else toast(`#${el.dataset.bid} ${el.dataset.lab}` + (el.dataset.ord === "null" ? " (order=None)" : ` (order=${el.dataset.ord})`) + " · 该块无对应正文(被剔除/无内容)");
  });

  // ---------- 右栏标记清单 ----------
  function renderAnnList() {
    const d = pages[cur];
    const rows = [...ann.entries()].filter(([, v]) => v.page === d.page);
    $("annSec").hidden = !rows.length;
    $("annList").innerHTML = rows.map(([k, v]) =>
      `<div class="annitem" data-k="${k}">
        <div class="annrow">
          <span class="cat ${v.cat}">${CAT_NAMES[v.cat]}</span>
          <span class="atext">▭ [${v.bbox.map(Math.round)}]${v.block_id != null ? " · #" + v.block_id : ""}</span>
          <span class="dots">${CATS.map((c) => `<span class="setcat${c === v.cat ? " on" : ""}" data-c="${c}" title="${CAT_NAMES[c]}" style="background:${CATC[c]}"></span>`).join("")}</span>
          <button class="del" title="删除">✕</button>
        </div>
        ${v.note ? `<div class="nsnip" title="${esc(v.note)}">${esc(v.note)}</div>` : ""}
        <textarea class="annnote" placeholder="描述具体问题…(自动保存)" hidden>${esc(v.note || "")}</textarea>
      </div>`).join("");
    $("annList").querySelectorAll(".annitem").forEach((row) => {
      const k = row.dataset.k;
      row.querySelectorAll(".setcat").forEach((p) => (p.onclick = (ev) => { ev.stopPropagation(); const v = ann.get(k); v.cat = p.dataset.c; ann.set(k, v); saveAnn(); drawAnn(); }));
      row.querySelector(".del").onclick = (ev) => { ev.stopPropagation(); delAnn(k); };
      const ta = row.querySelector(".annnote");
      ta.addEventListener("click", (ev) => ev.stopPropagation());
      ta.addEventListener("input", () => { const v = ann.get(k); if (ta.value.trim()) v.note = ta.value; else delete v.note; ann.set(k, v); saveAnn(); });
      ta.addEventListener("keydown", (ev) => { if (ev.key === "Escape") ta.blur(); });
      row.addEventListener("mouseenter", () => { const b = ov.querySelector(`.annbox[data-k="${k}"]`); if (b) b.classList.add("hot"); });
      row.addEventListener("mouseleave", () => { const b = ov.querySelector(`.annbox[data-k="${k}"]`); if (b) b.classList.remove("hot"); });
      row.addEventListener("click", () => {
        selKey = k; drawAnn();
        const b = ov.querySelector(`.annbox[data-k="${k}"]`); if (b) { b.scrollIntoView({ block: "center", behavior: "smooth" }); flashEl(b); }
        const t2 = $("annList").querySelector(`.annitem[data-k="${k}"] .annnote`);
        if (t2) { t2.hidden = false; t2.focus(); t2.selectionStart = t2.value.length; }
      });
    });
  }
  $("annBtn").onclick = () => { annMode = !annMode; $("annBtn").classList.toggle("on", annMode); st.classList.toggle("annmode", annMode); toast(annMode ? "标记模式开:点块框打标 / 空白拖框画区域" : "标记模式关"); };

  // ---------- 跳页(采纳/驳回是即时写盘但不即时刷新;真正离开本页时才刷新一次,
  // 一页多处要改时不必点一次刷一次) ----------
  function gotoIndex(i) {
    if (corrDirty) {
      localStorage.setItem("tbdbgpage:" + stem, i);
      // 翻页时把已采纳修正落进 md(后端 dirty 门控:无改动则秒回、不空跑)
      fetch("/reassemble", { method: "POST" }).catch(() => {});
      location.reload();
      return;
    }
    render(i);
    // 翻页时把已采纳修正落进 md(后端 dirty 门控:无改动则秒回、不空跑)
    fetch("/reassemble", { method: "POST" }).catch(() => {});
  }

  // ---------- 渲染页 ----------
  function render(i) {
    cur = i; const d = pages[i];
    localStorage.setItem("tbdbgpage:" + stem, i);
    closePop(); selKey = null;
    $("pgin").value = d.page;
    const af0 = activeFilter();
    const lo = af0 && af0.length ? af0[0].i : 0;
    const hi = af0 && af0.length ? af0[af0.length - 1].i : pages.length - 1;
    $("prev").disabled = i <= lo; $("next").disabled = i >= hi;
    updatePgTotal(); syncFilm();

    // 左栏
    const img = $("img");
    if (d.image_b64) {
      $("noImg").hidden = true; st.style.display = ""; img.hidden = false;
      img.onload = () => { drawBoxes(d); drawAnn(); syncH(); };
      img.src = "data:image/jpeg;base64," + d.image_b64;
    } else {
      st.style.display = "none"; $("noImg").hidden = false; ov.innerHTML = "";
    }

    // 右栏
    const nOrderNone = d.blocks.filter((b) => b.order === null).length;
    const byG = {}; d.blocks.forEach((b) => { const g = groupOf(b.label); byG[g] = (byG[g] || 0) + 1; });
    const gStr = ["formula", "visual", "title", "pass", "code", "noise"].filter((g) => byG[g])
      .map((g) => `${g} <b>${byG[g]}</b>`).join(" · ");
    $("stats").innerHTML = `<span>块 <b>${d.blocks.length}</b> · order=None <b>${nOrderNone}</b></span>` + (gStr ? `<span>${gStr}</span>` : "");

    const s = d.signals || {}, badges = [];
    const errN = (d.render_errors || []).length;
    if (errN) badges.push(`<span class="badge err">KaTeX 报错 ${errN}</span>`);
    const susp = d.suspicions || [];
    if (susp.length) {
      const detailTitle = [...new Set(susp.map((x) => x.detail))].join("\n");
      badges.push(`<span class="badge warn" title="${detailTitle}">疑似识别错误 ${susp.length}(${[...new Set(susp.map((x) => x.op))].join(",")})</span>`);
    }
    const nRev = nPendingReview(d);
    if (nRev) badges.push(`<span class="badge review">★ 待审修正 ${nRev}(见下方卡片,一键采纳/驳回)</span>`);
    if (s.column_suspected) badges.push(`<span class="badge col">双栏嫌疑</span>`);
    (s.unhandled_labels || []).forEach((l) => badges.push(`<span class="badge warn">未知 label: ${esc(l)}</span>`));
    (s.visual_warnings || []).forEach((w) => badges.push(`<span class="badge warn">${esc(w.kind)}</span>`));
    if (!badges.length) badges.push(`<span class="badge ok">无信号</span>`);
    $("signals").innerHTML = badges.join("");

    const out = $("mdOut");
    const frags = d.frags && d.frags.length ? d.frags : [{ bids: [], md: d.md || "" }];
    out.innerHTML = frags.map((f, fi) => {
      const bids = (f.bids || []).filter((x) => x != null).join(" ");
      const sus = f.suspicions && f.suspicions.length;
      const cls = "mdblk" + (sus ? " susp" : "");
      const ttl = sus ? ` title="疑似识别错误:${f.suspicions.join(" , ")}"` : "";
      const card = f.correction ? renderCorrCard(f.correction, d.page, fi) : "";
      return `<div class="${cls}" data-bids="${bids}"${ttl}>${mdit.render(f.md || "")}${card}</div>`;
    }).join("");
    out.querySelectorAll(".katex-error").forEach((e) => { (e.closest(".katex-display") || e.closest(".katex") || e).classList.add("err-formula"); });
    wireLink();
    wireCorrCards(frags);
    if (!d.image_b64) { renderAnnList(); }
  }

  // ---------- 待审修正卡片(人工确认门:AI 提案 → 一键采纳/驳回,写回 corrections.json) ----------
  // 采纳/驳回后卡片仍显示对照 + 两个按钮(高亮当前状态),可随时改判;写回即时落盘,
  // 但不即时刷新整页——真正翻页/跳页时(gotoIndex)才刷新一次,一页多处要改不必点一次刷一次。
  function renderCorrCard(c, page, fi) {
    // 左边优先放"原图裁切"(真实来源,不是引擎转写)——比对时只看这一张图跟右边是否
    // 一致即可,不必先在脑内确认引擎渲染有没有跟原图一致这一层。没有裁图(旧产物/被清)
    // 才退回渲染引擎 LaTeX 兜底。
    const orig = c.crop_b64
      ? `<img class="corrphoto" src="data:image/png;base64,${c.crop_b64}" alt="原图裁切">`
      : mdit.render(c.engine_latex || "");
    const stCls = c.status === "accepted" ? "st-accepted" : c.status === "rejected" ? "st-rejected" : "st-pending";
    const stText = c.status === "accepted" ? "✓ 已采纳" : c.status === "rejected" ? "✕ 已驳回" : "待审";
    return `<div class="corrcard ${stCls}" data-page="${page}" data-bid="${c.block_id}" data-fi="${fi}">
      <div class="corrhead"><span class="corrtag">AI 提案</span><span class="corrstatus">${stText}</span><span class="corrconf">置信度:${esc(c.confidence || "?")}</span></div>
      <div class="corrcompare">
        <div class="corrpane"><span class="corrlabel">${c.crop_b64 ? "原图裁切" : "引擎原文"}</span><div class="corrmd">${orig}</div></div>
        <div class="corrarrow">→</div>
        <div class="corrpane"><span class="corrlabel">AI 修正</span><div class="corrmd">${mdit.render(c.corrected_latex || "")}</div></div>
      </div>
      <div class="corractions">
        <button class="corrbtn accept${c.status === "accepted" ? " active" : ""}">✓ 采纳</button>
        <button class="corrbtn reject${c.status === "rejected" ? " active" : ""}">✕ 驳回</button>
      </div>
    </div>`;
  }
  function wireOneCorrCard(card, frags) {
    const page = +card.dataset.page, bid = +card.dataset.bid, fi = +card.dataset.fi;
    const accept = card.querySelector(".accept"), reject = card.querySelector(".reject");
    if (!SERVE) {
      [accept, reject].forEach((b) => { b.disabled = true; b.title = "跑 --serve 才能一键采纳/驳回(静态导出只读)"; });
      return;
    }
    const send = async (status) => {
      accept.disabled = reject.disabled = true;
      try {
        const r = await fetch("/corrections", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ page, block_id: bid, status }),
        });
        if (!r.ok) throw new Error("HTTP " + r.status);
        const frag = frags[fi];
        frag.correction = { ...frag.correction, status };
        corrDirty = true;
        const holder = document.createElement("div");
        holder.innerHTML = renderCorrCard(frag.correction, page, fi);
        const fresh = holder.firstElementChild;
        card.replaceWith(fresh);
        wireOneCorrCard(fresh, frags);
        toast((status === "accepted" ? "已采纳" : "已驳回") + "(翻页/跳页时刷新生效)");
      } catch (e) { toast("写回失败: " + e.message); accept.disabled = reject.disabled = false; }
    };
    accept.onclick = () => send("accepted");
    reject.onclick = () => send("rejected");
  }
  function wireCorrCards(frags) {
    $("mdOut").querySelectorAll(".corrcard").forEach((card) => wireOneCorrCard(card, frags));
  }

  // ---------- 手动同步落盘(页尾兜底:停在最后一页采纳、不再翻页也能落 md) ----------
  function syncToMd() {
    fetch("/reassemble", { method: "POST" })
      .then(() => toast("已同步到 md"))
      .catch(() => {});
  }
  $("syncmd").onclick = syncToMd;

  // 右栏片段 hover → 高亮左栏对应叠框(反向:见 drawBoxes 的 linkBlk)
  function wireLink() {
    $("mdOut").querySelectorAll(".mdblk").forEach((blk) => {
      const bids = (blk.dataset.bids || "").split(" ").filter(Boolean);
      if (!bids.length) return;
      const set = (on) => bids.forEach((id) => { const b = ov.querySelector(`.box[data-bid="${id}"]`); if (b) b.classList.toggle("hot", on); });
      blk.addEventListener("mouseenter", () => set(true));
      blk.addEventListener("mouseleave", () => set(false));
    });
  }
  function linkBlk(bid, on) {
    if (bid == null) return;
    $("mdOut").querySelectorAll(`.mdblk[data-bids~="${bid}"]`).forEach((blk) => blk.classList.toggle("link-hot", on));
  }

  function drawBoxes(d) {
    ov.innerHTML = "";
    if (!d.width || !d.height) return;
    const suspBids = new Set((d.suspicions || []).flatMap((x) => x.bids).filter((x) => x != null));
    for (const b of d.blocks) {
      const el = document.createElement("div");
      el.className = "box g-" + groupOf(b.label) + (suspBids.has(b.block_id) ? " susp" : "");
      el.style.left = pct(b.bbox[0], d.width); el.style.top = pct(b.bbox[1], d.height);
      el.style.width = pct(b.bbox[2] - b.bbox[0], d.width); el.style.height = pct(b.bbox[3] - b.bbox[1], d.height);
      el.dataset.bid = b.block_id; el.dataset.lab = b.label; el.dataset.ord = b.order; el.dataset.b = b.bbox.join(",");
      el.title = `#${b.block_id} ${b.label}` + (b.order === null ? " (order=None)" : ` (order=${b.order})`) + (b.content_head ? "\n" + b.content_head : "");
      el.addEventListener("mouseenter", () => linkBlk(b.block_id, true));   // 左框 hover → 高亮右栏对应片段
      el.addEventListener("mouseleave", () => linkBlk(b.block_id, false));
      ov.appendChild(el);
    }
  }

  // ---------- 翻页 / 快捷键 ----------
  function step(dir) {
    const af = activeFilter();
    if (af && af.length) {
      const idxs = af.map((r) => r.i);
      const nxt = dir > 0 ? idxs.find((p) => p > cur) : [...idxs].reverse().find((p) => p < cur);
      if (nxt !== undefined) gotoIndex(nxt);
    } else { const t = cur + dir; if (t >= 0 && t < pages.length) gotoIndex(t); }
  }
  $("prev").onclick = () => step(-1);
  $("next").onclick = () => step(1);
  $("pgin").addEventListener("change", () => { const pg = +$("pgin").value; const i = pages.findIndex((p) => p.page === pg); gotoIndex(i >= 0 ? i : cur); });
  $("pgin").addEventListener("keydown", (e) => { if (e.key === "Enter") $("pgin").blur(); });
  document.addEventListener("keydown", (e) => {
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
    if (e.key === "ArrowRight") step(1);
    else if (e.key === "ArrowLeft") step(-1);
    else if (e.key === "m" || e.key === "M") $("annBtn").click();
    else if (e.key === "e" || e.key === "E") $("errBtn").click();
    else if (e.key === "r" || e.key === "R") $("reviewBtn").click();
    else if (e.key === "s" || e.key === "S") syncToMd();
    else if (e.key === "Delete" && selKey) { delAnn(selKey); closePop(); }
    else if (e.key === "Escape") { closePop(); selKey = null; drawAnn(); }
  });

  // ---------- init ----------
  function init() {
    $("pgtotal").textContent = "/ " + pages.length;
    $("pgin").max = pages.length;
    applyTheme(localStorage.getItem("tbdbgtheme") || "dark");
    setInv(localStorage.getItem("tbdbginv") !== "0");
    applyZoom(); updateDirty();

    probPages = computeProblems();
    reviewPages = probPages.filter((r) => r.nReview);
    const nErrP = probPages.filter((r) => r.nErr).length, nSuspP = probPages.filter((r) => r.nSusp).length;
    const sel = $("errIndex");
    sel.innerHTML = `<option value="">问题索引 (${nErrP} 红 / ${nSuspP} 疑似 / ★${reviewPages.length} 待审)</option>` +
      // 待审(有 AI 提案,可一键采纳/驳回)排在前面优先看,其余按页序
      [...reviewPages, ...probPages.filter((r) => !r.nReview)]
        .map((r) => `<option value="${r.i}">${r.nReview ? "★ " : ""}p${r.page} — ${probLabel(r)}</option>`).join("");
    sel.onchange = () => { if (sel.value !== "") gotoIndex(+sel.value); };
    if (!reviewPages.length) $("reviewBtn").classList.add("empty");
    buildFilm();

    const saved = +(localStorage.getItem("tbdbgpage:" + stem) || 0);
    render(saved >= 0 && saved < pages.length ? saved : 0);
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", init);
  else init();
})();
