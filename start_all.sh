#!/usr/bin/env bash
# ==============================================================================
# Failure Synthesis System (FSS) — Unified Startup Script
#
# Launches the integrated end-to-end FSS stack:
#   1. Process-group cleanup trap & signal handling (SIGTERM, 5s wait, SIGKILL)
#   2. Verifies JDK 16+ (JEP-380) and Python 3.11+
#   3. Verifies Java Retrieval IPC freshness vs sources; recompiles if stale
#   4. Verifies editable package installation (pip install -e .)
#   5. Confirms write permissions on BlockStore storage directory
#   6. Probes local LLM server (Ollama /api/ps or llama-server /health, context guard)
#   7. Launches modernized Streamlit dashboard via app.py
# ==============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Ensure local loopback bypasses proxy
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="127.0.0.1,localhost"

# Configuration defaults
PORT="${PORT:-8501}"
SERVER_ADDRESS="${SERVER_ADDRESS:-${STREAMLIT_SERVER_ADDRESS:-127.0.0.1}}"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"

# CLI options parsing
while [ $# -gt 0 ]; do
  case "$1" in
    --port)
      PORT="$2"
      shift 2
      ;;
    --address)
      SERVER_ADDRESS="$2"
      shift 2
      ;;
    -h|--help)
      cat << 'EOF'
Failure Synthesis System (FSS) Startup Launcher

Usage:
  ./start.sh [OPTIONS]
  ./start_all.sh [OPTIONS]

Options:
  -h, --help            Show this help message and exit
  --port <PORT>         Override the Streamlit server port (default: 8501 or $PORT)
  --address <ADDR>      Override the server listen address (default: 127.0.0.1 or $SERVER_ADDRESS)

Environment Variables:
  PORT                  Port to listen on (default: 8501)
  SERVER_ADDRESS        Address to bind to (e.g. 0.0.0.0 in Docker, default: 127.0.0.1)
  OLLAMA_URL            Ollama API endpoint (default: http://127.0.0.1:11434)
EOF
      exit 0
      ;;
    *)
      break
      ;;
  esac
done

HEALTH_URL="http://127.0.0.1:$PORT"
URL="http://${SERVER_ADDRESS}:$PORT"

# Color output helpers
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

# ------------------------------------------------------------------------------
# 0. Process-Group Cleanup Trap
# ------------------------------------------------------------------------------
cleanup() {
  local exit_code=$?
  trap - EXIT INT TERM
  # Clean up stale IPC socket
  rm -f /tmp/fss_bridge.sock

  # Terminate child background processes
  local child_pids
  child_pids=$(jobs -p 2>/dev/null || true)
  if [ -n "$child_pids" ]; then
    # shellcheck disable=SC2086
    kill -TERM $child_pids 2>/dev/null || true
    for _ in 1 2 3 4 5; do
      sleep 1
      child_pids=$(jobs -p 2>/dev/null || true)
      [ -z "$child_pids" ] && break
    done
    if [ -n "$child_pids" ]; then
      # shellcheck disable=SC2086
      kill -KILL $child_pids 2>/dev/null || true
    fi
  fi
  exit "$exit_code"
}
trap cleanup EXIT INT TERM

# Ensure any stale socket is removed prior to startup
rm -f /tmp/fss_bridge.sock

echo "================================================================================"
echo "  Failure Synthesis System (FSS) — System Pre-Flight & Integration Checks"
echo "================================================================================"

# ------------------------------------------------------------------------------
# 1. Environment and Runtimes Verification (Java 16+/21+, Python 3.11+)
# ------------------------------------------------------------------------------
echo "[1/5] Checking Runtimes (Java 16+ and Python 3.11+)..."

# Source environment definitions if present
if [ -f "fss_env.sh" ]; then
  # shellcheck source=/dev/null
  source fss_env.sh
fi

# Activate virtual environment if present
if [ -d ".venv" ] && [ -z "${VIRTUAL_ENV:-}" ]; then
  if [ -f ".venv/bin/activate" ]; then
    # shellcheck source=/dev/null
    source .venv/bin/activate
  elif [ -f ".venv/Scripts/activate" ]; then
    # shellcheck source=/dev/null
    source .venv/Scripts/activate
  fi
fi

# Verify Java: probe $JAVA_HOME/bin/java first, fallback to PATH
JAVA_BIN=""
if [ -n "${JAVA_HOME:-}" ] && [ -x "$JAVA_HOME/bin/java" ]; then
  JAVA_BIN="$JAVA_HOME/bin/java"
elif command -v java >/dev/null 2>&1; then
  JAVA_BIN="$(command -v java)"
else
  die "Java runtime not found on PATH or JAVA_HOME. Java 16+ (recommended 21+) is required."
fi

JAVA_VERSION_STR="$("$JAVA_BIN" -version 2>&1 | awk -F '"' '/version/ {print $2}' | head -n 1)"
JAVA_MAJOR="$(echo "$JAVA_VERSION_STR" | awk -F '.' '{print $1}')"
if [ -z "$JAVA_MAJOR" ] || [ "$JAVA_MAJOR" -lt 16 ]; then
  die "Detected Java version $JAVA_VERSION_STR. FSS requires at least Java 16+ for JEP-380 Unix domain sockets (found: $JAVA_MAJOR)."
