# Failure Synthesis System (FSS) — Integration Notes

These integration notes provide the technical specification, architectural rationale, binary wire mappings, and compliance audit for the FSS Java Core & Python Intelligence linkage.

---

## 1. System Architecture & Bridge Design

The Failure Synthesis System (FSS) addresses incident investigation in distributed, autonomous cyber-physical systems (e.g. multi-drone fleets, autonomous robotics, microservice meshes). The architecture strictly decouples low-level binary storage and durability (implemented in high-throughput Java) from semantic payload decoding, evidence synthesis, and LLM-driven root cause reasoning (implemented in Python).

### Separation of Concerns & Isolation Guarantee
- **Core Immutability**: ZERO changes were introduced into the core Java source code (`fss-core/src/main/java/` or `fss-core/src/test/java/`) or core Python package (`fss_py/`).
- **Bridge Encapsulation**: All linkage artifacts reside strictly within `bridge/`, `run_pipeline.sh`, `fss_env.sh`, and documentation files.
- **Ownership Boundary**:
  - **Java Subsystem**: Owns on-disk record framing, block management, CRC-32C verification, crash recovery, and bounded time-window retrieval. Java **never** decodes payload bytes or applies domain schemas.
  - **Python Subsystem**: Owns schema loading, binary payload decoding, epistemic tagging, evidence graph assembly, LLM context generation, and output validation. Python **never** reads Java storage files or accesses block/offset internals.

```
+-------------------------------------------------------------------------+
|                               JAVA CORE                                 |
|                      (fss-core/target/classes)                          |
|                                                                         |
|  - BlockStore: 64 KB zero-filled blocks, append-only, crash recovery    |
|  - EnvelopeCodec: Wire envelope decode/encode & CRC-32C Castagnoli      |
|  - RetrieveRequest / RetrieveResult: Window-bounded query engine        |
+------------------------------------+------------------------------------+
                                     |
                       JavaStoreBridge CLI (bridge/)
                                     |
                    IPC: Binary Envelopes via stdio
                                     |
+------------------------------------v------------------------------------+
|                            PYTHON LAYER                                 |
|                               (fss_py)                                  |
|                                                                         |
|  - JavaRetriever: Subprocess bridge conforming to Retriever Protocol    |
|  - Decoder & Schema Registry: Deterministic payload unpacking           |
|  - Evidence Builder: fss-evidence/v0 formatting & gap quarantine        |
|  - Synthesizer & Validator: Grounded LLM reasoning & hallucination gate  |
+-------------------------------------------------------------------------+
```

---

## 2. Contract v0 Binary Format & Wire Mapping

Contract v0 is the frozen specification governing all byte interactions between producers, storage, and consumers.

### 2.1 Conventions
- **Byte Order**: All multi-byte integers are serialized as **little-endian** (`<` in Python `struct`, `ByteOrder.LITTLE_ENDIAN` in Java `ByteBuffer`).
- **Unsigned Arithmetic**: Numeric types `u8`, `u16`, `u32`, and `u64` are unsigned. In Java, all comparisons and arithmetic are performed unsigned (`Integer.toUnsignedLong`, `Long.compareUnsigned`). Timestamps $\ge 2^{63}$ are valid.
- **Producer Timestamps**: `timestamp_ns` is the producer's local clock timestamp in nanoseconds since UTC epoch. It is preserved verbatim and never altered or synchronized by storage.

### 2.2 Wire Envelope Format (24-byte Header)
The canonical wire format for all events:

| Offset | Size | Type | Field | Specification / Rule |
|---:|---:|:---:|:---|:---|
| 0 | 2 | `u16` | `magic` | `0xF55A` (on wire as bytes `5A F5`) |
| 2 | 1 | `u8` | `version` | `1` (v0 contract) |
| 3 | 1 | `u8` | `flags` | `0` (reserved for future use) |
| 4 | 4 | `u32` | `source_id` | Unsigned 32-bit producer/agent identifier |
| 8 | 8 | `u64` | `timestamp_ns` | Nanosecond producer timestamp |
| 16 | 4 | `u32` | `event_type` | Schema discriminator key |
| 20 | 4 | `u32` | `payload_len` | Size of payload ($0 \le \text{len} \le 65\,504$) |
| 24 | $n$ | `bytes` | `payload` | Opaque application payload ($n = \text{payload\_len}$) |

