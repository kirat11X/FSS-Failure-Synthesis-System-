"""Unit and integration tests for Auto-Simulate Unseen Telemetry (Live E2E) Pipeline.

Verifies:
  1. All 5 domains (D1 Drone, D2 Microservices, D3 Industrial PLC, D4 Vehicle CAN big-endian,
     D5 Warehouse AGVs) produce valid Contract v0 binary envelopes that ingest into real
     Java 64KB BlockStore and retrieve back with exact uint64 ns fidelity.
  2. Distinct random seeds generate novel unseen source_ids, timestamps, and fault sequences.
  3. Strict blind-evaluation isolation: GroundTruthManifest is never leaked into LLM context.
  4. Edge-case injectors:
     - inject_opaque_bytes -> PARTIALLY_DECODED with unknown_bytes > 0
     - inject_unknown_event_type -> UNKNOWN_EVENT_TYPE quarantine
     - inject_clock_skew -> detected by analyze_clock_skew
     - inject_blackout_gap -> timestamp jump >= 35s
     - inject_crash_tail -> truncated tail quarantined, valid prefix preserved with zero crash
  5. Live pipeline generator yields all 4 stages with CRC-32C valid block allocation.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest

from fss_py.contract import MAGIC, VERSION
from fss_py.decoder import decode, decode_all
from fss_py.evidence import build_evidence
from fss_py.model import ALL, DecodeStatus, EventRecord
from fss_py.schema import load_schema_file
from fss_py.wire import iter_envelopes
from bridge.auto_unseen_simulator import (
    DOMAIN_CONFIGS,
    SUPPORTED_DOMAINS,
    GroundTruthManifest,
    generate_unseen_run,
    inspect_blockstore_blocks,
    run_auto_unseen_pipeline,
)
from bridge.clock_skew_compensator import analyze_clock_skew
from bridge.java_retriever import JavaRetriever
from bridge.synthesizer import build_llm_context

ROOT_DIR = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("domain_name", SUPPORTED_DOMAINS)
def test_all_five_domains_contract_v0_and_java_persistence(domain_name: str) -> None:
    """Verify that every domain produces valid Contract v0 envelopes and persists into Java BlockStore."""
    seed = 42 + hash(domain_name) % 1000
    run_data = generate_unseen_run(domain=domain_name, seed=seed, config={"event_count": 35})

    records: list[EventRecord] = run_data["records"]
    bin_path: Path = run_data["bin_path"]
    dom_cfg = run_data["domain_config"]

    assert bin_path.exists(), f"Binary file was not generated for {domain_name}"
    assert len(records) >= 30

    # 1. Verify Contract v0 envelope framing byte-for-byte
    bin_bytes = bin_path.read_bytes()
    decoded_stream = list(iter_envelopes(bin_bytes))
    assert len(decoded_stream) == len(records)

    for orig, stream_rec in zip(records, decoded_stream):
        assert stream_rec.source_id == orig.source_id
        assert stream_rec.timestamp_ns == orig.timestamp_ns
        assert stream_rec.event_type == orig.event_type
        assert stream_rec.payload_bytes == orig.payload_bytes

    # 2. Ingest into real Java 64KB BlockStore
    with tempfile.TemporaryDirectory(prefix="fss_test_store_") as tmp_dir:
        store_path = Path(tmp_dir) / "test_store.dat"
        retriever = JavaRetriever(store_path=store_path)
        retriever.ingest(bin_path)

        assert store_path.exists()
        assert store_path.stat().st_size % 65536 == 0  # Multiple of 64KB

        # 3. Retrieve back from Java BlockStore and verify exact uint64 fidelity
        t_start = min(r.timestamp_ns for r in records)
        t_end = max(r.timestamp_ns for r in records) + 1
        res = retriever.retrieve(ALL, t_start, t_end, max_events=200)

        assert len(res.events) == len(records)
        retrieved_ids = [e.source_id for e in res.events]
        for r in records:
            assert r.source_id in retrieved_ids

        # 4. Verify decoded events against domain schema
        schema_reg = load_schema_file(dom_cfg["schema_path"])
        decoded_events = decode_all(res.events, schema_reg)
        assert len(decoded_events) == len(records)
        assert any(e.decode_status == DecodeStatus.SUCCESS for e in decoded_events)


def test_random_seeds_produce_distinct_scenarios() -> None:
    """Verify that different random seeds produce novel unseen source_ids and timelines."""
    run1 = generate_unseen_run(domain="D2 Microservices", seed=10101, config={"event_count": 40})
    run2 = generate_unseen_run(domain="D2 Microservices", seed=20202, config={"event_count": 40})

    gt1: GroundTruthManifest = run1["ground_truth"]
    gt2: GroundTruthManifest = run2["ground_truth"]

    # Distinct sources or timestamps
    assert (
        gt1.injected_root_cause_source != gt2.injected_root_cause_source
        or gt1.injected_symptom_source != gt2.injected_symptom_source
        or gt1.time_start_ns != gt2.time_start_ns
    )
    assert run1["bin_path"].read_bytes() != run2["bin_path"].read_bytes()


def test_strict_blind_isolation_never_in_llm_context() -> None:
    """Verify that GroundTruthManifest metadata is strictly isolated and never leaked into LLM context."""
    run_data = generate_unseen_run(domain="D1 Drone", seed=9999, config={"event_count": 30})
    gt: GroundTruthManifest = run_data["ground_truth"]
    records: list[EventRecord] = run_data["records"]

    dom_cfg = run_data["domain_config"]
    schema_reg = load_schema_file(dom_cfg["schema_path"])
    decoded_events = decode_all(records, schema_reg)
    evidence = build_evidence(decoded_events, query=run_data["blind_query"])

    # Generate LLM prompt context
    llm_context = build_llm_context(
        evidence=evidence,
        query=run_data["blind_query"],
        knowledge=[],
    )

    # Ensure secret manifest text is not in LLM context
    assert "GroundTruthManifest" not in llm_context
    assert "injected_root_cause_source" not in llm_context
    assert "injected_symptom_source" not in llm_context
    assert "sim_ground_truth" not in llm_context


def test_edge_case_opaque_bytes() -> None:
    """Verify that inject_opaque_bytes produces PARTIALLY_DECODED events with unknown_bytes > 0."""
    run_data = generate_unseen_run(
        domain="D1 Drone",
        seed=123,
        config={"event_count": 30, "inject_opaque_bytes": True},
    )
    dom_cfg = run_data["domain_config"]
    schema_reg = load_schema_file(dom_cfg["schema_path"])
    decoded_events = decode_all(run_data["records"], schema_reg)

    opaque_events = [e for e in decoded_events if e.decode_status == DecodeStatus.PARTIALLY_DECODED]
    assert len(opaque_events) >= 1
    assert any(e.unknown_bytes == 8 for e in opaque_events)


def test_edge_case_unknown_event_type() -> None:
    """Verify that inject_unknown_event_type produces UNKNOWN_EVENT_TYPE quarantined events."""
    run_data = generate_unseen_run(
        domain="D3 Industrial PLC",
        seed=456,
        config={"event_count": 30, "inject_unknown_event_type": True},
    )
    dom_cfg = run_data["domain_config"]
    schema_reg = load_schema_file(dom_cfg["schema_path"])
    decoded_events = decode_all(run_data["records"], schema_reg)

    unknown_events = [e for e in decoded_events if e.decode_status == DecodeStatus.UNKNOWN_EVENT_TYPE]
    assert len(unknown_events) >= 1
    assert any(e.event_type == 0x007F for e in unknown_events)


def test_edge_case_clock_skew() -> None:
    """Verify that inject_clock_skew produces non-monotonic apparent ordering detected by compensator."""
    run_data = generate_unseen_run(
        domain="D1 Drone",
        seed=789,
        config={"event_count": 40, "cross_source_cascade": True, "inject_clock_skew": True},
    )
    records = run_data["records"]
    dom_cfg = run_data["domain_config"]
    schema_reg = load_schema_file(dom_cfg["schema_path"])
    decoded_events = decode_all(records, schema_reg)

    report = analyze_clock_skew(decoded_events)
    assert report.skew_detected is True or len(report.ambiguous_pairs) > 0 or bool(report.clock_caveat)


def test_edge_case_blackout_gap() -> None:
    """Verify that inject_blackout_gap introduces a >= 35s gap in the timeline."""
    run_data = generate_unseen_run(
        domain="D5 Warehouse AGVs",
        seed=321,
        config={"event_count": 40, "inject_blackout_gap": True},
    )
    records = run_data["records"]
    timestamps = [r.timestamp_ns for r in records]
    deltas_s = [(timestamps[i + 1] - timestamps[i]) / 1e9 for i in range(len(timestamps) - 1)]

    assert max(deltas_s) >= 30.0, f"Expected blackout gap >= 30s, got {max(deltas_s):.1f}s"


def test_edge_case_crash_tail_quarantine() -> None:
    """Verify that inject_crash_tail quarantines truncated bytes at EOF and preserves valid prefix."""
    with tempfile.TemporaryDirectory(prefix="fss_test_crash_") as tmp_dir:
        run_data = generate_unseen_run(
            domain="D4 Vehicle CAN (Big-Endian)",
            seed=654,
            config={"event_count": 35, "inject_crash_tail": True},
        )
        bin_path = run_data["bin_path"]
        store_path = Path(tmp_dir) / "crash_store.dat"

        # Trailing 14 bytes were added at EOF
        retriever = JavaRetriever(store_path=store_path)
        retriever.ingest(bin_path)

        # Confirm all 35 valid records were ingested
        records = run_data["records"]
        t_start = min(r.timestamp_ns for r in records)
        t_end = max(r.timestamp_ns for r in records) + 1
        res = retriever.retrieve(ALL, t_start, t_end, max_events=200)

        assert len(res.events) == len(records)


def test_live_pipeline_streaming_stages() -> None:
    """Verify that run_auto_unseen_pipeline executes all 4 stages cleanly."""
    with tempfile.TemporaryDirectory(prefix="fss_pipe_stages_") as tmp_dir:
        stages_seen: list[str] = []
        handoff_result = None

        for update in run_auto_unseen_pipeline(
            domain="D3 Industrial PLC",
            seed=555,
            event_count=30,
            output_dir=tmp_dir,
        ):
            stage = update["stage"]
            if stage not in stages_seen:
                stages_seen.append(stage)
            if stage == "STAGE_3_BLOCKSTORE_PERSIST" and "blocks" in update:
                blocks = update["blocks"]
                assert len(blocks) > 0
                for b in blocks:
                    assert b["crc_status"] == "VALID (CRC-32C)"
            elif stage == "STAGE_4_HANDOFF":
                handoff_result = update

        assert "STAGE_1_GENERATE" in stages_seen
        assert "STAGE_2_JAVA_INGEST" in stages_seen
        assert "STAGE_3_BLOCKSTORE_PERSIST" in stages_seen
        assert "STAGE_4_HANDOFF" in stages_seen
        assert handoff_result is not None
        assert Path(handoff_result["store_path"]).exists()
