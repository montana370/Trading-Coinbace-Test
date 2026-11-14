#!/usr/bin/env bash
# Arranca Core Engine + Dashboard en el mismo contenedor (comparten el volumen SQLite).
# Si cualquiera de los dos termina, el contenedor sale y Railway lo reinicia.
set -uo pipefail

export DB_PATH="${DB_PATH:-/data/trading_state.db}"
mkdir -p "$(dirname "$DB_PATH")"

if [[ -n "${RAILWAY_ENVIRONMENT:-}" && -z "${DASHBOARD_PASSWORD:-}" ]]; then
  echo "ERROR: DASHBOARD_PASSWORD es obligatoria en Railway (el dashboard es público y tiene Kill-Switch)." >&2
  exit 1
fi

python main.py &
ENGINE=$!

streamlit run dashboard.py \
  --server.port "${PORT:-8501}" --server.address 0.0.0.0 \
  --server.headless true --browser.gatherUsageStats false &
UI=$!

shutdown() {
  kill -TERM "$ENGINE" "$UI" 2>/dev/null
  wait "$ENGINE" "$UI" 2>/dev/null
  exit 0
}
trap shutdown TERM INT

wait -n "$ENGINE" "$UI"
echo "Un proceso terminó inesperadamente; reiniciando contenedor." >&2
kill -TERM "$ENGINE" "$UI" 2>/dev/null
wait 2>/dev/null
exit 1
