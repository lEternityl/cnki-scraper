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
    if (tab === "records") refreshRecordFilters();
    if (tab === "advsearch") refreshAdvMeta();
    if (tab === "stats") refreshStats();
    if (tab === "pdf") refreshPdfFilters();
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
async function refreshRecordFilters() {
  try {
    const [journals, stats] = await Promise.all([
      apiGet("/api/journals"),
      apiGet("/api/records/stats"),
    ]);
    fillSelect($("#rec-journal"), journals.in_records);
    fillSelect($("#pdf-journal"), journals.in_records);
    const years = stats.by_year.map(y => y.year);
    fillSelect($("#rec-year"), years);
    fillSelect($("#pdf-year"), years);
    if (!$("#rec-table tbody").children.length) searchRecords();
  } catch (e) { console.warn(e); }
}
function fillSelect(sel, items) {
  const cur = sel.value;
  sel.innerHTML = `<option value="">全部</option>` +
    items.map(i => `<option value="${esc(i)}">${esc(i)}</option>`).join("");
  if (cur) sel.value = cur;
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
    data.items.forEach((r, i) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${esc(r["篇名"] || "")}</td>
        <td>${esc(r["作者"] || "")}</td>
        <td>${esc(r["刊名"] || "")}</td>
        <td>${esc(r["发表时间"] || "")}</td>
        <td>${esc(r["关键词"] || "")}</td>`;
      const detail = document.createElement("tr");
      detail.className = "row-detail";
      detail.innerHTML = `<td colspan="5">
        <div><b>检索期刊：</b>${esc(r["检索期刊"] || "-")}</div>
        <div><b>摘要：</b>${esc(r["摘要"] || "-")}</div>
        ${r["链接"] ? `<div><a class="link" href="${esc(r["链接"])}" target="_blank">${esc(r["链接"])}</a></div>` : ""}
      </td>`;
      tr.addEventListener("click", () => {
        tr.classList.toggle("expanded");
        detail.classList.toggle("show");
      });
      tbody.appendChild(tr);
      tbody.appendChild(detail);
    });
    $("#rec-meta").textContent = `共 ${data.total} 条，第 ${data.page}/${Math.ceil(data.total / data.page_size) || 1} 页`;
    renderPagination(data.total, data.page, data.page_size);
  } catch (e) { toast(e.message, "error"); }
}

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

async function refreshAdvMeta() {
  if (advFields.length) return;
  try {
    const m = await apiGet("/api/search/fields");
    advFields = m.fields;
    advSrcCats = m.source_categories;
    // 渲染来源类别复选框
    $("#adv-src-cats").innerHTML = advSrcCats.map(c =>
      `<label><input type="checkbox" value="${c.code}" /> ${esc(c.title)}</label>`).join("");
    // 渲染第一个条件行
    $("#adv-conditions").innerHTML = "";
    addAdvCondition();
  } catch (e) { toast(e.message, "error"); }
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
    page: advPage,
    page_size: parseInt($("#adv-page-size").value, 10) || 20,
  };
  try {
    const data = await apiPost("/api/search", body);
    const tbody = $("#adv-table tbody");
    tbody.innerHTML = "";
    (data.items || []).forEach(r => {
      const tr = document.createElement("tr");
      const link = r["链接"] ? `<a class="link" href="${esc(r["链接"])}" target="_blank">${esc(r["篇名"] || "")}</a>` : esc(r["篇名"] || "");
      tr.innerHTML = `<td>${link}</td><td>${esc(r["作者"] || "")}</td><td>${esc(r["刊名"] || "")}</td><td>${esc(r["发表时间"] || "")}</td>`;
      tbody.appendChild(tr);
    });
    $("#adv-meta").textContent = `${data.aside || ""} 共 ${data.total} 条，第 ${data.page}/${data.pages || 1} 页`;
    renderAdvPagination(data.total, data.page, data.page_size);
  } catch (e) {
    toast("检索失败: " + e.message, "error");
  }
}
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
$("#adv-search-btn").addEventListener("click", () => { advPage = 1; advSearch(); });

async function advCollectStart() {
  const conds = collectAdvConditions();
  if (!conds.length) { toast("请至少填写一个检索条件", "error"); return; }
  const body = {
    conditions: conds,
    start_year: $("#adv-start-year").value.trim() || null,
    end_year: $("#adv-end-year").value.trim() || null,
    source_categories: collectAdvSrcCats(),
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

// ===================== 统计可视化 =======================================
async function refreshStats() {
  try {
    const s = await apiGet("/api/records/stats");
    $("#stats-total").textContent = `（共 ${s.total} 条）`;
    renderBars("#stats-journal", s.by_journal);
    renderBars("#stats-year", s.by_year.map(y => ({ name: y.year, count: y.count })));
    renderBars("#stats-author", s.by_author);
    renderBars("#stats-keyword", s.by_keyword);
  } catch (e) { toast(e.message, "error"); }
}
function renderBars(sel, items) {
  const el = $(sel);
  if (!items || !items.length) { el.innerHTML = `<div class="muted">暂无数据</div>`; return; }
  const max = Math.max(...items.map(i => i.count));
  el.innerHTML = items.slice(0, 20).map(i => `
    <div class="bar-row">
      <div class="name" title="${esc(i.name)}">${esc(i.name)}</div>
      <div class="track"><div class="fill" style="width:${(i.count / max * 100).toFixed(1)}%"></div></div>
      <div class="count">${i.count}</div>
    </div>`).join("");
}
$("#stats-refresh-btn").addEventListener("click", refreshStats);

// ===================== PDF 下载 ==========================================
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
async function refreshPdfFilters() {
  try {
    const journals = await apiGet("/api/journals");
    fillSelect($("#pdf-journal"), journals.in_records);
    const stats = await apiGet("/api/records/stats");
    fillSelect($("#pdf-year"), stats.by_year.map(y => y.year));
    refreshPdfStatus();
    refreshPdfFiles();
  } catch (e) { console.warn(e); }
}

$("#pdf-start-btn").addEventListener("click", async () => {
  const body = {
    keyword: $("#pdf-keyword").value.trim() || null,
    journal: $("#pdf-journal").value || null,
    author: $("#pdf-author").value.trim() || null,
    year: $("#pdf-year").value || null,
    overwrite: $("#pdf-overwrite").checked,
    min_sleep: parseFloat($("#pdf-min-sleep").value) || 0.6,
    max_sleep: parseFloat($("#pdf-max-sleep").value) || 1.5,
  };
  try {
    await apiPost("/api/pdf/start", body);
    toast("PDF 下载已启动", "ok");
    $("#pdf-log").textContent = "";
    setPill($("#pdf-status-pill"), "running", "运行中");
    if (pdfES) pdfES.close();
    pdfES = attachSSE("/api/pdf/logs", $("#pdf-log"), $("#pdf-status-pill"), () => {
      refreshPdfStatus();
      refreshPdfFiles();
    });
  } catch (e) { toast("启动失败: " + e.message, "error"); }
});
$("#pdf-stop-btn").addEventListener("click", async () => {
  try { await apiPost("/api/pdf/stop"); toast("已请求停止", "ok"); }
  catch (e) { toast(e.message, "error"); }
});

async function refreshPdfFiles() {
  try {
    const data = await apiGet("/api/pdf/files");
    const el = $("#pdf-files");
    if (!data.items.length) { el.innerHTML = `<div class="muted">暂无已下载文件</div>`; return; }
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
