/* Remote Management Console — dashboard client (LAN IP + password model) */

let ws = null;
let device = null;        // the device we're attached to
let lastProcs = [];
let cfg = null;           // { port, path } for building a direct URL to a partner IP
let streaming = { screen: false, webcam: false };  // ignore late frames after Stop

async function loadConfig() {
  try {
    const r = await fetch("/api/config", { cache: "no-store" });
    cfg = await r.json();
  } catch (e) {
    cfg = null;
  }
}

/* Build the direct WebSocket URL to a partner's machine on the LAN. */
function dashboardUrl(ip) {
  const port = (cfg && cfg.port) || 8000;
  const path = (cfg && cfg.path) || "/ws/dashboard";
  const scheme = location.protocol === "https:" ? "wss" : "ws";
  return `${scheme}://${ip}:${port}${path}`;
}

const $ = (s) => document.querySelector(s);
const $$ = (s) => document.querySelectorAll(s);

/* ===================== Connection ===================== */
async function connect() {
  const ip = $("#loginId").value.trim();
  const pw = $("#loginPw").value;
  if (!ip || !pw) { loginError("Enter both the Partner IP address and password."); return; }

  if (!location.host || location.protocol === "file:") {
    loginError("Open the console via http://localhost:8000/ — not by opening the HTML file.");
    return;
  }
  loginError("");
  $("#loginBtn").querySelector("span").textContent = "Connecting…";

  if (!cfg) await loadConfig();

  try {
    ws = new WebSocket(dashboardUrl(ip));
  } catch (err) {
    loginError("Could not open connection: " + err.message);
    $("#loginBtn").querySelector("span").textContent = "Connect to partner";
    return;
  }

  ws.onopen = () => ws.send(JSON.stringify({ type: "connect", address: ip, password: pw }));
  ws.onmessage = (ev) => handleMessage(JSON.parse(ev.data));
  ws.onclose = () => {
    if ($("#login").classList.contains("hidden")) {
      // was inside the app -> connection dropped
      setStatus(false);
    } else {
      // still on the login screen -> the connection never came up
      $("#loginBtn").querySelector("span").textContent = "Connect to partner";
      if (!$("#loginError").textContent) {
        loginError(`Couldn't reach ${ip}:${(cfg && cfg.port) || 8000}. Check the IP, that both PCs are on the same network, and that the partner's app is running (allow it through Windows Firewall).`);
      }
    }
  };
  ws.onerror = () => {};
}

function handleMessage(msg) {
  switch (msg.type) {
    case "connect_result":
      if (msg.ok) {
        device = msg.device;
        enterApp();
      } else {
        loginError(msg.error || "Could not connect.");
        $("#loginBtn").querySelector("span").textContent = "Connect";
      }
      break;
    case "device_offline":
      toast("The device went offline", "err");
      setStatus(false);
      setTimeout(disconnect, 1200);
      break;
    case "data":
      routeData(msg.channel, msg.payload);
      break;
  }
}

function enterApp() {
  $("#login").classList.add("hidden");
  $("#app").classList.remove("hidden");
  setStatus(true);
  $("#deviceIdText").textContent = device.device_id;
  renderDevice();
  showDevice();
}

function disconnect() {
  if (ws) { try { ws.close(); } catch (e) {} }
  ws = null;
  device = null;
  $("#app").classList.add("hidden");
  $("#login").classList.remove("hidden");
  $("#loginBtn").querySelector("span").textContent = "Connect";
  $("#loginPw").value = "";
}

function setStatus(on) {
  const pill = $("#statusPill");
  pill.className = "pill " + (on ? "pill-ok" : "");
  $("#statusText").textContent = on ? "connected" : "offline";
}

function send(action, params = {}) {
  if (!ws || ws.readyState !== WebSocket.OPEN) return toast("Not connected", "err");
  ws.send(JSON.stringify({ type: "command", action, params }));
}

