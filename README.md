# Quant Auto-Trading Platform — Coinbase Advanced Trade (v3)

Plataforma de auto-trading cripto **asíncrona, modular y orientada a preservar capital**, construida sobre
`coinbase-advanced-py` (claves CDP / JWT) y cuentas en **USDC**. Motor de trading y panel visual corren como
**procesos independientes** comunicados por **SQLite en modo WAL**.

> ⚠️ Software experimental. Opera en `PAPER` hasta validar el comportamiento con tu cuenta. `LIVE` requiere
> `TRADING_MODE=LIVE` **y** `ALLOW_LIVE_TRADING=true` (doble interlock). Nada de esto es asesoramiento financiero.

---

## 1. Arquitectura

```
                         ┌──────────────────────── Coinbase Advanced Trade ────────────────────────┐
                         │  WS público: level2 · market_trades · ticker · heartbeats               │
                         │  WS usuario: user (estado de órdenes)        REST v3: órdenes/cuentas   │
                         └───────┬───────────────────────┬──────────────────────────┬──────────────┘
                                 │ (hilo SDK, json.loads) │                          │ asyncio.to_thread
             ┌───────────────────▼───────────────────────▼──────────┐               │ (latencia medida)
             │ CORE ENGINE  (main.py · proceso 1 · asyncio)          │               │
             │                                                       │               │
             │  call_soon_threadsafe ─► ws_queue ─► ws_dispatch      │               │
             │                                  │                    │               │
             │            ┌─────────────────────▼─────────────┐      │               │
             │            │ market_data.MarketState × 4       │      │               │
             │            │  OrderBook L2 · CVD · barras 1m   │      │               │
             │            │  VWAP sesión ±σ · secuencias      │      │               │
             │            └───────┬───────────────────────────┘      │               │
             │                    │ eval_loop (1 s)                  │               │
             │   ┌────────────────▼────────────────────────────┐     │               │
             │   │ strategy_engine                             │     │               │
             │   │  Régimen (Hurst·ADX·ATR%) → Breakout|MR     │     │               │
             │   │  OBI L2 → Oráculo BTC → Fee-Hurdle → Signal │     │               │
             │   └────────────────┬────────────────────────────┘     │               │
             │                    │ VETO TOTAL                       │               │
             │   ┌────────────────▼────────────────────────────┐     │               │
             │   │ risk_manager  estado·sizing·RR neto·CBs     │     │               │
             │   └────────────────┬────────────────────────────┘     │               │
             │   ┌────────────────▼────────────────────────────┐     │   ┌───────────┴───────────┐
             │   │ execution_handler                           │◄────┼──►│ exchange.Gateway       │
             │   │  Smart Chasing Post-Only · TPs escalonados  │     │   │  CoinbaseGateway (LIVE)│
             │   │  Fee-Adj. Breakeven · Trailing · Kill-Switch│     │   │  PaperGateway  (PAPER) │
             │   └───────┬──────────────────────────┬──────────┘     │   └────────────────────────┘
             │           │ notify_* (put_nowait)    │ AsyncDatabase (hilo writer dedicado)
             │   ┌───────▼────────────┐             │                │
             │   │ telegram_bot       │             │                │
             │   │ PriorityQueue+aiohttp            │                │
             │   │ long-poll comandos ─┼──► commands │                │
             │   └────────────────────┘             │                │
             │  watchdog · equity/DD · reconcile · ws_supervisor     │
             └──────────────────────────────────────┼────────────────┘
                                                    ▼
                         ┌──────────────── SQLite WAL (data/trading_state.db) ─────────────────┐
                         │ kv · pair_snapshot · positions · orders · trades · equity · events  │
                         │ commands  (bus de control: PANIC prioridad 100 > PAUSE > MODE > RESUME)
                         └──────────────────────────────────────────▲──────────────────────────┘
                                                                    │ lecturas concurrentes / INSERT commands
                                     ┌──────────────────────────────┴───────────────┐
                                     │ DASHBOARD (dashboard.py · proceso 2)          │
                                     │ Streamlit · st.fragment(run_every=1s)         │
                                     │ Telemetría · Matriz 4 pares · KPIs · Equity   │
                                     │ Posiciones/Órdenes · PANIC · Pausa · Modo     │
                                     └───────────────────────────────────────────────┘
```

**Por qué la UI nunca frena los WebSockets:** el dashboard es otro proceso; sólo lee SQLite (WAL permite
lectores concurrentes sin bloquear al escritor). En el engine, todo el I/O de disco pasa por un hilo
dedicado (`AsyncDatabase`) y Telegram por una cola (`put_nowait`).

## 2. Árbol de directorios

```
.
├── config.py              # Universo precalibrado, comisiones, riesgo, credenciales (.env)
├── models.py              # Tipos de dominio: Signal, Position, OrderUpdate, enums
├── database.py            # Estado compartido SQLite WAL + AsyncDatabase + bus de comandos
├── indicators.py          # Hurst, ADX, ATR, RSI, Bollinger, Keltner, squeeze, OBI (numpy)
├── market_data.py         # OrderBook L2, CVD, barras 1m/5m, VWAP de sesión, secuencias
├── exchange.py            # CoinbaseGateway (REST v3) + PaperGateway (matching con cola L2)
├── strategy_engine.py     # Régimen, Breakout+OrderFlow, MeanReversion+Micro, Oráculo BTC, Fee-Hurdle
├── risk_manager.py        # Estados, sizing 1%/Quarter-Kelly, RR neto, BE ajustado, circuit breakers
├── execution_handler.py   # Smart Order Chasing Maker-Only, TPs, trailing, Kill-Switch, reconciliación
├── telegram_bot.py        # Alertas asíncronas + /status /pause /resume /panic
├── main.py                # Orquestador asíncrono (Core Engine)
├── dashboard.py           # Terminal visual Streamlit
├── tests/                 # Unitarios + integración PAPER + engine end-to-end offline
├── requirements.txt / requirements-dev.txt / .env.example / pytest.ini
```

