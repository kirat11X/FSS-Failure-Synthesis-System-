# Failure Synthesis System

FSS combines a Java durable event store and Retrieval IPC server with a Python query,
decoding, local-RAG, analysis, and independent-review workflow.

The current dashboard flow:

1. Interprets the user's root-cause question and requires an explicit local-time window of at most
   one hour; it does not assume a default window.
2. Converts inclusive endpoints to Unix epoch nanoseconds and makes at most three Java retrieval
   calls, each capped at 5,000 latest records.
3. Keeps the existing payload decoder (`schemas/drone_v0.yaml`) and retrieves domain references
   from the recursively indexed repository-root `knowledge_base/` folder.
4. Uses the same configured local model for analysis and a separate evidence reviewer. It displays
   observed facts separately from inferences and discloses truncation and provenance.

The fixture-driven CLI and prototype documentation remain available for legacy demonstrations.

- Results: [RESULTS.md](RESULTS.md)
- Mentor demo: [DEMO.md](DEMO.md)
- Open questions and next step: [OPEN_QUESTIONS.md](OPEN_QUESTIONS.md)

```
fss-core/src/main/java/       Java storage, ingestion, and retrieval implementation
bridge/retrieval_ipc.py       persistent framed-JSON client for Java Retrieval IPC
bridge/investigation.py       bounded planning, iterative retrieval, analysis, and review
bridge/knowledge_base.py      local document extraction and provenance-preserving chunks
knowledge_base/               place domain/reference documents here for automatic indexing
fss_py/                       payload decoder, schemas, retrieval models, and evidence builder
schemas/drone_v0.yaml          payload schema used by the existing Python decoder
tests/                         Python unit and integration tests
```

## Setup

Install **Python 3.11+** application dependencies:

```bash
python -m pip install -r requirements-app.txt
```

The Java store requires **JDK 21 and Maven**. Install and start a local model runtime separately
(Ollama or `llama-server`); model weights are not installed by pip. See [RUNBOOK.md](RUNBOOK.md)
for model selection, Windows setup, and store configuration.

## Run

| What | Command (from the repo root) |
|---|---|
| Launch the query-driven dashboard | `./start.sh` |
| Install Python application dependencies | `python -m pip install -r requirements-app.txt` |
| Check that fixtures and vectors are current | `python fixtures/generate_fixtures.py --check` |
| Regenerate fixtures and vectors | `python fixtures/generate_fixtures.py` |
| Python tests | `python -m pytest` |
| Longer fuzz run | `FSS_FUZZ_ITERATIONS=300000 FSS_FUZZ_SEED=1 python -m pytest tests/test_fuzz.py` |
| Legacy fixture demo | see [DEMO.md](DEMO.md), or `python -m fss_py --help` |
| Compile Java and generate the runtime classpath | `mvn -q -DskipTests compile dependency:build-classpath "-Dmdep.outputFile=fss-core/target/dependency-classpath.txt"` |
| Legacy full crash harness | `./fss-core/crash-harness.sh` |

`./crash-harness.sh` defaults to 60 SIGKILL runs, a truncation at every one of 327,681 byte
offsets, and 64,816 bit-flip trials. It takes about 30 s and ends with a markdown results table.
Useful options:

```bash
./crash-harness.sh --tests a --kill-chains 40          # only the kill test, 200 runs
./crash-harness.sh --tests bc --flips 100000           # only the sweeps, more random flips
./crash-harness.sh --dir /some/disk/path --tests a     # run on a real filesystem
```

By default the harness works under `java.io.tmpdir`, which is tmpfs here. The sweeps rewrite a
320 KiB file hundreds of thousands of times, so don't point `--dir` at an SSD for `--tests bc`.

## Where the hard rules are enforced

| Rule | Enforcement | Test |
|---|---|---|
| Java never interprets payload bytes | `EnvelopeCodec` reads only the 24-byte header; the payload is copied | `ContractVectorsTest` |
| Python never reads Java storage files | The query dashboard uses framed Retrieval IPC; payload bytes are decoded by `fss_py` | `test_retrieval_ipc.py`, `test_rule_01_*` |
| Every dashboard retrieval is bounded | Inclusive windows are at most one hour, each response has at most 5,000 records, and each query has at most three Java calls | `test_retrieval_ipc.py`, `test_query_investigation.py` |
| Unknowns stay unknown | `UNKNOWN_EVENT_TYPE`, `PARTIALLY_DECODED` and `MALFORMED_PAYLOAD` (all-or-nothing); reserved `UNKNOWN` enum label; opaque bytes never reach the evidence | `test_rule_10..13`, `test_fuzz.py` |
| Producer timestamp preserved; no global order | Raw `timestamp_ns` end to end, unsigned compares; evidence grouped by source plus a clock caveat | `test_rule_05/06`, `clock_skew_pair` fixture |
