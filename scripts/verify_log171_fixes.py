#!/usr/bin/env python3
"""Verification script for log171.bin fixes on branch integration-bridge.

Prints:
  1. Total ingested events, min/max timestamps, and exact duration in seconds/minutes.
  2. Total retrieved events in Full Dataset mode via Java BlockStore IPC.
  3. All decoded ERR events (subsystem_code, error_code) — verifying zero None values.
  4. Count of matched ardupilot_error_codes.md rules (> 0).
  5. Post-compaction event count at num_ctx=16384 (100–220 events, not 7).
"""

from __future__ import annotations

import json
import struct
import tempfile
from pathlib import Path

from fss_py.decoder import decode_all
from fss_py.schema import load_schema_file
from fss_py.wire import iter_envelopes

from bridge.bin_ingestor import BinCacheManager
from bridge.error_catalog import (
    enrich_evidence_with_manual,
    load_builtin_manual,
)
from bridge.java_retriever import ALL, JavaRetriever
from bridge.synthesizer import (
    build_llm_context,
    estimate_tokens,
    preflight_budget_guard,
)
from scripts.convert_ardupilot_bin_to_fss import convert_log

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG171_PATH = PROJECT_ROOT / "test_data" / "irl" / "log171.bin"


def main():
    print("================================================================================")
    print("  VERIFYING LOG171.BIN RECOVERY & PIPELINE ACCURACY")
    print("================================================================================")

    # 1. Ingestion / Conversion
    out_fss = PROJECT_ROOT / "test_data" / "uploads" / "log171_verified.fss.bin"
    meta = convert_log(LOG171_PATH, out_fss, agent_id=1, max_records=5000)

    duration_s = meta["duration_s"]
    duration_min = duration_s / 60.0
    print(f"\n[1. Flight Duration & Unified Clock]")
    print(f"  Input Log: {LOG171_PATH.name} ({LOG171_PATH.stat().st_size:,} bytes)")
    print(f"  Total Ingested Envelopes: {meta['total_envelopes']:,}")
    print(f"  Min Timestamp: {meta['time_start_ns']} ns")
    print(f"  Max Timestamp: {meta['time_end_ns']} ns")
    print(f"  Flight Duration: {duration_s:.2f} s ({duration_min:.2f} minutes)")
    assert duration_s < 3600.0, f"Flight duration {duration_s}s exceeds 3600s!"
    assert 200.0 <= duration_s <= 300.0, f"Unexpected duration {duration_s}s!"

    # 2. Java BlockStore Ingest & Full Dataset IPC Retrieval
    tmp_store = Path(tempfile.NamedTemporaryFile(suffix=".dat", delete=False).name)
    jr = JavaRetriever(store_path=tmp_store, mode="cli")
    jr.ingest_clean(out_fss)
    env = jr.retrieve_full_dataset(source=ALL, max_events=100000)

    print(f"\n[2. Java BlockStore Retrieval (Full Dataset)]")
    print(f"  Retrieval Status: {env.status}")
    print(f"  Events Retrieved via IPC: {len(env.events):,}")
    print(f"  Store Min Timestamp: {env.min_ts_ns}")
    print(f"  Store Max Timestamp: {env.max_ts_ns}")
    retrieved_span_s = (env.max_ts_ns - env.min_ts_ns) / 1e9
    print(f"  Retrieved Duration Span: {retrieved_span_s:.2f} s")
    assert len(env.events) > 1000, f"Expected > 1000 events retrieved, got {len(env.events)} (stale 5-event bug)!"

    # 3. Decoded ERR/EV Events & Non-None Verification
    raw_data = out_fss.read_bytes()
    records = list(iter_envelopes(raw_data))
    schema_reg = load_schema_file(str(PROJECT_ROOT / "schemas" / "drone_v0.yaml"))
    decoded = decode_all(records, schema_reg)

    err_events = [e for e in decoded if e.event_type == 0x45]
    print(f"\n[3. Decoded ERROR_EVENT (0x45) Records: {len(err_events)} total]")
    for idx, e in enumerate(err_events):
        sub = e.fields.get("subsystem_code")
        err = e.fields.get("error_code")
        print(f"  #{idx+1} @ {e.timestamp_ns}: subsystem_code={sub}, error_code={err}")
        assert sub is not None, f"subsystem_code is None in {e}!"
        assert err is not None, f"error_code is None in {e}!"

    # 4. Error-Manual Matching
    catalog = load_builtin_manual("ardupilot")
    ev_dicts = [e.to_dict() for e in decoded]
    matched = enrich_evidence_with_manual({"events": ev_dicts}, catalog)
    print(f"\n[4. ArduPilot Error-Manual Matching]")
    print(f"  Loaded Rules in Manual: {len(catalog)}")
    print(f"  Matched Rules: {len(matched)}")
    for m in matched:
        print(f"  - [{m.get('vendor', 'ardupilot').upper()}] {m.get('error_code')}: {m.get('title')} (Subsystem: {m.get('subsystem')})")
    assert len(matched) > 0, "Expected > 0 matched error-manual rules!"
    matched_str = " ".join(m.get("title", "") for m in matched).upper()
    assert "LIDAR_TIMEOUT" not in matched_str, "Spurious LIDAR_TIMEOUT found in matched rules!"

    # 5. Token Budget Compaction at num_ctx=16384
    evidence = {
        "events": ev_dicts[:1671],
        "retrieval": {"returned": len(ev_dicts[:1671])},
        "sources": [1],
    }
    compacted, was_c, w_msg = preflight_budget_guard(
        evidence,
        query="Diagnose ArduPilot flight failure",
        knowledge=[],
        effective_num_ctx=16384,
        enable_thinking=False,
    )
    post_count = len(compacted["events"])
    print(f"\n[5. Token Budget Compaction at num_ctx=16384]")
    print(f"  Initial Events: {len(evidence['events']):,}")
    print(f"  Compacted Events: {post_count}")
    print(f"  Compacted Warning: {w_msg}")
    assert 100 <= post_count <= 220, f"Compacted event count {post_count} is outside [100, 220]!"

    print("\n================================================================================")
    print("  ALL LOG171 VERIFICATION CHECKS PASSED ✅")
    print("================================================================================")


if __name__ == "__main__":
    main()
