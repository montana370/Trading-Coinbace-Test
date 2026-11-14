"""
dashboard.py — Terminal visual en tiempo real (Streamlit).

Proceso independiente del Core Engine: sólo LEE el estado compartido en
SQLite (WAL, lectores concurrentes sin bloquear al escritor) y ESCRIBE en el
bus `commands`. El refresco usa `st.fragment(run_every=…)`, que re-renderiza
sólo los paneles vivos sin recargar la página ni los controles.

Uso:  streamlit run dashboard.py
"""
from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

from config import SETTINGS
from database import Database

REFRESH_S = 1.0

st.set_page_config(page_title="Quant Terminal · Coinbase", page_icon="📈", layout="wide")

st.markdown(
    """
<style>
:root { --up:#16a34a; --down:#dc2626; --warn:#d97706; --muted:#6b7280; }
.block-container { padding-top: 1.2rem; }
.badge { display:inline-block; padding:4px 12px; border-radius:6px; font-weight:700; font-size:0.95rem; }
.b-run { background:#16a34a22; color:#16a34a; border:1px solid #16a34a; }
.b-pause { background:#d9770622; color:#d97706; border:1px solid #d97706; }
.b-kill { background:#dc262622; color:#dc2626; border:1px solid #dc2626; }
.b-off { background:#6b728022; color:#6b7280; border:1px solid #6b7280; }
.card { border:1px solid #8884; border-radius:10px; padding:12px 14px; height:100%; }
.card h4 { margin:0 0 4px 0; }
.kv { display:flex; justify-content:space-between; font-size:0.86rem; margin:2px 0; }
.kv span:first-child { color:var(--muted); }
.obi-track { position:relative; height:10px; background:#8883; border-radius:5px; margin:4px 0 2px; }
.obi-mid { position:absolute; left:50%; top:-2px; width:1px; height:14px; background:#888; }
.obi-fill { position:absolute; top:0; height:10px; border-radius:5px; }
.small { font-size:0.78rem; color:var(--muted); }
</style>
""",
    unsafe_allow_html=True,
)


@st.cache_resource
def get_db() -> Database:
    return Database(SETTINGS.runtime.db_path)


db = get_db()


def fmt_px(x: Any) -> str:
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return "—"
    x = float(x)
    if x >= 1000:
        return f"{x:,.2f}"
    if x >= 1:
        return f"{x:,.4f}"
    return f"{x:.6f}"


def fmt_ts(ts: float | None) -> str:
    if not ts:
        return "—"
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%m-%d %H:%M:%S")


def status_badge(status: str) -> str:
    cls = {"RUNNING": "b-run", "PAUSED": "b-pause", "SAFE": "b-pause", "KILLED": "b-kill"}.get(status, "b-off")
    return f'<span class="badge {cls}">{status}</span>'


def obi_bar(obi: float | None) -> str:
    v = 0.0 if obi is None else max(-1.0, min(1.0, float(obi)))
    width = abs(v) * 50
    left = 50 if v >= 0 else 50 - width
    color = "#16a34a" if v >= 0 else "#dc2626"
    return (
        f'<div class="obi-track"><div class="obi-mid"></div>'
        f'<div class="obi-fill" style="left:{left}%;width:{width}%;background:{color}"></div></div>'
    )


