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
