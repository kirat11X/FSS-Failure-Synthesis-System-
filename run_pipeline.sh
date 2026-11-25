#!/usr/bin/env bash
# run_pipeline.sh - End-to-end trigger for FSS Pipeline
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Source environment if available
if [ -f "fss_env.sh" ]; then
    # shellcheck disable=SC1091
    source fss_env.sh
fi

# Ensure Java Storage & Retrieval IPC classes are compiled
if [ ! -f "fss-core/target/classes/Retrieval/RetrievalMain.class" ] || [ ! -f "fss-core/target/dependency-classpath.txt" ]; then
    echo "Compiling Java Storage & Retrieval IPC classes..."
    bash bridge/compile.sh
fi

# Resolve Python interpreter
PYTHON_BIN=""
if [ -x "${ROOT_DIR}/.venv/bin/python" ]; then
    PYTHON_BIN="${ROOT_DIR}/.venv/bin/python"
elif [ -x "${ROOT_DIR}/.venv/Scripts/python.exe" ]; then
    PYTHON_BIN="${ROOT_DIR}/.venv/Scripts/python.exe"
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "${VIRTUAL_ENV}/bin/python" ]; then
    PYTHON_BIN="${VIRTUAL_ENV}/bin/python"
elif [ -n "${VIRTUAL_ENV:-}" ] && [ -x "${VIRTUAL_ENV}/Scripts/python.exe" ]; then
    PYTHON_BIN="${VIRTUAL_ENV}/Scripts/python.exe"
elif command -v python3 >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python3)"
elif command -v python >/dev/null 2>&1; then
    PYTHON_BIN="$(command -v python)"
else
    echo "Error: Python 3.11+ interpreter not found" >&2
    exit 1
fi

# Normalize arguments for pipeline_runner.py
ARGS=()
HAS_FILE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --scenario)
            SC="$2"
            shift 2
            [[ "$SC" != *.bin ]] && SC="${SC}.bin"
            [[ "$SC" != fixtures/* ]] && SC="fixtures/golden/${SC}"
            ARGS+=(--file "$SC")
            HAS_FILE=1
            ;;
        --fixture)
            ARGS+=(--file "$2")
            shift 2
            HAS_FILE=1
            ;;
        --file)
            ARGS+=(--file "$2")
            shift 2
            HAS_FILE=1
            ;;
        *)
            ARGS+=("$1")
            shift
            ;;
    esac
done

if [ "$HAS_FILE" -eq 0 ] && [ -t 0 ]; then
    ARGS=(--file "fixtures/golden/scenario_298_474.bin" "${ARGS[@]}")
fi

exec "$PYTHON_BIN" bridge/pipeline_runner.py "${ARGS[@]}"
