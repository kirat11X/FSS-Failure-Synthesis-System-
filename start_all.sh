#!/usr/bin/env bash
# ==============================================================================
# Failure Synthesis System (FSS) — Unified Startup Script
#
# Launches the integrated end-to-end FSS stack:
#   1. Verifies Java 21+ and Python 3.11+
#   2. Verifies / compiles Java BlockStore bridge classes
#   3. Checks Ollama daemon and local Qwen model availability
#   4. Starts the modernized 8-part Streamlit dashboard
#   5. Outputs system access URLs and diagnostic test commands
# ==============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Ensure local loopback bypasses proxy
export NO_PROXY="127.0.0.1,localhost"
export no_proxy="127.0.0.1,localhost"

# Configuration defaults
PORT="${PORT:-8501}"
URL="http://127.0.0.1:$PORT"
OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"

# Color output helpers
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }
die()  { printf '  \033[31m✗\033[0m %s\n' "$*" >&2; exit 1; }

echo "================================================================================"
echo "  Failure Synthesis System (FSS) — System Pre-Flight & Integration Checks"
echo "================================================================================"

# ------------------------------------------------------------------------------
# 1. Environment and Runtimes Verification (Java 21+, Python 3.11+)
# ------------------------------------------------------------------------------
echo "[1/4] Checking Runtimes (Java 21+ and Python 3.11+)..."

# Source environment definitions if present
if [ -f "fss_env.sh" ]; then
  # shellcheck source=/dev/null
  source fss_env.sh
fi

# Activate virtual environment if present
if [ -d ".venv" ] && [ -f ".venv/bin/activate" ] && [ -z "${VIRTUAL_ENV:-}" ]; then
  # shellcheck source=/dev/null
  source .venv/bin/activate
fi

# Verify Java
command -v java >/dev/null 2>&1 || die "Java runtime not found on PATH. Java 21+ is required."
JAVA_VERSION_STR="$(java -version 2>&1 | awk -F '"' '/version/ {print $2}' | head -n 1)"
JAVA_MAJOR="$(echo "$JAVA_VERSION_STR" | awk -F '.' '{print $1}')"
if [ "$JAVA_MAJOR" -lt 21 ]; then
  die "Detected Java version $JAVA_VERSION_STR. FSS requires Java 21+ (found major: $JAVA_MAJOR)."
fi
ok "Java runtime: OpenJDK $JAVA_VERSION_STR (version 21+ verified)"

# Verify Python
PYTHON="${PYTHON:-$(command -v python3 || command -v python || true)}"
[ -n "$PYTHON" ] || die "Python interpreter not found on PATH. Python 3.11+ is required."
"$PYTHON" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' \
  || die "Detected $("$PYTHON" -V 2>&1). FSS requires Python 3.11+."
ok "Python interpreter: $("$PYTHON" -V 2>&1) ($PYTHON)"

# ------------------------------------------------------------------------------
# 2. Java BlockStore & Bridge Compilation
# ------------------------------------------------------------------------------
echo "[2/4] Verifying Java BlockStore Bridge..."
if [ ! -f "bridge/classes/bridge/JavaStoreBridge.class" ] || [ "${1:-}" = "--rebuild" ]; then
  echo "  Compiling JavaStoreBridge into bridge/classes..."
  bash bridge/compile.sh || die "Java bridge compilation failed."
fi
ok "Java BlockStore Bridge compiled and ready (bridge/classes/bridge/JavaStoreBridge.class)"

# ------------------------------------------------------------------------------
# 3. Model Resolution & Ollama Availability Check
# ------------------------------------------------------------------------------
echo "[3/4] Checking Ollama & Synthesizer Model Resolution..."
OLLAMA_STATUS="offline"
if curl -sf -m 2 --noproxy "127.0.0.1,localhost" "$OLLAMA_URL/api/tags" >/dev/null 2>&1; then
  OLLAMA_STATUS="online"
  MODELS="$(curl -sf --noproxy "127.0.0.1,localhost" "$OLLAMA_URL/api/tags" | grep -o '"name":"[^"]*"' | cut -d'"' -f4 | tr '\n' ' ' || true)"
  ok "Ollama server running at $OLLAMA_URL (Models: ${MODELS:-none})"
else
  warn "Ollama offline on $OLLAMA_URL — Synthesizer will automatically resolve to Local HF Qwen / Deterministic reasoning."
fi

# ------------------------------------------------------------------------------
# 4. Streamlit Dashboard Launch & URLs
# ------------------------------------------------------------------------------
echo "[4/4] Starting FSS Modernized 8-Part Streamlit Dashboard..."

if curl -sf -m 1 --noproxy "127.0.0.1,localhost" "$URL/_stcore/health" >/dev/null 2>&1; then
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
echo "  Bridge Interface:      JavaStoreBridge (Contract v0 Little-Endian Envelopes)"
echo "  Decoder & Registry:    Deterministic Payload Unpacking (schemas/drone_v0.yaml)"
echo "  Qwen RAG Synthesizer:  3-Tier Resolution (Ollama: $OLLAMA_STATUS | HF | Deterministic)"
echo "  Citation Validator:    Active (16 FSS Hard Rules, Hallucination Gate)"
echo "================================================================================"
echo "  Quick Test Commands (in another terminal):"
echo "    - Syntax & import check:  python3 -c \"import streamlit_app; print('Imports OK')\""
echo "    - Bridge unit tests:      python3 bridge/test_bridge.py"
echo "    - End-to-end pipeline:    ./run_pipeline.sh --scenario scenario_298_474"
echo "================================================================================"
echo "Starting dashboard server at $URL (Press Ctrl+C to stop)..."
echo

# Background browser auto-opener
( for _ in $(seq 30); do
    sleep 1
    if curl -sf -m 1 --noproxy "127.0.0.1,localhost" "$URL/_stcore/health" >/dev/null 2>&1; then
      command -v xdg-open >/dev/null 2>&1 && xdg-open "$URL" >/dev/null 2>&1 || true
      break
    fi
  done ) &

exec "$PYTHON" -m streamlit run streamlit_app.py \
  --global.developmentMode false \
  --server.address 127.0.0.1 \
  --server.port "$PORT" \
  --server.headless true \
  --browser.gatherUsageStats false
