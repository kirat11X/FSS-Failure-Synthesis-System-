# Failure Synthesis System (FSS) — Operational Runbook

This runbook provides the operational reference and execution guide for the Failure Synthesis System (FSS). It documents the single-command launch procedure, CLI parameters, supported failure scenarios, 8-part synthesis report structure, and the underlying Java-to-Python runtime linkage.

---

## 1. Quick Start: Single-Command Launch

To execute the end-to-end linked system (ingesting/opening the durable Java `BlockStore`, executing bounded retrieval, piping wire envelopes to the Python intelligence layer, decoding payloads, and generating the validated 8-part failure synthesis report):

```bash
./run_pipeline.sh
```

### Web Dashboard & Live Service Launch

To launch the full interactive web application (connecting the Streamlit UI to the Java `BlockStore` engine and Python Qwen RAG pipeline), use either launcher:

```bash
./start.sh      # Single-command launcher (accepts --port, --address, -h)
# or
./start_all.sh  # Underlying hardened orchestrator
```
*Access the live dashboard in your browser:* `http://127.0.0.1:8501` (or specify `./start.sh --port 8502`).

### Docker Deployment

To build and run the multi-runtime container (OpenJDK 21 + Python 3.11 + Streamlit dashboard):

```bash
# Build the container image
docker build -t fss:latest .

# Run the container exposing port 8501
docker run -d --name fss-app -p 8501:8501 fss:latest

# Execute the 10-suite verification tests inside the container
docker run --rm fss:latest pytest tests/

# Execute an end-to-end pipeline run inside the container
docker run --rm fss:latest ./run_pipeline.sh --scenario scenario_298_474
```

### Environment Setup & Editable Package Installation

FSS eliminates fragile `sitecustomize.py` interpreter hooks in favor of canonical editable package installation via `pyproject.toml`. This prevents silent shadowing by distribution packages and guarantees importability across containerized and multi-user environments.

```bash
# 1. Activate Python 3.11+ virtual environment
source .venv/bin/activate

# 2. Install dependencies and register bridge and fss_py in editable mode
pip install --upgrade pip
pip install -r requirements.txt -r requirements-app.txt
pip install -e .

# 3. Verify editable installation
python -c "import bridge; import fss_py; print('FSS editable package registered OK')"
```

Streamlit is launched canonically via `exec "$VENV_PYTHON" -m streamlit run app.py` with `PYTHONUNBUFFERED=1` and `XLA_PYTHON_CLIENT_PREALLOCATE=false`. This ensures the active virtual-environment interpreter is executed directly without JAX VRAM preallocation.

### What Happens Automatically
When `./run_pipeline.sh` is executed without arguments:
1. **Environment Configuration**: Sources `fss_env.sh` (or `.env`) to set `JAVA_HOME`, `PYTHONPATH`, classpath, and model paths.
2. **Bridge Compilation**: Checks for compiled bridge classes in `bridge/classes`; if absent, compiles `bridge/JavaStoreBridge.java` against `fss-core` classes.
3. **Store Auto-Provisioning**: Checks for `bridge/store_scenario_298_474.dat`. If not present, ingests `fixtures/golden/scenario_298_474.bin` into a durable Java `BlockStore` formatted in 64 KB zero-filled blocks with CRC-32C Castagnoli integrity.
4. **Bounded Java Retrieval**: Java `BlockStore` executes `retrieve(source=ALL, [1791454021200000000, 1791454024300000000), max_events=100)`.
5. **IPC Pipe**: Java spawns Python (`bridge/pipeline_runner.py`) via `ProcessBuilder` and streams canonical binary wire envelopes (Magic `0xF55A`, Version `1`) through standard input.
6. **Deterministic Decoding**: Python decodes envelopes against `schemas/drone_v0.yaml`, isolating unknown/opaque regions as data gaps.
7. **Synthesis & Local Model**: The synthesis engine invokes the local offline model (Ollama / HuggingFace local Qwen 2.5 1.5B / deterministic reasoning engine).
8. **Validator & Report Output**: The output validator inspects every cited event ID (`ev-...`), confirms clock skew caveats, and prints the complete 8-part report to stdout.

---

## 2. Command-Line Arguments & Options

The pipeline accepts standard CLI options passed directly to `./run_pipeline.sh`:

```bash
./run_pipeline.sh [OPTIONS]
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `--query <text>` | String | `"Why did drone 474 leave formation?"` | Natural-language operator query to investigate. |
| `--fixture <path>` | Path | `fixtures/golden/scenario_298_474.bin` | Path to binary wire envelope stream (`.bin`) to ingest into BlockStore. |
| `--store <path>` | Path | `bridge/store_<fixture_basename>.dat` | Explicit path to an existing or new Java `BlockStore` data file. |
| `--source <ALL\|id>` | String | `ALL` | Source/agent filter (`ALL` or an unsigned 32-bit integer, e.g. `474`). |
| `--start <ns\|ISO>` | String | Auto-detected from store `min_ts` | Half-open window start timestamp (epoch nanoseconds or ISO-8601 string). |
| `--end <ns\|ISO>` | String | Auto-detected from store `max_ts` | Half-open window end timestamp (`end > start`, span $\le 1\text{ hour}$). |
| `--event-type <id>` | Int/Hex | `None` (any) | Numeric event type filter (e.g. `0x10`, `0x20`, `16`). |
| `--max-events <n>` | Int | `100` | Hard bounded limit of events to retrieve ($1 \le n \le 10\,000$). |
| `--schema <path>` | Path | `schemas/drone_v0.yaml` | Path to domain YAML schema specification. |
| `--python <bin>` | Path | `.venv/bin/python` or `python3` | Explicit Python interpreter to execute. |

### Direct Invocation via Java
You can also invoke the Java entrypoint directly:
```bash
java -cp "fss-core/target/classes:bridge/classes" bridge.JavaStoreBridge pipeline \
    --fixture fixtures/golden/scenario_298_474.bin \
    --query "Why did drone 474 leave formation?"
```

---

## 3. Supported Scenarios & Execution Matrix

### Scenario 1: Multi-Agent Cascade & Opaque Bytes (Drone 474 Formation Departure)
- **Goal**: Investigate why Drone 474 abruptly executed a collision avoidance manoeuvre and broke formation.
- **Root Cause**: Cross-agent cascade. Drone 298 experienced 3 consecutive LiDAR timeouts (`ev-2e92ae032f03`), drifting into Drone 474's trajectory. Drone 298 also reported a degraded diagnostic state with 8 undecoded vendor bytes (`ev-9d2fbc08bffc`).
- **Command**:
  ```bash
  ./run_pipeline.sh \
      --fixture fixtures/golden/scenario_298_474.bin \
      --query "Why did drone 474 leave formation?"
  ```
- **Key Observation**:
  - `AVOIDANCE` (`[ev-722d707b684c]`) on Drone 474 is caused by Drone 298's failure.
  - Multi-source query (`source=ALL`) discovers the cross-agent cause that querying only Drone 474 (`source=474`) would miss.
  - 8 opaque vendor bytes in `DIAGNOSTIC` are reported under Data Gaps, completely hidden from the LLM prompt.

### Scenario 2: Clock Skew & Causality Boundaries (`clock_skew_pair.bin`)
- **Goal**: Evaluate how the system handles unsynchronized producer clocks between independent agents.
- **Fixture**: `fixtures/golden/clock_skew_pair.bin` (Agents 115 and 220).
- **Command**:
  ```bash
  ./run_pipeline.sh \
      --fixture fixtures/golden/clock_skew_pair.bin \
      --query "What caused the collision risk between the drones?"
  ```
- **Key Observation**:
  - In raw timestamps, Drone 220's avoidance is logged at `10:09:05.080`, while Drone 115's LiDAR timeout is logged at `10:09:05.250`.
  - The system **refuses to assert temporal causality as fact** because producer clocks are unsynchronized (Hard Rule 6).
  - The synthesis flags the link as plausible/inferred, and the validator ensures the clock caveat is present.

### Scenario 3: Isolated Opaque Vendor Payload (`partial_opaque.bin`)
- **Goal**: Verify handling of payloads with unconfigured trailer bytes (`PARTIALLY_DECODED`).
- **Fixture**: `fixtures/golden/partial_opaque.bin`.
- **Command**:
  ```bash
  ./run_pipeline.sh \
      --fixture fixtures/golden/partial_opaque.bin \
      --query "What diagnostic issues are reported?"
  ```
- **Key Observation**:
  - Event `ev-e8857406cdcf` has `temperature_c=81.5` and `status_name=FAULT`.
  - 8 trailing vendor bytes are tagged `OPAQUE_BYTES` and quarantined under Data Gaps.
  - The bytes never appear in the prompt context.

### Scenario 4: Machine-Readable JSON Output
- **Goal**: Export structured JSON for downstream orchestrators or automated testing pipelines.
- **Command**:
  ```bash
  python bridge/pipeline_runner.py \
      --file fixtures/golden/scenario_298_474.bin \
      --query "Why did drone 474 leave formation?" \
      --json-out
  ```
- **Output**: JSON payload containing `evidence`, `synthesis`, `validation`, and `model` fields.

---

## 4. The 8-Part Synthesis Report Structure

The system renders an 8-part synthesis report to stdout:

```
================================================================================
  FAILURE SYNTHESIS SYSTEM (FSS) END-TO-END REPORT
  Model: Offline deterministic reasoning
