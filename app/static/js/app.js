// Confirmation prompts for destructive actions (CSP forbids inline handlers).
document.addEventListener("click", (ev) => {
  const btn = ev.target.closest("[data-confirm]");
  if (btn && !window.confirm(btn.dataset.confirm)) {
    ev.preventDefault();
  }
});

// Live status: any element with data-live="<printer id>" data-f="<field>" is refreshed.
const fmtTemp = (v) => (v === null || v === undefined ? "—" : `${Number(v).toFixed(1)} °C`);

function applyStatus(p) {
  document.querySelectorAll(`[data-live="${p.id}"]`).forEach((el) => {
    const f = el.dataset.f;
    if (f === "online") {
      const label = !p.enabled ? "disabled" : p.online ? "online" : "offline";
      el.textContent = label;
      el.className = "badge " + (!p.enabled ? "text-bg-secondary" : p.online ? "text-bg-success" : "text-bg-danger");
    } else if (f.endsWith("_c")) {
      el.textContent = fmtTemp(p[f]);
    } else if (f === "last_error") {
      el.textContent = p.enabled && p.last_error ? p.last_error : "";
    } else {
      el.textContent = p[f] ?? "—";
    }
  });
}

// Filament colour swatches: set from JS because the CSP forbids inline styles.
function paintSwatches(root) {
  root.querySelectorAll("[data-jf-color]").forEach((el) => {
    const c = el.dataset.jfColor;
    el.hidden = !/^#[0-9a-fA-F]{6}$/.test(c);
    if (!el.hidden) el.style.backgroundColor = c;
  });
}
paintSwatches(document);

function applyJob(p) {
  const box = document.querySelector(`[data-job="${p.id}"]`);
  if (!box) return;
  const job = p.job;
  box.hidden = !job;
  if (!job) return;
  const set = (k, v) => box.querySelectorAll(`[data-jf="${k}"]`).forEach((el) => { el.textContent = v ?? "—"; });
  set("name", job.name);
  box.querySelector('[data-jf="name"]').title = job.name;
  set("state_label", job.state_label);
  set("percent", job.percent);
  set("layers", job.layer === null ? null : `${job.layer} / ${job.total_layers ?? "?"}`);
  set("remaining", job.remaining);
  set("eta", job.eta);
  set("stage", job.stage);
  set("speed", job.speed);
  set("started", job.started);
  set("elapsed", job.elapsed);
  set("total", job.total);
  set("fans", job.fans);
  const fil = job.filament;
  set("filament", fil ? `${fil.type}${fil.remain !== null ? ` · ${fil.remain}% left` : ""}` : null);
  box.querySelectorAll('[data-jf="filament_source"]').forEach((el) => { el.textContent = fil ? fil.source : ""; });
  box.querySelectorAll("[data-jf-color]").forEach((el) => {
    el.dataset.jfColor = fil && fil.color ? fil.color : "";
  });
  const problems = [job.error ? `Printer error ${job.error}` : null,
    job.hms_count ? `${job.hms_count} health warning(s)` : null].filter(Boolean).join(" · ");
  set("problems", problems);
  box.querySelectorAll('[data-jf-row="problems"]').forEach((el) => { el.hidden = !problems; });
  box.querySelectorAll('[data-jf-row="eta"]').forEach((el) => { el.hidden = !job.eta; });
  paintSwatches(box);
  const bar = box.querySelector('[data-jf="percent_bar"]');
  bar.style.width = `${job.percent ?? 0}%`;
  bar.classList.toggle("bg-success", job.state === "FINISH");
  bar.classList.toggle("bg-danger", job.state === "FAILED");
  bar.classList.toggle("bg-warning", job.state === "PAUSE");
}

async function refreshStatus() {
  try {
    const resp = await fetch("/api/status", { headers: { Accept: "application/json" } });
    if (!resp.ok || !resp.headers.get("content-type")?.includes("json")) return;
    (await resp.json()).printers.forEach((p) => { applyStatus(p); applyJob(p); });
  } catch (_) {
    /* transient network error; next poll retries */
  }
}

if (document.querySelector("[data-live]")) {
  setInterval(refreshStatus, 5000);
}

// Temperature chart on the printer page.
const COLORS = ["#ff6b6b", "#ffa94d", "#74c0fc", "#63e6be", "#b197fc"];

async function drawChart(canvas) {
  const resp = await fetch(canvas.dataset.src);
  if (!resp.ok) return;
  const data = await resp.json();
  const empty = document.getElementById(canvas.dataset.empty);
  if (empty) empty.hidden = data.labels.length > 0;
  const datasets = Object.entries(data.series).map(([label, values], i) => ({
    label, data: values, borderColor: COLORS[i % COLORS.length], backgroundColor: COLORS[i % COLORS.length],
    borderWidth: 2, pointRadius: 0, spanGaps: true, borderDash: label === "Bed target" ? [6, 4] : [],
  }));
  if (canvas.chart) {
    canvas.chart.data.labels = data.labels;
    canvas.chart.data.datasets = datasets;
    canvas.chart.update("none");
    return;
  }
  canvas.chart = new Chart(canvas, {
    type: "line",
    data: { labels: data.labels, datasets },
    options: {
      animation: false, maintainAspectRatio: false, interaction: { mode: "index", intersect: false },
      scales: {
        x: { ticks: { color: "#adb5bd", maxTicksLimit: 8 }, grid: { color: "#343a40" } },
        y: { title: { display: true, text: "°C", color: "#adb5bd" }, ticks: { color: "#adb5bd" }, grid: { color: "#343a40" } },
      },
      plugins: { legend: { labels: { color: "#dee2e6" } } },
    },
  });
}

