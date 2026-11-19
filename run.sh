#!/usr/bin/env bash
# Start the FSS web UI (streamlit_app.py) after checking its dependencies.
#
#   ./run.sh                 # http://127.0.0.1:8501
#   PORT=8502 ./run.sh       # another port
#   PYTHON=python3.12 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

PORT="${PORT:-8501}"
URL="http://127.0.0.1:$PORT"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
export PYTHONWARNINGS="${PYTHONWARNINGS:-ignore::Warning:requests}"  # hide urllib3/chardet noise

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

echo "Checking dependencies"

PYTHON="${PYTHON:-$(command -v python3 || command -v python || true)}"
[ -n "$PYTHON" ] || die "Python not found (need 3.11+)"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || die "$("$PYTHON" -V 2>&1) is too old (need 3.11+)"
ok "$("$PYTHON" -V 2>&1) ($PYTHON)"

if ! "$PYTHON" -c 'import yaml, streamlit' 2>/dev/null; then
  warn "Installing missing Python packages from requirements-app.txt"
  "$PYTHON" -m pip install -q -r requirements-app.txt || die "pip install failed"
fi
ok "PyYAML $("$PYTHON" -c 'import yaml; print(yaml.__version__)'), streamlit $("$PYTHON" -c 'import streamlit; print(streamlit.__version__)')"

if curl -sf -m 3 "$OLLAMA_URL/api/tags" >/dev/null; then
  ok "Ollama running at $OLLAMA_URL"
  # The last "inference compute" log line says what Ollama's latest start chose.
  compute="$(journalctl -u ollama -b --no-pager 2>/dev/null | grep 'inference compute' | tail -1 || true)"
  if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1 \
     && [[ "$compute" == *library=cpu* ]]; then
    warn "Ollama is using the CPU although an NVIDIA GPU exists; answers will be slow."
    warn "Fix: sudo systemctl restart ollama"
  fi
else
  warn "Ollama not reachable at $OLLAMA_URL: local models will not be listed."
  warn "Start it with: sudo systemctl start ollama   (or: ollama serve)"
fi

[ -n "${ANTHROPIC_API_KEY:-}" ] && ok "ANTHROPIC_API_KEY set: Claude is offered too"

if curl -sf -m 2 "$URL/_stcore/health" >/dev/null; then
  echo; ok "The app is already running at $URL (it reloads code changes itself)"
  command -v xdg-open >/dev/null && xdg-open "$URL" >/dev/null 2>&1 || true
  exit 0
fi

echo; echo "Starting $URL  (Ctrl+C to stop)"
( for _ in $(seq 30); do
    sleep 1
    if curl -sf -m 1 "$URL/_stcore/health" >/dev/null; then
      command -v xdg-open >/dev/null && xdg-open "$URL" >/dev/null 2>&1
      break
    fi
  done ) &
exec "$PYTHON" -m streamlit run streamlit_app.py \
  --server.address 127.0.0.1 --server.port "$PORT" --server.headless true \
  --browser.gatherUsageStats false
