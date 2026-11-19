#!/usr/bin/env bash
# run_pipeline.sh - End-to-end trigger for FSS Java Core & Python Synthesis Pipeline
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Source environment if available
if [ -f "fss_env.sh" ]; then
    # shellcheck disable=SC1091
    source fss_env.sh
fi

# Ensure bridge classes are compiled
if [ ! -f "bridge/classes/bridge/JavaStoreBridge.class" ]; then
    echo "Compiling JavaStoreBridge..."
    bridge/compile.sh
fi

CP="${ROOT_DIR}/fss-core/target/classes:${ROOT_DIR}/bridge/classes"
JAVA_BIN="${JAVA_HOME:-/usr}/bin/java"
if [ ! -x "$JAVA_BIN" ]; then
    JAVA_BIN="java"
fi

exec "$JAVA_BIN" -cp "$CP" bridge.JavaStoreBridge pipeline "$@"