## 3. Puesta en marcha

```bash
python3.11 -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env            # rellena claves CDP y Telegram
pytest -q                       # 14 tests
python main.py                  # proceso 1: Core Engine
streamlit run dashboard.py      # proceso 2: Dashboard  →  http://localhost:8501
```

Claves CDP: crea una API key en el *Coinbase Developer Platform* con permisos **View + Trade** (sin
Transfer). Para `TELEGRAM_CHAT_ID` habla con tu bot y consulta `getUpdates`.

## 4. Universo precalibrado

| Par | Rol | Asign. máx | Spread máx | Estrategias |
|---|---|---|---|---|
| SOL-USDC | Motor Principal de Alfa | 35 % | 0.04 % | Breakout + MR |
| BTC-USDC | Ancla de Liquidez y Oráculo Macro | 30 % | 0.02 % | Breakout + MR |
| ETH-USDC | Especialista Mean-Reversion | 20 % | 0.03 % | MR |
| SUI-USDC | Captura de Impulso / Volatilidad | 15 % | 0.06 % | Breakout |

## 5. Lógica clave

**Régimen (barras 5m):** `Hurst ≥ 0.55 ∧ ADX ≥ 25 → TREND`; `Hurst ≤ 0.45 ∧ ADX ≤ 20 → RANGE`;
percentil de ATR% ≥ 97 o ≤ 3, o cualquier zona intermedia → `NOISE` (bloqueo).

**Breakout + Order Flow:** liberación de squeeze (BB dentro de Keltner) en las últimas 6 barras, cierre > BB
superior, precio > VWAP, volumen ≥ 1.5× media, CVD 5m positivo.

**Mean-Reversion + Microestructura:** precio ≤ VWAP − 2.5σ, RSI 1m ≤ 25 y 5m ≤ 35, absorción (venta agresora
≥ 1.5× compra, rango < 0.35 ATR y OBI > 0). TP = VWAP (cap 4 ATR).

**Filtros previos a la orden:** spread ≤ máx del par · OBI(10 niveles) ≥ +0.10 · Oráculo BTC (caída 5m ≥ 0.6 %,
CVD/volumen ≤ −0.35 u OBI ≤ −0.35 ⇒ veto de LONG en SOL/ETH/SUI, liberación tras 3 min sin disparos) ·
**Fee-Hurdle:** recorrido a TP ≥ `max(1.25 %, 3 × (2·maker + spread))` · RR neto ≥ 1.8.

**Sizing:** riesgo = 1 % equity (o Quarter-Kelly acotado 0.25 %–2 % tras ≥ 30 trades) ÷ pérdida por unidad
*incluyendo* fee de entrada y fee taker de salida; tope por asignación del par y por caja libre. Máx. 2
posiciones simultáneas global.

**Salidas:** stop 2 ATR · a +1.2R stop → `entry·(1+f_entry)/(1−f_taker)` (pérdida neta cero exacta) · trailing
2.5 ATR desde máximos · TPs Post-Only en reposo 40 % @1.6R, 30 % @2.6R, resto @TP, ajustados justo por debajo
de muros de venta L2.

**Circuit breakers:** DD diario 3 % ⇒ Kill-Switch + bloqueo 24 h (no se salta con `/resume`) · 3 pérdidas
seguidas ⇒ pausa 6 h · heartbeat > 5 s, WS caído o latencia REST > 500 ms (3 lecturas seguidas) ⇒ `SAFE`
(sin nuevas entradas, entradas en curso canceladas, posiciones gestionadas).

## 6. Decisiones de diseño y limitaciones conocidas

- **LONG-only.** Coinbase spot no permite cortos sin margen; las señales bajistas se usan como vetos.
- **Stops en software.** Los stops/trailing se ejecutan en el motor (orden a mercado, taker). Los TPs sí
  reposan en el exchange. Si el proceso cae, las posiciones quedan sin stop: ejecuta el engine bajo un
  supervisor (systemd `Restart=always`, Docker `restart: unless-stopped`). Al reiniciar se restauran las
  posiciones y se reconcilian sus TPs.
- **Lado del agresor en `market_trades`.** Se asume que `side` es el del taker; verifícalo empíricamente y
  ajusta `TRADE_SIDE_IS_TAKER` si fuese necesario (afecta al CVD).
- **Comisiones.** El modelo usa `max(tier real, configurado)` → nunca subestima costes.
- **Latencia WS** se calcula con el `timestamp` del mensaje vs reloj local: sincroniza el host con NTP.
- **Cambio de modo en caliente** sólo con la cuenta plana (sin posiciones ni entradas en curso).
- La validación en vivo contra Coinbase no se ha podido ejecutar en el entorno de desarrollo (red
  restringida); el motor completo se ha verificado con mensajes WebSocket sintéticos con el formato real
  (`tests/test_engine_offline.py`) y el ciclo de vida de órdenes con el simulador PAPER.