fi
if [ "$JAVA_MAJOR" -lt 21 ]; then
  warn "Detected Java version $JAVA_VERSION_STR. FSS recommends Java 21+ for production AOT/JIT optimizations."
fi
ok "Java runtime: OpenJDK $JAVA_VERSION_STR ($JAVA_BIN)"

# Verify Python. Some Windows installations expose a Microsoft Store alias as
# `python`/`python3`; validate each candidate before selecting it.
PYTHON_CMD=()
PYTHON_LABEL=""
try_python() {
  local -a candidate=("$@")
  if "${candidate[@]}" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' >/dev/null 2>&1; then
    PYTHON_CMD=("${candidate[@]}")
    PYTHON_LABEL="${candidate[*]}"
    return 0
  fi
  return 1
}

if [ -x "$ROOT_DIR/.venv/Scripts/python.exe" ]; then
  try_python "$ROOT_DIR/.venv/Scripts/python.exe" || true
fi
if [ "${#PYTHON_CMD[@]}" -eq 0 ] && [ -x "$ROOT_DIR/.venv/bin/python" ]; then
  try_python "$ROOT_DIR/.venv/bin/python" || true
fi
if [ "${#PYTHON_CMD[@]}" -eq 0 ] && [ -n "${VIRTUAL_ENV:-}" ]; then
  if [ -x "$VIRTUAL_ENV/Scripts/python.exe" ]; then
    try_python "$VIRTUAL_ENV/Scripts/python.exe" || true
  elif [ -x "$VIRTUAL_ENV/bin/python" ]; then
    try_python "$VIRTUAL_ENV/bin/python" || true
  fi
fi
if [ "${#PYTHON_CMD[@]}" -eq 0 ] && command -v python3 >/dev/null 2>&1; then
  try_python "$(command -v python3)" || true
fi
if [ "${#PYTHON_CMD[@]}" -eq 0 ] && command -v python >/dev/null 2>&1; then
  try_python "$(command -v python)" || true
fi
if [ "${#PYTHON_CMD[@]}" -eq 0 ] && command -v py >/dev/null 2>&1; then
  try_python py -3 || true
fi
if [ "${#PYTHON_CMD[@]}" -eq 0 ]; then
  die "Python 3.11+ interpreter not found. Install Python 3.11+ or ensure 'py -3' resolves to it."
fi

PYTHON_VERSION="$("${PYTHON_CMD[@]}" -V 2>&1)"
ok "Python interpreter: $PYTHON_VERSION ($PYTHON_LABEL)"

# ------------------------------------------------------------------------------
# 2. Editable Package & Import Check
# ------------------------------------------------------------------------------
echo "[2/5] Verifying Editable Package Installation (pip install -e .)..."
if ! "${PYTHON_CMD[@]}" -c "import bridge; import fss_py" >/dev/null 2>&1; then
  echo "  Package 'bridge' or 'fss_py' not found on Python path; installing in editable mode..."
  "${PYTHON_CMD[@]}" -m pip install -e . || die "Failed to install package in editable mode via pip install -e ."
fi
"${PYTHON_CMD[@]}" -c "import bridge; import fss_py" || die "Failed to import bridge/fss_py after editable install check."
ok "Editable package verified (import bridge, fss_py OK)"

# ------------------------------------------------------------------------------
# 3. Java BlockStore & Bridge Compilation Freshness
# ------------------------------------------------------------------------------
echo "[3/5] Verifying Java Retrieval IPC Freshness..."
TARGET_CLASS="fss-core/target/classes/Retrieval/RetrievalMain.class"
NEEDS_REBUILD=0

if [ ! -f "$TARGET_CLASS" ] || [ "${1:-}" = "--rebuild" ]; then
  NEEDS_REBUILD=1
elif [ ! -f "fss-core/target/dependency-classpath.txt" ]; then
  NEEDS_REBUILD=1
else
  # Check if any .java source file is newer than TARGET_CLASS
  NEWER_SRC="$(find fss-core/src/main/java -name "*.java" -newer "$TARGET_CLASS" 2>/dev/null | head -n 1 || true)"
  if [ -n "$NEWER_SRC" ]; then
    echo "  Detected newer Java source file: $NEWER_SRC; triggering recompilation..."
    NEEDS_REBUILD=1
  fi
fi

if [ "$NEEDS_REBUILD" -eq 1 ]; then
  echo "  Compiling Java Storage and Retrieval IPC with Maven..."
  bash bridge/compile.sh || die "Java Storage/Retrieval compilation failed."
fi
ok "Java Retrieval IPC compiled and up-to-date ($TARGET_CLASS)"

