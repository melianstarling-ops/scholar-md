(() => {
  "use strict";
  const data = JSON.parse(document.getElementById("report-data").textContent);
  const state = { filter: "all", view: "grouped" };
  const $ = (id) => document.getElementById(id);

  function node(tag, className, text) {
    const el = document.createElement(tag);
    if (className) el.className = className;
    if (text !== undefined) el.textContent = text;
    return el;
  }

  function renderMath(target, latex, classification) {
    if (latex === null || latex === undefined || latex === "") {
      target.append(node("span", "empty-math", `[${classification || "no latex"}]`));
      return true;
    }
    katex.render(latex, target, {
      displayMode: true,
      throwOnError: false,
      strict: "ignore",
      trust: false,
      output: "html",
    });
    const failed = target.querySelector(".katex-error") !== null;
    if (failed) {
      target.classList.add("render-failed");
      target.prepend(node("span", "render-warning", "KaTeX 无法渲染；已展开原始 LaTeX"));
    }
    return !failed;
  }

  function addRaw(card, latex) {
    const details = node("details", "raw");
    details.append(node("summary", "", "查看 LaTeX"));
    details.append(node("code", "", latex === null ? "null" : String(latex)));
    card.append(details);
    return details;
  }

  function renderSummary() {
    const metrics = [
      [data.summary.candidate_count, "裁图"],
      [data.summary.valid_model_count, "有效模型配置"],
      [data.summary.failed_model_count, "协议/权限失败"],
      [data.disagreement_count, "存在分歧的候选"],
    ];
    for (const [value, label] of metrics) {
      const metric = node("div", "metric");
      metric.append(node("strong", "", String(value)), node("span", "", label));
      $("summary").append(metric);
    }
  }

  function renderLedger() {
    const table = node("table", "run-table");
    const thead = node("thead");
    const head = node("tr");
    for (const label of ["轮次", "供应商 / 模型 / effort", "耗时", "状态", "错误"])
      head.append(node("th", "", label));
    thead.append(head);
    const body = node("tbody");
    for (const model of data.models) {
      const row = node("tr");
      row.append(node("td", "", `R${model.round_index} · ${model.round_id}`));
      row.append(node("td", "", model.label.replace(/^R\d+ · /, "")));
      row.append(node("td", "", model.duration_seconds == null ? "—" : `${Number(model.duration_seconds).toFixed(1)} s`));
      row.append(node("td", "status" + (model.valid ? "" : " failed"), model.valid ? "有效39条" : "失败"));
      row.append(node("td", "error-cell", model.error || ""));
      body.append(row);
    }
    table.append(thead, body);
    $("run-ledger").append(table);
  }

  function sourceCard(candidate) {
    const card = node("article", "row-card source-card");
    const head = node("div", "card-head");
    head.append(node("span", "card-label", "原始裁图"), node("span", "candidate-id", candidate.candidate_id));
    const body = node("div", "source-body");
    const image = node("img");
    image.src = `data:image/png;base64,${candidate.image_b64}`;
    image.alt = `${candidate.candidate_id} 原始公式裁图`;
    image.addEventListener("click", () => {
      $("dialog-image").src = image.src;
      $("image-dialog").showModal();
    });
    body.append(image);
    const reasons = node("div", "reason-list");
    for (const reason of candidate.reasons || []) reasons.append(node("span", "reason", reason));
    card.append(head, body, reasons);
    return card;
  }

  function rootCard(candidate) {
    const card = node("article", "row-card root-card");
    const head = node("div", "card-head");
    head.append(node("span", "card-label", "ROOT · 我的完整审阅"),
                node("span", "support-count", candidate.root.classification));
    const body = node("div", "math-body");
    const rendered = renderMath(body, candidate.root.latex, candidate.root.classification);
    card.append(head, body);
    const raw = addRaw(card, candidate.root.latex);
    raw.open = !rendered;
    return card;
  }

  function answerCard(answer, grouped) {
    const matchesRoot = grouped ? answer.matches_root : answer.matches_root;
    const card = node("article", `answer-card ${matchesRoot ? "match" : "different"}`);
    const head = node("div", "card-head");
    const label = grouped
      ? (matchesRoot ? "与 ROOT 相同" : "不同转写")
      : answer.label;
    head.append(node("span", "card-label", label));
    if (grouped) head.append(node("span", "support-count", `${answer.supporters.length} 个配置`));
    else head.append(node("span", "support-count", answer.confidence || "?"));
    const body = node("div", "math-body");
    const rendered = renderMath(body, answer.latex, answer.classification);
    card.append(head, body);
    const raw = addRaw(card, answer.latex);
    raw.open = !rendered;
    const supporters = node("div", "supporters");
    const items = grouped ? answer.supporters : [{label: answer.label, confidence: answer.confidence}];
    for (const supporter of items) {
      const confidence = supporter.confidence ? ` · ${supporter.confidence}` : "";
      const chip = node("span", "supporter", `${supporter.label}${confidence}`);
      if (supporter.note) chip.title = supporter.note;
      supporters.append(chip);
    }
    card.append(supporters);
    return card;
  }

  function renderRows() {
    const list = $("review-list");
    list.replaceChildren();
    let visible = 0;
    for (const candidate of data.candidates) {
      if (state.filter === "differences" && !candidate.has_disagreement) continue;
      visible += 1;
      const row = node("section", "review-row" + (candidate.has_disagreement ? " disagreement" : ""));
      row.dataset.candidateId = candidate.candidate_id;
      row.append(sourceCard(candidate), rootCard(candidate));
      const lane = node("div", "answer-lane");
      const strip = node("div", "answer-strip");
      if (state.view === "grouped") {
        for (const group of candidate.groups) strip.append(answerCard(group, true));
      } else {
        for (const model of candidate.all_models) {
          model.matches_root = candidate.groups.some((group) =>
            group.matches_root && group.supporters.some((supporter) => supporter.model_key === model.model_key));
          strip.append(answerCard(model, false));
        }
      }
      lane.append(strip);
      row.append(lane);
      list.append(row);
    }
    $("visible-count").textContent = `当前显示 ${visible} / ${data.candidates.length} 项；橙色左轨表示至少一个答案与 ROOT 不同。`;
  }

  function activate(selector, value, attribute) {
    document.querySelectorAll(selector).forEach((button) => {
      button.classList.toggle("active", button.dataset[attribute] === value);
    });
  }

  document.querySelectorAll("[data-filter]").forEach((button) => button.addEventListener("click", () => {
    state.filter = button.dataset.filter;
    activate("[data-filter]", state.filter, "filter");
    renderRows();
  }));
  document.querySelectorAll("[data-view]").forEach((button) => button.addEventListener("click", () => {
    state.view = button.dataset.view;
    activate("[data-view]", state.view, "view");
    renderRows();
  }));
  $("toggle-ledger").addEventListener("click", () => {
    const ledger = $("run-ledger");
    ledger.hidden = !ledger.hidden;
    $("toggle-ledger").textContent = ledger.hidden ? "展开状态" : "收起状态";
    $("toggle-ledger").setAttribute("aria-expanded", String(!ledger.hidden));
  });
  $("close-dialog").addEventListener("click", () => $("image-dialog").close());
  $("image-dialog").addEventListener("click", (event) => {
    if (event.target === $("image-dialog")) $("image-dialog").close();
  });

  renderSummary();
  renderLedger();
  renderRows();
})();