/* ===================== Device panel ===================== */
function renderDevice() {
  const ul = $("#agentList");
  ul.innerHTML = "";
  const li = document.createElement("li");
  li.className = "agent-item selected";
  li.innerHTML = `
    <div class="ic">${initials(device.hostname || device.device_id)}</div>
    <div class="tx">
      <div class="h">${esc(device.hostname || device.device_id)}</div>
      <div class="m">ID ${esc(device.device_id)}</div>
    </div>`;
  ul.appendChild(li);
}

function showDevice() {
  $("#empty").classList.add("hidden");
  $("#panel").classList.remove("hidden");
  $("#agentAvatar").textContent = initials(device.hostname || device.device_id);
  $("#agentName").textContent = device.hostname || device.device_id;
  $("#agentMeta").textContent =
    `ID ${device.device_id}  ·  ${device.username || ""}  ·  ${device.platform || ""}`;
  $("#virtualBanner").classList.toggle("hidden", !device.virtual);
  if (device.virtual) toast("Virtual account — view-only demo, not a real remote PC", "err");
  send("list_apps");      // Applications is the default tab
  send("list_processes");
  send("system_stats");   // populate the battery/power indicator right away
}

/* ===================== Incoming data ===================== */
function routeData(channel, payload) {
  switch (channel) {
    case "processes":
      if (payload.list) {
        renderProcesses(payload.list, payload.icons);
        if (payload.killed) toast(`Killed PID ${payload.killed} — ${payload.list.length} processes running`, "ok");
      }
      break;
    case "apps":
      handleApps(payload);
      break;
    case "screen":
      if (streaming.screen) showFrame("screenImg", "screenPlaceholder", payload.image);
      break;
    case "webcam":
      if (streaming.webcam) showFrame("webcamImg", "webcamPlaceholder", payload.image);
      break;
    case "keylog":
      appendKeylog(payload.text);
      break;
    case "power":
      toast(`Power: ${payload.mode} — ${payload.status}`, "ok");
      break;
    case "system":
      renderEnergy(payload);
      break;
    case "file":
      handleFile(payload);
      break;
    case "error":
      toast(`${payload.where || "error"}: ${payload.message}`, "err");
      break;
  }
}

function showFrame(imgId, phId, b64) {
  $("#" + imgId).src = "data:image/jpeg;base64," + b64;
  $("#" + phId).classList.add("hidden");
}

/* ===================== Applications (grouped) ===================== */
function openApps() { send("list_apps"); }

function handleApps(p) {
  if (p.op === "list") { renderApps(p); return; }
  if (p.op === "action") {
    if (p.action === "stop") toast(`Stopped app (${p.count} process${p.count === 1 ? "" : "es"})`, "ok");
    else if (p.action === "start") p.ok ? toast(`Started ${p.name}`, "ok") : toast("Start failed: " + p.error, "err");
  }
}

function renderApps(p) {
  if (p.icons) procIcons = Object.assign(procIcons, p.icons);
  const c = p.counts || {};
  $("#cntHigh").textContent = c.high_cpu || 0;
  $("#cntRun").textContent = c.running || 0;
  $("#cntBg").textContent = c.background || 0;
  $("#appsCount").textContent = `${(c.running || 0) + (c.high_cpu || 0) + (c.background || 0)} applications`;
  fillAppList("listHigh", p.high_cpu, "cntHigh");
  fillAppList("listRun", p.running, "cntRun");
  fillAppList("listBg", p.background, "cntBg");
}

function bumpCount(cntId, delta) {
  const el = $("#" + cntId);
  el.textContent = Math.max(0, (parseInt(el.textContent) || 0) + delta);
  const total = ["cntHigh", "cntRun", "cntBg"]
    .reduce((s, i) => s + (parseInt($("#" + i).textContent) || 0), 0);
  $("#appsCount").textContent = `${total} applications`;
}

