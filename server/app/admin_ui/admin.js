"use strict";
// Painel administrativo: usa a mesma API do app, autenticado por cookie HttpOnly.
const $ = (s, el = document) => el.querySelector(s);
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const fmtBytes = (b) => b == null ? "—" : b > 1e9 ? (b / 1e9).toFixed(2) + " GB" : b > 1e6 ? (b / 1e6).toFixed(1) + " MB" : (b / 1e3).toFixed(0) + " KB";
const fmtTime = (t) => t ? new Date(t * 1000).toLocaleString("pt-BR") : "—";
const pill = (ok, txtOk, txtBad, warn) => `<span class="pill ${ok ? "ok" : warn ? "warn" : "bad"}">${ok ? txtOk : txtBad}</span>`;

async function api(path, opts = {}) {
  const res = await fetch("/api/v1" + path, {
    credentials: "same-origin",
    ...opts,
    headers: { "Content-Type": "application/json", "X-Requested-With": "monitor", ...(opts.headers || {}) },
    body: opts.body && typeof opts.body !== "string" ? JSON.stringify(opts.body) : opts.body,
  });
  if (res.status === 401) { showLogin(); throw new Error("não autenticado"); }
  const data = res.headers.get("content-type")?.includes("json") ? await res.json() : await res.text();
  if (!res.ok) throw new Error(data?.detail ? (typeof data.detail === "string" ? data.detail : JSON.stringify(data.detail)) : res.statusText);
  return data;
}

function showLogin() { $("#app").classList.add("hidden"); $("#login").classList.remove("hidden"); }
function showApp() { $("#login").classList.add("hidden"); $("#app").classList.remove("hidden"); }

$("#loginForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  $("#loginError").textContent = "";
  try {
    await api("/auth/login", { method: "POST", body: { username: f.get("username"), password: f.get("password"), cookie: true, device: "painel" } });
    e.target.reset();
    boot();
  } catch (err) { $("#loginError").textContent = err.message; }
});
$("#logout").onclick = async () => { await api("/auth/logout", { method: "POST" }).catch(() => {}); showLogin(); };

// ---------------------------------------------------------------- navegação
function route() {
  const tab = (location.hash || "#status").slice(1);
  document.querySelectorAll("main > section").forEach((s) => s.classList.toggle("hidden", s.id !== "sec-" + tab));
  document.querySelectorAll("header nav a").forEach((a) => a.classList.toggle("active", a.getAttribute("href") === "#" + tab));
  ({ status: loadStatus, cameras: loadCameras, storage: loadStorage, users: loadUsers, logs: loadLogs }[tab] || loadStatus)();
}
window.addEventListener("hashchange", route);

