/* Quant Terminal — SPA (vanilla JS, sin build) */
"use strict";

const $ = (sel, el = document) => el.querySelector(sel);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const TZ = -new Date().getTimezoneOffset() * 60; // las gráficas muestran hora local
const PRICE_DEC = { BTC: 2, ETH: 2, SOL: 2, SUI: 4 };
const TFS = ["1m", "5m", "15m", "1h", "6h", "1d"];

const S = {
  tab: "home", asset: "SOL-USDC", tf: "6h", data: null, assetData: null,
  selectedTrade: null, polling: null, chart: null, candleSeries: null, volSeries: null,
  priceLines: [], lastBar: null, tfSeconds: 900, candlesTimer: null, equityChart: null,
  portfolio: null, creds: null, events: [], built: {},
};

/* ── utilidades ─────────────────────────────────────────────────────────── */
const base = (pid) => pid.split("-")[0];
function dec(pid, px) { const b = base(pid); if (b in PRICE_DEC) return PRICE_DEC[b]; return px >= 10 ? 2 : px >= 1 ? 4 : 6; }
function fpx(pid, v) { if (v == null || isNaN(v)) return "—"; return Number(v).toLocaleString("es-ES", { minimumFractionDigits: dec(pid, v), maximumFractionDigits: dec(pid, v) }); }
function fusd(v, d = 2) { if (v == null || isNaN(v)) return "—"; return Number(v).toLocaleString("es-ES", { minimumFractionDigits: d, maximumFractionDigits: d }); }
function fsign(v, d = 2) { if (v == null || isNaN(v)) return "—"; return (v >= 0 ? "+" : "") + fusd(v, d); }
function fpct(v, d = 2) { if (v == null || isNaN(v)) return "—"; return (v >= 0 ? "+" : "") + Number(v).toFixed(d) + "%"; }
function cls(v) { return v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : ""; }
function ago(ts) { if (!ts) return "—"; const s = Math.max(0, Date.now() / 1000 - ts); if (s < 60) return `${s | 0}s`; if (s < 3600) return `${(s / 60) | 0}m`; if (s < 86400) return `${(s / 3600) | 0}h`; return `${(s / 86400) | 0}d`; }
function dt(ts) { return ts ? new Date(ts * 1000).toLocaleString("es-ES", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—"; }
function dur(s) { s = s | 0; const h = (s / 3600) | 0, m = ((s % 3600) / 60) | 0; return h ? `${h}h ${m}m` : `${m}m ${s % 60}s`; }
function tfLabel() { const m = (S.data && S.data.telemetry && S.data.telemetry.signal_tf_minutes) || 5; return m >= 60 ? `${m / 60}h` : `${m}m`; }
function css(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function coin(pid, small) { const b = base(pid); const k = ["SOL", "BTC", "ETH", "SUI"].includes(b) ? b : "default"; return `<div class="coin coin-${k}"${small ? "" : ""}>${esc(b.slice(0, 3))}</div>`; }
function regimePill(r) { const m = { TREND: "pill-accent", RANGE: "pill-violet", NOISE: "pill-down", WARMUP: "" }; return `<span class="pill ${m[r] || ""}">${esc(r || "—")}</span>`; }
function obiBar(v) { v = Math.max(-1, Math.min(1, v || 0)); const w = Math.abs(v) * 50; const left = v >= 0 ? 50 : 50 - w; return `<div class="obi"><i style="left:${left}%;width:${w}%;background:${v >= 0 ? "var(--up)" : "var(--down)"}"></i></div>`; }

function toast(msg, ms = 2600) { const t = $("#toast"); t.textContent = msg; t.hidden = false; clearTimeout(toast._t); toast._t = setTimeout(() => (t.hidden = true), ms); }

async function api(path, opts = {}) {
  const res = await fetch(path, { credentials: "same-origin", headers: { "Content-Type": "application/json" }, ...opts });
  if (res.status === 401 && !path.startsWith("/api/login")) { showLogin(); throw new Error("401"); }
  let body = null; try { body = await res.json(); } catch { /* sin cuerpo */ }
  if (!res.ok) throw new Error((body && body.detail) || `HTTP ${res.status}`);
  return body;
}

/* ── sesión ─────────────────────────────────────────────────────────────── */
function showLogin() { stopPolling(); $("#app").hidden = true; $("#login").hidden = false; setTimeout(() => $("#login-pass").focus(), 50); }
function showApp() { $("#login").hidden = true; $("#app").hidden = false; route(); startPolling(); }

$("#login-form").addEventListener("submit", async (e) => {
  e.preventDefault(); $("#login-error").textContent = "";
  const btn = e.target.querySelector("button"); btn.disabled = true;
  try { await api("/api/login", { method: "POST", body: JSON.stringify({ password: $("#login-pass").value }) }); $("#login-pass").value = ""; showApp(); }
  catch (err) { $("#login-error").textContent = err.message === "401" ? "Contraseña incorrecta" : err.message; }
  finally { btn.disabled = false; }
});

/* ── navegación ─────────────────────────────────────────────────────────── */
function route() {
  const h = location.hash.slice(1) || "home";
  const [tab, arg] = h.split("/");
  S.tab = ["home", "asset", "portfolio", "settings"].includes(tab) ? tab : "home";
  if (S.tab === "asset" && arg) S.asset = decodeURIComponent(arg);
  document.querySelectorAll(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === S.tab));
  ["home", "asset", "portfolio", "settings"].forEach((v) => ($(`#view-${v}`).hidden = v !== S.tab));
  if (S.tab !== "asset") destroyChart();
  if (S.equityChart) { S.equityChart.remove(); S.equityChart = null; }
  S.built = {};
  window.scrollTo(0, 0);
  render(true);
}
window.addEventListener("hashchange", route);

function render(full) {
  if (!S.data) return;
  if (!S.built[S.tab]) { S.built[S.tab] = true; full = true; }
  renderTopbar();
  if (S.tab === "home") renderHome(full);
  else if (S.tab === "asset") renderAsset(full);
  else if (S.tab === "portfolio") renderPortfolio(full);
  else if (S.tab === "settings") renderSettings(full);
}

/* ── polling ────────────────────────────────────────────────────────────── */
async function pollState() {
  try {
    S.data = await api("/api/state");
    if (S.tab === "asset") S.assetData = await api(`/api/asset/${encodeURIComponent(S.asset)}`);
    render(false);
    if (S.tab === "asset") applyOverlays();
  } catch (e) { if (e.message !== "401") console.warn(e); }
}
function startPolling() { stopPolling(); pollState(); S.polling = setInterval(() => { if (document.visibilityState === "visible") pollState(); }, 2000); }
function stopPolling() { clearInterval(S.polling); S.polling = null; clearInterval(S.candlesTimer); }
document.addEventListener("visibilitychange", () => { if (document.visibilityState === "visible" && S.polling) pollState(); });

/* ── topbar ─────────────────────────────────────────────────────────────── */
function renderTopbar() {
  const t = S.data.telemetry || {}; const alive = S.data.engine_alive;
  const st = alive ? (t.bot_status || "—") : "OFFLINE";
  const pc = { RUNNING: "pill-up", PAUSED: "pill-warn", SAFE: "pill-warn", KILLED: "pill-down" }[st] || "pill-down";
  const s = $("#tb-status"); s.className = `pill ${pc}`; s.textContent = st;
  const m = $("#tb-mode"); m.textContent = (t.mode || "—") + (t.venue === "PERPS" ? ` · PERPS ${t.max_leverage || 2}×` : ""); m.className = `pill ${t.mode === "LIVE" ? "pill-down" : "pill-ghost"}`;
  $("#tb-ws").textContent = t.ws_latency_ms != null ? `${Math.round(t.ws_latency_ms)}ms` : "—";
  $("#tb-api").textContent = t.api_latency_ms != null ? `API ${Math.round(t.api_latency_ms)}ms` : "";
  $("#tb-dot").className = "dot " + (alive && t.ws_connected ? "ok" : "bad");
  $("#offline-banner").hidden = alive;
}

/* ═════════════════════════════════════════════════════════════════════════
   INICIO
   ═════════════════════════════════════════════════════════════════════════ */
function renderHome(full) {
  const d = S.data, t = d.telemetry || {}, k = d.kpis || {}, kt = d.kpis_today || {};
  const o = t.oracle || {};
  const dayPnl = (t.equity || 0) - (t.day_start_equity || t.equity || 0);
  const el = $("#view-home");
  if (full) {
    if (S.equityChart) { S.equityChart.remove(); S.equityChart = null; }
    el.innerHTML = `
      <div class="home-grid">
        <div class="stack">
          <div class="card"><div id="h-hero"></div><div class="mini-chart" id="h-equity"></div></div>
          <div class="controls" id="h-controls">
            <button class="btn" id="btn-pause">⏸ Pausar</button>
            <button class="btn" id="btn-resume">▶ Reanudar</button>
            <button class="btn panic" id="btn-panic"><i class="fill"></i><span>MANTÉN PULSADO · PANIC KILL-SWITCH</span></button>
          </div>
          <div class="card card-tight" id="h-oracle"></div>
          <div class="section"><div class="section-head"><h2>Rendimiento</h2><span class="faint" style="font-size:12px">neto de comisiones</span></div>
            <div class="grid-3 kpis" id="h-kpis"></div></div>
        </div>
        <div class="stack">
          <div class="section"><div class="section-head"><h2>Mercados</h2><span class="faint" style="font-size:12px">toca para ver la gráfica</span></div>
            <div class="card" style="padding:0" id="h-assets"></div></div>
          <div class="section"><div class="section-head"><h2>Posiciones abiertas</h2><span class="faint" id="h-poscount"></span></div>
            <div id="h-positions" class="stack"></div></div>
        </div>
      </div>`;
    $("#btn-pause").onclick = () => sendCommand("PAUSE");
    $("#btn-resume").onclick = () => sendCommand("RESUME");
    bindPanic($("#btn-panic"));
  }
  $("#h-hero").innerHTML = `
    <div class="row"><span class="hero-label">Equity · ${esc(t.mode || "")}</span><span class="faint" style="font-size:12px">${esc(t.status_detail || "")}</span></div>
    <div class="hero-value">${fusd(t.equity)} <span class="muted" style="font-size:15px;font-weight:500">USDC</span></div>
    <div class="hero-sub"><span class="${cls(dayPnl)}">Hoy ${fsign(dayPnl)}</span><span class="muted">Caja ${fusd(t.cash)}</span>
      <span class="muted">DD diario ${fusd(t.daily_dd_pct)}% / 3%</span></div>`;
  drawEquity();
  $("#h-oracle").innerHTML = `
    <div class="row"><div><div style="font-weight:600">Oráculo BTC</div>
      <div class="faint" style="font-size:12px">Δ5m ${fpct(o.change_5m_pct)} · CVD ${fsign(o.cvd_ratio)} · OBI ${fsign(o.obi)}</div></div>
      ${o.veto ? `<span class="pill pill-down">VETO BAJISTA</span>` : `<span class="pill pill-up">RISK-ON</span>`}</div>
    ${o.veto ? `<div class="notice notice-danger" style="margin-top:10px">${esc(o.reason)} — compras en SOL/ETH/SUI bloqueadas</div>` : ""}`;
  const pf = k.profit_factor;
  $("#h-kpis").innerHTML = [
    stat("PnL neto", fsign(k.net), cls(k.net), `bruto ${fsign(k.gross)}`),
    stat("Comisiones", fusd(k.fees), "", "pagadas"),
    stat("Win rate", k.win_rate != null ? k.win_rate.toFixed(1) + "%" : "—", "", `${k.trades || 0} trades`),
    stat("Profit factor", pf != null ? pf.toFixed(2) : "—", pf != null ? (pf >= 1 ? "up" : "down") : "", ""),
    stat("Drawdown", fusd(k.drawdown_pct) + "%", "", `máx ${fusd(k.max_drawdown_pct)}%`),
    stat("Hoy", fsign(kt.net), cls(kt.net), `${kt.trades || 0} trades`),
  ].join("");
  $("#h-assets").innerHTML = d.pairs.map(assetRow).join("");
  document.querySelectorAll("#h-assets .asset-row").forEach((r) => (r.onclick = () => (location.hash = `asset/${r.dataset.pid}`)));
  $("#h-poscount").textContent = `${d.positions.length}/${t.max_open_positions || 2}`;
  $("#h-positions").innerHTML = d.positions.length ? d.positions.map(positionCard).join("") : `<div class="card empty">Sin posiciones abiertas. El bot entra automáticamente cuando todos los filtros se cumplen.</div>`;
}

function stat(label, value, klass, sub) { return `<div class="stat"><div class="stat-label">${esc(label)}</div><div class="stat-value ${klass}">${value}</div>${sub ? `<div class="stat-sub">${sub}</div>` : ""}</div>`; }

function assetRow(p) {
  const sp = p.spread_pct, ok = sp != null && sp <= p.max_spread_pct;
  return `<div class="asset-row" data-pid="${esc(p.product_id)}">
    ${coin(p.product_id)}
    <div style="min-width:0"><div class="asset-name">${esc(p.base)} <span class="faint" style="font-weight:400;font-size:12px">${esc(p.role || "")}</span></div>
      <div class="asset-meta">${regimePill(p.regime)}<span class="${ok ? "" : "down"}">spread ${sp != null ? sp.toFixed(3) : "—"}%</span>
        <span>${p.fee_filter_ok ? "✅" : "⛔"} ${p.projected_move_pct != null ? p.projected_move_pct.toFixed(2) + "%" : ""}</span></div>
      <div style="margin-top:6px;max-width:180px">${obiBar(p.obi)}</div></div>
    <div class="asset-price"><div class="num">${fpx(p.product_id, p.price)}</div><div class="faint" style="font-size:11px">OBI ${fsign(p.obi)}</div></div></div>`;
}

const isTrend = (p) => p.strategy === "TREND_6H";
function positionCard(p) {
  const trend = isTrend(p), short = p.side === "SELL";
  const top = trend ? (short ? Math.min(p.mark_price, p.entry_price) * 0.92 : Math.max(p.mark_price, p.entry_price) * 1.08) : p.take_profit;
  const pid = p.product_id;
  const lo = Math.min(p.stop_price, p.mark_price, top), hi = Math.max(p.stop_price, top, p.mark_price);
  const pos = (v) => Math.max(0, Math.min(100, ((v - lo) / (hi - lo || 1)) * 100));
  return `<div class="card"><div class="pos-head"><div class="row" style="gap:10px">${coin(pid)}<div><div style="font-weight:600">${esc(base(pid))} · ${p.side === "SELL" ? `<span class="down">CORTO</span>` : `<span class="up">LARGO</span>`}</div>
      <div class="faint" style="font-size:12px">${esc(p.strategy)} · ${dt(p.opened_at)}</div></div></div>
      <div style="text-align:right"><div class="pos-pnl ${cls(p.unrealized_net)}">${fsign(p.unrealized_net)}</div><div class="faint" style="font-size:11px">PnL neto flotante</div></div></div>
    <div class="range"><b style="left:${pos(p.stop_price)}%;background:var(--down)"></b><b style="left:${pos(p.entry_price)}%;background:var(--accent)"></b>
      <b style="left:${pos(p.mark_price)}%;background:var(--text)"></b>${trend ? "" : `<b style="left:${pos(p.take_profit)}%;background:var(--up)"></b>`}
      <em style="left:${pos(p.stop_price)}%">${trend ? "Trailing" : "SL"}</em>${Math.min(Math.abs(pos(p.entry_price) - pos(p.stop_price)), Math.abs(pos(top) - pos(p.entry_price))) > 18 ? `<em style="left:${pos(p.entry_price)}%">Entrada</em>` : ""}${trend ? "" : `<em style="left:${pos(p.take_profit)}%">TP</em>`}</div>
    <div class="kv"><span>Entrada · Tamaño</span><span>${fpx(pid, p.entry_price)} · ${Number(p.size).toPrecision(6)}</span></div>
    <div class="kv"><span>Precio actual</span><span>${fpx(pid, p.mark_price)}</span></div>
    <div class="kv"><span>${trend ? "Stop (trailing)" : "Stop Loss"}</span><span class="down">${fpx(pid, p.stop_price)} (${fpct(short ? p.dist_sl_pct : -p.dist_sl_pct)})</span></div>
    ${trend ? `<div class="kv"><span>Salida</span><span>trailing por ATR (6h) o cierre fuera del canal</span></div>`
      : `<div class="kv"><span>Take Profit</span><span class="up">${fpx(pid, p.take_profit)} (${fpct(p.dist_tp_pct)})</span></div>`}
    <div class="kv"><span>Breakeven (fees)</span><span>${fpx(pid, p.breakeven_price)} ${p.breakeven_armed ? "🛡️" : ""}</span></div></div>`;
}

function drawEquity() {
  const box = $("#h-equity"); if (!box || !window.LightweightCharts) return;
  const pts = (S.data.equity || []).map((r) => ({ time: Math.floor(r.t) + TZ, value: r.v }));
  const uniq = []; let last = -1; for (const p of pts) { if (p.time > last) { uniq.push(p); last = p.time; } }
  if (!uniq.length) { if (!S.equityChart) box.innerHTML = `<div class="faint" style="font-size:12px;text-align:center;padding-top:44px">La curva aparece tras el primer muestreo</div>`; return; }
  if (!S.equityChart) {
    box.innerHTML = "";
    S.equityChart = LightweightCharts.createChart(box, {
      autoSize: true, layout: { background: { type: "solid", color: "transparent" }, textColor: css("--faint"), fontFamily: "Inter" },
      grid: { vertLines: { visible: false }, horzLines: { visible: false } }, rightPriceScale: { visible: false }, leftPriceScale: { visible: false },
      timeScale: { visible: false }, crosshair: { horzLine: { visible: false }, vertLine: { visible: false } }, handleScroll: false, handleScale: false,
    });
    S.equityN = -1;
    S.equitySeries = S.equityChart.addAreaSeries({ lineColor: css("--accent"), topColor: "rgba(59,130,246,.28)", bottomColor: "rgba(59,130,246,0)", lineWidth: 2, priceLineVisible: false, lastValueVisible: false });
  }
  const n = uniq.length;
  if (n !== S.equityN) { S.equitySeries.setData(uniq); S.equityChart.timeScale().fitContent(); S.equityN = n; }
}

/* ── PANIC: mantener pulsado 1.5 s ──────────────────────────────────────── */
function bindPanic(btn) {
  let raf = null, t0 = 0; const HOLD = 1500; const fill = btn.querySelector(".fill");
  const reset = () => { cancelAnimationFrame(raf); raf = null; fill.style.width = "0"; btn.classList.remove("holding"); };
  const tick = () => { const p = Math.min(1, (performance.now() - t0) / HOLD); fill.style.width = p * 100 + "%";
    if (p >= 1) { reset(); if (navigator.vibrate) navigator.vibrate(80); sendCommand("PANIC", {}, "PANIC"); return; } raf = requestAnimationFrame(tick); };
  const start = (e) => { e.preventDefault(); t0 = performance.now(); btn.classList.add("holding"); raf = requestAnimationFrame(tick); };
  btn.addEventListener("pointerdown", start); ["pointerup", "pointerleave", "pointercancel"].forEach((ev) => btn.addEventListener(ev, reset));
  btn.addEventListener("contextmenu", (e) => e.preventDefault());
}

async function sendCommand(command, payload = {}, confirm = "") {
  try {
    const r = await api("/api/command", { method: "POST", body: JSON.stringify({ command, payload, confirm }) });
    toast(`${command} enviado…`);
    for (let i = 0; i < 40; i++) {
      await new Promise((ok) => setTimeout(ok, 500));
      const c = await api(`/api/commands/${r.id}`);
      if (c.processed_at) { toast(c.result || "Hecho", 4500); pollState(); return c; }
    }
    toast("El motor no ha confirmado todavía el comando");
  } catch (e) { toast("Error: " + e.message, 4000); }
}

/* ═════════════════════════════════════════════════════════════════════════
   ACTIVOS
   ═════════════════════════════════════════════════════════════════════════ */
function renderAsset(full) {
  const el = $("#view-asset"); const d = S.data;
  if (!d.pairs.find((p) => p.product_id === S.asset)) S.asset = d.pairs[0].product_id;
  const p = d.pairs.find((x) => x.product_id === S.asset);
  if (full || el.dataset.asset !== S.asset) {
    S.built.asset = true;
    el.dataset.asset = S.asset; S.selectedTrade = null; S.assetData = null;
    el.innerHTML = `
      <div class="stack">
        <div class="chips">${d.pairs.map((x) => `<button class="chip ${x.product_id === S.asset ? "active" : ""}" data-pid="${esc(x.product_id)}">${coin(x.product_id)}${esc(x.base)}</button>`).join("")}</div>
        <div class="asset-grid">
          <div class="stack">
            <div class="asset-head"><div><div class="muted" style="font-size:13px">${esc(p.product_id)} · ${esc(p.role || "")}</div>
              <div class="price" id="a-price">—</div><div id="a-sub" style="font-size:13px"></div></div>
              <a class="btn btn-sm btn-ghost" target="_blank" rel="noopener" href="https://www.tradingview.com/chart/?symbol=COINBASE:${esc(base(S.asset))}USDC">TradingView ↗</a></div>
            <div class="tf-bar">${TFS.map((t) => `<button class="tf ${t === S.tf ? "active" : ""}" data-tf="${t}">${t}</button>`).join("")}</div>
            <div class="card chart-card" style="position:relative"><div class="chart" id="chart"></div><div class="chart-status" id="chart-status">Cargando velas de Coinbase…</div>
              <div class="chart-legend"><span><i style="background:var(--accent)"></i>Entrada</span><span><i style="background:var(--down)"></i>Stop Loss</span>
                <span><i style="background:var(--up)"></i>Take Profit</span><span><i style="background:var(--warn)"></i>Breakeven</span><span>▲ largo · ▼ corto / salida</span></div></div>
            <div id="a-position"></div>
          </div>
          <div class="stack">
            <div class="card card-tight" id="a-metrics"></div>
            <div class="section"><div class="section-head"><h2>Rendimiento ${esc(p.base)}</h2></div><div class="grid-3" id="a-kpis"></div></div>
            <div class="section"><div class="section-head"><h2>Historial ${esc(p.base)}</h2><span class="faint" id="a-count" style="font-size:12px"></span></div>
              <div class="card" style="padding:0" id="a-trades"></div></div>
          </div>
        </div>
      </div>`;
    el.querySelectorAll(".chip").forEach((c) => (c.onclick = () => (location.hash = `asset/${c.dataset.pid}`)));
    el.querySelectorAll(".tf").forEach((b) => (b.onclick = () => { S.tf = b.dataset.tf; el.querySelectorAll(".tf").forEach((x) => x.classList.toggle("active", x === b)); loadCandles(true); }));
    destroyChart(); initChart(); loadCandles(true);
    clearInterval(S.candlesTimer); S.candlesTimer = setInterval(() => { if (document.visibilityState === "visible" && S.tab === "asset") loadCandles(false); }, 5000);
    api(`/api/asset/${encodeURIComponent(S.asset)}`).then((a) => { S.assetData = a; renderAssetDetail(p); applyOverlays(); }).catch(() => {});
  }
  renderAssetDetail(p);
  liveTick(p.price);
}

function renderAssetDetail(p) {
  const pid = p.product_id;
  $("#a-price").textContent = fpx(pid, p.price);
  $("#a-sub").innerHTML = `${regimePill(p.regime)} <span class="muted">bid ${fpx(pid, p.best_bid)} · ask ${fpx(pid, p.best_ask)}</span>`;
  const sp = p.spread_pct;
  $("#a-metrics").innerHTML = `
    <div class="kv"><span>Spread / máx</span><span class="${sp != null && sp <= p.max_spread_pct ? "up" : "down"}">${sp != null ? sp.toFixed(3) : "—"}% / ${p.max_spread_pct}%</span></div>
    <div class="kv"><span>OBI L2 (±0.10%)</span><span>${fsign(p.obi)}</span></div><div style="padding:2px 0 8px">${obiBar(p.obi)}</div>
    <div class="kv"><span>ATR (régimen 5m)</span><span>${fpx(pid, p.atr)} (${p.atr_pct != null ? p.atr_pct.toFixed(2) : "—"}%)</span></div>
    <div class="kv"><span>Hurst · ADX (5m)</span><span>${p.hurst != null ? p.hurst.toFixed(2) : "—"} · ${p.adx != null ? p.adx.toFixed(1) : "—"}</span></div>
    <div class="kv"><span>VWAP sesión</span><span>${fpx(pid, p.vwap)}</span></div>
    <div class="kv"><span>CVD 5m</span><span class="${cls(p.cvd_5m)}">${fsign(p.cvd_5m, 3)}</span></div>
    ${p.trend_entry_level != null ? `
    <div class="kv"><span>Nivel de ruptura (máx. 20×6h)</span><span>${fpx(pid, p.trend_entry_level)} (${fpct((p.trend_entry_level - p.price) / p.price * 100)})</span></div>
    <div class="kv"><span>EMA 200 (6h)</span><span class="${p.price > p.trend_ema ? "up" : "down"}">${fpx(pid, p.trend_ema)}</span></div>
    <div class="kv"><span>Salida de canal (mín. 20×6h)</span><span>${fpx(pid, p.trend_exit_level)}</span></div>
    <div class="kv"><span>ATR (6h)</span><span>${fpx(pid, p.trend_atr)}</span></div>` : `
    <div class="kv"><span>Recorrido proyectado</span><span>${p.projected_move_pct != null ? p.projected_move_pct.toFixed(2) + "%" : "—"}</span></div>
    <div class="kv"><span>Filtro comisiones</span><span class="${p.fee_filter_ok ? "up" : "down"}">${p.fee_filter_ok ? "PASA" : "BLOQUEA"}</span></div>`}
    <div class="kv"><span>Asignación máx</span><span>${(p.max_allocation * 100).toFixed(0)}%</span></div>
    <div class="notice" style="margin-top:8px">${esc(p.signal || "—")}</div>`;
  const a = S.assetData;
  if (!a) return;
  const k = a.kpis;
  $("#a-kpis").innerHTML = [stat("Neto", fsign(k.net), cls(k.net), `${k.trades} trades`),
    stat("Win rate", k.win_rate != null ? k.win_rate.toFixed(0) + "%" : "—", "", ""),
    stat("Fees", fusd(k.fees), "", "")].join("");
  $("#a-position").innerHTML = a.position ? positionCard(a.position) : "";
  $("#a-count").textContent = a.trades.length ? `${a.trades.length} operaciones` : "";
  $("#a-trades").innerHTML = a.trades.length ? a.trades.map((t) => `
    <div class="trade ${S.selectedTrade === t.trade_id ? "selected" : ""}" data-id="${esc(t.trade_id)}">
      <div><div class="trade-title">${t.net_pnl >= 0 ? "✅" : "🔴"} ${t.side === "SELL" ? `<span class="pill pill-warn">CORTO</span>` : `<span class="pill pill-accent">LARGO</span>`} ${esc(t.exit_reason)}</div>
        <div class="trade-sub">${dt(t.opened_at)} → ${dt(t.closed_at)} · ${dur(t.duration_s)}</div>
        <div class="trade-sub mono">${fpx(pid, t.entry_price)} → ${fpx(pid, t.exit_price)} · fees ${fusd(t.fees)}</div></div>
      <div class="trade-pnl ${cls(t.net_pnl)}">${fsign(t.net_pnl)}<div style="font-size:12px">${fpct(t.net_pct)}</div></div></div>`).join("")
    : `<div class="empty">Aún no hay operaciones cerradas en ${esc(base(pid))}.</div>`;
  document.querySelectorAll("#a-trades .trade").forEach((r) => (r.onclick = () => selectTrade(r.dataset.id)));
}

/* ── gráfica ────────────────────────────────────────────────────────────── */
function initChart() {
  const box = $("#chart"); if (!box || !window.LightweightCharts) { setTimeout(() => S.tab === "asset" && !S.chart && initChart(), 300); return; }
  const pid = S.asset, d = dec(pid, 1);
  S.chart = LightweightCharts.createChart(box, {
    autoSize: true,
    layout: { background: { type: "solid", color: css("--surface") }, textColor: css("--muted"), fontFamily: "Inter", fontSize: 11 },
    grid: { vertLines: { color: css("--chart-grid") }, horzLines: { color: css("--chart-grid") } },
    rightPriceScale: { borderColor: css("--border"), scaleMargins: { top: 0.08, bottom: 0.22 } },
    timeScale: { borderColor: css("--border"), timeVisible: true, secondsVisible: false, rightOffset: 6 },
    crosshair: { mode: 1 }, localization: { locale: "es-ES" },
  });
  S.candleSeries = S.chart.addCandlestickSeries({
    upColor: css("--up"), downColor: css("--down"), wickUpColor: css("--up"), wickDownColor: css("--down"), borderVisible: false,
    priceFormat: { type: "price", precision: d, minMove: Math.pow(10, -d) },
  });
  S.volSeries = S.chart.addHistogramSeries({ priceFormat: { type: "volume" }, priceScaleId: "", lastValueVisible: false, priceLineVisible: false });
  S.volSeries.priceScale().applyOptions({ scaleMargins: { top: 0.82, bottom: 0 } });
}
function destroyChart() { clearInterval(S.candlesTimer); if (S.chart) { S.chart.remove(); } S.chart = null; S.candleSeries = null; S.volSeries = null; S.priceLines = []; S.lastBar = null; }

async function loadCandles(reset) {
  if (!S.chart) { initChart(); if (!S.chart) return; }
  const pid = S.asset, tf = S.tf;
  try {
    const r = await api(`/api/candles/${encodeURIComponent(pid)}?tf=${tf}`);
    if (pid !== S.asset || tf !== S.tf || !S.candleSeries) return;
    S.tfSeconds = r.seconds;
    const bars = r.candles.map((c) => ({ time: c.time + TZ, open: c.open, high: c.high, low: c.low, close: c.close }));
    const vols = r.candles.map((c) => ({ time: c.time + TZ, value: c.volume, color: c.close >= c.open ? "rgba(22,199,132,.35)" : "rgba(234,57,67,.35)" }));
    if (reset || !S.lastBar) { S.candleSeries.setData(bars); S.volSeries.setData(vols); S.chart.timeScale().fitContent(); S.chart.timeScale().scrollToRealTime(); }
    else { for (const b of bars) if (b.time >= S.lastBar.time) S.candleSeries.update(b); for (const v of vols) if (v.time >= S.lastBar.time) S.volSeries.update(v); }
    S.lastBar = bars.length ? { ...bars[bars.length - 1] } : null;
    $("#chart-status").hidden = bars.length > 0;
    if (!bars.length) $("#chart-status").textContent = "Sin velas";
    applyOverlays();
  } catch (e) { if (e.message !== "401") { $("#chart-status").hidden = false; $("#chart-status").textContent = "No se pudieron cargar las velas (" + e.message + ")"; } }
}

function liveTick(price) {
  if (!S.candleSeries || !S.lastBar || price == null) return;
  const now = Math.floor(Date.now() / 1000) + TZ, start = Math.floor(now / S.tfSeconds) * S.tfSeconds;
  if (start > S.lastBar.time) S.lastBar = { time: start, open: S.lastBar.close, high: price, low: price, close: price };
  else S.lastBar = { ...S.lastBar, high: Math.max(S.lastBar.high, price), low: Math.min(S.lastBar.low, price), close: price };
  S.candleSeries.update(S.lastBar);
}

const barTime = (ts) => Math.floor((ts + TZ) / S.tfSeconds) * S.tfSeconds;

function applyOverlays() {
  if (!S.candleSeries) return;
  const a = S.assetData, pid = S.asset;
  S.priceLines.forEach((l) => S.candleSeries.removePriceLine(l)); S.priceLines = [];
  const line = (price, color, title, style = 2, width = 1) => { if (price == null) return; S.priceLines.push(S.candleSeries.createPriceLine({ price, color, lineWidth: width, lineStyle: style, axisLabelVisible: true, title })); };
  const markers = [];
  if (a) {
    for (const t of a.trades) {
      const sh = t.side === "SELL";
      markers.push({ time: barTime(t.opened_at), position: sh ? "aboveBar" : "belowBar", color: sh ? css("--warn") : css("--accent"), shape: sh ? "arrowDown" : "arrowUp", text: sh ? "Corto" : "" });
      markers.push({ time: barTime(t.closed_at), position: sh ? "belowBar" : "aboveBar", color: t.net_pnl >= 0 ? css("--up") : css("--down"), shape: sh ? "arrowUp" : "arrowDown", text: fpct(t.net_pct, 1) });
    }
    const p = a.position;
    if (p) {
      const psh = p.side === "SELL";
      markers.push({ time: barTime(p.opened_at), position: psh ? "aboveBar" : "belowBar", color: psh ? css("--warn") : css("--accent"), shape: psh ? "arrowDown" : "arrowUp", text: psh ? "Corto" : "Largo" });
      line(p.entry_price, css("--accent"), "Entrada", 0, 2);
      if (isTrend(p)) {
        line(p.stop_price, css("--down"), "Trailing ATR", 0, 2);
      } else {
        line(p.stop_price, css("--down"), p.breakeven_armed ? "SL (trailing)" : "SL", 0, 2);
        (p.tp_levels || []).forEach((lv, i) => line(lv.price, css("--up"), `TP${i + 1} ${(lv.fraction * 100).toFixed(0)}%${lv.filled ? " ✓" : ""}`));
        if (!p.tp_levels || !p.tp_levels.length) line(p.take_profit, css("--up"), "TP");
        line(p.breakeven_price, css("--warn"), "BE", 1);
      }
    }
    const pr = S.data && S.data.pairs.find((x) => x.product_id === pid);
    if (pr && pr.trend_entry_level != null && !p) {
      line(pr.trend_entry_level, css("--accent"), "Ruptura 6h", 3);
      line(pr.trend_ema, css("--violet"), "EMA200", 3);
    }
    if (pr && pr.trend_exit_level != null && p && isTrend(p)) line(pr.trend_exit_level, css("--warn"), "Salida canal", 3);
    const sel = a.trades.find((t) => t.trade_id === S.selectedTrade);
    if (sel) {
      line(sel.entry_price, css("--accent"), "Entrada", 1);
      line(sel.exit_price, sel.net_pnl >= 0 ? css("--up") : css("--down"), "Salida", 1);
      if (sel.initial_stop != null) line(sel.initial_stop, css("--down"), "SL", 1);
      if (sel.take_profit != null && sel.strategy !== "TREND_6H") line(sel.take_profit, css("--up"), "TP", 1);
    }
  }
  markers.sort((x, y) => x.time - y.time);
  S.candleSeries.setMarkers(markers);
}

function selectTrade(id) {
  S.selectedTrade = S.selectedTrade === id ? null : id;
  const t = S.assetData && S.assetData.trades.find((x) => x.trade_id === id);
  document.querySelectorAll("#a-trades .trade").forEach((r) => r.classList.toggle("selected", r.dataset.id === S.selectedTrade));
  applyOverlays();
  if (t && S.selectedTrade && S.chart) {
    const pad = S.tfSeconds * 30;
    try { S.chart.timeScale().setVisibleRange({ from: barTime(t.opened_at) - pad, to: barTime(t.closed_at) + pad }); } catch { /* fuera de rango */ }
    $("#chart").scrollIntoView({ behavior: "smooth", block: "center" });
  }
}

/* ═════════════════════════════════════════════════════════════════════════
   PORTAFOLIO
   ═════════════════════════════════════════════════════════════════════════ */
async function renderPortfolio(full) {
  const el = $("#view-portfolio");
  if (!full) return;   // el portafolio se refresca bajo demanda (botón)
  el.innerHTML = `<div class="stack"><div class="section-head"><h2>Portafolio Coinbase</h2><button class="btn btn-sm" id="pf-refresh">Actualizar</button></div><div id="pf-body"><div class="card empty"><div class="spinner" style="margin:auto"></div></div></div></div>`;
  $("#pf-refresh").onclick = () => loadPortfolio(true);
  loadPortfolio(false);
}
async function loadPortfolio(refresh) {
  const box = $("#pf-body"); if (!box) return;
  try {
    const p = await api(`/api/portfolio${refresh ? "?refresh=true" : ""}`);
    if (!p.configured) {
      box.innerHTML = `<div class="card empty">🔑 Conecta tu API de Coinbase para detectar tu portafolio automáticamente.<br><a class="btn btn-primary" href="#settings">Conectar API</a></div>`;
      return;
    }
    const perm = p.permissions || {};
    box.innerHTML = `
      <div class="card"><div class="hero-label">${esc(p.portfolio_name)} · ${esc(p.portfolio_type || "")}</div>
        <div class="hero-value">$${fusd(p.total_usd)}</div>
        <div class="hero-sub"><span class="muted">Efectivo $${fusd(p.cash_usd)}</span><span class="muted">${p.positions.length} activos</span><span class="faint">actualizado hace ${ago(p.fetched_at)}</span></div>
        <div class="row" style="justify-content:flex-start;gap:6px;margin-top:10px">
          <span class="pill ${perm.can_view ? "pill-up" : "pill-down"}">Ver</span><span class="pill ${perm.can_trade ? "pill-up" : "pill-down"}">Operar</span>
          <span class="pill ${perm.can_transfer ? "pill-down" : "pill-ghost"}">Transferir ${perm.can_transfer ? "⚠️" : "✕"}</span></div></div>
      <div class="card" style="padding:0">${p.positions.length ? p.positions.map((x) => `
        <div class="asset-row" style="cursor:default">${coin(x.asset + "-USD")}
          <div style="min-width:0"><div class="asset-name">${esc(x.asset)} ${x.is_cash ? `<span class="pill">efectivo</span>` : ""}</div>
            <div class="asset-meta mono">${Number(x.balance).toLocaleString("es-ES", { maximumFractionDigits: 8 })}</div>
            <div class="alloc"><i style="width:${Math.min(100, x.allocation * 100)}%"></i></div></div>
          <div class="asset-price"><div class="num">$${fusd(x.value_usd)}</div>
            <div style="font-size:11px" class="${cls(x.unrealized_pnl)}">${x.is_cash ? (x.allocation * 100).toFixed(1) + "%" : fsign(x.unrealized_pnl) + " PnL"}</div></div></div>`).join("")
        : `<div class="empty">El portafolio está vacío.</div>`}</div>`;
  } catch (e) { if (e.message !== "401") box.innerHTML = `<div class="card notice notice-danger">${esc(e.message)}</div>`; }
}

/* ═════════════════════════════════════════════════════════════════════════
   AJUSTES — sesión de API, modo, eventos
   ═════════════════════════════════════════════════════════════════════════ */
async function renderSettings(full) {
  const el = $("#view-settings"); const t = S.data.telemetry || {};
  if (full) {
    el.innerHTML = `<div class="stack">
      <div class="section"><div class="section-head"><h2>API de Coinbase</h2></div><div class="card" id="st-api"><div class="spinner"></div></div></div>
      <div class="section"><div class="section-head"><h2>Modo de ejecución</h2></div><div class="card stack" id="st-mode"></div></div>
      <div class="section"><div class="section-head"><h2>Actividad del motor</h2><button class="btn btn-sm" id="st-ev-refresh">Actualizar</button></div><div class="card card-tight" id="st-events"></div></div>
      <button class="btn btn-ghost btn-block" id="st-logout">Cerrar sesión</button></div>`;
    $("#st-logout").onclick = async () => { await api("/api/logout", { method: "POST" }); showLogin(); };
    $("#st-ev-refresh").onclick = loadEvents;
    loadCreds(); loadEvents();
  }
  const m = $("#st-mode");
  if (m) {
    const live = t.mode === "LIVE";
    m.innerHTML = `
      <div class="segmented"><button class="${!live ? "active" : ""}" data-m="PAPER">PAPER (simulado)</button><button class="${live ? "active live" : ""}" data-m="LIVE">LIVE (dinero real)</button></div>
      <div class="notice ${live ? "notice-danger" : ""}">${live ? "El bot está enviando órdenes reales a Coinbase." : "El bot opera automáticamente con datos reales y un simulador de ejecución (sin dinero real)."}</div>
      ${!t.live_allowed ? `<div class="notice notice-warn">LIVE está bloqueado en el servidor: define <b>ALLOW_LIVE_TRADING=true</b> en Railway → Variables para habilitarlo.</div>` : ""}
      <div class="kv"><span>Credenciales en uso</span><span>${esc(t.credentials_source || "ninguna")}</span></div>`;
    m.querySelectorAll("button[data-m]").forEach((b) => (b.onclick = () => { if (b.dataset.m !== t.mode) confirmMode(b.dataset.m); }));
  }
}

async function loadCreds() {
  const box = $("#st-api"); if (!box) return;
  try { S.creds = await api("/api/credentials"); } catch (e) { box.innerHTML = `<div class="notice notice-danger">${esc(e.message)}</div>`; return; }
  const c = S.creds;
  if (!c.encryption_ready) { box.innerHTML = `<div class="notice notice-warn">El servidor no tiene <b>CREDENTIALS_KEY</b>; no se pueden guardar claves de forma cifrada.</div>`; return; }
  if (c.configured) {
    const p = c.permissions || {};
    box.innerHTML = `<div class="notice notice-ok" style="margin-bottom:10px">✅ API conectada y guardada (cifrada)</div>
      <div class="kv"><span>API Key</span><span style="font-size:12px">${esc(c.masked_key)}</span></div>
      <div class="kv"><span>Guardada</span><span>${dt(c.saved_at)}</span></div>
      <div class="kv"><span>Permisos</span><span>${p.can_view ? "Ver" : ""}${p.can_trade ? " · Operar" : ""}</span></div>
      <div class="kv"><span>Portafolio</span><span>${esc(p.portfolio_type || "—")}</span></div>
      <div class="grid-2" style="margin-top:12px"><a class="btn" href="#portfolio">Ver portafolio</a><button class="btn" id="api-replace">Reemplazar</button></div>
      <button class="btn btn-ghost btn-block down" id="api-delete" style="margin-top:8px;color:var(--down)">Eliminar clave</button>`;
    $("#api-replace").onclick = () => credsForm(box, true);
    $("#api-delete").onclick = async () => { if (!confirm("¿Eliminar la clave API guardada?")) return; await api("/api/credentials", { method: "DELETE" }); toast("Clave eliminada"); loadCreds(); };
  } else credsForm(box, false);
}

function credsForm(box, replacing) {
  box.innerHTML = `
    <form id="api-form" class="stack" autocomplete="off">
      <div class="notice">Crea la clave en <a href="https://portal.cdp.coinbase.com/projects/api-keys" target="_blank" rel="noopener">Coinbase Developer Platform</a>:
        <ol class="steps"><li>Algoritmo de firma: <b>ECDSA</b></li><li>Permisos: <b>View</b> + <b>Trade</b> (nunca Transfer)</li><li>Copia el <i>API key name</i> y la <i>Private key</i> completa</li></ol></div>
      <div><label for="api-key">API Key name</label><input id="api-key" placeholder="organizations/…/apiKeys/…" required spellcheck="false" autocapitalize="off"></div>
      <div><label for="api-secret">Private key</label><textarea id="api-secret" placeholder="-----BEGIN EC PRIVATE KEY-----&#10;…&#10;-----END EC PRIVATE KEY-----" required spellcheck="false" autocapitalize="off"></textarea></div>
      <button class="btn btn-primary btn-block" type="submit">Validar y guardar</button>
      ${replacing ? `<button class="btn btn-ghost btn-block" type="button" id="api-cancel">Cancelar</button>` : ""}
      <div id="api-result"></div>
      <p class="faint" style="font-size:12px;margin:0">La clave se valida contra Coinbase, se cifra en el servidor y nunca vuelve a mostrarse completa.</p></form>`;
  if (replacing) $("#api-cancel").onclick = loadCreds;
  $("#api-form").onsubmit = async (e) => {
    e.preventDefault(); const btn = e.target.querySelector("button[type=submit]"); const out = $("#api-result");
    btn.disabled = true; btn.innerHTML = `<span class="spinner"></span> Validando con Coinbase…`; out.innerHTML = "";
    try {
      const r = await api("/api/credentials", { method: "POST", body: JSON.stringify({ api_key: $("#api-key").value, api_secret: $("#api-secret").value }) });
      const pf = r.portfolio || {};
      toast("API guardada ✓");
      out.innerHTML = pf.error ? `<div class="notice notice-warn">${esc(pf.error)}</div>` :
        `<div class="notice notice-ok">Portafolio detectado: <b>${esc(pf.portfolio_name)}</b> · $${fusd(pf.total_usd)} en ${pf.positions.length} activos</div>`;
      setTimeout(() => { loadCreds(); }, 2500);
    } catch (err) { out.innerHTML = `<div class="notice notice-danger">${esc(err.message)}</div>`; }
    finally { btn.disabled = false; btn.textContent = "Validar y guardar"; }
  };
}

function confirmMode(mode) {
  const live = mode === "LIVE";
  openSheet(`<h3 style="margin-bottom:8px">Cambiar a ${mode}</h3>
    <p class="muted" style="font-size:14px">${live ? "El bot enviará <b>órdenes reales</b> a Coinbase con tu saldo. Requiere la cuenta del bot sin posiciones abiertas." : "El bot dejará de operar con dinero real. Requiere no tener posiciones abiertas."}</p>
    ${live ? `<label for="sh-confirm">Escribe LIVE para confirmar</label><input id="sh-confirm" autocapitalize="characters" autocomplete="off">` : ""}
    <div class="grid-2" style="margin-top:14px"><button class="btn" id="sh-cancel">Cancelar</button><button class="btn ${live ? "btn-danger" : "btn-primary"}" id="sh-ok">Confirmar</button></div>`);
  $("#sh-cancel").onclick = closeSheet;
  $("#sh-ok").onclick = async () => { const conf = live ? ($("#sh-confirm").value || "").trim().toUpperCase() : ""; if (live && conf !== "LIVE") { toast("Escribe LIVE"); return; } closeSheet(); await sendCommand("SET_MODE", { mode }, conf); if (S.tab === "settings") renderSettings(false); };
}

async function loadEvents() {
  const box = $("#st-events"); if (!box) return;
  try {
    const ev = await api("/api/events?limit=50");
    const color = { CRITICAL: "down", ERROR: "down", WARN: "warn", WARNING: "warn" };
    box.innerHTML = ev.length ? ev.map((e) => `<div class="event"><span class="faint mono">${dt(e.ts)}</span><span><b class="${color[e.level] || ""}">${esc(e.category)}</b> ${esc(e.message)}</span></div>`).join("") : `<div class="empty">Sin eventos</div>`;
  } catch { /* */ }
}

function openSheet(html) { $("#sheet").innerHTML = html; $("#sheet").hidden = false; $("#sheet-backdrop").hidden = false; }
function closeSheet() { $("#sheet").hidden = true; $("#sheet-backdrop").hidden = true; }
$("#sheet-backdrop").onclick = closeSheet;

/* ── arranque ───────────────────────────────────────────────────────────── */
(async () => {
  try { const m = await api("/api/me"); if (m.authenticated) showApp(); else showLogin(); } catch { showLogin(); }
})();
