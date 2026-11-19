# Failure Synthesis System (FSS) — Operational Runbook

This runbook provides the operational reference and execution guide for the Failure Synthesis System (FSS). It documents the single-command launch procedure, CLI parameters, supported failure scenarios, 8-part synthesis report structure, and the underlying Java-to-Python runtime linkage.

---

## 1. Quick Start: Single-Command Launch

To execute the end-to-end linked system (ingesting/opening the durable Java `BlockStore`, executing bounded retrieval, piping wire envelopes to the Python intelligence layer, decoding payloads, and generating the validated 8-part failure synthesis report):

```bash
./run_pipeline.sh
```

### Web Dashboard & Live Service Launch

To launch the full interactive web application (connecting the Streamlit UI to the Java `BlockStore` engine and Python Qwen RAG pipeline):

```bash
./start_all.sh
```
*Access the live dashboard in your browser:* `http://127.0.0.1:8501` (or specify `PORT=8502 ./start_all.sh`).

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

## 6. Verification & Test Execution

### 1. Integration Tests
To run the automated integration test suite covering Java compilation, BlockStore ingestion, bounded retrieval, Python retriever protocol conformance, citation validation, hallucination rejection, and end-to-end ProcessBuilder execution:

```bash
python -m unittest bridge/test_bridge.py
```
*Expected: 6 tests passing in under 2 seconds.*

### 2. Manual Recompilation of Bridge
If modifications are made to `bridge/JavaStoreBridge.java`:

```bash
bash bridge/compile.sh
```

### 3. Launching the Interactive Web UI
To launch the Streamlit frontend with interactive scenario selection and timeline visualization:

```bash
./run.sh
```
*Accessible at `http://127.0.0.1:8501`.*
