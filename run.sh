#!/usr/bin/env bash
# Start the FSS web UI (streamlit_app.py) after checking its dependencies.
#
#   ./run.sh                 # http://127.0.0.1:8501
#   PORT=8502 ./run.sh       # another port
#   PYTHON=python3 ./run.sh
set -euo pipefail
cd "$(dirname "$0")"

# Local proxy compatibility
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="127.0.0.1,localhost"

# Source environment variables if fss_env.sh exists
if [ -f "fss_env.sh" ]; then
  # shellcheck source=/dev/null
  source fss_env.sh
fi

# Python virtual environment activation
if [ -d ".venv" ] && [ -z "${VIRTUAL_ENV:-}" ]; then
  if [ -f ".venv/bin/activate" ]; then
    # shellcheck source=/dev/null
    source .venv/bin/activate
  elif [ -f ".venv/Scripts/activate" ]; then
    # shellcheck source=/dev/null
    source .venv/Scripts/activate
  fi
fi

PORT="${PORT:-8501}"
URL="http://127.0.0.1:$PORT"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
export PYTHONWARNINGS="${PYTHONWARNINGS:-ignore::Warning:requests}"  # hide urllib3/chardet noise

ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

echo "Checking FSS dependencies & runtime environment"

# Check Python
PYTHON="${PYTHON:-$(command -v python3 || command -v python || true)}"
[ -n "$PYTHON" ] || die "Python not found (need 3.11+)"
"$PYTHON" -c 'import sys; sys.exit(sys.version_info < (3, 11))' \
  || die "$("$PYTHON" -V 2>&1) is too old (need 3.11+)"
ok "$("$PYTHON" -V 2>&1) ($PYTHON)"

# Ensure Java Storage & Retrieval IPC classes are compiled
if [ ! -f "fss-core/target/classes/Retrieval/RetrievalMain.class" ] || [ ! -f "fss-core/target/dependency-classpath.txt" ]; then
  echo "Compiling Java Storage & Retrieval IPC..."
  bash bridge/compile.sh || die "Failed to compile Java classes"
fi
ok "Java Storage & Retrieval IPC compiled"

# Check Python packages
if ! "$PYTHON" -c 'import yaml, streamlit' 2>/dev/null; then
  warn "Installing missing Python packages from requirements-app.txt"
  "$PYTHON" -m pip install -q -r requirements-app.txt || die "pip install failed"
fi
ok "PyYAML $("$PYTHON" -c 'import yaml; print(yaml.__version__)'), streamlit $("$PYTHON" -c 'import streamlit; print(streamlit.__version__)')"

# Check Ollama
if curl -sf -m 3 --noproxy "127.0.0.1,localhost" "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  ok "Ollama running at $OLLAMA_URL"
  compute="$(journalctl -u ollama -b --no-pager 2>/dev/null | grep 'inference compute' | tail -1 || true)"
  if command -v nvidia-smi >/dev/null && nvidia-smi -L >/dev/null 2>&1 \
     && [[ "$compute" == *library=cpu* ]]; then
    warn "Ollama is using the CPU although an NVIDIA GPU exists; answers will be slow."
    warn "Fix: sudo systemctl restart ollama"
  fi
else
  warn "Ollama not reachable at $OLLAMA_URL: falling back to Local HF Qwen / deterministic reasoning."
fi

[ -n "${ANTHROPIC_API_KEY:-}" ] && ok "ANTHROPIC_API_KEY set: Claude is offered too"

# Terminate any existing/stale Streamlit process listening on PORT or running app.py/streamlit_app.py
STALE_PIDS=$(pgrep -f "streamlit run (app\.py|streamlit_app\.py)" 2>/dev/null || true)
if [ -n "$STALE_PIDS" ]; then
  echo "  Terminating stale Streamlit process(es): $STALE_PIDS..."
  kill -TERM $STALE_PIDS 2>/dev/null || true
  for _ in 1 2 3; do
    sleep 0.5
    REMAINING=$(pgrep -f "streamlit run (app\.py|streamlit_app\.py)" 2>/dev/null || true)
    [ -z "$REMAINING" ] && break
  done
  REMAINING=$(pgrep -f "streamlit run (app\.py|streamlit_app\.py)" 2>/dev/null || true)
  if [ -n "$REMAINING" ]; then
    kill -KILL $REMAINING 2>/dev/null || true
  fi
fi

# Ensure port $PORT is free before binding
if command -v fuser >/dev/null 2>&1; then
  fuser -k "${PORT}/tcp" 2>/dev/null || true
elif command -v lsof >/dev/null 2>&1; then
  PORT_PID=$(lsof -ti :"$PORT" 2>/dev/null || true)
  if [ -n "$PORT_PID" ]; then
    kill -TERM $PORT_PID 2>/dev/null || true
    sleep 0.5
    kill -KILL $PORT_PID 2>/dev/null || true
  fi
fi

echo; echo "Starting FSS Dashboard at $URL  (Ctrl+C to stop)"
( for _ in $(seq 30); do
    sleep 1
    if curl -sf -m 1 --noproxy "127.0.0.1,localhost" "$URL/_stcore/health" >/dev/null 2>&1; then
      command -v xdg-open >/dev/null && xdg-open "$URL" >/dev/null 2>&1 || true
      break
    fi
  done ) &
exec "$PYTHON" -m streamlit run streamlit_app.py \
  --global.developmentMode false \
  --server.address 127.0.0.1 --server.port "$PORT" --server.headless true \
  --browser.gatherUsageStats false
