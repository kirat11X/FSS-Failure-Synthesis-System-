#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

mkdir -p bridge/classes fss-core/target/classes fss-core/lib

# Ensure Jackson dependencies exist for Retrieval IPC
if [ ! -f "fss-core/lib/jackson-databind-2.17.0.jar" ]; then
  curl -sSL "https://repo1.maven.org/maven2/com/fasterxml/jackson/core/jackson-core/2.17.0/jackson-core-2.17.0.jar" -o fss-core/lib/jackson-core-2.17.0.jar 2>/dev/null || true
  curl -sSL "https://repo1.maven.org/maven2/com/fasterxml/jackson/core/jackson-databind/2.17.0/jackson-databind-2.17.0.jar" -o fss-core/lib/jackson-databind-2.17.0.jar 2>/dev/null || true
  curl -sSL "https://repo1.maven.org/maven2/com/fasterxml/jackson/core/jackson-annotations/2.17.0/jackson-annotations-2.17.0.jar" -o fss-core/lib/jackson-annotations-2.17.0.jar 2>/dev/null || true
fi

# Build classpath for Jackson
JACKSON_CP=""
if [ -f "fss-core/lib/jackson-databind-2.17.0.jar" ]; then
  JACKSON_CP="fss-core/lib/jackson-databind-2.17.0.jar:fss-core/lib/jackson-core-2.17.0.jar:fss-core/lib/jackson-annotations-2.17.0.jar"
fi

# 1. Compile fss.core storage engine sources
java bridge/CompileHelper.java -d fss-core/target/classes \
    fss-core/src/main/java/fss/core/*.java

# 2. Compile Storage subsystem sources
java bridge/CompileHelper.java -cp "fss-core/target/classes" -d fss-core/target/classes \
    fss-core/src/main/java/Storage/BlockAllocator.java \
    fss-core/src/main/java/Storage/BlockMetadata.java \
    fss-core/src/main/java/Storage/BlockMetadataStore.java \
    fss-core/src/main/java/Storage/BlockStore.java \
    fss-core/src/main/java/Storage/EnvelopeConfig.java \
    fss-core/src/main/java/Storage/SegmentManager.java \
    fss-core/src/main/java/Storage/SegmentMetadataStore.java \
    fss-core/src/main/java/Storage/StorageConfig.java \
    fss-core/src/main/java/Storage/StorageMain.java \
    fss-core/src/main/java/Storage/StorageWorker.java \
    fss-core/src/main/java/Storage/TelemetryBuffer.java \
    fss-core/src/main/java/Storage/DynamicSegmentCompactor.java

# 3. Compile Ingestion & Retrieval subsystem sources
if [ -n "$JACKSON_CP" ]; then
  java bridge/CompileHelper.java -cp "fss-core/target/classes:$JACKSON_CP" -d fss-core/target/classes \
      fss-core/src/main/java/Ingestion/IngestionResult.java \
      fss-core/src/main/java/Ingestion/TelemetryIngestor.java \
      fss-core/src/main/java/Ingestion/FileIngestor.java \
      fss-core/src/main/java/Retrieval/RetrievalRecord.java \
      fss-core/src/main/java/Retrieval/RetrievalRequest.java \
      fss-core/src/main/java/Retrieval/RetrievalResponse.java \
      fss-core/src/main/java/Retrieval/RetrievalService.java \
      fss-core/src/main/java/Retrieval/RetrievalIpcServer.java \
      fss-core/src/main/java/Retrieval/RetrievalMain.java \
      fss-core/src/main/java/Retrieval/UdsBridgeServer.java \
      fss-core/src/main/DirectIngestionRetrievalTest.java
fi

# 4. Compile all bridge Java sources into bridge/classes
java bridge/CompileHelper.java -cp "fss-core/target/classes:bridge/classes" -d bridge/classes \
    bridge/StoreLockManager.java \
    bridge/JavaBridgeWorker.java \
    bridge/JavaStoreBridge.java

echo "Successfully compiled Java core, Storage, Retrieval, and Bridge classes"