================================================================================

[1] Question
> Why did drone 474 leave formation?

[2] Retrieval
- Call: retrieve(source=ALL, [2026-10-08 10:07:01.200, 2026-10-08 10:07:04.300) UTC, max_events=100)
- Result: returned 9 events, truncated: no

[3] Events
Sources involved: 298, 474, 901
Events are grouped by source_id and sorted by producer timestamp within each source. No order across sources is implied.
⏱️ Events come from 3 sources (298, 474, 901). Producer clocks are not synchronized...

Source     | Producer Time (UTC)     | Event Type       | Decode Status      | Epistemic Tag              | Event ID        | Fields
-----------------------------------------------------------------------------------------------------------------------------
298        | 2026-10-08 10:07:03.050 | LIDAR_STATUS     | SUCCESS            | decoded + documented       | ev-2e92ae032f03 | status_code=17, status_name=LIDAR_TIMEOUT, timeout_count=3
...

[4] Data gaps
- [OPAQUE_BYTES] 8 payload bytes of DIAGNOSTIC (0x30) event ev-9d2fbc08bffc from source 298 were not decoded...

[5] Knowledge
Retrieved 5 relevant reference chunks:
- `d1_drone_fleet/avoidance#1` (manual, d1_drone_fleet; explains AVOIDANCE, COLLISION_RISK)
...

[6] LLM context
```text
=== USER QUERY === ...
=== DECODED EVENTS === ...
=== DOMAIN KNOWLEDGE === ...
=== DATA QUALITY / DECODING WARNINGS === ...
```

[7] Synthesis
**Summary:** Drone 474 left formation 7 (LEFT_FORMATION [ev-1c432aca07e2]) after an avoidance manoeuvre for COLLISION_RISK [ev-722d707b684c] with a minimum separation of 3.2 m.
**Likely cause:** Drone 298 reported LIDAR_TIMEOUT (3 consecutive missed scans [ev-2e92ae032f03])...
**Confidence:** medium
**Contributing events** (verified against evidence):
- ✓ ev-2e92ae032f03: Source 298 LIDAR_STATUS at 2026-10-08 10:07:03.050
- ✓ ev-722d707b684c: Source 474 AVOIDANCE at 2026-10-08 10:07:03.120
- ✓ ev-1c432aca07e2: Source 474 FORMATION_STATE at 2026-10-08 10:07:03.180
**Uncertainty:**
- Producer clocks are not synchronised across sources...
**Reported Data Gaps:**
- Event ev-9d2fbc08bffc: 8 payload bytes of DIAGNOSTIC (0x30)...