# Confirm write permissions on BlockStore storage directory
STORE_DIR="$ROOT_DIR/bridge"
[ -d "$STORE_DIR" ] || mkdir -p "$STORE_DIR"
TEST_WRITE_FILE="$STORE_DIR/.perm_check_$$"
if touch "$TEST_WRITE_FILE" 2>/dev/null; then
  rm -f "$TEST_WRITE_FILE"
  ok "BlockStore storage directory ($STORE_DIR) is writable"
else
  die "BlockStore storage directory ($STORE_DIR) is not writable. Check filesystem permissions."
fi

# ------------------------------------------------------------------------------
# 4. Model Resolution & Local LLM Context Probe
# ------------------------------------------------------------------------------
echo "[4/5] Checking Local LLM Server & Context Budget..."
OLLAMA_STATUS="offline"
if curl -sf -m 2 --noproxy "127.0.0.1,localhost" "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  OLLAMA_STATUS="online"
  MODELS="$(curl -sf --noproxy "127.0.0.1,localhost" "$OLLAMA_URL/api/tags" | grep -o '"name":"[^"]*"' | cut -d'"' -f4 | tr '\n' ' ' || true)"
  ok "Ollama server running at $OLLAMA_URL (Models: ${MODELS:-none})"

  # Probe active context size via /api/ps or ollama ps
  PS_JSON="$(curl -sf -m 2 --noproxy "127.0.0.1,localhost" "$OLLAMA_URL/api/ps" 2>/dev/null || true)"
  if [ -n "$PS_JSON" ] && command -v jq >/dev/null 2>&1; then
    ACTIVE_CTX="$(echo "$PS_JSON" | jq -r '.models[0].context_length // empty' 2>/dev/null || true)"
    if [ -n "$ACTIVE_CTX" ] && [ "$ACTIVE_CTX" -lt 16384 ]; then
      warn "WARNING: Ollama active model context length is $ACTIVE_CTX (< 16384). FSS recommends num_ctx >= 16384 to prevent silent truncation."
    fi
  elif command -v ollama >/dev/null 2>&1; then
    PS_OUTPUT="$(ollama ps 2>/dev/null || true)"
    if echo "$PS_OUTPUT" | grep -qi "4096\|2048"; then
      warn "WARNING: Ollama reports active context < 16384 tokens in 'ollama ps'. Set OLLAMA_CONTEXT_LENGTH=16384."
    fi
  fi
elif curl -sf -m 2 --noproxy "127.0.0.1,localhost" "http://127.0.0.1:8080/health" >/dev/null 2>&1; then
  ok "llama-server running at http://127.0.0.1:8080"
else
  warn "Local LLM server offline — query analysis will fail explicitly until a configured local model is available."
fi

# ------------------------------------------------------------------------------
# 5. Streamlit Dashboard Launch & URLs
# ------------------------------------------------------------------------------
echo "[5/5] Preparing FSS Query-Driven Investigation Dashboard..."

if curl -sf -m 1 --noproxy "127.0.0.1,localhost" "$HEALTH_URL/_stcore/health" >/dev/null 2>&1; then
  echo
  ok "FSS Dashboard is already running at $URL (code reload active)"
  command -v xdg-open >/dev/null 2>&1 && xdg-open "$URL" >/dev/null 2>&1 || true
  exit 0
fi

echo
echo "================================================================================"
echo "  FAILURE SYNTHESIS SYSTEM (FSS) — ALL SERVICES INITIALIZED"
echo "================================================================================"
echo "  Dashboard Access URL:  $URL"
echo "  Storage Engine:        Java BlockStore (64KB Blocks, CRC-32C, Zero-filled recovery)"
echo "  Bridge Interface:      RetrievalMain (Length-Prefixed JSON IPC)"
echo "  Decoder & Registry:    Deterministic Payload Unpacking (schemas/drone_v0.yaml)"
echo "  Local LLM:             Ollama status: $OLLAMA_STATUS; no deterministic fallback"
echo "  Knowledge Base:        knowledge_base/ (local document and structured-data RAG)"
echo "================================================================================"
echo "  Quick Test Commands (in another terminal):"
echo "    - 10-Suite Pytest:        pytest tests/"
echo "    - Bridge unit tests:      python3 bridge/test_bridge.py"
echo "    - End-to-end pipeline:    ./run_pipeline.sh --scenario scenario_298_474"
echo "================================================================================"
echo "Starting dashboard server at $URL (Press Ctrl+C to stop)..."
echo

# Background browser auto-opener
( for _ in $(seq 30); do
    sleep 1
    if curl -sf -m 1 --noproxy "127.0.0.1,localhost" "$HEALTH_URL/_stcore/health" >/dev/null 2>&1; then
      command -v xdg-open >/dev/null 2>&1 && xdg-open "$URL" >/dev/null 2>&1 || true
      break
    fi
  done ) &

PYTHONUNBUFFERED=1 XLA_PYTHON_CLIENT_PREALLOCATE=false exec "${PYTHON_CMD[@]}" -m streamlit run app.py \
  --global.developmentMode false \
  --server.address "$SERVER_ADDRESS" \
  --server.port "$PORT" \
  --server.headless true \
  --browser.gatherUsageStats false