# ═════════════════════════════════════════════════════════════════════════════
# Controles (fuera del fragmento: no se re-renderizan cada segundo)
# ═════════════════════════════════════════════════════════════════════════════
with st.sidebar:
    st.header("🎛️ Control")
    st.caption("Las órdenes viajan por el bus de comandos (SQLite) al Core Engine.")

    st.subheader("🚨 Emergencia")
    def _fire_panic() -> None:
        st.session_state["panic_cid"] = db.push_command("PANIC", "dashboard")
        st.session_state["arm_panic"] = False   # desarmar tras el disparo

    armed = st.checkbox("Armar Kill-Switch", key="arm_panic")
    st.button("PANIC KILL-SWITCH", type="primary", disabled=not armed, width="stretch",
              on_click=_fire_panic)
    if st.session_state.get("panic_cid"):
        st.error(f"PANIC enviado (#{st.session_state['panic_cid']}). Cancelando órdenes y liquidando…")

    st.subheader("Entradas")
    c1, c2 = st.columns(2)
    if c1.button("⏸️ Pausar", width="stretch"):
        db.push_command("PAUSE", "dashboard")
        st.toast("PAUSE enviado")
    if c2.button("▶️ Reanudar", width="stretch"):
        db.push_command("RESUME", "dashboard")
        st.toast("RESUME enviado")

    st.subheader("Modo")
    current_mode = db.get_kv("mode", "PAPER")
    target = st.radio("Modo de ejecución", ["PAPER", "LIVE"], index=0 if current_mode == "PAPER" else 1,
                      horizontal=True)
    if target != current_mode:
        st.warning("Cambiar de modo exige estar sin posiciones. LIVE requiere ALLOW_LIVE_TRADING=true.")
        confirm = st.text_input(f"Escribe {target} para confirmar", key="mode_confirm")
        if st.button(f"Cambiar a {target}", disabled=confirm != target, width="stretch"):
            db.push_command("SET_MODE", "dashboard", {"mode": target})
            st.toast(f"SET_MODE {target} enviado")

    st.subheader("Últimos comandos")
    cmds = db.recent_commands(8)
    if cmds:
        st.dataframe(
            pd.DataFrame([{
                "cmd": c["command"], "origen": c["source"], "enviado": fmt_ts(c["created_at"]),
                "resultado": c["result"] or "⏳ pendiente",
            } for c in cmds]),
            hide_index=True, width="stretch",
        )


# ═════════════════════════════════════════════════════════════════════════════
# Paneles vivos
# ═════════════════════════════════════════════════════════════════════════════
@st.fragment(run_every=REFRESH_S)
def live_header() -> None:
    kv = db.all_kv()
    hb = kv.get("engine_heartbeat", 0)
    engine_alive = time.time() - (hb or 0) < 5
    status = kv.get("bot_status", "OFFLINE") if engine_alive else "OFFLINE"
    oracle = kv.get("oracle") or {}
    mode = kv.get("mode", "—")

    cols = st.columns([1.3, 1, 1, 1, 1, 2.2])
    cols[0].markdown(f"**Estado**<br>{status_badge(status)}", unsafe_allow_html=True)
    mode_cls = "b-kill" if mode == "LIVE" else "b-off"
    cols[1].markdown(f"**Modo**<br><span class='badge {mode_cls}'>{mode}</span>", unsafe_allow_html=True)
    cols[2].metric("Latencia WS", f"{kv.get('ws_latency_ms', 0):.0f} ms")
    cols[3].metric("Latencia API", f"{kv.get('api_latency_ms', 0):.0f} ms")
    hb_age = kv.get("heartbeat_age_s")
    cols[4].metric("Heartbeat", f"{hb_age:.1f} s" if hb_age is not None else "—")
    if oracle.get("veto"):
        cols[5].markdown(f"**Oráculo BTC**<br><span class='badge b-kill'>VETO BAJISTA ACTIVO</span>"
                         f"<div class='small'>{oracle.get('reason','')}</div>", unsafe_allow_html=True)
    else:
        cols[5].markdown(
            f"**Oráculo BTC**<br><span class='badge b-run'>RISK-ON</span>"
            f"<div class='small'>Δ5m {oracle.get('change_5m_pct',0):+.2f}% · CVD {oracle.get('cvd_ratio',0):+.2f}"
            f" · OBI {oracle.get('obi',0):+.2f}</div>", unsafe_allow_html=True)
    st.caption(
        f"{kv.get('status_detail', '')} · Posiciones {kv.get('open_positions', 0)}/{kv.get('max_open_positions', 2)}"
        f" · Pérdidas consecutivas {kv.get('consecutive_losses', 0)}"
        f" · Fees maker {kv.get('fees', {}).get('maker', 0)*100:.2f}% / taker {kv.get('fees', {}).get('taker', 0)*100:.2f}%"
        + ("" if engine_alive else " · ⚠️ CORE ENGINE SIN LATIDO")
    )