**Validation Sequence**: An envelope is valid if and only if:
1. `TOO_SHORT`: Total buffer length $\ge 24$.
2. `BAD_MAGIC`: Buffer starts with `5A F5`.
3. `BAD_VERSION`: Version byte equals `1`.
4. `BAD_FLAGS`: Flags byte equals `0`.
5. `PAYLOAD_TOO_LARGE`: `payload_len` $\le 65\,504$.
6. `LENGTH_MISMATCH`: Total buffer length equals $24 + \text{payload\_len}$.

### 2.3 Java On-Disk Record Format (8-byte Framing)
Inside the Java `BlockStore`, each record is framed with length and checksum:

| Offset | Size | Type | Field | Description |
|---:|---:|:---:|:---|:---|
| 0 | 4 | `u32` | `record_len` | Length of envelope ($24 + \text{payload\_len}$). `0` indicates end-of-data. |
| 4 | 4 | `u32` | `crc32c` | CRC-32C Castagnoli over bytes $[0, 4)$ and $[8, 8 + \text{record\_len})$. |
| 8 | `record_len` | `bytes` | `envelope` | Canonical wire envelope as defined in §2.2. |

- **CRC-32C Parameters**: Castagnoli polynomial `0x82F63B78`, Init `0xFFFFFFFF`, XorOut `0xFFFFFFFF`. (RFC 3720 test vector `"123456789"` yields `0xE3069283`).
- **Total Record Length**: $8 + \text{record\_len} \le 65\,536$ (cannot exceed a single block).

### 2.4 Java BlockStore Structure
- **Block Size**: Exactly $65\,536$ bytes (64 KB). The file size is always an exact multiple of $65\,536$.
- **Allocation**: Blocks are zero-filled upon creation.
- **Single Source Ownership**: Each block belongs exclusively to the `source_id` of the record at offset 0. Records from other sources are forbidden in that block.
- **No Boundary Crossing**: If a record exceeds the remaining space in a block, the block is closed, and a new block is allocated. The unused tail remains zero.
- **Crash Recovery Scan**: On startup, each block is scanned from offset 0. The scan halts at `record_len == 0`, CRC failure, or source mismatch. Any non-zero bytes past the valid prefix are zeroed and synced (`fdatasync`). All metadata is rebuilt from the raw records.

### 2.5 Event Identifier Derivation
The unique event identifier (`event_id`) is content-addressed:
$$\text{event\_id} = \text{"ev-"} + \text{first 12 lowercase hex characters of SHA-256}(\text{canonical envelope bytes})$$
This guarantees that `event_id` is globally unique, deterministic, independent of storage offsets, and stable across retrievals.

---

## 3. The 16 Hard Rules Compliance Audit

The system enforces 16 architectural boundary rules defined in the master specification. All 16 rules were audited and verified:

| # | Rule Title | Architectural Enforcement Mechanism | Status |
|:---:|:---|:---|:---:|
| **1** | **Python never reads Java storage files** | Python never accesses `.dat` storage files. The `fss_py` codebase contains no record reader or on-disk framing parser. Communication occurs solely via wire envelope streams over standard input or `JavaStoreBridge` subprocesses. | **COMPLIANT** |
| **2** | **Java owns retrieval** | Selection and filtering occur solely in Java's `BlockStore.retrieve()`. The Python decoder and evidence builder never filter or discard retrieved events. | **COMPLIANT** |
| **3** | **Every retrieval is time-bounded** | `RetrieveRequest` in Java and `RetrievalRequest` in Python mandate a half-open window $[t_{\text{start}}, t_{\text{end}})$ where $t_{\text{end}} > t_{\text{start}}$ and $t_{\text{end}} - t_{\text{start}} \le 3\,600\,000\,000\,000\text{ ns}$ (1 hour). Unbounded queries cannot be constructed and are rejected with errors. | **COMPLIANT** |
| **4** | **Retrieval returns complete EventRecords** | Retrieval returns complete `EventRecord(source_id, timestamp_ns, event_type, payload_bytes)`. On-disk framing (`record_len`, `crc32c`) is stripped inside Java before transmission. | **COMPLIANT** |
| **5** | **Producer timestamp preserved** | The 64-bit `timestamp_ns` is preserved verbatim from the wire envelope. It is never overwritten with ingestion or retrieval time. | **COMPLIANT** |
| **6** | **No global ordering assumed across sources** | Events are grouped by `source_id` and sorted by producer timestamp within each source. Timestamps from different sources are treated as incomparable. Whenever multiple sources are present, a prominent clock caveat is inserted. | **COMPLIANT** |
| **7** | **Payload stays binary until decoding** | Payloads are held as opaque byte sequences through ingestion, storage, retrieval, and transport until passed to `fss_py.decoder.decode()`. | **COMPLIANT** |
| **8** | **Binary decoding is deterministic** | `decode(record, registry)` is a pure, idempotent function. Identical inputs yield byte-for-byte identical DecodedEvent dictionaries across repeated runs. | **COMPLIANT** |
| **9** | **Layout never reaches the LLM** | Field offsets, byte sizes, endianness indicators, struct formatting strings, and raw payload byte buffers are strictly purged before constructing the LLM prompt context. | **COMPLIANT** |
| **10** | **Unknown event types are not guessed** | If an `event_type` is not present in the active YAML schema, decode status is set to `UNKNOWN_EVENT_TYPE`. Fields evaluate to `{}` and are never guessed. | **COMPLIANT** |
| **11** | **Unknown payload regions remain opaque** | If a payload contains bytes beyond configured schema fields, decode status is `PARTIALLY_DECODED`. The `unknown_bytes` count is tracked, and fields are limited to documented offsets. | **COMPLIANT** |
| **12** | **Unknown bytes are never given to the LLM** | Undecoded and opaque bytes are quarantined. They are never serialized into prompt context as hex, base64, integer lists, or ASCII strings. Only the structural gap description appears under Data Gaps. | **COMPLIANT** |
| **13** | **Domain documentation supplies meaning** | Enum labels and human-readable names originate from YAML documentation and are segregated into `interpreted_fields`, separating documented meaning from decoded facts. | **COMPLIANT** |
| **14** | **LLM input is only decoded evidence** | The LLM receives strictly: user query, decoded event fields, domain reference documents, and data quality warnings. No storage or execution artifacts are leaked. | **COMPLIANT** |
| **15** | **Cross-agent retrieval through ALL** | Multi-agent investigations use the sentinel `source=ALL`, retrieving events across all active producers without requiring prior knowledge of agent IDs. | **COMPLIANT** |
| **16** | **Storage internals stay hidden** | `RetrievalResult` and `Evidence` omit block indexes, file offsets, block sizes, segment identifiers, and CRC checksums. | **COMPLIANT** |

---

## 4. Local Model Resolution & Offline Inference Strategy

To guarantee operational autonomy, air-gapped readiness, zero external API costs, and full determinism, the Failure Synthesis System implements a 3-tier local model resolution cascade.

### 4.1 Resolution Hierarchy

