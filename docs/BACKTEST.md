# Informe de backtest y calibración

Datos: velas oficiales de Coinbase (API pública). Comisiones: maker 0,40 %, taker 0,60 %.
Herramientas: `history.py` (descarga), `backtest.py` (estrategia del bot), `research_trend.py` (alternativa).

## 1. Estrategia actual (régimen + Breakout/OrderFlow + Mean-Reversion)

180 días de velas de 1 minuto, 4 pares, cartera completa (máx. 2 posiciones, oráculo BTC,
breakeven ajustado por comisiones, trailing, TPs parciales, circuit breakers).
Limitación: las velas no contienen el libro L2 (OBI no simulable; CVD y absorción aproximados).

| Configuración | Operaciones en 180 días | Resultado |
|---|---|---|
| Actual (señales en 5 m) | **0** | El bot nunca entra |
| Señales en 15 m, setups relajados | 0 | Ratio R/B neto < 1,8 por comisiones |
| Señales en 1 h / 2 h / 4 h (calibración: 162 × 128 × 54 combinaciones) | máx. 14 en entrenamiento, 2 en prueba | Ninguna combinación rentable de forma robusta |

**Diagnóstico:**
1. En 5 m el ATR mediano de SOL es 0,21 %: el TP (6·ATR ≈ 1,2 %) nunca supera el filtro de
   comisiones (≈ 2,46 % = 3 × (2·maker + spread)).
2. Con ~1 % de coste de ida y vuelta (maker + taker en el stop), el ratio riesgo/beneficio neto de
   operaciones intradía cae por debajo de 1.
3. Exigir a la vez régimen + squeeze + Bollinger + VWAP + volumen + CVD + RSI multi-TF produce una
   intersección casi vacía: ~3 setups por par cada 6 meses.

**Conclusión:** la estrategia, tal como está especificada, no tiene una ventaja demostrable con las
comisiones de Coinbase y no puede calibrarse sin sobreajustar.

## 2. Alternativa investigada: seguimiento de tendencia en 6 h / 1 D

Ruptura de Donchian (N barras) + filtro EMA 200, salida por trailing ATR o mínimo de M barras, solo
largos, 1 % de riesgo por operación. ~4 años de datos; parámetros elegidos solo en la primera mitad
(alcista 2022-24) y evaluados en la segunda (bajista 2024-26).

| Par (6 h) | Comprar y mantener (prueba) | Estrategia (prueba) | Operaciones | Máx. DD |
|---|---|---|---|---|
| SOL | −66,6 % | +2,84 % | 24 | 3,4 % |
| BTC | −26,6 % | −0,22 % | 27 | 4,0 % |
| ETH | −42,2 % | −2,45 % | 27 | 6,6 % |
| SUI | −80,4 % | +1,91 % | 18 | 4,8 % |

- En el tramo alcista (entrenamiento, optimizado): +6 % a +21 % por par con drawdown < 6 %.
- En el tramo bajista (fuera de muestra): ≈ 0 % en conjunto, con pérdidas máximas pequeñas.
- 89 % (6 h) y 100 % (1 D) de las combinaciones probadas fueron positivas en entrenamiento: el
  resultado no depende de un ajuste fino.

**Lectura honesta:** el seguimiento de tendencia protege el capital en mercados bajistas y captura
parte de las tendencias alcistas, pero no hay evidencia de beneficio en cualquier mercado. Con 1 % de
riesgo por operación los rendimientos son modestos.

## 3. Perpetuos US de Coinbase (largo y corto)

Mercados (Coinbase Financial Markets, regulado por la CFTC): `BIP-20DEC30-CDE` (BTC, 0,01 BTC),
`ETP-20DEC30-CDE` (ETH, 0,1 ETH), `SLP-20DEC30-CDE` (SOL, 5 SOL), `SUP-20DEC30-CDE` (SUI, 500 SUI).

Supuestos conservadores (`research_perps.py`): maker 0,02 %, taker 0,05 %, financiación 0,01 % cada
8 h cobrada siempre (también a los cortos), apalancamiento máx. 2×, 1 % de riesgo por operación.
Precio: spot de Coinbase como aproximación del perpetuo.

Mismo sistema de tendencia que en la sección 2, simétrico (cortos en rupturas bajistas bajo la EMA 200).

| 6 h, fuera de muestra (2024-26) | SOL | BTC | ETH | SUI |
|---|---|---|---|---|
| Largo y corto | **+9,6 %** | **+14,5 %** | **+16,1 %** | **+4,9 %** |
| Solo largo (comisiones de perpetuos) | +7,6 % | +6,5 % | +4,0 % | +6,0 % |
| Solo largo (comisiones spot, sección 2) | +2,8 % | −0,2 % | −2,5 % | +1,9 % |
| Comprar y mantener | −66,6 % | −26,6 % | −42,2 % | −80,4 % |

- Unas 75-85 operaciones por par en 2 años (~3 al mes), con drawdown máximo de 5-7 %.
- Robustez: **18 de 18** combinaciones de parámetros tienen media positiva fuera de muestra y en
  **12 de 18** los cuatro pares son positivos.
- Parámetros elegidos en entrenamiento: N = 20, M = 10, k = 2,5·ATR.

Limitaciones: el periodo de prueba es mayormente bajista (los cortos ayudan); no modela el límite de
2 posiciones simultáneas, el deslizamiento real ni la liquidez de SUI (volumen bajo).

## 4. ¿Operar todos los días? Intradía y más mercados (perpetuos)

### 4.1 Estrategias intradía (`research_intraday.py`)
1 h (4 años) y 15 min (1,5 años), largo y corto, comisiones de perpetuos, 60 % entrenamiento / 40 % prueba.

| Estrategia | Combinaciones positivas fuera de muestra | Mejor combinación en prueba |
|---|---|---|
| Ruptura 1 h | 9 / 18 (azar) | pierde en 3 de 4 pares, DD 20,6 % |
| Reversión 1 h (RSI2 + Bollinger) | 0 / 16 | pierde |
| Ruptura 15 min | 0 / 18 | pierde en 3 de 4 pares, DD 31,5 % |
| Reversión 15 min | 0 / 16 | pierde |

**Conclusión:** no hay ventaja intradía demostrable en estas familias, ni siquiera con comisiones de perpetuos.

### 4.2 Más mercados con el sistema 6 h (`research_portfolio.py`)
Por mercado y por separado, 15 de 18 perpetuos adicionales son positivos con parámetros fijos. **Pero
como cartera con capital compartido** (22 mercados, sin descartar perdedores) el resultado es ≈ 0 %
con caídas del 12-28 %: las altcoins están muy correlacionadas y añaden ruido, no ventaja.

| Cartera | 4 años | Últimos 2 años | Máx. DD | Operaciones/día |
|---|---|---|---|---|
| 22 mercados, 0,5 % riesgo, máx. 6 posiciones | +3,2 % | −3,2 % | 15,3 % | 1,0 |
| 22 mercados, 0,35 % riesgo, máx. 12 posiciones | −1,0 % | +0,7 % | 17,4 % | 1,6 |
| **4 pares, 1 % riesgo, máx. 2 posiciones** (desplegado) | +47,9 % | **+12,2 %** | 14,0 % | 0,3 |
| **4 pares, 0,5 % riesgo, máx. 4 posiciones** | +46,8 % | **+15,1 %** | **10,6 %** | 0,44 |

**Conclusión:** la mejor configuración con evidencia es la cartera de 4 pares principales con 0,5 % de
riesgo y hasta 4 posiciones (mismo riesgo total, menor caída, ~3 operaciones por semana). Operar a
diario con ventaja no está respaldado por los datos.