[8] Validator
**Verdict:** ACCEPTED
✓ All cited event IDs verified against retrieved evidence.
✓ Multi-source clock caveats present and verified.
✓ Data gaps accurately covered.
================================================================================
```

### Explanation of the 8 Parts

1. **[1] Question**: Verbatim user query indicating the target investigation focus.
2. **[2] Retrieval**: Exact bounded retrieval request parameters (`source`, `[t_start, t_end)`, `max_events`), result event count, and whether the retrieval was truncated.
3. **[3] Events**: Tabular timeline showing all retrieved events grouped by `source_id`. Displays producer timestamp, event type, decode status, epistemic tag (`decoded + documented`, `unknown bytes`), deterministic SHA-256 event ID (`ev-<hex12>`), and decoded fields.
4. **[4] Data Gaps**: Explicit catalog of structural gaps (e.g. `OPAQUE_BYTES`, `UNKNOWN_EVENT_TYPE`, `MALFORMED_PAYLOAD`, or query truncation).
5. **[5] Knowledge**: Domain knowledge chunks retrieved from domain documentation based on terms present in the evidence.
6. **[6] LLM Context**: The exact text prompt formatted according to `fss-evidence/v0`, ensuring no binary layout or raw bytes reach the LLM.
7. **[7] Synthesis**: Structured answer including summary with bracketed citations (`[ev-...]`), root cause explanation, confidence rating (`low`, `medium`, `high`), contributing events checklist, uncertainty statements, and data gap citations.
8. **[8] Validator**: Post-generation validator verdict (`ACCEPTED`, `ACCEPTED_WITH_CORRECTIONS`, or `REJECTED`). Validates that all cited event IDs exist in the retrieved evidence, rejects any hallucinated event IDs, ensures multi-source clock caveats are stated, and confirms data gaps are reported.

---

## 5. Architecture: Java BlockStore to Python Pipeline

The end-to-end execution path strictly isolates storage concerns from intelligence concerns:

```
+-------------------------------------------------------------+
|                     JAVA SUBSYSTEM                          |
|                                                             |
|   fixtures/golden/*.bin (Wire Envelopes)                    |
|           |                                                 |
|           v (ingest)                                        |
|   BlockStore (64 KB Blocks, CRC-32C, Zero-Filled Tails)    |
|           |                                                 |
|           v (bounded retrieve: window, max_events, source)   |
|   JavaStoreBridge CLI                                       |
+-----------------------------+-------------------------------+
                              |
                     Standard Unix Pipe
           (canonical wire envelopes streamed via stdin)
                              |
+-----------------------------v-------------------------------+
|                    PYTHON SUBSYSTEM                         |
|                                                             |
|   pipeline_runner.py (iter_envelopes from stdin)            |
|           |                                                 |
|           v                                                 |
|   Deterministic Decoder (schemas/drone_v0.yaml)             |
|           |                                                 |
|           v                                                 |
|   Evidence Builder (fss-evidence/v0, Epistemic Tags)        |
|           |                                                 |
|           v                                                 |
|   Domain Knowledge Matcher (RAG Chunks)                     |
|           |                                                 |
|           v                                                 |
|   Model Resolution Cascade:                                 |
|     1. Local Ollama (qwen2.5:3b / qwen3:4b / qwen2.5:1.5b)  |
|     2. Local HF Model (Qwen 2.5 1.5B offline snapshot)      |
|     3. Deterministic Reasoning Synthesizer (Golden Rules)    |
|           |                                                 |
|           v                                                 |
|   Output Validator (Citation & Clock Caveat Verification)   |
|           |                                                 |
|           v                                                 |
|   8-Part Failure Synthesis Report                           |
+-------------------------------------------------------------+
```

### Durability & Storage Mechanics
- **Block Allocation**: File size is always an exact multiple of 65,536 bytes. Each block is zero-filled upon allocation.
- **Single Source Per Block**: Each 64 KB block is owned exclusively by one `source_id`.
- **CRC-32C Checksums**: Every on-disk record has an 8-byte frame (`record_len` u32, `crc32c` u32 Castagnoli). Corrupt records are rejected.
- **Crash Recovery**: On startup, `BlockStore.open()` scans blocks and stops at the first invalid record or zero length, zeroing any corrupted tail (`fdatasync`).

---

## 6. Local Verification Suite (10-Suite pytest + Hypothesis Harness)

FSS provides a comprehensive, deterministic 10-suite verification harness combining unit, property-based (Hypothesis), concurrency, and contract testing. All suites run offline without requiring external network access or dedicated GPU hardware.

### 10-Suite Verification Matrix

| # | Test Suite File | Target Layer | Exact Method / Invariant Under Test | Pass Criterion |
|---|---|---|---|---|
| 1 | `tests/test_uint64_roundtrip.py` | Java -> JSON -> Python -> UI | Hypothesis `@given(st.integers(0, (1<<64)-1))` and boundary seeds (`2^53-1`, `2^53`, `2^63-1`, `2^63`, `2^64-1`, `1795139460123456789` vs `+100 ns`) persisted in BlockStore and retrieved. | Exact bitwise equality in Python `int`, monotonic `findMinTs()`, distinct display strings for 100 ns pairs. |
| 2 | `tests/test_pipe_deadlock_64kb.py` | `bridge/java_retriever.py` | Trigger concurrent retrieval returning >256KB NDJSON on stdout while Java writes >128KB logs to stderr. | Completes cleanly under 5.0s with zero hang and zero JSON corruption. |
| 3 | `tests/test_timeout_orphan_cleanup.py` | `bridge/java_retriever.py` | Invoke simulated hung JVM with `timeout=1.0`; inspect OS process tree for surviving/defunct PIDs. | `TimeoutError` handled cleanly; exactly 0 live and 0 `<defunct>` zombie child JVM processes remain. |
| 4 | `tests/test_stdout_pollution.py` | Wire Protocol | Launch `JavaRetriever` with `JAVA_TOOL_OPTIONS="-Dtest.flag=1"` and `_JAVA_OPTIONS` injected into child env. | Pydantic v2 `TypeAdapter` parses framed response with zero JSON decode errors. |
| 5 | `tests/test_zero_event_contract.py` | End-to-End Pipeline | Query a time range with 0 persisted events across Java BlockStore -> Python -> UI. | Returns typed `status="EMPTY"`, activates Data Gaps Quarantine, short-circuits LLM into `INSUFFICIENT_EVIDENCE`. |
| 6 | `tests/test_blockstore_concurrency.py` | Java BlockStore + Locking | Spawn 8 concurrent Python threads/processes issuing overlapping BlockStore writes and bounded reads. | 0 `OverlappingFileLockException`, 0 torn 64KB blocks (file size % 65536 == 0), 100% read-back integrity. |
| 7 | `tests/test_streamlit_rerun_delegation.py` | `app.py` & `streamlit_app.py` | Simulate initial load plus 3 consecutive widget reruns against `app.py`. | All 8 UI sections render across every rerun; zero blank-page regression; zero `torch.classes` exceptions. |
| 8 | `tests/test_context_budget_guard.py` | Local LLM Client | Feed synthetic 500-event timeline against simulated `num_ctx=4096` and `num_ctx=16384`. | Pre-flight guard triggers deterministic compaction/warning before dispatch; never sends silent overflow. |
| 9 | `tests/test_qwen3_think_budget.py` | Local LLM Client | Simulate an unclosed `<think>` stream exceeding `thinking_budget` tokens without closing token 151668. | Guard injects `</think>` close prompt, emits `reasoning_truncated=True`, produces schema-valid JSON. |
| 10 | `tests/test_adversarial_citations.py` | Validation Badges | Fuzz outputs containing `[ev-fake]`, `[drone-298]`, entity-swapped valid IDs, and correlation-to-causation overclaims. | 100% rejection of fake/swapped IDs; `FORCE_OVERCLAIM` badge triggered on uncalibrated causal claims. |

### Running All 10 Verification Suites

To run all 10 verification test suites in a single command:

```bash
pytest \
  tests/test_uint64_roundtrip.py \
  tests/test_pipe_deadlock_64kb.py \
  tests/test_timeout_orphan_cleanup.py \
  tests/test_stdout_pollution.py \
  tests/test_zero_event_contract.py \
  tests/test_blockstore_concurrency.py \
  tests/test_streamlit_rerun_delegation.py \
  tests/test_context_budget_guard.py \
  tests/test_qwen3_think_budget.py \
  tests/test_adversarial_citations.py \
  -v
```

To run the complete repository test suite:
```bash
pytest tests/ -v
```

---

## 7. Persistent Worker IPC Mode (JEP-380 AF_UNIX)

For interactive serving and high-frequency queries across concurrent Streamlit sessions, FSS supports a persistent Java daemon (`JavaBridgeWorker`) running over standard Linux Unix domain sockets (JEP-380 AF_UNIX). This eliminates repeated ~60–900 ms JVM cold-start latency and keeps JIT compilations and BlockStore file descriptors warm.

### Socket Configuration & Mechanics

- **Socket Path**: `/tmp/fss_bridge.sock` (path length < 100 bytes to satisfy Linux `sockaddr_un` limits).
- **Wire Framing**: Single-line UTF-8 Newline-Delimited JSON (NDJSON) with strict separation of stdout (protocol channel) and stderr (diagnostics).
- **Process Cleanup**: `start_all.sh` registers a signal trap (`trap cleanup EXIT INT TERM`) that sends SIGTERM, waits 5 seconds, issues SIGKILL to child workers, and deletes stale socket files (`rm -f /tmp/fss_bridge.sock`).

### Starting the Persistent Worker

To launch the persistent Java worker independently:
```bash
java -cp "fss-core/target/classes:bridge/classes" bridge.JavaBridgeWorker --socket /tmp/fss_bridge.sock
```

To control the worker programmatically from Python:
```python
from bridge.java_retriever import JavaRetriever
from fss_py.model import ALL

# 1. Start worker daemon
proc = JavaRetriever.start_worker(socket_path="/tmp/fss_bridge.sock")

# 2. Query retriever via AF_UNIX socket
retriever = JavaRetriever("bridge/store_scenario_298_474.dat", mode="worker", socket_path="/tmp/fss_bridge.sock")
envelope = retriever.retrieve_envelope(ALL, 1791454021200000000, 1791454024300000000, max_events=100)

# 3. Stop worker daemon cleanly
JavaRetriever.stop_worker(socket_path="/tmp/fss_bridge.sock")
```

---

## 8. Local LLM Configuration & Context Budget Guard (`num_ctx >= 16384`)

FSS synthesizes multi-agent root causes through an 8-part structured diagnostic prompt requiring significant token headroom (system instructions, domain runbooks, timeline telemetry, data gaps, and Qwen3 reasoning traces).

### Sizing the Ollama Context Window

Ollama defaults to a 4,096-token context window on systems with less than 24 GiB VRAM. Under context overflow, Ollama silently drops older prompt content with HTTP 200 without warning.

To prevent silent truncation, configure `num_ctx >= 16384`:

1. **Environment Configuration**:
   ```bash
   # Set active server context length (do NOT use OLLAMA_NUM_CTX; it is ignored by Ollama)
   export OLLAMA_CONTEXT_LENGTH=16384
   ```

2. **Systemd Linux Service** (if running as a daemon):
   ```bash
   sudo systemctl edit ollama
   # Add in the override section:
   # [Service]
   # Environment="OLLAMA_CONTEXT_LENGTH=16384"
   sudo systemctl restart ollama
   ```

3. **Modelfile Configuration**:
   ```dockerfile
   FROM qwen2.5:3b
   PARAMETER num_ctx 16384
   ```

4. **Verify Active Context**:
   ```bash
   ollama ps
   # Check the CONTEXT column to confirm >= 16384 tokens are active
   ```

### Pre-Flight Token Budget Guard

Before dispatching prompts to local LLMs, FSS runs `preflight_budget_guard(evidence, query, num_ctx)`:
- Computes conservative token estimates (1 token per 3.8 characters).
- Enforces minimum reserved output headroom (1,200 tokens non-thinking; 3,200 tokens thinking).
- Deterministically compacts nominal timeline events if prompt size threatens to overflow `num_ctx`, emitting an explicit warning in the UI rather than permitting silent truncation.

### Qwen3 Sampling & Think-Budget Guard

When using Qwen3 thinking models:
- **Never use greedy decoding (`temperature=0.0`) in thinking mode**: causes infinite repetition loops. Use recommended sampling defaults: `temperature=0.6, top_p=0.95, top_k=20, min_p=0, presence_penalty=1.5`.
- **Think-Budget Guard**: If decoding reaches `thinking_budget` (e.g. 1,024 tokens) without emitting closing token 151668 (`</think>`), the guard injects `</think>`, sets `reasoning_truncated=True`, and enforces JSON grammar masks for the final answer.

---

## 9. Dual-Runtime GitHub Actions CI

Continuous integration is automated via `.github/workflows/ci.yml`. On every push and pull request to `main` and `integration-bridge`, the workflow executes:

1. **Dual Runtimes**: Pairs Eclipse Temurin JDK 21 (`actions/setup-java@v4`) and Python 3.11 / 3.12 (`actions/setup-python@v5`).
2. **Deterministic Build**: Compiles `fss-core` via Maven and bridge classes via `bridge/compile.sh`.
3. **Editable Python Environment**: Installs requirements and registers `pip install -e .`.
4. **10-Suite Verification**: Executes all 10 pytest and Hypothesis suites without requiring a GPU.
5. **Headless Streamlit Health Check**: Boots `app.py` in headless mode and polls `/_stcore/health` to guarantee zero blank-page regressions.