```
                      +-----------------------------+
                      |   Evidence & Context Built  |
                      +--------------+--------------+
                                     |
                                     v
                       [ Tier 1: Local Ollama Daemon ]
                       URL: http://127.0.0.1:11434
                       Models: qwen2.5:3b, qwen3:4b,
                               qwen2.5:1.5b, qwen2.5:7b
                                     |
                         (Reachable & Model Pulled?)
                             /               \
                          YES                 NO
                          /                     \
                         v                       v
               +-------------------+    [ Tier 2: Local HF Snapshot ]
               | Ollama Inference  |    Path: ~/.cache/huggingface/hub/...
               +-------------------+    Model: Qwen2.5-1.5B-Instruct
                                                 |
                                         (Weights Exist & Torch?)
                                             /               \
                                          YES                 NO
                                          /                     \
                                         v                       v
                               +-------------------+    +--------------------+
                               | PyTorch Inference |    | Tier 3: Rule-Based |
                               +-------------------+    | Deterministic      |
                                                        | Synthesizer        |
                                                        +--------------------+
```

1. **Tier 1 — Local Ollama Service**:
   - Queries `http://127.0.0.1:11434/api/tags`.
   - If active, issues streaming or JSON-formatted chat completion against pulled local models (e.g. `qwen2.5:3b` or `qwen2.5:1.5b`) with temperature $0.1$ and context size $8192$.
2. **Tier 2 — Local HuggingFace Weights Snapshot**:
   - Inspects the local cache directory:
     `/home/kirat/.cache/huggingface/hub/models--Qwen--Qwen2.5-1.5B-Instruct/snapshots/989aa7980e4cf806f80c7fef2b1adb7bc71aa306`
   - Enforces `HF_HUB_OFFLINE=1` and `TRANSFORMERS_OFFLINE=1` to prevent outbound network access.
   - Executes local causal language generation directly via PyTorch (`torch.float32`, `device_map="auto"`).
3. **Tier 3 — Deterministic Offline Reasoning Synthesizer**:
   - Fully deterministic fallback requiring zero external libraries or GPU/torch dependencies.
   - Evaluates domain anomaly rules, correlates cross-agent cascades (e.g., Drone 298 `LIDAR_TIMEOUT` causing Drone 474 `AVOIDANCE` for `COLLISION_RISK`), and produces golden-standard JSON output conforming to the FailureSynthesis schema.

### 4.2 Epistemic Grounding & Hallucination Gate
All model outputs (from Ollama, HuggingFace, or deterministic synthesizer) pass through the post-synthesis **Validator**:
- **Event ID Citation Extraction**: Parses all occurrences of `[ev-<hex12>]` across `summary`, `likely_cause`, and `contributing_events`.
- **Hallucination Detection**: Verifies every cited event ID against the set of events in the retrieved evidence. If a model generates an event ID not present in the evidence (e.g. `[ev-ffffffffffff]`), the report is marked **`REJECTED`**.
- **Clock Caveat Verification**: Checks that multi-source syntheses include explicit caveats stating that producer clocks are not synchronized. If omitted, the validator automatically inserts the caveat and updates the verdict to **`ACCEPTED_WITH_CORRECTIONS`**.
- **Data Gap Verification**: Ensures that any data gaps reported by the decoder (e.g. opaque vendor bytes) are explicitly acknowledged.

---

## 5. Verification & Audit Results

### Repository Git Status & Diff Audit
- Core Java files (`fss-core/src/main/java/` and `fss-core/src/test/java/`): **ZERO changes**.
- Core Python package (`fss_py/`): **ZERO changes**.
- Bridge files (`bridge/`): Fully isolated additions (`JavaStoreBridge.java`, `CompileHelper.java`, `compile.sh`, `java_retriever.py`, `synthesizer.py`, `pipeline_runner.py`, `test_bridge.py`).

### Test Suite Execution
The automated integration test suite (`bridge/test_bridge.py`) was executed:
```
Ran 6 tests in 1.426s
OK
```
Covering:
1. Java CLI help and argument parsing.
2. Binary fixture ingestion into durable Java `BlockStore`.
3. Bounded retrieval and binary envelope streaming with JSON metadata headers.
4. Python `JavaRetriever` protocol conformance and bounded window error enforcement.
5. Synthesizer reasoning, citation validation, and hallucination rejection.
6. Pipeline runner 8-part report generation and JSON output modes.
