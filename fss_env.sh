#!/usr/bin/env bash
# Source this file to set up the FSS development and execution environment:
#   source fss_env.sh

export FSS_ROOT="/home/kirat/coding/minor"
export PYTHONPATH="${FSS_ROOT}:${PYTHONPATH:-}"

# Python virtual environment (if present)
if [ -d "${FSS_ROOT}/.venv" ]; then
    export VIRTUAL_ENV="${FSS_ROOT}/.venv"
    export PATH="${VIRTUAL_ENV}/bin:${PATH}"
fi

# Java Classpath & Runtime
export JAVA_HOME="/usr/lib/jvm/java-25-openjdk"
export FSS_CORE_CLASSES="${FSS_ROOT}/fss-core/target/classes"
export FSS_CORE_TEST_CLASSES="${FSS_ROOT}/fss-core/target/test-classes"
export CLASSPATH="${FSS_CORE_CLASSES}:${FSS_CORE_TEST_CLASSES}:${CLASSPATH:-}"

# LLM and Model Locations
export OLLAMA_URL="${OLLAMA_URL:-http://127.0.0.1:11434}"
export LOCAL_MODEL_PATH="/home/kirat/.cache/huggingface/hub/models--Qwen--Qwen2.5-1.5B-Instruct/snapshots/989aa7980e4cf806f80c7fef2b1adb7bc71aa306"
export QWEN_MODEL_SNAPSHOT="${LOCAL_MODEL_PATH}"
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

# FSS Contract and Schema Locations
export FSS_CONTRACT_DIR="${FSS_ROOT}/contract"
export FSS_DEFAULT_SCHEMA="${FSS_ROOT}/schemas/drone_v0.yaml"
