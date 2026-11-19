#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p bridge/classes

# Compile JavaStoreBridge into bridge/classes
java bridge/CompileHelper.java -cp fss-core/target/classes -d bridge/classes bridge/JavaStoreBridge.java
echo "Successfully compiled JavaStoreBridge.java into bridge/classes"