@st.fragment(run_every=REFRESH_S)
def live_pairs() -> None:
    st.subheader("Matriz en vivo")
    rows = {r["product_id"]: r for r in db.query("SELECT * FROM pair_snapshot")}
    cols = st.columns(4)
    for col, pid in zip(cols, SETTINGS.pairs):
        r = rows.get(pid)
        cfg = SETTINGS.pairs[pid]
        with col:
            if not r:
                st.markdown(f"<div class='card'><h4>{pid}</h4><div class='small'>sin datos</div></div>",
                            unsafe_allow_html=True)
                continue
            spread = r["spread_pct"]
            sp_ok = spread is not None and spread <= cfg.max_spread_pct
            sp_txt = f"{spread:.3f}%" if spread is not None else "—"
            fee_ok = bool(r["fee_filter_ok"])
            regime = r["regime"]
            reg_color = {"TREND": "#2563eb", "RANGE": "#7c3aed", "NOISE": "#dc2626"}.get(regime, "#6b7280")
            st.markdown(
                f"""<div class='card'>
<h4>{pid.split('-')[0]} <span class='small'>{cfg.role.value}</span></h4>
<div style='font-size:1.5rem;font-weight:700'>{fmt_px(r['price'])}</div>
<div class='kv'><span>Spread / máx</span><span style='color:{"#16a34a" if sp_ok else "#dc2626"}'>{sp_txt} / {cfg.max_spread_pct:.2f}%</span></div>
<div class='kv'><span>OBI L2</span><span>{(r['obi'] or 0):+.2f}</span></div>
{obi_bar(r['obi'])}
<div class='kv'><span>ATR (5m)</span><span>{fmt_px(r['atr'])} ({(r['atr_pct'] or 0):.2f}%)</span></div>
<div class='kv'><span>Régimen</span><span style='color:{reg_color};font-weight:700'>{regime}</span></div>
<div class='kv'><span>Hurst / ADX</span><span>{(r['hurst'] or 0):.2f} / {(r['adx'] or 0):.1f}</span></div>
<div class='kv'><span>VWAP</span><span>{fmt_px(r['vwap'])}</span></div>
<div class='kv'><span>Recorrido proy.</span><span>{(r['projected_move_pct'] or 0):.2f}%</span></div>
<div class='kv'><span>Filtro comisiones</span><span>{'✅ PASA' if fee_ok else '❌ BLOQUEA'}</span></div>
<div class='small' style='margin-top:6px'>{r['signal'] or ''}</div>
<div class='small'>Asignación máx {cfg.max_allocation*100:.0f}%</div>
</div>""",
                unsafe_allow_html=True,
            )


def compute_kpis(trades: pd.DataFrame, equity: pd.DataFrame) -> dict[str, Any]:
    out: dict[str, Any] = {"gross": 0.0, "net": 0.0, "fees": 0.0, "win_rate": None, "pf": None,
                           "n": 0, "dd": 0.0, "max_dd": 0.0}
    if not trades.empty:
        out["gross"] = trades["gross_pnl"].sum()
        out["net"] = trades["net_pnl"].sum()
        out["fees"] = trades["fees"].sum()
        out["n"] = len(trades)
        wins = trades[trades["net_pnl"] > 0]["net_pnl"]
        losses = trades[trades["net_pnl"] < 0]["net_pnl"]
        out["win_rate"] = len(wins) / len(trades) * 100
        out["pf"] = (wins.sum() / -losses.sum()) if len(losses) and losses.sum() != 0 else None
    if not equity.empty:
        eq = equity["equity"]
        peak = eq.cummax()
        dd = (peak - eq) / peak * 100
        out["dd"] = float(dd.iloc[-1])
        out["max_dd"] = float(dd.max())
    return out


@st.fragment(run_every=REFRESH_S * 3)
def live_performance() -> None:
    kv = db.all_kv()
    mode = kv.get("mode", "PAPER")
    trades = pd.DataFrame(db.trades(mode))
    equity = pd.DataFrame(db.equity_curve(mode))
    k = compute_kpis(trades, equity)
    st.subheader(f"Rendimiento ({mode})")
    c = st.columns(8)
    c[0].metric("Equity", f"{kv.get('equity', 0):,.0f}")
    c[1].metric("PnL Bruto", f"{k['gross']:+,.2f}")
    c[2].metric("PnL Neto", f"{k['net']:+,.2f}", delta=f"-{k['fees']:,.2f} fees", delta_color="off")
    c[3].metric("Win Rate", f"{k['win_rate']:.1f}%" if k["win_rate"] is not None else "—")
    c[4].metric("Profit Factor", f"{k['pf']:.2f}" if k["pf"] is not None else "—")
    c[5].metric("Drawdown", f"{k['dd']:.2f}%", delta=f"máx {k['max_dd']:.2f}%", delta_color="off")
    c[6].metric("DD diario", f"{kv.get('daily_dd_pct', 0):.2f}%", delta="límite 3.00%", delta_color="off")
    c[7].metric("Trades", f"{k['n']}")

    if not equity.empty:
        equity["time"] = pd.to_datetime(equity["ts"], unit="s", utc=True)
        fig = go.Figure()
        fig.add_trace(go.Scatter(x=equity["time"], y=equity["equity"], mode="lines", name="Equity",
                                 line=dict(width=2, color="#2563eb"), fill="tozeroy",
                                 fillcolor="rgba(37,99,235,0.08)"))
        lo, hi = equity["equity"].min(), equity["equity"].max()
        pad = max((hi - lo) * 0.15, hi * 0.002)
        fig.update_layout(height=280, margin=dict(l=10, r=10, t=10, b=10), yaxis_range=[lo - pad, hi + pad],
                          hovermode="x unified", showlegend=False)
        st.plotly_chart(fig, width="stretch", key="equity_chart")
    else:
        st.info("Curva de equity disponible tras el primer muestreo del Core Engine.")


