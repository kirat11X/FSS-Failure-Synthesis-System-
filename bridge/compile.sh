#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mvn -q -DskipTests compile dependency:build-classpath \
  -Dmdep.outputFile=fss-core/target/dependency-classpath.txt

echo "Successfully compiled Java Storage and Retrieval IPC classes"