// -------------------------------------------------------------------- status
let ws;
function renderStatus(s) {
  const sv = s.server;
  $("#sys").innerHTML = `<dl>
    <dt>Versão</dt><dd>${esc(sv.version)}</dd>
    <dt>Ativo há</dt><dd>${Math.floor(sv.uptime_seconds / 3600)}h ${Math.floor(sv.uptime_seconds % 3600 / 60)}min</dd>
    <dt>CPU total</dt><dd>${sv.cpu_percent}% (servidor ${sv.process_cpu_percent}%)</dd>
    <dt>Memória</dt><dd>${sv.memory_percent}% · servidor ${fmtBytes(sv.process_memory_bytes)} · ffmpeg ${fmtBytes(sv.ffmpeg_memory_bytes)}</dd>
    <dt>Processos FFmpeg</dt><dd>${sv.ffmpeg_processes}</dd>
    <dt>Disco local livre</dt><dd>${fmtBytes(sv.local_disk?.free_bytes)}</dd></dl>`;
  const st = s.storage;
  $("#stor").innerHTML = `<dl>
    <dt>Status</dt><dd>${pill(st.status === "ok", "OK", esc(st.status), st.status === "degraded")} ${esc(st.reason || "")}</dd>
    <dt>Livre / total</dt><dd>${fmtBytes(st.free_bytes)} / ${fmtBytes(st.total_bytes)}</dd>
    <dt>Usado pelo sistema</dt><dd>${fmtBytes(st.used_by_system_bytes)} (limite ${fmtBytes(st.max_storage_bytes)})</dd>
    <dt>Buffer local</dt><dd>${fmtBytes(st.buffer_bytes)} · ${st.upload_backlog} na fila</dd>
    <dt>Latência escrita</dt><dd>${st.write_latency_ms ?? "—"} ms</dd></dl>`;
  $("#live").innerHTML = s.live.length ? s.live.map((l) => `câmera ${l.camera_id} (${l.quality})`).join("<br>") : '<span class="muted">nenhum fluxo ativo</span>';
  $("#camStatus").innerHTML = `<tr><th>Câmera</th><th>Conexão</th><th>Gravação</th><th>Último dado</th><th>Reinícios</th><th>Erro</th><th></th></tr>` +
    s.cameras.map((c) => `<tr><td>${esc(c.name)}<br><span class="muted">${c.width}x${c.height} ${esc(c.codec)}</span></td>
      <td>${pill(c.online, "online", "offline")}</td>
      <td>${c.record_enabled ? pill(c.recording, "gravando", esc(c.recorder_state)) : '<span class="muted">desativada</span>'}</td>
      <td>${fmtTime(c.last_seen)}</td><td>${c.restarts}</td><td class="muted">${esc(c.last_error).slice(0, 160)}</td>
      <td><button class="small ghost" data-restart="${c.id}">Reiniciar</button></td></tr>`).join("");
}
async function loadStatus() {
  renderStatus(await api("/system/status"));
  const ev = await api("/system/events?limit=40");
  $("#events").innerHTML = `<tr><th>Quando</th><th>Nível</th><th>Tipo</th><th>Mensagem</th></tr>` +
    ev.map((e) => `<tr><td>${fmtTime(e.ts)}</td><td>${pill(e.level === "info", "info", esc(e.level), e.level === "warning")}</td><td>${esc(e.kind)}</td><td>${esc(e.message)}</td></tr>`).join("");
  if (!ws || ws.readyState > 1) {
    ws = new WebSocket((location.protocol === "https:" ? "wss://" : "ws://") + location.host + "/api/v1/ws");
    ws.onmessage = (m) => { const d = JSON.parse(m.data); if (d.type === "status" && location.hash.replace("#", "") in { "": 1, status: 1 }) renderStatus(d.data); };
  }
}
document.addEventListener("click", async (e) => {
  const id = e.target.dataset?.restart;
  if (id) { await api(`/cameras/${id}/restart`, { method: "POST" }); e.target.textContent = "Solicitado"; }
});

