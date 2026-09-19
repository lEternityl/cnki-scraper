// CNKI 抓取与检索系统前端逻辑
// 全局 fetch 封装 + 各 Tab 的渲染与事件

const API = "";

function $(sel) { return document.querySelector(sel); }
function $$(sel) { return Array.from(document.querySelectorAll(sel)); }

function toast(msg, type) {
  const el = $("#toast");
  el.textContent = msg;
  el.className = "toast show" + (type ? " " + type : "");
  clearTimeout(el._t);
  el._t = setTimeout(() => { el.className = "toast"; }, 2400);
}

async function apiGet(path) {
  const res = await fetch(API + path);
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}
async function apiPost(path, body) {
  const res = await fetch(API + path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}
async function apiPostForm(path, formFieldName, file) {
  const fd = new FormData();
  fd.append(formFieldName, file);
  const res = await fetch(API + path, { method: "POST", body: fd });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}
async function apiDelete(path) {
  const res = await fetch(API + path, { method: "DELETE" });
  if (!res.ok) throw new Error(`${res.status} ${await res.text()}`);
  return res.json();
}

// -- Tab 切换 -------------------------------------------------------------
$$(".tab").forEach(btn => {
  btn.addEventListener("click", () => {
    $$(".tab").forEach(b => b.classList.remove("active"));
    btn.classList.add("active");
    $$(".panel").forEach(p => p.classList.remove("active"));
    $("#tab-" + btn.dataset.tab).classList.add("active");
    const tab = btn.dataset.tab;
    if (tab === "records") { refreshRecordFilters(); refreshPdfFiles(); }
    if (tab === "advsearch") refreshAdvMeta();
    if (tab === "cookie") refreshCookieStatus();
  });
});

// -- SSE 日志订阅 --------------------------------------------------------
function attachSSE(path, logEl, pillEl, onDone) {
  const es = new EventSource(API + path);
  es.onmessage = (ev) => {
    const line = JSON.parse(ev.data);
    logEl.textContent += line + "\n";
    logEl.scrollTop = logEl.scrollHeight;
    if (typeof line === "string" && line.indexOf("[done]") === 0) {
      es.close();
      if (onDone) onDone();
    }
  };
  es.onerror = () => { /* 由心跳维持，无需特殊处理 */ };
  return es;
}

function setPill(el, state, text) {
  el.className = "pill " + state;
  el.textContent = text;
}

// ===================== 抓取任务 ==========================================
let scrapeES = null;
async function refreshScrapeStatus() {
  try {
    const s = await apiGet("/api/scrape/status");
    const grid = $("#scrape-status");
    const elapsed = s.started_at && s.finished_at
      ? ((s.finished_at - s.started_at) / 60).toFixed(1) + "m"
      : s.started_at ? ((Date.now() / 1000 - s.started_at) / 60).toFixed(1) + "m" : "-";
    grid.innerHTML = `
      <div class="item"><div class="k">当前期刊</div><div class="v">${esc(s.current_journal) || "-"}</div></div>
      <div class="item"><div class="k">页码</div><div class="v">${s.current_page}/${s.total_pages || "-"}</div></div>
      <div class="item"><div class="k">本任务已写</div><div class="v">${s.records_written_now}</div></div>
      <div class="item"><div class="k">已耗时</div><div class="v">${elapsed}</div></div>
      ${s.last_error ? `<div class="item"><div class="k">错误</div><div class="v">${esc(s.last_error)}</div></div>` : ""}
    `;
    if (s.running) setPill($("#scrape-status-pill"), "running", "运行中");
    else if (s.last_error) setPill($("#scrape-status-pill"), "error", "出错");
    else if (s.finished_at) setPill($("#scrape-status-pill"), "done", "已完成");
    else setPill($("#scrape-status-pill"), "idle", "空闲");
  } catch (e) { console.warn(e); }
}

$("#scrape-start-btn").addEventListener("click", async () => {
  const journalsRaw = $("#scrape-journals").value.trim();
  const journals = journalsRaw ? journalsRaw.split(/[\n,，]/).map(s => s.trim()).filter(Boolean) : null;
  const body = {
    journals,
    start_year: $("#scrape-start-year").value.trim(),
    end_year: $("#scrape-end-year").value.trim(),
    page_size: parseInt($("#scrape-page-size").value, 10) || 50,
    max_pages: parseInt($("#scrape-max-pages").value, 10) || 0,
    min_sleep: parseFloat($("#scrape-min-sleep").value) || 1.2,
    max_sleep: parseFloat($("#scrape-max-sleep").value) || 2.2,
    fresh: $("#scrape-fresh").checked,
  };
  try {
    await apiPost("/api/scrape/start", body);
    toast("抓取已启动", "ok");
    $("#scrape-log").textContent = "";
    setPill($("#scrape-status-pill"), "running", "运行中");
    if (scrapeES) scrapeES.close();
    scrapeES = attachSSE("/api/scrape/logs", $("#scrape-log"), $("#scrape-status-pill"), () => {
      refreshScrapeStatus();
    });
  } catch (e) {
    toast("启动失败: " + e.message, "error");
  }
});

$("#scrape-stop-btn").addEventListener("click", async () => {
  try {
    await apiPost("/api/scrape/stop");
    toast("已请求停止", "ok");
  } catch (e) { toast(e.message, "error"); }
});

// ===================== 文献检索 ==========================================
let recPage = 1;
const recSelected = new Set();  // 勾选记录的 _idx 集合（跨页保留）
const DOC_TYPE_MAP = {
  "J": "学术期刊", "J/OL": "学术期刊(网络首发)", "N": "报纸",
  "D": "学位论文", "C": "会议", "M": "图书", "R": "报告",
};

async function refreshRecordFilters() {
  try {
    const [journals, stats] = await Promise.all([
      apiGet("/api/journals"),
      apiGet("/api/records/stats"),
    ]);
    fillSelect($("#rec-journal"), journals.in_records);
    const years = stats.by_year.map(y => y.year);
    fillSelect($("#rec-year"), years);
    if (!$("#rec-table tbody").children.length) searchRecords();
  } catch (e) { console.warn(e); }
}
function fillSelect(sel, items) {
  const cur = sel.value;
  sel.innerHTML = `<option value="">全部</option>` +
    items.map(i => `<option value="${esc(i)}">${esc(i)}</option>`).join("");
  if (cur) sel.value = cur;
}

function updateSelectedCount() {
  $("#rec-selected-count").textContent = recSelected.size;
}

// 生成单条记录的 GB/T 7714 引文（与后端 export_gbt7714 逻辑一致）
function gbt7714Citation(r) {
  const docType = (r["文献类型"] || "J").split("/")[0] || "J";
  const pub = (r["发表时间"] || "").trim();
  const pubPart = (docType === "N" && pub) ? pub : ((pub.match(/\d{4}/) || [pub])[0]);
  const authors = (r["作者"] || "").trim();
  const authorsPart = authors ? authors.replace(/;\s*/g, ",") : "";
  const pieces = [];
  if (authorsPart) pieces.push(authorsPart + ".");
  pieces.push((r["篇名"] || "").trim() + `[${docType}].`);
  const journal = (r["刊名"] || "").trim();
  if (journal) pieces.push(pubPart ? `${journal},${pubPart}.` : `${journal}.`);
  else if (pubPart) pieces.push(`${pubPart}.`);
  const link = (r["链接"] || "").trim();
  if (link) pieces.push(`${link}.`);
  return pieces.join(" ");
}

async function copyToClipboard(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (e) {
    // 回退方案
    const ta = document.createElement("textarea");
    ta.value = text;
    ta.style.position = "fixed";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand("copy");
    ta.remove();
    return ok;
  }
}

async function searchRecords() {
  const params = new URLSearchParams({
    page: recPage,
    page_size: $("#rec-page-size").value,
  });
  const kw = $("#rec-keyword").value.trim();
  const j = $("#rec-journal").value;
  const a = $("#rec-author").value.trim();
  const y = $("#rec-year").value;
  if (kw) params.set("keyword", kw);
  if (j) params.set("journal", j);
  if (a) params.set("author", a);
  if (y) params.set("year", y);
  try {
    const data = await apiGet("/api/records?" + params);
    const tbody = $("#rec-table tbody");
    tbody.innerHTML = "";
    data.items.forEach((r) => {
      const idx = r["_idx"];
      const docType = DOC_TYPE_MAP[r["文献类型"]] || (r["文献类型"] ? r["文献类型"] : "学术期刊");
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td><input type="checkbox" class="rec-check" data-idx="${idx}" ${recSelected.has(idx) ? "checked" : ""} /></td>
        <td class="t-title">${esc(r["篇名"] || "")}</td>
        <td>${esc(r["作者"] || "")}</td>
        <td>${esc(r["刊名"] || "")}</td>
        <td>${esc(r["发表时间"] || "")}</td>
        <td>${esc(docType)}</td>
        <td>
          <button class="ghost rec-dl-btn" data-idx="${idx}">下载PDF</button>
          <button class="ghost rec-cite-btn" title="复制 GB/T 7714 格式引文">复制引文</button>
        </td>`;
      const detail = document.createElement("tr");
      detail.className = "row-detail";
      detail.innerHTML = `<td colspan="7">
        <div><b>检索期刊：</b>${esc(r["检索期刊"] || "-")}</div>
        <div><b>关键词：</b>${esc(r["关键词"] || "-")}</div>
        <div><b>摘要：</b>${esc(r["摘要"] || "-")}</div>
        ${r["链接"] ? `<div><a class="link" href="${esc(r["链接"])}" target="_blank">${esc(r["链接"])}</a></div>` : ""}
      </td>`;
      // 勾选与操作按钮不触发行展开
      tr.querySelector(".rec-check").addEventListener("click", (ev) => {
        ev.stopPropagation();
        if (ev.target.checked) recSelected.add(idx); else recSelected.delete(idx);
        updateSelectedCount();
      });
      tr.querySelector(".rec-dl-btn").addEventListener("click", (ev) => {
        ev.stopPropagation();
        startPdfDownload([idx]);
      });
      tr.querySelector(".rec-cite-btn").addEventListener("click", async (ev) => {
        ev.stopPropagation();
        const ok = await copyToClipboard(gbt7714Citation(r));
        toast(ok ? "已复制 GB/T 7714 引文" : "复制失败", ok ? "ok" : "error");
      });
      tr.addEventListener("click", () => {
        tr.classList.toggle("expanded");
        detail.classList.toggle("show");
      });
      tbody.appendChild(tr);
      tbody.appendChild(detail);
    });
    // 本页全选状态
    const checks = $$("#rec-table .rec-check");
    $("#rec-check-all").checked = checks.length > 0 && checks.every(c => c.checked);
    $("#rec-meta").textContent = `共 ${data.total} 条，第 ${data.page}/${Math.ceil(data.total / data.page_size) || 1} 页，已勾选 ${recSelected.size} 条`;
    renderPagination(data.total, data.page, data.page_size);
  } catch (e) { toast(e.message, "error"); }
}

$("#rec-check-all").addEventListener("click", (ev) => {
  const on = ev.target.checked;
  $$("#rec-table .rec-check").forEach(c => {
    c.checked = on;
    const idx = parseInt(c.dataset.idx, 10);
    if (on) recSelected.add(idx); else recSelected.delete(idx);
  });
  updateSelectedCount();
  $("#rec-meta").textContent = $("#rec-meta").textContent.replace(/已勾选 \d+ 条/, `已勾选 ${recSelected.size} 条`);
});

$("#rec-download-selected-btn").addEventListener("click", () => {
  if (!recSelected.size) { toast("请先勾选要下载的文献", "error"); return; }
  startPdfDownload(Array.from(recSelected));
});

function renderPagination(total, page, size) {
  const pages = Math.ceil(total / size) || 1;
  const el = $("#rec-pagination");
  el.innerHTML = "";
  const mk = (label, p, active) => {
    const b = document.createElement("button");
    b.textContent = label;
    if (active) b.classList.add("active");
    b.addEventListener("click", () => { recPage = p; searchRecords(); });
    return b;
  };
  el.appendChild(mk("上一页", Math.max(1, page - 1), false));
  // 简单分页：最多展示 10 个页码
  const start = Math.max(1, page - 5);
  const end = Math.min(pages, start + 9);
  for (let p = start; p <= end; p++) el.appendChild(mk(String(p), p, p === page));
  el.appendChild(mk("下一页", Math.min(pages, page + 1), false));
}

$("#rec-search-btn").addEventListener("click", () => { recPage = 1; searchRecords(); });

$("#rec-export-btn").addEventListener("click", () => {
  const params = new URLSearchParams();
  const kw = $("#rec-keyword").value.trim();
  const jr = $("#rec-journal").value;
  const au = $("#rec-author").value.trim();
  const yr = $("#rec-year").value;
  if (kw) params.set("keyword", kw);
  if (jr) params.set("journal", jr);
  if (au) params.set("author", au);
  if (yr) params.set("year", yr);
  window.open("/api/records/export?" + params.toString(), "_blank");
});

// ===================== 题录导入 ==========================================
function renderImportResult(res) {
  $("#import-result").innerHTML = `
    <div class="item"><div class="k">识别题录</div><div class="v">${res.total_in_file}</div></div>
    <div class="item"><div class="k">本次导入</div><div class="v">${res.imported}</div></div>
    <div class="item"><div class="k">跳过重复</div><div class="v">${res.skipped_dup}</div></div>
    <div class="item"><div class="k">本地总记录</div><div class="v">${res.records_total}</div></div>
  `;
}

$("#import-btn").addEventListener("click", async () => {
  const file = $("#import-file").files[0];
  if (!file) { toast("请先选择文件", "error"); return; }
  try {
    const res = await apiPostForm("/api/records/import", "file", file);
    toast(`导入成功：新增 ${res.imported} 条，跳过重复 ${res.skipped_dup} 条`, "ok");
    renderImportResult(res);
  } catch (e) {
    toast("导入失败: " + e.message, "error");
  }
});

$("#import-text-btn").addEventListener("click", async () => {
  const text = $("#import-text").value.trim();
  if (!text) { toast("请先粘贴题录文本", "error"); return; }
  try {
    const res = await apiPost("/api/records/import_text", { text });
    toast(`导入成功：新增 ${res.imported} 条，跳过重复 ${res.skipped_dup} 条`, "ok");
    renderImportResult(res);
    $("#import-text").value = "";
  } catch (e) {
    toast("导入失败: " + e.message, "error");
  }
});

// ===================== 高级检索（CNKI 实时）=============================
let advFields = [];
let advSrcCats = [];
let advPage = 1;
let advCollectES = null;
const advSelected = new Map();  // cid → {篇名, 发表时间, 下载链接}（跨页保留）

async function refreshAdvMeta() {
  if (advFields.length) return;
  try {
    const m = await apiGet("/api/search/fields");
    advFields = m.fields;
    advSrcCats = m.source_categories;
    // 渲染来源类别复选框
    $("#adv-src-cats").innerHTML = advSrcCats.map(c =>
      `<label><input type="checkbox" value="${c.code}" /> ${esc(c.title)}</label>`).join("");
    // 渲染数据库多选（默认全选 = 总库）
    const dbs = await apiGet("/api/search/databases");
    $("#adv-subdbs").innerHTML = dbs.databases.map(d =>
      `<label><input type="checkbox" value="${d.code}" checked /> ${esc(d.name)}</label>`).join("");
    // 渲染第一个条件行
    $("#adv-conditions").innerHTML = "";
    addAdvCondition();
  } catch (e) { toast(e.message, "error"); }
}

function collectAdvSubDbs() {
  const all = $$("#adv-subdbs input[type=checkbox]");
  const checked = all.filter(c => c.checked);
  if (!all.length || checked.length === all.length) return null;  // 全选/未初始化 = 总库
  return checked.map(c => c.value);
}

function addAdvCondition() {
  const row = document.createElement("div");
  row.className = "cond-row";
  const isNotFirst = $("#adv-conditions").children.length > 0;
  const logicOpts = `<option value="0">AND</option><option value="1">OR</option><option value="2">NOT</option>`;
  const fieldOpts = advFields.map(f => `<option value="${f.code}">${esc(f.title)}</option>`).join("");
  row.innerHTML = `
    <select class="cond-logic" ${isNotFirst ? "" : "disabled"}>${logicOpts}</select>
    <select class="cond-field">${fieldOpts}</select>
    <input class="cond-value" type="text" placeholder="输入检索词" />
    <button class="ghost" data-act="del" title="删除">✕</button>`;
  row.querySelector('[data-act="del"]').addEventListener("click", () => {
    if ($("#adv-conditions").children.length > 1) row.remove();
    // 第一行禁用逻辑
    const first = $("#adv-conditions").firstChild;
    if (first) first.querySelector(".cond-logic").disabled = true;
  });
  $("#adv-conditions").appendChild(row);
}

function collectAdvConditions() {
  const conds = [];
  $$("#adv-conditions .cond-row").forEach((row, i) => {
    const value = row.querySelector(".cond-value").value.trim();
    if (!value) return;
    const field = row.querySelector(".cond-field").value;
    const logic = i === 0 ? 0 : parseInt(row.querySelector(".cond-logic").value, 10);
    conds.push({ field, value, logic });
  });
  return conds;
}
function collectAdvSrcCats() {
  return $$('#adv-src-cats input[type=checkbox]:checked').map(c => c.value);
}

async function advSearch() {
  const conds = collectAdvConditions();
  if (!conds.length) { toast("请至少填写一个检索条件", "error"); return; }
  const body = {
    conditions: conds,
    start_year: $("#adv-start-year").value.trim() || null,
    end_year: $("#adv-end-year").value.trim() || null,
    source_categories: collectAdvSrcCats(),
    sub_dbs: collectAdvSubDbs(),
    page: advPage,
    page_size: parseInt($("#adv-page-size").value, 10) || 20,
    sort_field: $("#adv-sort button.active").dataset.sort || "",
    sort_type: "DESC",
  };
  try {
    const data = await apiPost("/api/search", body);
    const tbody = $("#adv-table tbody");
    tbody.innerHTML = "";
    const start = (data.page - 1) * data.page_size + 1;
    (data.items || []).forEach((r, i) => {
      const tr = document.createElement("tr");
      const link = r["链接"] ? `<a class="link" href="${esc(r["链接"])}" target="_blank">${esc(r["篇名"] || "")}</a>` : esc(r["篇名"] || "");
      const btns = [`<button class="ghost tiny adv-detail" data-href="${esc(r["链接"] || "")}" ${r["链接"] ? "" : "disabled"}>详情</button>`];
      if (r["下载链接"]) {
        btns.push(`<button class="ghost tiny adv-dl" data-title="${esc(r["篇名"] || "")}" data-time="${esc(r["发表时间"] || "")}" data-href="${esc(r["下载链接"])}">下载PDF</button>`);
      }
      const cid = r["cid"] || `${r["篇名"]}|${r["发表时间"]}`;
      const checked = advSelected.has(cid) ? "checked" : "";
      const canSel = r["下载链接"] ? "" : "disabled";
      tr.innerHTML = `<td><input type="checkbox" class="adv-row-check" data-cid="${esc(cid)}" ${checked} ${canSel} title="${r["下载链接"] ? "" : "无下载链接"}" /> <span class="muted">${start + i}</span></td><td>${link}</td><td>${esc(r["作者"] || "")}</td><td>${esc(r["刊名"] || "")}</td><td>${esc(r["发表时间"] || "")}</td><td>${esc(r["数据库"] || "")}</td><td>${esc(r["被引"] || "")}</td><td>${esc(r["下载"] || "")}</td><td>${btns.join("")}</td>`;
      tbody.appendChild(tr);
    });
    syncAdvCheckAll();
    $("#adv-meta").textContent = `${data.aside || ""} 共 ${data.total} 条，第 ${data.page}/${data.pages || 1} 页`;
    renderAdvPagination(data.total, data.page, data.page_size);
  } catch (e) {
    toast("检索失败: " + e.message, "error");
  }
}

// —— 高级检索多选（跨页保留）——
function updateAdvSelCount() {
  const n = advSelected.size;
  $("#adv-sel-count").textContent = n ? `已选 ${n} 条` : "";
  $("#adv-dl-selected").disabled = n === 0;
  $("#adv-dl-selected").textContent = n ? `下载选中 (${n})` : "下载选中";
}
function syncAdvCheckAll() {
  const boxes = $$("#adv-table .adv-row-check");
  const selectable = boxes.filter(b => !b.disabled);
  $("#adv-check-all").checked = selectable.length > 0
    && selectable.every(b => b.checked);
  updateAdvSelCount();
}
$("#adv-table").addEventListener("change", (e) => {
  if (e.target.id === "adv-check-all") {
    const on = e.target.checked;
    $$("#adv-table .adv-row-check").forEach(b => {
      if (b.disabled) return;
      b.checked = on;
      const cid = b.dataset.cid;
      if (on) {
        const row = b.closest("tr");
        const dl = row.querySelector(".adv-dl");
        if (dl) advSelected.set(cid, {
          "篇名": dl.dataset.title, "发表时间": dl.dataset.time, "下载链接": dl.dataset.href,
        });
      } else advSelected.delete(cid);
    });
    updateAdvSelCount();
    return;
  }
  if (!e.target.classList.contains("adv-row-check")) return;
  const cid = e.target.dataset.cid;
  if (e.target.checked) {
    const dl = e.target.closest("tr").querySelector(".adv-dl");
    if (dl) advSelected.set(cid, {
      "篇名": dl.dataset.title, "发表时间": dl.dataset.time, "下载链接": dl.dataset.href,
    });
  } else advSelected.delete(cid);
  updateAdvSelCount();
});
// 批量下载选中（串行 + 间隔，避免触发风控）
$("#adv-dl-selected").addEventListener("click", async () => {
  if (!advSelected.size) return;
  const rows = Array.from(advSelected.values());
  const btn = $("#adv-dl-selected");
  btn.disabled = true;
  let ok = 0, fail = 0, skip = 0;
  for (let i = 0; i < rows.length; i++) {
    btn.textContent = `下载中 ${i + 1}/${rows.length}...`;
    try {
      const res = await apiPost("/api/download/adv-row", rows[i]);
      if (res.skipped) skip++; else ok++;
    } catch (err) { fail++; }
    if (i < rows.length - 1) await new Promise(r => setTimeout(r, 800));
  }
  updateAdvSelCount();
  toast(`批量下载完成：成功 ${ok}，已存在 ${skip}，失败 ${fail}`, fail ? "error" : "ok");
});

// 高级检索行内：详情 / 下载 PDF
document.addEventListener("click", async (e) => {
  const detail = e.target.closest(".adv-detail");
  if (detail && detail.dataset.href) { window.open(detail.dataset.href, "_blank"); return; }
  const dl = e.target.closest(".adv-dl");
  if (!dl) return;
  dl.disabled = true;
  const old = dl.textContent;
  dl.textContent = "下载中...";
  try {
    const res = await apiPost("/api/download/adv-row", {
      "篇名": dl.dataset.title, "发表时间": dl.dataset.time, "下载链接": dl.dataset.href,
    });
    dl.textContent = res.skipped ? "已存在" : "已下载";
    toast(`已保存: ${res.file}`, "ok");
  } catch (err) {
    dl.textContent = "失败";
    toast("下载失败: " + err.message, "error");
  }
  setTimeout(() => { dl.textContent = old; dl.disabled = false; }, 2500);
});

// 排序切换：切换后回到第 1 页重新检索
$$("#adv-sort button").forEach(btn => btn.addEventListener("click", () => {
  $$("#adv-sort button").forEach(b => b.classList.toggle("active", b === btn));
  advPage = 1;
  if (collectAdvConditions().length) advSearch();
}));
// 数据库多选：全选/清空切换
$("#adv-subdb-all").addEventListener("click", () => {
  const all = $$("#adv-subdbs input[type=checkbox]");
  const allChecked = all.every(c => c.checked);
  all.forEach(c => { c.checked = !allChecked; });
});
// 数据库勾选变化：回第 1 页自动重检（与官网筛选体验一致）
$("#adv-subdbs").addEventListener("change", () => {
  advPage = 1;
  if (collectAdvConditions().length) advSearch();
});
function renderAdvPagination(total, page, size) {
  const pages = Math.ceil(total / size) || 1;
  const el = $("#adv-pagination");
  el.innerHTML = "";
  const mk = (label, p, active) => {
    const b = document.createElement("button");
    b.textContent = label;
    if (active) b.classList.add("active");
    b.addEventListener("click", () => { advPage = p; advSearch(); });
    return b;
  };
  el.appendChild(mk("上一页", Math.max(1, page - 1), false));
  const start = Math.max(1, page - 5);
  const end = Math.min(pages, start + 9);
  for (let p = start; p <= end; p++) el.appendChild(mk(String(p), p, p === page));
  el.appendChild(mk("下一页", Math.min(pages, page + 1), false));
}

$("#adv-add-btn").addEventListener("click", addAdvCondition);
$("#adv-search-btn").addEventListener("click", () => {
  advPage = 1;
  advSelected.clear();  // 新检索清空多选
  updateAdvSelCount();
  advSearch();
});

async function advCollectStart() {
  const conds = collectAdvConditions();
  if (!conds.length) { toast("请至少填写一个检索条件", "error"); return; }
  const body = {
    conditions: conds,
    start_year: $("#adv-start-year").value.trim() || null,
    end_year: $("#adv-end-year").value.trim() || null,
    source_categories: collectAdvSrcCats(),
    sub_dbs: collectAdvSubDbs(),
    page_size: 50,
    max_pages: parseInt($("#adv-collect-max-pages").value, 10) || 0,
    min_sleep: parseFloat($("#adv-collect-min-sleep").value) || 1.2,
    max_sleep: parseFloat($("#adv-collect-max-sleep").value) || 2.2,
  };
  try {
    await apiPost("/api/search/collect", body);
    toast("采集已启动", "ok");
    $("#adv-collect-log").textContent = "";
    setPill($("#adv-collect-pill"), "running", "运行中");
    if (advCollectES) advCollectES.close();
    advCollectES = attachSSE("/api/search/collect/logs", $("#adv-collect-log"), $("#adv-collect-pill"), () => {
      refreshAdvCollectStatus();
    });
  } catch (e) { toast("启动失败: " + e.message, "error"); }
}
$("#adv-collect-btn").addEventListener("click", advCollectStart);
$("#adv-collect-stop-btn").addEventListener("click", async () => {
  try { await apiPost("/api/search/collect/stop"); toast("已请求停止", "ok"); }
  catch (e) { toast(e.message, "error"); }
});

async function refreshAdvCollectStatus() {
  try {
    const s = await apiGet("/api/search/collect/status");
    const grid = $("#adv-collect-status");
    const elapsed = s.started_at && s.finished_at
      ? ((s.finished_at - s.started_at) / 60).toFixed(1) + "m"
      : s.started_at ? ((Date.now() / 1000 - s.started_at) / 60).toFixed(1) + "m" : "-";
    grid.innerHTML = `
      <div class="item"><div class="k">页码</div><div class="v">${s.current_page}/${s.total_pages || "-"}</div></div>
      <div class="item"><div class="k">已写</div><div class="v">${s.records_written_now}</div></div>
      <div class="item"><div class="k">已耗时</div><div class="v">${elapsed}</div></div>
      ${s.last_error ? `<div class="item"><div class="k">错误</div><div class="v">${esc(s.last_error)}</div></div>` : ""}
    `;
    if (s.running) setPill($("#adv-collect-pill"), "running", "运行中");
    else if (s.last_error) setPill($("#adv-collect-pill"), "error", "出错");
    else if (s.finished_at) setPill($("#adv-collect-pill"), "done", "已完成");
    else setPill($("#adv-collect-pill"), "idle", "空闲");
  } catch (e) { /* quiet */ }
}
setInterval(refreshAdvCollectStatus, 5000);

// ===================== PDF 下载（并入文献检索页）=========================
let pdfES = null;
async function refreshPdfStatus() {
  try {
    const s = await apiGet("/api/pdf/status");
    const grid = $("#pdf-status");
    const pct = s.total ? ((s.done + s.skipped + s.failed) / s.total * 100).toFixed(0) + "%" : "-";
    grid.innerHTML = `
      <div class="item"><div class="k">进度</div><div class="v">${s.done + s.skipped + s.failed}/${s.total} (${pct})</div></div>
      <div class="item"><div class="k">已下载</div><div class="v">${s.done}</div></div>
      <div class="item"><div class="k">跳过(已存在)</div><div class="v">${s.skipped}</div></div>
      <div class="item"><div class="k">失败</div><div class="v">${s.failed}</div></div>
      <div class="item"><div class="k">当前</div><div class="v">${esc(s.current_title) || "-"}</div></div>
      ${s.last_error ? `<div class="item"><div class="k">错误</div><div class="v">${esc(s.last_error)}</div></div>` : ""}
    `;
    if (s.running) setPill($("#pdf-status-pill"), "running", "运行中");
    else if (s.last_error) setPill($("#pdf-status-pill"), "error", "出错");
    else if (s.finished_at) setPill($("#pdf-status-pill"), "done", "已完成");
    else setPill($("#pdf-status-pill"), "idle", "空闲");
  } catch (e) { console.warn(e); }
}

async function startPdfDownload(indices) {
  try {
    const res = await apiPost("/api/pdf/start", { indices, overwrite: false });
    toast(`PDF 下载已启动：${res.selected} 篇`, "ok");
    $("#pdf-log").textContent = "";
    setPill($("#pdf-status-pill"), "running", "运行中");
    if (pdfES) pdfES.close();
    pdfES = attachSSE("/api/pdf/logs", $("#pdf-log"), $("#pdf-status-pill"), () => {
      refreshPdfStatus();
      refreshPdfFiles();
    });
  } catch (e) { toast("启动失败: " + e.message, "error"); }
}

$("#pdf-stop-btn").addEventListener("click", async () => {
  try { await apiPost("/api/pdf/stop"); toast("已请求停止", "ok"); }
  catch (e) { toast(e.message, "error"); }
});

async function refreshPdfFiles() {
  try {
    const data = await apiGet("/api/pdf/files");
    const el = $("#pdf-files");
    if (!data.items.length) { el.innerHTML = `<div class="muted">暂无已下载 PDF</div>`; return; }
    el.innerHTML = data.items.map(f => {
      const sizeKB = (f.size / 1024).toFixed(0);
      const date = new Date(f.mtime * 1000).toLocaleString();
      return `<div class="file-row">
        <div class="name" title="${esc(f.name)}">${esc(f.name)}</div>
        <div class="muted">${sizeKB} KB · ${date}</div>
        <a href="/api/pdf/files/${encodeURIComponent(f.name)}" target="_blank">下载</a>
      </div>`;
    }).join("");
  } catch (e) { console.warn(e); }
}
$("#pdf-files-refresh").addEventListener("click", refreshPdfFiles);

// ===================== Cookie 管理 =======================================
async function refreshCookieStatus() {
  try {
    const s = await apiGet("/api/cookie");
    const el = $("#cookie-status");
    if (!s.exists) {
      el.innerHTML = `<div class="item"><div class="v">未上传 Cookie</div></div>`;
      return;
    }
    if (s.invalid) {
      el.innerHTML = `<div class="item"><div class="k">状态</div><div class="v">JSON 格式错误</div></div>`;
      return;
    }
    el.innerHTML = `
      <div class="item"><div class="k">状态</div><div class="v">已上传</div></div>
      <div class="item"><div class="k">条目数</div><div class="v">${s.count}</div></div>
      <div class="item"><div class="k">域名</div><div class="v">${(s.domains || []).join(", ")}</div></div>
    `;
  } catch (e) { toast(e.message, "error"); }
}
$("#cookie-upload-btn").addEventListener("click", async () => {
  const file = $("#cookie-file").files[0];
  if (!file) { toast("请先选择文件", "error"); return; }
  try {
    const res = await apiPostForm("/api/cookie", "file", file);
    toast(`已上传 ${res.count} 条 Cookie`, "ok");
    refreshCookieStatus();
  } catch (e) { toast(e.message, "error"); }
});
$("#cookie-text-btn").addEventListener("click", async () => {
  const text = $("#cookie-text").value.trim();
  if (!text) { toast("请先粘贴 Cookie JSON 文本", "error"); return; }
  try {
    const res = await apiPost("/api/cookie/text", { text });
    toast(`已保存 ${res.count} 条 Cookie`, "ok");
    $("#cookie-text").value = "";
    refreshCookieStatus();
  } catch (e) { toast("保存失败: " + e.message, "error"); }
});

$("#cookie-delete-btn").addEventListener("click", async () => {
  if (!confirm("确认删除 Cookie 文件？")) return;
  try {
    await apiDelete("/api/cookie");
    toast("已删除", "ok");
    refreshCookieStatus();
  } catch (e) { toast(e.message, "error"); }
});

// -- 工具 ----------------------------------------------------------------
function esc(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

// -- 启动 ----------------------------------------------------------------
refreshScrapeStatus();
refreshCookieStatus();
setInterval(refreshScrapeStatus, 5000);
setInterval(refreshPdfStatus, 5000);