function fillAppList(id, apps, cntId) {
  const box = $("#" + id);
  box.innerHTML = "";
  if (!apps || !apps.length) { box.innerHTML = '<div class="app-empty">None</div>'; return; }
  for (const a of apps) {
    const ic = procIcons[a.exe];
    const icon = ic ? `<img class="pico" src="data:image/png;base64,${ic}" alt="" />` : `<span class="pico pico-def">▢</span>`;
    const sub = [a.title, `${a.count} process${a.count === 1 ? "" : "es"}`].filter(Boolean).join("  ·  ");
    const row = document.createElement("div");
    row.className = "app-row";
    row.innerHTML = `
      ${icon}
      <div class="app-info"><b>${esc(a.name)}</b><small>${esc(sub)}</small></div>
      <div class="app-cpu">CPU <b>${a.cpu}%</b> · ${a.mem}% mem</div>
      <span class="app-status running">Running</span>
      <button class="app-act stop">Stop</button>`;
    const statusEl = row.querySelector(".app-status");
    const btn = row.querySelector(".app-act");
    let running = true;   // per-row state, avoids relying on class checks
    btn.onclick = () => {
      if (running) {
        running = false;
        send("stop_app", { pids: a.pids });
        statusEl.textContent = "Stopped"; statusEl.className = "app-status stopped";
        bumpCount(cntId, -1);                   // count drops immediately
        if (a.exe) { btn.textContent = "Start"; btn.className = "app-act start"; }
        else { btn.textContent = "Stopped"; btn.className = "app-act"; btn.disabled = true; }
      } else {
        running = true;
        send("start_app", { exe: a.exe, name: a.name });
        statusEl.textContent = "Running"; statusEl.className = "app-status running";
        bumpCount(cntId, +1);
        btn.textContent = "Stop"; btn.className = "app-act stop";
      }
    };
    box.appendChild(row);
  }
}