document.querySelectorAll("canvas[data-src]").forEach((c) => {
  if (typeof Chart === "undefined") return;
  drawChart(c);
  setInterval(() => drawChart(c), 30000);
});

// Live view of a running process (heater test, drying, dashboard).
const fmtDur = (s) => (s === null || s === undefined ? "—"
  : `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`);

async function refreshProcess(card) {
  try {
    const resp = await fetch(card.dataset.src, { headers: { Accept: "application/json" } });
    if (!resp.ok || !resp.headers.get("content-type")?.includes("json")) return;
    const p = await resp.json();
    if (p.terminal) { window.location.reload(); return; }
    const set = (k, v) => card.querySelectorAll(`[data-pf="${k}"]`).forEach((el) => { el.textContent = v; });
    set("state", p.state);
    set("bed_temp_c", fmtTemp(p.bed_temp_c));
    set("bed_target_c", fmtTemp(p.bed_target_c));
    set("elapsed_s", fmtDur(p.elapsed_s));
    set("remaining_s", fmtDur(p.remaining_s));
    set("scheduled_end", p.scheduled_end || "—");
    set("note", p.note || "");
    const bar = card.querySelector('[data-pf="progress"]');
    if (bar && p.progress !== null && p.progress !== undefined) bar.style.width = `${p.progress}%`;
  } catch (_) { /* retry next poll */ }
}

document.querySelectorAll("[data-process][data-src]").forEach((card) => {
  refreshProcess(card);
  setInterval(() => refreshProcess(card), 3000);
});


// Printer cameras: poll snapshots only while a camera is shown and the tab is visible.
const cameraTimers = new Map();

async function refreshCamera(box) {
  if (box.dataset.busy) return;
  box.dataset.busy = "1";
  const img = box.querySelector("img");
  const status = box.querySelector("[data-camera-status]");
  try {
    const resp = await fetch(`${box.dataset.src}?t=${Date.now()}`, { cache: "no-store" });
    if (resp.ok) {
      const url = URL.createObjectURL(await resp.blob());
      const old = img.src;
      img.src = url;
      img.hidden = false;
      status.textContent = `Updated ${new Date().toLocaleTimeString()}`;
      if (old.startsWith("blob:")) URL.revokeObjectURL(old);
    } else {
      status.textContent = await resp.text();
    }
  } catch (_) {
    status.textContent = "Can't reach the server.";
  } finally {
    delete box.dataset.busy;
  }
}

function setCamera(box, on) {
  clearInterval(cameraTimers.get(box.id));
  cameraTimers.delete(box.id);
  if (on) {
    refreshCamera(box);
    cameraTimers.set(box.id, setInterval(() => refreshCamera(box), 500));
  }
}

document.querySelectorAll("[data-camera-toggle]").forEach((btn) => {
  btn.addEventListener("click", () => {
    const box = document.getElementById(btn.dataset.cameraToggle);
    box.hidden = !box.hidden;
    btn.textContent = box.hidden ? "Show camera" : "Hide camera";
    const fsBtn = document.querySelector(`[data-camera-fullscreen="${box.id}"]`);
    if (fsBtn) fsBtn.hidden = box.hidden;
    if (box.hidden) exitCameraFullscreen(box);
    setCamera(box, !box.hidden);
  });
});

// Full screen: the browser's Fullscreen API where available, else a full-window overlay (e.g. iPhone Safari,
// which only allows full screen for <video>).
function inCameraFullscreen(box) {
  return document.fullscreenElement === box || box.classList.contains("camera-overlay");
}

function enterCameraFullscreen(box) {
  if (box.requestFullscreen && document.fullscreenEnabled) {
    box.requestFullscreen().catch(() => box.classList.add("camera-overlay"));
  } else {
    box.classList.add("camera-overlay");
  }
}

function exitCameraFullscreen(box) {
  if (document.fullscreenElement === box) document.exitFullscreen();
  box.classList.remove("camera-overlay");
}

function toggleCameraFullscreen(box) {
  if (inCameraFullscreen(box)) exitCameraFullscreen(box);
  else enterCameraFullscreen(box);
}

document.querySelectorAll("[data-camera-fullscreen]").forEach((btn) => {
  btn.addEventListener("click", () => toggleCameraFullscreen(document.getElementById(btn.dataset.cameraFullscreen)));
});

document.querySelectorAll(".camera-box").forEach((box) => {
  box.addEventListener("dblclick", () => toggleCameraFullscreen(box));
  const exitBtn = box.querySelector("[data-camera-exit]");
  if (exitBtn) exitBtn.addEventListener("click", () => exitCameraFullscreen(box));
});

document.addEventListener("keydown", (ev) => {
  if (ev.key === "Escape") document.querySelectorAll(".camera-overlay").forEach((box) => exitCameraFullscreen(box));
});

document.addEventListener("visibilitychange", () => {
  document.querySelectorAll(".camera-box").forEach((box) => setCamera(box, !box.hidden && !document.hidden));
});


// Initial print-job progress bars (widths are set from JS because the CSP forbids inline styles).
if (document.querySelector("[data-job]")) refreshStatus();
