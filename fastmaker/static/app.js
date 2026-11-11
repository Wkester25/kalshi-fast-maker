"use strict";
const $ = (s, el = document) => el.querySelector(s);
const $$ = (s, el = document) => [...el.querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const usd = (c, sign = true) => c == null ? "—" : (sign && c > 0 ? "+" : c < 0 ? "−" : "") + "$" + Math.abs(c / 100).toFixed(2);
const cents = (c, d = 2) => c == null ? "—" : (c > 0 ? "+" : c < 0 ? "−" : "") + Math.abs(c).toFixed(d) + "¢";
const cls = (v) => v > 0.0001 ? "pos" : v < -0.0001 ? "neg" : "";
const mmss = (s) => s == null ? "—" : `${Math.floor(s / 60)}:${String(Math.floor(s % 60)).padStart(2, "0")}`;
const hhmm = (ts) => new Date(ts * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit", hour12: false });
const px = (v) => v == null ? "—" : (+v).toFixed(1).replace(/\.0$/, "");

let S = null;               // last /api/state
let formLoaded = false;     // settings form filled from the server (not overwritten while editing)

async function api(path, body) {
  const r = await fetch(path, body === undefined ? {} : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
  if (r.status === 401 && path === "/api/state") { location.href = "/login"; throw new Error("signed out"); }
  let d = {};
  try { d = await r.json(); } catch { /* empty */ }
  if (!r.ok && !d.error) d.error = `HTTP ${r.status}`;
  return d;
}

/* ---------------- theme + tabs */
const root = document.documentElement;
try { const t = localStorage.getItem("fm-theme"); if (t) root.dataset.theme = t; } catch { /* storage off */ }
$("#theme").onclick = () => {
  const dark = root.dataset.theme ? root.dataset.theme === "dark" : matchMedia("(prefers-color-scheme: dark)").matches;
  root.dataset.theme = dark ? "light" : "dark";
  try { localStorage.setItem("fm-theme", root.dataset.theme); } catch { /* storage off */ }
};
function show(v) {
  $$("#tabs button").forEach((b) => b.classList.toggle("on", b.dataset.v === v));
  $$(".view").forEach((x) => x.classList.toggle("on", x.id === "v-" + v));
  try { localStorage.setItem("fm-tab", v); } catch { /* storage off */ }
}
$$("#tabs button").forEach((b) => (b.onclick = () => show(b.dataset.v)));
document.addEventListener("click", (e) => { const g = e.target.closest("[data-go]"); if (g) show(g.dataset.go); });
try { const t = localStorage.getItem("fm-tab"); if (t && $("#v-" + t)) show(t); } catch { /* storage off */ }
$("#logout").onclick = async () => { await api("/logout", {}); location.href = "/login"; };

/* ---------------- password modal */
function confirmPw(title, text, okLabel) {
  return new Promise((resolve) => {
    $("#m-title").textContent = title; $("#m-text").textContent = text; $("#m-ok").textContent = okLabel;
    $("#m-pw").value = ""; $("#m-msg").textContent = "";
    $("#modal").classList.add("on"); setTimeout(() => $("#m-pw").focus(), 30);
    const done = (v) => { $("#modal").classList.remove("on"); $("#m-ok").onclick = $("#m-cancel").onclick = null; $("#m-pw").onkeydown = null; resolve(v); };
    $("#m-ok").onclick = () => done($("#m-pw").value);
    $("#m-cancel").onclick = () => done(null);
    $("#m-pw").onkeydown = (e) => { if (e.key === "Enter") done($("#m-pw").value); if (e.key === "Escape") done(null); };
  });
}

/* ---------------- controls */
$("#trading").onchange = async (e) => {
  const on = e.target.checked;
  let body = { on };
  if (on && S && S.mode === "live") {
    const pw = await confirmPw("Start live trading", "Real orders with real money. Enter your web password to start.", "Start live trading");
    if (pw == null) { e.target.checked = false; return; }
    body.password = pw;
  }
  const r = await api("/api/trading", body);
  if (r.error) { alert(r.error); }
  refresh();
};
$$("#mode-seg button").forEach((b) => (b.onclick = async () => {
  const m = b.dataset.m;
  if (!S || m === S.mode) return;
  let body = { mode: m };
  if (m === "live") {
    if (!S.has_key) { show("keys"); return; }
    const pw = await confirmPw("Switch to live", "Live mode sends real orders. Resting paper bids are cancelled; trading stays as it is now (" + (S.trading ? "ON" : "off") + ").", "Switch to live");
    if (pw == null) return;
    body.password = pw;
  }
  const r = await api("/api/mode", body);
  if (r.error) alert(r.error);
  refresh();
}));
$("#stopall").onclick = async () => {
  if (!confirm("Switch trading off and cancel every resting order on your Kalshi account?")) return;
  await api("/api/stop-all", {}); refresh();
};

/* ---------------- instant switches (rule on/off, markets) */
let toastT = 0;
function toast(text, ok) {
  const t = $("#toast");
  t.textContent = text; t.className = "toast on " + (ok ? "ok" : "err");
  clearTimeout(toastT); toastT = setTimeout(() => (t.className = "toast"), ok ? 2500 : 7000);
}
async function quickSave(body, label) {
  const r = await api("/api/settings", body);
  if (r.ok) toast(label + " - saved", true);
  else toast("Not saved: " + (r.errors || [r.error]).join(" · "), false);
  await refresh();
  return r.ok;
}
async function setRule(g, on) {
  const ok = await quickSave({ rules: { [g]: { enabled: on } } }, `${g === "crypto" ? "Crypto" : "Commodity"} rule ${on ? "on" : "off"}`);
  const card = $(`#rules [data-g="${g}"] input[data-k="enabled"]`);
  if (card && S) card.checked = !!S.settings.rules[g].enabled;
  return ok;
}
for (const g of ["crypto", "commodity"]) {
  const q = $("#q-" + g);
  q.onchange = async () => { q.dataset.busy = "1"; await setRule(g, q.checked); delete q.dataset.busy; };
}

/* ---------------- render */
function render() {
  const s = S;
  // pills
  const fe = $("#p-feed"), ok = s.feed === "connected";
  fe.querySelector(".dot").className = "dot " + (ok ? "good" : s.feed.startsWith("no API") ? "warn" : "bad");
  fe.querySelector("span:last-child").innerHTML = ok ? "Feed <b>live</b>" : esc(s.feed);
  $("#p-lat").innerHTML = `REST <b>${s.rest_ms == null ? "—" : Math.round(s.rest_ms) + " ms"}</b> · clock <b>${s.skew_ms == null ? "—" : (s.skew_ms > 0 ? "+" : "") + Math.round(s.skew_ms) + " ms"}</b>`;
  const pm = $("#p-mode");
  pm.className = "pill" + (s.mode === "live" ? " live" : "");
  pm.innerHTML = `<b>${s.mode === "live" ? "LIVE" : "Paper"}</b>${s.env === "demo" ? " · demo" : ""}`;

  // banners
  $("#b-halt").classList.toggle("on", !!s.halted);
  $("#b-halt").textContent = s.halted ? "Halted: " + s.halted + ". Check the activity log, then switch trading back on." : "";
  $("#b-key").classList.toggle("on", !s.has_key);

  // control strip
  $("#trading").checked = !!s.trading;
  $("#trading-l").textContent = s.trading ? (s.mode === "live" ? "Trading LIVE" : "Paper trading") : "Trading off";
  $$("#mode-seg button").forEach((b) => { b.classList.toggle("on", b.dataset.m === s.mode); b.classList.toggle("live", b.dataset.m === "live"); });
  const rules = s.settings.rules;
  const on = Object.entries(rules).filter(([, r]) => r.enabled).map(([g, r]) => `${g} ${winSummary(r)}`);
  $("#ctl-why").textContent = on.length ? on.join("  ·  ") : "both rules are off - nothing will be bid";
  for (const g of ["crypto", "commodity"]) { const q = $("#q-" + g); if (!q.dataset.busy) q.checked = !!rules[g].enabled; }

  // kpis
  const t = s.today, a = s.all;
  $("#k-pnl").textContent = usd(t.pnl_c); $("#k-pnl").className = "v " + cls(t.pnl_c);
  $("#k-pnl-s").textContent = s.mode === "live" ? `paper today ${usd(s.other_today.pnl_c)}` : `live today ${usd(s.other_today.pnl_c)}`;
  $("#k-pc").textContent = t.contracts ? cents(t.per_contract_c) : "—"; $("#k-pc").className = "v " + cls(t.per_contract_c);
  $("#k-n").textContent = t.trades;
  $("#k-n-s").textContent = `${t.lost} lost · ${t.stopped} stopped`;
  $("#k-risk").textContent = usd(s.open_risk_c, false);
  const lim = s.settings.limits;
  $("#k-risk-s").textContent = `daily loss limit $${lim.daily_loss_usd}`;
  $("#k-all").textContent = usd(a.pnl_c); $("#k-all").className = "v " + cls(a.pnl_c);
  $("#k-all-s").textContent = `${a.trades} trades · ${a.contracts ? cents(a.per_contract_c) : "—"}/contract`;
  $("#k-msgs").textContent = s.msgs.toLocaleString();
  $("#k-up").textContent = "messages · up " + mmss(s.uptime_s / 60).replace(":", "h ") + "m";

  // markets
  const live = s.markets.filter((m) => m.ticker).length;
  $("#mk-sub").textContent = `${live} open windows`;
  $("#mk").innerHTML = s.markets.map((m) => {
    const left = m.left_s, el = left == null ? 0 : Math.min(Math.max(900 - left, 0), 900);
    const fav = m.fav ? `<span class="tag ${m.fav}">${m.fav.toUpperCase()}</span>` : "—";
    let us = "—";
    if (m.held) us = `<span class="tag ${m.side}">holding ${m.held} ${m.side.toUpperCase()}</span>`;
    else if (m.order) us = `<span class="tag mut">bid ${m.order.side.toUpperCase()} ${px(m.order.price)}¢ ×${m.order.qty}</span>`;
    const idx = m.index != null && m.strike != null ? ` <span class="mono" style="color:var(--faint)">${(m.index - m.strike >= 0 ? "+" : "") + (m.index - m.strike).toPrecision(3)}</span>` : "";
    return `<tr class="${m.enabled ? "" : "off"}">
      <td><span class="coin">${esc(m.coin)}</span>${idx}</td>
      <td>${left == null ? "—" : `<span class="bar" title="${mmss(el)} into the window"><i style="width:${(el / 900) * 100}%"></i></span><span class="num">${mmss(left)}</span>`}</td>
      <td class="n">${px(m.yes_bid)} / ${px(m.yes_ask)}</td>
      <td>${fav}</td><td>${us}</td>
      <td class="note">${esc(m.enabled ? (m.note || "") : "switched off")}</td>
      <td class="n ${cls(m.pnl_today_c)}">${m.pnl_today_c ? usd(m.pnl_today_c) : ""}</td></tr>`;
  }).join("");

  // results
  $("#res-sub").textContent = s.results.length ? "" : "";
  $("#res").innerHTML = s.results.length ? s.results.map((r) => `<tr>
      <td class="num">${hhmm(r.ts)}${r.mode !== s.mode ? ` <span class="tag mut">${r.mode}</span>` : ""}</td>
      <td class="coin">${esc(r.coin)}</td><td><span class="tag ${r.side}">${String(r.side).toUpperCase()}</span></td>
      <td class="n">${r.bought}</td><td class="n">${px(r.avg_px)}¢</td>
      <td><span class="tag ${r.outcome}">${r.outcome}</span></td>
      <td class="n ${cls(r.pnl_c)}">${usd(r.pnl_c)}</td></tr>`).join("")
    : `<tr><td colspan="7" class="empty">No finished trades yet.</td></tr>`;

  $("#daily").innerHTML = s.daily.length ? s.daily.map((d) => `<tr><td class="num">${d.day}</td><td class="n">${d.trades}</td><td class="n">${d.contracts}</td>
      <td class="n ${cls(d.pnl_c)}">${usd(d.pnl_c)}</td><td class="n ${cls(d.pnl_c)}">${d.contracts ? cents(d.pnl_c / d.contracts) : "—"}</td></tr>`).join("")
    : `<tr><td colspan="5" class="empty">Nothing yet.</td></tr>`;

  // events
  $("#ev").innerHTML = s.events.length ? s.events.map((e) => `<div class="ev ${esc(e.level)}"><span class="t num">${hhmm(e.ts).slice(0, 5)}</span>
      <span class="m">${e.coin ? `<span class="c">${esc(e.coin)}</span>` : ""}${esc(e.msg)}</span></div>`).join("") : `<div class="empty">Quiet so far.</div>`;

  // keys page connection box
  $("#key-state").textContent = s.has_key ? "saved" : "not set";
  $("#conn").innerHTML = `
    <div class="status-box"><span class="dot ${s.has_key ? "good" : "warn"}"></span><span>${s.has_key ? "API key loaded" : esc(s.key_error || "no API key")}</span></div>
    <div class="status-box"><span class="dot ${ok ? "good" : "bad"}"></span><span>WebSocket: ${esc(s.feed)} · ${s.msgs.toLocaleString()} messages</span></div>
    <div class="status-box"><span class="dot ${s.rest_ms == null ? "" : s.rest_ms < 60 ? "good" : "warn"}"></span><span>REST round trip ${s.rest_ms == null ? "—" : Math.round(s.rest_ms) + " ms"} · clock offset ${s.skew_ms == null ? "—" : Math.round(s.skew_ms) + " ms"}</span></div>
    <div class="status-box"><span class="dot"></span><span>Program version ${esc(s.version || "?")}</span></div>`;

  if (!formLoaded) fillForm(s.settings);
}

/* ---------------- settings form */
const SHARED_FIELDS = [
  ["stop_c", "Market stop: sell if bid under (¢)"], ["stop_ms", "…for this long (ms)"],
  ["pull_drop_c", "Fast pull: mid drop (¢)"], ["pull_ms", "…within (ms)"], ["pull_cooldown_ms", "Pause after a pull (ms)"],
];
const WIN_COLS = [["start_s", "From"], ["end_s", "Until"], ["lo_c", "Bid ≥ ¢"], ["hi_c", "Bid < ¢"], ["contracts", "Qty"]];
const toSecs = (v) => { const t = String(v).trim(); if (t.includes(":")) { const [m, x] = t.split(":"); return (+m || 0) * 60 + (+x || 0); } return +t || 0; };
const winSummary = (r) => r.windows.map((w) => `${mmss(w.start_s)}–${mmss(w.end_s)} ${w.lo_c}–${w.hi_c}¢ ×${w.contracts}`).join(", ");

function winRow(w) {
  return `<div class="win-row">${WIN_COLS.map(([k]) => `<input class="in num" data-w="${k}" value="${k.endsWith("_s") ? mmss(w[k]) : w[k]}" inputmode="${k.endsWith("_s") ? "text" : "decimal"}" aria-label="${k}">`).join("")}
    <button class="icon-btn" data-del title="Remove window" aria-label="Remove window">×</button></div>`;
}
function readWins(card) {
  return $$(".win-row", card).map((row) => Object.fromEntries(WIN_COLS.map(([k]) => {
    const v = $(`[data-w="${k}"]`, row).value; return [k, k.endsWith("_s") ? toSecs(v) : v];
  })));
}
function drawTimeline(card) {
  const wins = readWins(card);
  $(".timeline", card).innerHTML = wins.map((w) => {
    const a = Math.max(0, Math.min(900, w.start_s)), b = Math.max(a, Math.min(900, w.end_s));
    return `<i style="left:${(a / 900) * 100}%;width:${Math.max(((b - a) / 900) * 100, 0.6)}%" title="${mmss(a)}–${mmss(b)}"></i>`;
  }).join("") + [0, 300, 600, 900].map((t) => `<b style="left:${(t / 900) * 100}%">${mmss(t)}</b>`).join("");
  const del = $$("[data-del]", card); del.forEach((d) => (d.disabled = del.length === 1));
}
const LIMIT_FIELDS = [["daily_loss_usd", "Daily loss limit ($)"], ["max_order_usd", "Max per order ($)"],
  ["max_open_crypto", "Max crypto markets at once"], ["max_open_commodity", "Max commodity markets at once"],
  ["stop_after_losses", "Stop for the day after N losses"]];

function fillForm(st) {
  $("#rules").innerHTML = ["crypto", "commodity"].map((g) => {
    const r = st.rules[g];
    return `<div class="card" data-g="${g}">
      <div class="card-h"><h2>${g === "crypto" ? "Crypto rule" : "Commodity rule"}</h2><span class="sub">early favourite maker</span><div class="spacer"></div>
        <label class="sw"><input type="checkbox" data-k="enabled" ${r.enabled ? "checked" : ""}><span class="track"></span></label></div>
      <div class="card-b">
        <div class="f"><label>Trading windows <span class="hint-inline">time into the 15:00 market (m:ss) · favourite's bid band · contracts</span></label></div>
        <div class="timeline"></div>
        <div class="win-head">${WIN_COLS.map(([, l]) => `<span>${l}</span>`).join("")}<span></span></div>
        <div class="wins">${r.windows.map(winRow).join("")}</div>
        <button class="btn sm" data-add style="margin-top:10px">+ Add window</button>
        <div class="sep"></div>
        <div class="fields">
          ${SHARED_FIELDS.map(([k, l]) => `<div class="f"><label>${l}</label><input class="in num" data-k="${k}" value="${r[k]}" inputmode="decimal"></div>`).join("")}
        </div>
        <div class="sep"></div>
        <label class="sw"><input type="checkbox" data-k="pull" ${r.pull ? "checked" : ""}><span class="track"></span><span>Fast pull (cancel the bid when the market drops sharply)</span></label>
        <div class="hint" style="margin-top:8px">The stop and fast pull apply to every window of this rule.</div>
      </div></div>`;
  }).join("");
  $$("#rules [data-g]").forEach((card) => {
    drawTimeline(card);
    card.addEventListener("input", (e) => { if (e.target.matches("[data-w]")) drawTimeline(card); });
    card.addEventListener("click", (e) => {
      if (e.target.closest("[data-del]")) { e.target.closest(".win-row").remove(); drawTimeline(card); dirty(true); }
      if (e.target.closest("[data-add]")) {
        const wins = readWins(card), last = wins[wins.length - 1];
        const start = last ? Math.min(last.end_s + 30, 840) : 540;
        const tpl = last || { lo_c: 97, hi_c: 99.5, contracts: 1 };
        $(".wins", card).insertAdjacentHTML("beforeend", winRow({ start_s: start, end_s: Math.min(start + 60, 899), lo_c: tpl.lo_c, hi_c: tpl.hi_c, contracts: tpl.contracts }));
        drawTimeline(card); dirty(true);
      }
    });
  });
  $$("#rules .card-h input[data-k='enabled']").forEach((i) => (i.onchange = () => setRule(i.closest("[data-g]").dataset.g, i.checked)));
  $("#mkts").innerHTML = Object.entries(st.markets).map(([c, on]) => `<label class="chip ${on ? "on" : ""}"><input type="checkbox" data-c="${c}" ${on ? "checked" : ""}>${c}</label>`).join("");
  $$("#mkts input").forEach((i) => (i.onchange = async () => {
    i.parentElement.classList.toggle("on", i.checked);
    await quickSave({ markets: { [i.dataset.c]: i.checked } }, `${i.dataset.c} ${i.checked ? "on" : "off"}`);
  }));
  $("#limits").innerHTML = LIMIT_FIELDS.map(([k, l]) => `<div class="f"><label>${l}</label><input class="in num" data-l="${k}" value="${st.limits[k]}" inputmode="decimal"></div>`).join("");
  $("#ns-on").checked = !!st.news_skip.enabled;
  $("#ns-times").value = (st.news_skip.times_et || []).join(", ");
  $$("#env-seg button").forEach((b) => b.classList.toggle("on", b.dataset.e === st.env));
  formLoaded = true;
  dirty(false);
}
function dirty(on) {
  const m = $("#save-msg");
  if (on) { m.className = "msg dirty"; m.textContent = "Unsaved changes"; }
  else if (m.classList.contains("dirty")) { m.textContent = ""; m.className = "msg"; }
}
$("#v-settings").addEventListener("input", (e) => {
  if (e.target.matches(".in") || e.target.matches("#ns-on")) dirty(true);
});
$$("#env-seg button").forEach((b) => (b.onclick = () => { $$("#env-seg button").forEach((x) => x.classList.toggle("on", x === b)); dirty(true); }));
$("#revert").onclick = () => { formLoaded = false; if (S) fillForm(S.settings); $("#save-msg").textContent = ""; };
$("#save").onclick = async () => {
  const rules = {};
  $$("#rules [data-g]").forEach((card) => {
    const r = {};
    $$("input[data-k]", card).forEach((i) => (r[i.dataset.k] = i.type === "checkbox" ? i.checked : i.value));
    r.windows = readWins(card);
    rules[card.dataset.g] = r;
  });
  const markets = {}; $$("#mkts input").forEach((i) => (markets[i.dataset.c] = i.checked));
  const limits = {}; $$("#limits input").forEach((i) => (limits[i.dataset.l] = i.value));
  const env = $("#env-seg button.on")?.dataset.e;
  const r = await api("/api/settings", { rules, markets, limits, env, news_skip: { enabled: $("#ns-on").checked, times_et: $("#ns-times").value } });
  const m = $("#save-msg");
  await refresh();
  if (r.ok) { formLoaded = false; fillForm(S.settings); m.className = "msg ok"; m.textContent = "Saved — in effect now."; }
  else { m.className = "msg err"; m.textContent = (r.errors || [r.error]).join(" · "); }   // keep what was typed
};

/* ---------------- keys */
const drop = $("#drop"), file = $("#file");
drop.onclick = () => file.click();
["dragenter", "dragover"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.add("hot"); }));
["dragleave", "drop"].forEach((ev) => drop.addEventListener(ev, (e) => { e.preventDefault(); drop.classList.remove("hot"); }));
drop.addEventListener("drop", (e) => { if (e.dataTransfer.files[0]) readKey(e.dataTransfer.files[0]); });
file.onchange = () => { if (file.files[0]) readKey(file.files[0]); };
async function readKey(f) {
  if (f.size > 20000) { keyMsg("That file is too big to be a private key.", false); return; }
  const t = await f.text();
  if (!t.includes("PRIVATE KEY")) { keyMsg("That file doesn't look like a private key.", false); return; }
  $("#pem").value = t.trim(); drop.textContent = "Loaded " + f.name; keyMsg("", true);
}
function keyMsg(t, ok) { const m = $("#key-msg"); m.className = "msg " + (ok ? "ok" : "err"); m.textContent = t; }
function keyResult(r) {
  if (r.ok) keyMsg(`Working — balance $${r.cash_usd.toFixed(2)}, round trip ${r.rtt_ms} ms.`, true);
  else keyMsg(r.error || "failed", false);
}
$("#key-save").onclick = async () => {
  const b = $("#key-save"); b.disabled = true; keyMsg("Checking…", true);
  const r = await api("/api/keys", { key_id: $("#kid").value, pem: $("#pem").value });
  b.disabled = false; keyResult(r);
  if (r.ok) { $("#pem").value = ""; $("#kid").value = ""; drop.innerHTML = "Drop the key file here, or <u>choose a file</u>"; }
  refresh();
};
$("#key-test").onclick = async () => { keyMsg("Checking…", true); keyResult(await api("/api/keys/test", {})); };
$("#pw-save").onclick = async () => {
  const r = await api("/api/password", { password: $("#pw-old").value, new: $("#pw-new").value });
  const m = $("#pw-msg"); m.className = "msg " + (r.ok ? "ok" : "err"); m.textContent = r.ok ? "Changed." : r.error;
  if (r.ok) { $("#pw-old").value = $("#pw-new").value = ""; }
};

/* ---------------- loop */
async function refresh() {
  try { S = await api("/api/state"); render(); } catch (e) { $("#p-feed").querySelector("span:last-child").textContent = "server unreachable"; }
}
refresh();
setInterval(() => { if (!document.hidden) refresh(); }, 1000);