@st.fragment(run_every=REFRESH_S)
def live_positions() -> None:
    kv = db.all_kv()
    mode = kv.get("mode", "PAPER")
    st.subheader("Posiciones abiertas")
    pos = db.open_positions(mode)
    if pos:
        rows = []
        for p in pos:
            mark = p["mark_price"] or p["entry_price"]
            rows.append({
                "Par": p["product_id"],
                "Estrategia": p["strategy"],
                "Entrada": fmt_px(p["entry_price"]),
                "Tamaño": f"{p['size']:g}",
                "Mark": fmt_px(mark),
                "PnL neto flotante": round(p["unrealized_net"] or 0.0, 2),
                "BE ajustado fees": fmt_px(p["breakeven_price"]),
                "BE armado": "✅" if p["breakeven_armed"] else "—",
                "Stop": fmt_px(p["stop_price"]),
                "Dist. SL %": round((mark - p["stop_price"]) / mark * 100, 2) if mark else None,
                "TP": fmt_px(p["take_profit"]),
                "Dist. TP %": round((p["take_profit"] - mark) / mark * 100, 2) if mark else None,
                "Abierta": fmt_ts(p["opened_at"]),
            })
        st.dataframe(pd.DataFrame(rows), hide_index=True, width="stretch")
    else:
        st.caption("Sin posiciones abiertas.")

    st.subheader("Órdenes activas")
    orders = [o for o in db.active_orders() if o["mode"] == mode]
    if orders:
        st.dataframe(pd.DataFrame([{
            "Par": o["product_id"], "Lado": o["side"], "Tipo": o["purpose"],
            "Precio": fmt_px(o["price"]), "Tamaño": o["size"], "Llenado": o["filled_size"],
            "Estado": o["status"], "Creada": fmt_ts(o["created_at"]),
        } for o in orders]), hide_index=True, width="stretch")
    else:
        st.caption("Sin órdenes activas.")


@st.fragment(run_every=REFRESH_S * 3)
def live_history() -> None:
    kv = db.all_kv()
    mode = kv.get("mode", "PAPER")
    left, right = st.columns([3, 2])
    with left:
        st.subheader("Trades cerrados")
        trades = db.trades(mode)[-50:][::-1]
        if trades:
            st.dataframe(pd.DataFrame([{
                "Cierre": fmt_ts(t["closed_at"]), "Par": t["product_id"], "Estrategia": t["strategy"],
                "Entrada": fmt_px(t["entry_price"]), "Salida": fmt_px(t["exit_price"]),
                "Bruto": round(t["gross_pnl"], 2), "Fees": round(t["fees"], 4),
                "Neto": round(t["net_pnl"], 2), "Neto %": round(t["net_pct"], 2),
                "Motivo": t["exit_reason"], "Duración": f"{t['duration_s']/60:.1f}m",
            } for t in trades]), hide_index=True, width="stretch")
        else:
            st.caption("Sin trades todavía.")
    with right:
        st.subheader("Eventos")
        ev = db.recent_events(40)
        if ev:
            st.dataframe(pd.DataFrame([{
                "Hora": fmt_ts(e["ts"]), "Nivel": e["level"], "Cat.": e["category"], "Mensaje": e["message"],
            } for e in ev]), hide_index=True, width="stretch", height=360)


st.title("📈 Quant Terminal — Coinbase Advanced Trade")
live_header()
st.divider()
live_pairs()
st.divider()
live_performance()
st.divider()
live_positions()
st.divider()
live_history()
