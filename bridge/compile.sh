#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p bridge/classes

# Compile all bridge Java sources into bridge/classes
java bridge/CompileHelper.java -cp fss-core/target/classes:bridge/classes -d bridge/classes \
    bridge/StoreLockManager.java \
    bridge/JavaBridgeWorker.java \
    bridge/JavaStoreBridge.java

echo "Successfully compiled Java bridge classes into bridge/classes"
