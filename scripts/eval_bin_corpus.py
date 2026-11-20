#!/usr/bin/env python3
"""Automated end-to-end evaluation script for FSS binary corpus.

Evaluates all IRL ArduPilot (.fss.bin) logs and unseen synthetic (.bin) scenarios through:
  TelemetryIngestor (FileIngestor) -> Storage/BlockStore -> RetrievalMain (storage_ipc) ->
  Python Wire Decoder -> build_evidence -> Qwen RAG Synthesizer -> Epistemic Citation Auditor

Prints a structured Markdown summary table:
  | File Name | Events Ingested | Events Retrieved | Status | Validation Badge | Latency (ms) | Notes / Errors |
"""

from __future__ import annotations

import json
import struct
import time
from pathlib import Path
from typing import Any

from fss_py.decoder import decode_all
from fss_py.evidence import build_evidence
from fss_py.model import ALL, EventRecord
from fss_py.schema import load_schema_file
from bridge.java_retriever import JavaRetriever
from bridge.synthesizer import synthesize_and_validate


ROOT = Path(__file__).resolve().parent.parent
STORAGE_PROPS = ROOT / "fss-core" / "storage.properties"
SCHEMA_PATH = ROOT / "schemas" / "drone_v0.yaml"
IRL_DIR = ROOT / "test_data" / "irl"
UNSEEN_DIR = ROOT / "test_data" / "unseen"


def iter_any_envelopes(data: bytes) -> list[EventRecord]:
    """Decode envelopes from either Contract v0 wire (<HBBIQII) or envelope.cfg (>IQII)."""
    # 1. Check for Contract v0 MAGIC: 'FS' (0x4653)
    if len(data) >= 2 and data[:2] == b"\x53\x46":
        from fss_py.wire import iter_envelopes
        try:
            return list(iter_envelopes(data))
        except Exception:
            pass

    # 2. Decode Big-Endian envelope.cfg: >IQII (20-byte header)
    HEADER = struct.Struct(">IQII")
    records = []
    offset = 0
    while offset + 20 <= len(data):
        agent_id, ts, plen, etype = HEADER.unpack_from(data, offset)
        if plen < 0 or offset + 20 + plen > len(data):
            break
        payload = data[offset + 20 : offset + 20 + plen]
        # Normalize milliseconds to nanoseconds if ts < 10^14
        ts_ns = ts * 1_000_000 if (0 < ts < 100_000_000_000_000) else ts
        records.append(EventRecord(agent_id, ts_ns, etype, payload))
        offset += 20 + plen
    return records


def evaluate_file(
    file_path: Path,
    retriever: JavaRetriever,
    schema_registry: Any,
    query: str,
    max_events: int = 150,
) -> dict[str, Any]:
    """Execute end-to-end evaluation pipeline on a single binary file."""
    result: dict[str, Any] = {
        "filename": file_path.name,
        "category": "IRL" if "irl" in str(file_path) else "Unseen",
        "file_size_bytes": file_path.stat().st_size,
        "events_ingested": 0,
        "events_retrieved": 0,
        "status": "ERROR",
        "validation_badge": "UNKNOWN",
        "backend": "",
        "latency_ms": 0.0,
        "notes": "",
    }

    t0 = time.perf_counter()
    try:
        # 1. Read envelope timestamps to determine query window
        raw_bytes = file_path.read_bytes()
        sample_records = iter_any_envelopes(raw_bytes)
        if not sample_records:
            result["notes"] = "No envelopes found or corrupt file"
            return result

        t_min = min(r.timestamp_ns for r in sample_records)
        t_max = max(r.timestamp_ns for r in sample_records)
        # Bounded query contract: window cannot exceed 1 hour (3600s)
        if t_max - t_min > 3600 * 10**9:
            t_max = t_min + 3600 * 10**9

        # 2. Ingest file into Java storage via FileIngestor
        retriever.ingest_clean(file_path)
        result["events_ingested"] = len(sample_records)

        # 3. Retrieve through RetrievalMain IPC
        retrieval_res = retriever.retrieve(
            ALL,
            t_min,
            t_max,
            max_events=max_events,
        )
        result["events_retrieved"] = len(retrieval_res.events)
        result["status"] = "OK" if len(retrieval_res.events) > 0 else "EMPTY"

        # 4. Decode payloads via Contract v0 schemas
        decoded_events = decode_all(retrieval_res.events, schema_registry)

        # 5. Build canonical evidence artifact
        evidence = build_evidence(decoded_events, query=query, retrieval=retrieval_res)

        # 6. Synthesize & validate
        synthesis, report, backend = synthesize_and_validate(
            evidence,
            query=query,
            sufficiency_gate_enabled=True,
        )

        badge_val = report.badge.value if hasattr(report.badge, "value") else str(report.badge)
        result["validation_badge"] = badge_val
        result["backend"] = backend
        result["notes"] = f"{len(report.valid_citations)} valid citations"

    except Exception as exc:
        result["status"] = "FAIL"
        result["notes"] = str(exc)[:80]
    finally:
        result["latency_ms"] = round((time.perf_counter() - t0) * 1000, 2)

    return result


def main():
    print("=" * 80)
    print("🛸 Failure Synthesis System (FSS) - Corpus Evaluation Benchmark")
    print("=" * 80)

    schema_reg = load_schema_file(SCHEMA_PATH)
    retriever = JavaRetriever(
        mode="storage_ipc",
        storage_properties=STORAGE_PROPS,
        timeout=120.0,
    )

    # Gather test files
    irl_files = sorted(IRL_DIR.glob("*.fss.bin"))
    unseen_files = sorted(UNSEEN_DIR.glob("*.bin"))

    all_test_files = [("Real-World ArduPilot Telemetry", irl_files),
                      ("Unseen Synthetic Edge-Case Scenarios", unseen_files)]

    eval_results = []

    for suite_name, files in all_test_files:
        print(f"\n--- {suite_name} ({len(files)} files) ---")
        for f in files:
            print(f"Evaluating: {f.name}...", end=" ", flush=True)
            res = evaluate_file(
                f,
                retriever=retriever,
                schema_registry=schema_reg,
                query="Analyze telemetry and determine root cause of any failure, anomaly, or flight status.",
            )
            eval_results.append(res)
            print(f"[{res['status']}] Badge={res['validation_badge']} ({res['latency_ms']} ms)")

    # Print Formatted Markdown Table
    print("\n" + "=" * 80)
    print("📊 BENCHMARK EVALUATION SUMMARY TABLE")
    print("=" * 80 + "\n")

    headers = ["File Name", "Category", "Ingested", "Retrieved", "Status", "Validation Badge", "Latency (ms)", "Notes"]
    col_widths = [40, 10, 10, 10, 8, 26, 14, 25]

    row_fmt = "| " + " | ".join(f"{{:<{w}}}" for w in col_widths) + " |"
    sep_fmt = "| " + " | ".join("-" * w for w in col_widths) + " |"

    print(row_fmt.format(*headers))
    print(sep_fmt)

    for r in eval_results:
        print(row_fmt.format(
            r["filename"],
            r["category"],
            r["events_ingested"],
            r["events_retrieved"],
            r["status"],
            r["validation_badge"],
            r["latency_ms"],
            r["notes"][:25],
        ))

    # Save to JSON manifest
    summary_path = ROOT / "test_data" / "benchmark_summary.json"
    summary_path.write_text(json.dumps(eval_results, indent=2))
    print(f"\nDetailed evaluation results saved to {summary_path}")


if __name__ == "__main__":
    main()
