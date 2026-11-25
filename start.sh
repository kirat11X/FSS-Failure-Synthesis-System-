#!/usr/bin/env bash
# ==============================================================================
# Failure Synthesis System (FSS) — Startup Script (start.sh)
# ==============================================================================
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT_DIR"

# Ensure executable permissions on auxiliary launchers
chmod +x "$ROOT_DIR/start_all.sh" "$ROOT_DIR/run_pipeline.sh" "$ROOT_DIR/run.sh" "$ROOT_DIR/fss_env.sh" "$ROOT_DIR/bridge/compile.sh" 2>/dev/null || true

# Forward directly to the hardened startup orchestrator
exec "$ROOT_DIR/start_all.sh" "$@"
