const $ = (selector) => document.querySelector(selector);
const selected = new Set();
let lastChecked = [];
let currentJob = null;

const stateNames = {
  new: "待确认", queued: "排队中", uploading: "正在上传", processing: "Strava 处理中",
  success: "已同步", failed: "失败，可重试", unknown: "需要检查", running: "正在同步",
};

function showNotice(message, kind = "info") {
  const el = $("#global-notice");
  el.textContent = message;
  el.className = `notice ${kind}`;
  el.classList.remove("hidden");
}

function hideNotice() { $("#global-notice").classList.add("hidden"); }

async function api(url, options = {}) {
  const response = await fetch(url, {
    credentials: "same-origin",
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  const payload = await response.json().catch(() => ({}));
  if (response.status === 401) throw new Error(payload.error || "登录或平台授权已失效。");
  if (!response.ok) throw new Error(payload.error || `请求失败（${response.status}）`);
  return payload;
}

function fmtDistance(meters) {
  if (!Number.isFinite(Number(meters))) return "距离未知";
  const km = Number(meters) / 1000;
  return `${km.toFixed(km < 10 ? 2 : 1)} 公里`;
}

function fmtDuration(seconds) {
  const n = Math.max(0, Math.round(Number(seconds) || 0));
  const h = Math.floor(n / 3600), m = Math.floor((n % 3600) / 60), s = n % 60;
  return h ? `${h}小时 ${m}分` : `${m}分 ${String(s).padStart(2, "0")}秒`;
}

function fmtDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? value : new Intl.DateTimeFormat("zh-CN", { dateStyle: "medium", timeStyle: "short" }).format(date);
}

function sportLabel(type) {
  return ({ Run: "跑步", VirtualRun: "室内跑步", Ride: "骑行", Hike: "徒步" })[type] || type;
}

function renderCandidates(rows) {
  lastChecked = rows;
  selected.clear();
  const list = $("#activity-list");
  $("#activity-toolbar").classList.toggle("hidden", rows.length === 0);
  $("#select-all").checked = false;
  if (!rows.length) {
    list.className = "activity-list empty-state";
    list.textContent = "最近两个月没有待同步的运动记录。已同步记录会自动跳过。";
    updateSelection();
    return;
  }
  list.className = "activity-list";
  list.replaceChildren(...rows.map((row) => {
    const label = document.createElement("label");
    label.className = "activity-row";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.dataset.id = row.source_key;
    checkbox.checked = row.state === "failed" || row.state === "processing";
    if (checkbox.checked) selected.add(row.source_key);
    checkbox.addEventListener("change", () => {
      checkbox.checked ? selected.add(row.source_key) : selected.delete(row.source_key);
      updateSelection();
    });
    const info = document.createElement("span");
    info.className = "activity-info";
    const title = document.createElement("strong");
    title.textContent = `${sportLabel(row.sport_type)} · ${fmtDistance(row.distance)}`;
    const meta = document.createElement("span");
    meta.textContent = `${fmtDate(row.started_at)} · ${fmtDuration(row.duration)}`;
    info.append(title, meta);
    const state = document.createElement("span");
    state.className = `mini-state ${row.state === "failed" ? "is-error" : ""}`;
    state.textContent = row.state === "failed" ? "重试" : (stateNames[row.state] || "待确认");
    label.append(checkbox, info, state);
    if (row.message) {
      const wrap = document.createElement("div");
      wrap.className = "activity-item";
      const detail = document.createElement("small");
      detail.textContent = row.message;
      wrap.append(label, detail);
      return wrap;
    }
    return label;
  }));
  updateSelection();
}

function updateSelection() {
  $("#selection-count").textContent = `已选 ${selected.size} 条`;
  $("#upload-button").disabled = selected.size === 0;
  const boxes = [...document.querySelectorAll("#activity-list input[type=checkbox]")];
  $("#select-all").checked = boxes.length > 0 && boxes.every((box) => box.checked);
}

function renderHistory(rows) {
  const list = $("#history-list");
  if (!rows.length) {
    list.className = "history-list empty-state";
    list.textContent = "还没有同步记录。";
    return;
  }
  list.className = "history-list";
  list.replaceChildren(...rows.map((row) => {
    const line = document.createElement("div");
    line.className = "history-row";
    const left = document.createElement("span");
    const name = document.createElement("strong");
    name.textContent = `${sportLabel(row.sport_type)} · ${fmtDistance(row.distance)}`;
    const date = document.createElement("small");
    date.textContent = fmtDate(row.started_at);
    left.append(name, date);
    const right = document.createElement("span");
    right.className = `status-text ${row.state === "success" ? "is-success" : row.state === "failed" ? "is-error" : ""}`;
    right.textContent = stateNames[row.state] || row.state;
    line.append(left, right);
    if (row.message) {
      const message = document.createElement("small");
      message.className = "history-message";
      message.textContent = row.message;
      line.append(message);
    }
    if (row.state === "processing") {
      const refresh = document.createElement("button");
      refresh.className = "text-button";
      refresh.type = "button";
      refresh.textContent = "刷新状态";
      refresh.addEventListener("click", async () => {
        try {
          await api("/api/refresh-status", { method: "POST", body: JSON.stringify({ activity_id: row.source_key }) });
          await reloadState();
        } catch (error) { showNotice(error.message, "error"); }
      });
      line.append(refresh);
    }
    return line;
  }));
}

async function reloadState() {
  const data = await api("/api/state");
  if (data.history_since) {
    $("#history-range").textContent = `检查范围：${fmtDate(data.history_since)} 至今。选择并确认后才会上传，已同步记录自动跳过。`;
  }
  $("#keep-badge").textContent = data.keep_connected ? "已连接" : "未连接";
  $("#keep-badge").classList.toggle("connected", data.keep_connected);
  $("#keep-login-form").classList.toggle("hidden", data.keep_connected);
  $("#keep-disconnect").classList.toggle("hidden", !data.keep_connected);
  $("#strava-badge").textContent = data.strava_connected ? "已连接" : "未连接";
  $("#strava-badge").classList.toggle("connected", data.strava_connected);
  $("#strava-connect").classList.toggle("hidden", data.strava_connected);
  $("#strava-disconnect").classList.toggle("hidden", !data.strava_connected);
  renderHistory(data.activities);
  if (currentJob) await pollJob(currentJob, false);
}

async function pollJob(jobId, announce = true) {
  for (let i = 0; i < 120; i += 1) {
    const data = await api(`/api/jobs/${encodeURIComponent(jobId)}`);
    renderHistory((await api("/api/state")).activities);
    if (["finished", "finished_with_errors", "interrupted"].includes(data.job.state)) {
      currentJob = null;
      if (announce) {
        const interrupted = data.job.state === "interrupted";
        showNotice(interrupted ? "服务重启前同步任务被中断，请检查状态后再继续。" : data.job.state === "finished" ? "选中记录已处理完毕。" : "同步结束，部分记录失败；可在状态列表查看原因并重试。", data.job.state === "finished" ? "success" : "error");
      }
      return;
    }
    await new Promise((resolve) => setTimeout(resolve, 1200));
  }
  showNotice("同步任务仍在进行，可稍后刷新状态。", "info");
}

$("#keep-login-form").addEventListener("submit", async (event) => {
  event.preventDefault(); hideNotice();
  const formElement = event.currentTarget;
  const form = new FormData(formElement);
  const button = formElement.querySelector("button");
  button.disabled = true; button.textContent = "正在连接…";
  try {
    await api("/api/keep/login", { method: "POST", body: JSON.stringify({ mobile: form.get("mobile"), password: form.get("password") }) });
    formElement.reset();
    showNotice("Keep 已连接。", "success");
    await reloadState();
  } catch (error) { showNotice(error.message, "error"); }
  finally { button.disabled = false; button.textContent = "连接 Keep"; }
});

$("#keep-disconnect").addEventListener("click", async () => {
  try { await api("/api/keep", { method: "DELETE" }); await reloadState(); showNotice("Keep 已断开。", "success"); }
  catch (error) { showNotice(error.message, "error"); }
});

$("#strava-disconnect").addEventListener("click", async () => {
  try { await api("/api/strava", { method: "DELETE" }); await reloadState(); showNotice("Strava 已断开并尝试撤销授权。", "success"); }
  catch (error) { showNotice(error.message, "error"); }
});

$("#check-button").addEventListener("click", async () => {
  hideNotice(); const button = $("#check-button");
  button.disabled = true; button.textContent = "正在检查…";
  try {
    const data = await api("/api/check", { method: "POST", body: "{}" });
    renderCandidates(data.activities);
    const warnings = data.warnings || [];
    showNotice(`检查完成，发现 ${data.activities.length} 条待同步记录。${warnings.length ? " 部分记录读取失败：" + warnings.join("；") : ""}`, warnings.length ? "info" : "success");
    await reloadState();
  } catch (error) { showNotice(error.message, "error"); }
  finally { button.disabled = false; button.textContent = "检查 Keep"; }
});

$("#select-all").addEventListener("change", (event) => {
  for (const box of document.querySelectorAll("#activity-list input[type=checkbox]")) {
    box.checked = event.target.checked;
    box.checked ? selected.add(box.dataset.id) : selected.delete(box.dataset.id);
  }
  updateSelection();
});

$("#upload-button").addEventListener("click", () => {
  const count = selected.size;
  $("#confirm-copy").textContent = `即将把 ${count} 条选中的 Keep 运动记录上传到 Strava。请确认运动类型、距离和日期都正确。`;
  $("#confirm-dialog").showModal();
});

$("#confirm-upload").addEventListener("click", async (event) => {
  event.preventDefault();
  $("#confirm-dialog").close();
  const button = $("#upload-button"); button.disabled = true; button.textContent = "正在创建同步任务…";
  try {
    const data = await api("/api/sync", { method: "POST", body: JSON.stringify({ activity_ids: [...selected] }) });
    currentJob = data.job_id;
    showNotice("已确认上传，正在处理记录…", "info");
    await pollJob(currentJob);
    await reloadState();
  } catch (error) { showNotice(error.message, "error"); }
  finally { button.textContent = "确认上传到 Strava"; updateSelection(); }
});

$("#reload-button").addEventListener("click", () => reloadState().catch((error) => showNotice(error.message, "error")));

const params = new URLSearchParams(location.search);
if (params.get("error")) showNotice(params.get("error"), "error");
else if (params.get("connected")) showNotice("Strava 已连接。", "success");
reloadState().catch((error) => showNotice(error.message, "error"));