// ------------------------------------------------------------------- câmeras
let cameras = [];
async function loadCameras() {
  cameras = await api("/cameras");
  $("#camTable").innerHTML = `<tr><th>Nome</th><th>Host</th><th>RTSP principal</th><th>RTSP secundário</th><th>Vídeo</th><th>Gravação</th><th></th></tr>` +
    cameras.map((c) => `<tr><td>${esc(c.name)}<br><span class="muted">pasta ${esc(c.folder)}</span></td><td>${esc(c.host)}:${c.onvif_port}</td>
      <td>${esc(c.main_url)}</td><td>${esc(c.sub_url)}</td><td>${c.width}x${c.height} ${esc(c.codec)} ${c.fps || ""}fps</td>
      <td>${c.record_enabled ? "sim" : "não"} / ao vivo ${c.live_enabled ? "sim" : "não"}</td>
      <td><button class="small ghost" data-edit="${c.id}">Editar</button> <button class="small ghost" data-test="${c.id}">Testar</button>
      <button class="small danger" data-del="${c.id}">Remover</button></td></tr>`).join("");
}
function openCamForm(cam) {
  const f = $("#camForm");
  f.reset();
  f.classList.remove("hidden");
  $("#camFormOut").textContent = "";
  $("#camFormTitle").textContent = cam ? `Editar ${cam.name}` : "Nova câmera";
  if (cam) for (const [k, v] of Object.entries(cam)) {
    const el = f.elements[k];
    if (!el) continue;
    if (el.type === "checkbox") el.checked = !!v; else el.value = v ?? "";
  } else f.elements.id.value = "";
  f.scrollIntoView({ behavior: "smooth" });
}
$("#newCam").onclick = () => openCamForm(null);
$("#camCancel").onclick = () => $("#camForm").classList.add("hidden");
$("#camTable").addEventListener("click", async (e) => {
  const d = e.target.dataset;
  if (d.edit) openCamForm(cameras.find((c) => c.id == d.edit));
  if (d.test) { $("#camOut").textContent = "Testando..."; $("#camOut").textContent = JSON.stringify(await api(`/cameras/${d.test}/test`, { method: "POST" }).catch((x) => ({ erro: x.message })), null, 2); loadCameras(); }
  if (d.del && confirm("Remover a câmera? As gravações existentes são mantidas até a retenção.")) { await api(`/cameras/${d.del}`, { method: "DELETE" }); loadCameras(); }
});
$("#camForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, body = {};
  for (const el of f.elements) {
    if (!el.name || el.name === "id") continue;
    if (el.type === "checkbox") body[el.name] = el.checked;
    else if (el.type === "number") body[el.name] = Number(el.value);
    else if (el.name === "password") { if (el.value) body.password = el.value; }
    else body[el.name] = el.value;
  }
  try {
    const id = f.elements.id.value;
    await api(id ? `/cameras/${id}` : "/cameras", { method: id ? "PATCH" : "POST", body });
    f.classList.add("hidden");
    loadCameras();
  } catch (err) { $("#camFormOut").textContent = err.message; }
});
$("#onvifFill").onclick = async () => {
  const f = $("#camForm");
  $("#camFormOut").textContent = "Consultando ONVIF...";
  try {
    const r = await api("/admin/onvif/probe", { method: "POST", body: { host: f.elements.host.value, port: Number(f.elements.onvif_port.value), username: f.elements.username.value, password: f.elements.password.value } });
    const profs = r.profiles.filter((p) => p.rtsp_url).sort((a, b) => b.width * b.height - a.width * a.height);
    if (profs[0]) { f.elements.main_url.value = profs[0].rtsp_url; f.elements.rtsp_port.value = new URL(profs[0].rtsp_url.replace("rtsp:", "http:")).port || 554; }
    if (profs.length > 1) f.elements.sub_url.value = profs[profs.length - 1].rtsp_url;
    $("#camFormOut").textContent = JSON.stringify(r, null, 2);
  } catch (err) { $("#camFormOut").textContent = err.message; }
};
$("#discover").onclick = async () => {
  $("#discoverResult").innerHTML = '<p class="muted">Procurando câmeras na rede local...</p>';
  const list = await api("/admin/onvif/discover", { method: "POST" }).catch((e) => ($("#discoverResult").textContent = e.message, []));
  $("#discoverResult").innerHTML = `<div class="card"><table><tr><th>IP</th><th>Porta ONVIF</th><th>Nome</th><th>Hardware</th><th></th></tr>` +
    list.map((d) => `<tr><td>${esc(d.host)}</td><td>${d.onvif_port}</td><td>${esc(d.name)}</td><td>${esc(d.hardware)}</td>
    <td>${d.registered ? '<span class="muted">cadastrada</span>' : `<button class="small" data-add='${esc(JSON.stringify(d))}'>Cadastrar</button>`}</td></tr>`).join("") + "</table></div>";
};
$("#discoverResult").addEventListener("click", (e) => {
  if (!e.target.dataset.add) return;
  const d = JSON.parse(e.target.dataset.add);
  openCamForm(null);
  const f = $("#camForm");
  f.elements.host.value = d.host; f.elements.onvif_port.value = d.onvif_port; f.elements.name.value = d.name || `Câmera ${d.host.split(".").pop()}`;
});
$("#importAgent").onclick = async () => {
  $("#camOut").textContent = "Importando do Agent DVR...";
  $("#camOut").textContent = JSON.stringify(await api("/admin/import/agentdvr", { method: "POST" }).catch((x) => ({ erro: x.message })), null, 2);
  loadCameras();
};