/* ===================== Processes ===================== */
let procIcons = {};   // exe path -> base64 PNG, accumulated across refreshes
function renderProcesses(list, icons) {
  lastProcs = list;
  if (icons) procIcons = Object.assign(procIcons, icons);
  const filter = $("#procFilter").value.toLowerCase();
  const rows = list.filter((p) => !filter || p.name.toLowerCase().includes(filter));
  $("#procCount").textContent = filter
    ? `${rows.length} of ${list.length} processes`
    : `${list.length} processes`;
  const body = $("#procBody");
  body.innerHTML = "";
  for (const p of rows) {
    const w = Math.min(100, (p.mem || 0) * 4);
    const ic = procIcons[p.exe];
    const icon = ic
      ? `<img class="pico" src="data:image/png;base64,${ic}" alt="" />`
      : `<span class="pico pico-def">▢</span>`;
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td class="pid">${p.pid}</td>
      <td class="name"><span class="pname">${icon}${esc(p.name)}</span></td>
      <td>${esc(p.username || "")}</td>
      <td><div class="membar"><i style="width:${w}%"></i></div><span class="memval">${p.mem}%</span></td>
      <td><button class="kill" data-pid="${p.pid}">Kill</button></td>`;
    body.appendChild(tr);
  }
  body.querySelectorAll(".kill").forEach((b) => {
    b.onclick = () => send("kill_process", { pid: b.dataset.pid });
  });
}

/* ===================== Files ===================== */
let currentPath = "";
let fileParent = null;

function fmtBytes(n) {
  if (n < 1024) return n + " B";
  if (n < 1048576) return (n / 1024).toFixed(1) + " KB";
  if (n < 1073741824) return (n / 1048576).toFixed(1) + " MB";
  return (n / 1073741824).toFixed(2) + " GB";
}
function joinPath(base, name) {
  if (!base) return name;                       // drives: name is the full path
  const sep = base.includes("/") && !base.includes("\\") ? "/" : "\\";
  return base.replace(/[\\/]+$/, "") + sep + name;
}

function openFiles() { send("list_dir", { path: currentPath }); }

function handleFile(p) {
  if (p.op === "list") return renderFiles(p);
  if (p.op === "download") {
    if (p.error) return toast("Download: " + p.error, "err");
    downloadBlob(p.name, p.data);
    toast(`Downloaded ${p.name} (${fmtBytes(p.size)})`, "ok");
  } else if (p.op === "upload") {
    if (p.error) toast("Upload: " + p.error, "err");
    else { toast(`Uploaded ${p.name}`, "ok"); openFiles(); }
  }
}

function renderFiles(p) {
  if (p.error) toast("Files: " + p.error, "err");
  currentPath = p.path;
  fileParent = p.parent;
  $("#filePath").textContent = p.path || "This PC — drives";
  $("#fileUp").disabled = (p.parent === null);
  const body = $("#fileBody");
  body.innerHTML = "";
  for (const e of (p.entries || [])) {
    const tr = document.createElement("tr");
    tr.className = "file-row " + (e.is_dir ? "dir" : "file");
    const full = joinPath(p.path, e.name);
    const fic = e.is_dir
      ? '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>'
      : '<svg viewBox="0 0 24 24" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.7"><path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8z"/><path d="M14 3v5h5"/></svg>';
    tr.innerHTML = `
      <td><span class="file-row-name"><span class="file-ic">${fic}</span>${esc(e.name)}</span></td>
      <td>${e.is_dir ? "" : fmtBytes(e.size)}</td>
      <td>${e.is_dir ? "" : '<button class="file-dl">Download</button>'}</td>`;
    if (e.is_dir) {
      tr.querySelector(".file-row-name").onclick = () => send("list_dir", { path: full });
    } else {
      tr.querySelector(".file-dl").onclick = () => { send("download_file", { path: full }); toast("Requesting " + e.name + "…"); };
    }
    body.appendChild(tr);
  }
}

function downloadBlob(name, b64) {
  const bin = atob(b64);
  const arr = new Uint8Array(bin.length);
  for (let i = 0; i < bin.length; i++) arr[i] = bin.charCodeAt(i);
  const url = URL.createObjectURL(new Blob([arr]));
  const a = document.createElement("a");
  a.href = url; a.download = name; a.click();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

function doUpload(file) {
  if (!currentPath) return toast("Open a folder first (can't upload to the drive list).", "err");
  if (file.size > 25 * 1048576) return toast("File too large (max 25 MB).", "err");
  const r = new FileReader();
  r.onload = () => {
    send("upload_file", { folder: currentPath, name: file.name, data: r.result.split(",")[1] });
    toast(`Uploading ${file.name}…`);
  };
  r.readAsDataURL(file);
}

/* ===================== Theme ===================== */
const THEME_ICONS = {
  dark: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8"><path d="M21 12.8A9 9 0 1 1 11.2 3 7 7 0 0 0 21 12.8z"/></svg>',
  light: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4 12H2M22 12h-2M5 5l1.4 1.4M17.6 17.6 19 19M5 19l1.4-1.4M17.6 6.4 19 5"/></svg>',
  system: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="4" width="18" height="12" rx="2"/><path d="M8 20h8M12 16v4"/></svg>',
};
function applyTheme(mode) {
  localStorage.setItem("rmc-theme", mode);
  const eff = mode === "system"
    ? (matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark")
    : mode;
  document.documentElement.setAttribute("data-theme", eff);
  $$(".theme-ic").forEach((e) => (e.innerHTML = THEME_ICONS[mode] || THEME_ICONS.dark));
}
function cycleTheme() {
  const order = ["dark", "light", "system"];
  const cur = localStorage.getItem("rmc-theme") || "dark";
  applyTheme(order[(order.indexOf(cur) + 1) % order.length]);
  toast("Theme: " + (localStorage.getItem("rmc-theme")), "ok");
}

/* ===================== Sustainability ===================== */
function fmtTime(s) {
  if (s == null) return "—";
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h > 0 ? `${h}h ${m}m` : `${m}m`;
}

function renderEnergy(d) {
  // Performance
  $("#ecoCpu").innerHTML = `${d.cpu_percent}<small>%</small>`;
  $("#ecoCpuBar").style.width = Math.min(100, d.cpu_percent) + "%";
  $("#ecoCpuSub").textContent =
    `${d.cpu_cores} cores${d.cpu_freq_mhz ? " · " + (d.cpu_freq_mhz / 1000).toFixed(1) + " GHz" : ""}`;

  // Temperature
  $("#ecoTemp").innerHTML = `${d.temperature_c}<small>°C</small>`;
  $("#ecoTempSub").textContent = d.temp_estimated ? "estimated from load" : "sensor reading";

  // Power
  $("#ecoPower").innerHTML = `${d.power_watts}<small>W</small>`;

  // header battery + power indicator (always visible while connected)
  $("#powerW").textContent = `${d.power_watts} W`;
  if (d.battery) {
    const b = d.battery;
    $("#battPct").textContent = `${b.percent}%`;
    const fill = $("#battFill");
    fill.style.width = Math.max(6, b.percent) + "%";
    fill.style.background = b.percent > 50 ? "var(--eco)" : b.percent > 20 ? "var(--warn)" : "var(--danger)";
    $("#battBolt").style.display = b.plugged ? "inline" : "none";
  } else {
    $("#battPct").textContent = "AC";
    $("#battFill").style.width = "100%";
    $("#battFill").style.background = "var(--accent)";
    $("#battBolt").style.display = "inline";
  }

  // Battery
  if (d.battery) {
    const b = d.battery;
    $("#ecoBattery").innerHTML = `${b.percent}<small>%</small>`;
    $("#ecoBatterySub").textContent = b.plugged
      ? "charging / plugged in"
      : (b.secsleft != null ? `${fmtTime(b.secsleft)} remaining` : "on battery");
  } else {
    $("#ecoBattery").innerHTML = `—`;
    $("#ecoBatterySub").textContent = "no battery (desktop)";
  }

  // Carbon: watts → kWh/h → gCO2 (world avg ~475 g/kWh)
  const gPerHour = (d.power_watts / 1000) * 475;
  $("#ecoCo2").textContent = `${gPerHour.toFixed(0)} g/hour  ·  ${(gPerHour * 24 / 1000).toFixed(2)} kg/day`;
  $("#ecoRam").textContent = `${d.ram_used_gb} / ${d.ram_total_gb} GB (${d.ram_percent}%)`;

  // power-saving action estimates (integrate power controls with the metrics)
  const lockSave = Math.max(1, Math.round(d.power_watts * 0.25));
  const sleepSave = Math.max(1, Math.round(d.power_watts - 1));
  if ($("#ecoLockSave")) $("#ecoLockSave").textContent = `≈ ${lockSave} W saved`;
  if ($("#ecoSleepSave")) $("#ecoSleepSave").textContent = `≈ ${sleepSave} W saved`;

  // Eco score (higher = greener): penalise load + heat
  let score = 100 - Math.round(d.cpu_percent * 0.55) - Math.round(Math.max(0, d.temperature_c - 50) * 0.6);
  score = Math.max(1, Math.min(100, score));
  $("#ecoScore").textContent = score;
  $("#ecoScoreLabel").textContent = score >= 80 ? "Excellent" : score >= 60 ? "Good" : "Could improve";

  // Tips
  const tips = [];
  if (d.cpu_percent > 60) tips.push(["warn", "High CPU load — close heavy apps to cut power & heat."]);
  if (d.temperature_c > 75) tips.push(["warn", "Running hot — check ventilation / clean fans."]);
  if (d.battery && !d.battery.plugged && d.battery.percent < 30) tips.push(["warn", "Low battery — turn on Battery Saver."]);
  if (d.battery && d.battery.plugged && d.battery.percent >= 100) tips.push(["", "Fully charged — unplug to avoid wasting energy."]);
  if (d.cpu_percent <= 25 && d.temperature_c <= 60) tips.push(["", "Low load — the system is running efficiently."]);
  if (!tips.length) tips.push(["", "Balanced usage. Dimming the screen saves the most extra power."]);
  $("#ecoTips").innerHTML = tips.map(([k, t]) => `<div class="eco-tip ${k}">${esc(t)}</div>`).join("");
}

/* ===================== Keylog ===================== */
function appendKeylog(text) {
  const el = $("#keylogOut");
  el.textContent += text;
  el.scrollTop = el.scrollHeight;
}

/* ===================== Helpers ===================== */
function initials(s) { return (s || "PC").replace(/[^a-zA-Z0-9]/g, "").slice(0, 2).toUpperCase() || "PC"; }
function esc(s) { return String(s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c])); }
function loginError(m) { $("#loginError").textContent = m; }

/* Show THIS PC's own ID + password on the login page (TeamViewer-style). */
async function fetchMyDevice() {
  try {
    const res = await fetch("/api/my-device", { cache: "no-store" });
    const d = await res.json();
    $("#myId").textContent = d.address || "unavailable";
    if (d.online && d.password) {
      $("#myPw").textContent = d.password;
      $("#myStatus").textContent = "Give these to a partner so they can control this PC.";
      $("#myStatus").classList.remove("offline");
    } else {
      $("#myPw").textContent = "— start agent —";
      $("#myStatus").textContent = "Run the agent on this PC to activate your password.";
      $("#myStatus").classList.add("offline");
    }
  } catch (e) {
    $("#myId").textContent = "unavailable";
    $("#myPw").textContent = "unavailable";
  }
}

function copyCred(el) {
  const txt = el.textContent.trim();
  if (!txt || txt.startsWith("—") || txt === "unavailable" || txt === "…") return;
  if (navigator.clipboard) navigator.clipboard.writeText(txt).catch(() => {});
  el.classList.add("copied");
  toast("Copied: " + txt, "ok");
  setTimeout(() => el.classList.remove("copied"), 1000);
}

let toastTimer = null;
function toast(text, kind = "") {
  const log = $("#log");
  log.innerHTML = "";                 // overwrite the previous notification
  if (toastTimer) clearTimeout(toastTimer);
  const div = document.createElement("div");
  div.className = "toast pop " + kind;
  div.textContent = text;
  log.appendChild(div);
  toastTimer = setTimeout(() => {
    div.style.opacity = "0";
    setTimeout(() => div.remove(), 300);
  }, 3600);
}

/* ===================== Remote control ===================== */
let controlOn = false;
let lastMove = 0;

function clamp(v) { return v < 0 ? 0 : v > 1 ? 1 : v; }

function frac(e) {
  const r = $("#screenImg").getBoundingClientRect();
  return { x: clamp((e.clientX - r.left) / r.width), y: clamp((e.clientY - r.top) / r.height) };
}

function screenActive() {
  return $('.tabpane[data-pane="screen"]').classList.contains("active");
}

function toggleControl() {
  controlOn = !controlOn;
  $("#screenControl").classList.toggle("on", controlOn);
  $("#screenControl").textContent = controlOn ? "🖱 Release control" : "🖱 Take control";
  $("#controlBadge").classList.toggle("hidden", !controlOn);
  $("#controlHint").classList.toggle("hidden", !controlOn);
  $("#screenViewport").classList.toggle("controlling", controlOn);
}

function initControl() {
  const img = $("#screenImg");
  $("#screenControl").onclick = toggleControl;

  img.addEventListener("mousemove", (e) => {
    if (!controlOn) return;
    const now = Date.now();
    if (now - lastMove < 45) return;
    lastMove = now;
    send("mouse_move", frac(e));
  });
  img.addEventListener("click", (e) => {
    if (!controlOn) return;
    send("mouse_click", { ...frac(e), button: "left", double: false });
  });
  img.addEventListener("dblclick", (e) => {
    if (!controlOn) return;
    send("mouse_click", { ...frac(e), button: "left", double: true });
  });
  img.addEventListener("contextmenu", (e) => {
    if (!controlOn) return;
    e.preventDefault();
    send("mouse_click", { ...frac(e), button: "right", double: false });
  });
  img.addEventListener("wheel", (e) => {
    if (!controlOn) return;
    e.preventDefault();
    send("mouse_scroll", { ...frac(e), dy: e.deltaY < 0 ? 1 : -1 });
  }, { passive: false });

  document.addEventListener("keydown", (e) => {
    if (!controlOn || !screenActive()) return;
    if (e.target.tagName === "INPUT" || e.target.tagName === "TEXTAREA") return;
    e.preventDefault();
    send("key_press", { key: e.key });
  });
}

/* Toggle the LIVE badge + placeholder for a stream tab */
function streamUI(startCmd) {
  const map = {
    screen_start: ["screenBadge", true], screen_stop: ["screenBadge", false, "screenPlaceholder"],
    webcam_start: ["webcamBadge", true], webcam_stop: ["webcamBadge", false, "webcamPlaceholder"],
    keylog_start: ["keylogBadge", true], keylog_stop: ["keylogBadge", false],
  };
  const m = map[startCmd];
  if (!m) return;
  $("#" + m[0]).classList.toggle("hidden", !m[1]);
  if (m[2]) $("#" + m[2]).classList.remove("hidden");   // show placeholder again
  // track streaming state + clear the frozen frame on stop so it actually vanishes
  if (startCmd === "screen_start") streaming.screen = true;
  if (startCmd === "webcam_start") streaming.webcam = true;
  if (startCmd === "screen_stop") { streaming.screen = false; $("#screenImg").removeAttribute("src"); }
  if (startCmd === "webcam_stop") { streaming.webcam = false; $("#webcamImg").removeAttribute("src"); }
}

/* ===================== Wiring ===================== */
document.addEventListener("DOMContentLoaded", () => {
  $("#loginBtn").onclick = connect;
  $("#loginId").addEventListener("keydown", (e) => { if (e.key === "Enter") $("#loginPw").focus(); });
  $("#loginPw").addEventListener("keydown", (e) => { if (e.key === "Enter") connect(); });
  $("#disconnectBtn").onclick = disconnect;

  // "Your ID / Your Password" panel
  $("#myId").onclick = () => copyCred($("#myId"));
  $("#myPw").onclick = () => copyCred($("#myPw"));
  loadConfig();
  fetchMyDevice();
  setInterval(() => { if (!$("#login").classList.contains("hidden")) fetchMyDevice(); }, 5000);

  $$(".tab").forEach((t) => {
    t.onclick = () => {
      $$(".tab").forEach((x) => x.classList.remove("active"));
      $$(".tabpane").forEach((x) => x.classList.remove("active"));
      t.classList.add("active");
      $(`.tabpane[data-pane="${t.dataset.tab}"]`).classList.add("active");
      if (t.dataset.tab === "energy") send("system_stats");  // immediate first reading
      if (t.dataset.tab === "files") openFiles();
      if (t.dataset.tab === "apps") openApps();
    };
  });

  $("#appsRefresh").onclick = () => { openApps(); toast("Refreshing applications…"); };

  // Files
  $("#fileUp").onclick = () => { if (fileParent !== null) send("list_dir", { path: fileParent }); };
  $("#fileRefresh").onclick = openFiles;
  $("#fileUpload").onclick = () => $("#fileInput").click();
  $("#fileInput").onchange = (e) => { if (e.target.files[0]) doUpload(e.target.files[0]); e.target.value = ""; };

  // Theme
  applyTheme(localStorage.getItem("rmc-theme") || "dark");
  $$(".theme-btn").forEach((b) => (b.onclick = cycleTheme));
  matchMedia("(prefers-color-scheme: light)").addEventListener("change", () => {
    if ((localStorage.getItem("rmc-theme") || "dark") === "system") applyTheme("system");
  });

  $$("[data-cmd]").forEach((b) => {
    b.onclick = () => {
      const confirmMsg = b.dataset.confirm;
      if (confirmMsg && !confirm(confirmMsg)) return;
      send(b.dataset.cmd);
      streamUI(b.dataset.cmd);
    };
  });

  $("#procRefresh").onclick = () => send("list_processes");
  $("#procFilter").oninput = () => renderProcesses(lastProcs);
  $("#keyClear").onclick = () => ($("#keylogOut").textContent = "");

  initControl();

  // keep the battery/power indicator (and Sustainability tab) live while connected
  setInterval(() => { if (device) send("system_stats"); }, 3000);

  // keep the Applications list live so closed apps drop off automatically
  setInterval(() => {
    if (device && $('.tabpane[data-pane="apps"]').classList.contains("active")) send("list_apps");
  }, 6000);
});