// -------------------------------------------------------------- armazenamento
async function loadStorage() {
  const [s, st] = await Promise.all([api("/admin/settings"), api("/system/storage")]);
  const sf = $("#settingsForm");
  for (const k of ["segment_seconds", "retention_days", "max_storage_gb", "min_free_gb", "buffer_max_gb"]) sf.elements[k].value = s[k];
  sf.elements.upload_enabled.checked = s.upload_enabled;
  const mf = $("#smbForm");
  mf.elements.share.value = s.smb_share; mf.elements.storage_root.value = s.storage_root; mf.elements.username.value = s.smb_username;
  $("#usage").innerHTML = `<tr><th>Câmera</th><th>Segmentos</th><th>Tamanho</th><th>Mais antigo</th><th>Mais recente</th></tr>` +
    st.by_camera.map((r) => `<tr><td>${esc(r.name)}</td><td>${r.segments}</td><td>${fmtBytes(r.bytes)}</td><td>${fmtTime(r.oldest)}</td><td>${fmtTime(r.newest)}</td></tr>`).join("");
}
$("#settingsForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target, body = {};
  for (const k of ["segment_seconds", "retention_days"]) body[k] = parseInt(f.elements[k].value, 10);
  for (const k of ["max_storage_gb", "min_free_gb", "buffer_max_gb"]) body[k] = parseFloat(f.elements[k].value);
  body.upload_enabled = f.elements.upload_enabled.checked;
  $("#storOut").textContent = await api("/admin/settings", { method: "PUT", body }).then(() => "Configurações salvas.").catch((x) => x.message);
});
$("#smbForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = e.target;
  $("#storOut").textContent = "Conectando...";
  $("#storOut").textContent = JSON.stringify(await api("/admin/storage/credentials", { method: "POST", body: { username: f.elements.username.value, password: f.elements.password.value, share: f.elements.share.value, storage_root: f.elements.storage_root.value } }).catch((x) => ({ erro: x.message })), null, 2);
  f.elements.password.value = "";
});
$("#smbTest").onclick = async () => { $("#storOut").textContent = JSON.stringify(await api("/admin/storage/test", { method: "POST" }), null, 2); };
$("#runRetention").onclick = async () => { $("#storOut").textContent = JSON.stringify(await api("/admin/maintenance/retention", { method: "POST" }), null, 2); loadStorage(); };
$("#runReconcile").onclick = async () => { $("#storOut").textContent = "Reconciliando..."; $("#storOut").textContent = JSON.stringify(await api("/admin/maintenance/reconcile", { method: "POST" }), null, 2); loadStorage(); };

// ----------------------------------------------------------------- usuários
async function loadUsers() {
  const users = await api("/admin/users");
  $("#userTable").innerHTML = `<tr><th>Usuário</th><th>Perfil</th><th>Criado</th><th></th></tr>` +
    users.map((u) => `<tr><td>${esc(u.username)}</td><td>${esc(u.role)}</td><td>${fmtTime(u.created_at)}</td><td><button class="small danger" data-deluser="${u.id}">Remover</button></td></tr>`).join("");
}
$("#userTable").addEventListener("click", async (e) => {
  if (e.target.dataset.deluser && confirm("Remover usuário?")) { await api(`/admin/users/${e.target.dataset.deluser}`, { method: "DELETE" }).catch((x) => alert(x.message)); loadUsers(); }
});
$("#userForm").addEventListener("submit", async (e) => {
  e.preventDefault();
  const f = new FormData(e.target);
  await api("/admin/users", { method: "POST", body: Object.fromEntries(f) }).then(() => e.target.reset()).catch((x) => alert(x.message));
  loadUsers();
});

// --------------------------------------------------------------------- logs
async function loadLogs() {
  const lvl = $("#logLevel").value;
  const lines = await api("/admin/logs?lines=800");
  $("#logs").textContent = lines.filter((l) => !lvl || l.level === lvl).reverse()
    .map((l) => `${l.ts} ${l.level.padEnd(7)} ${l.logger}: ${l.msg}${Object.entries(l).filter(([k]) => !["ts", "level", "logger", "msg"].includes(k)).map(([k, v]) => ` ${k}=${typeof v === "object" ? JSON.stringify(v) : v}`).join("")}`).join("\n");
}
$("#reloadLogs").onclick = loadLogs;
$("#logLevel").onchange = loadLogs;

async function boot() {
  try {
    const me = await api("/auth/me");
    if (me.role !== "admin") { alert("Requer perfil de administrador"); showLogin(); return; }
    $("#who").textContent = me.username;
    showApp();
    route();
  } catch { showLogin(); }
}
boot();
