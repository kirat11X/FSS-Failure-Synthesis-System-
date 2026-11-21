"""Regression test suite for unified ArduPilot epoch conversion, ERR/EV decoding,

error manual matching, and context token budget compaction.
"""

from __future__ import annotations

import struct
from pathlib import Path

import pytest
from fss_py.decoder import decode_all
from fss_py.schema import load_schema_file
from fss_py.wire import iter_envelopes

from bridge.error_catalog import (
    enrich_evidence_with_manual,
    extract_error_codes_from_events,
    load_builtin_manual,
)
from bridge.synthesizer import (
    build_llm_context,
    estimate_tokens,
    preflight_budget_guard,
)
from scripts.convert_ardupilot_bin_to_fss import convert_log

PROJECT_ROOT = Path(__file__).resolve().parent.parent
LOG171_PATH = PROJECT_ROOT / "test_data" / "irl" / "log171.bin"


@pytest.fixture(scope="module")
def converted_log171(tmp_path_factory) -> tuple[Path, dict]:
    """Convert log171.bin once for the test module."""
    if not LOG171_PATH.exists():
        pytest.skip(f"Test fixture not found: {LOG171_PATH}")
    tmp_dir = tmp_path_factory.mktemp("ardupilot_test")
    out_fss = tmp_dir / "log171.fss.bin"
    meta = convert_log(LOG171_PATH, out_fss, agent_id=1, max_records=5000)
    return out_fss, meta


def test_unified_epoch_conversion(converted_log171):
    """Verify log171 conversion enforces ONE unified clock base and realistic flight duration (<3600s)."""
    out_fss, meta = converted_log171
    assert out_fss.exists()
    assert meta["total_envelopes"] == 5000

    # True flight duration is ~242s (4.04 min), NOT 270,454,531s (~8.57 years)
    assert meta["duration_s"] < 3600.0, f"Duration {meta['duration_s']}s exceeds 1-hour window limit!"
    assert 200.0 <= meta["duration_s"] <= 300.0, f"Unexpected flight duration: {meta['duration_s']}s"

    # Verify all records decode cleanly with valid Contract v0 MAGIC (0xF55A)
    raw_data = out_fss.read_bytes()
    records = list(iter_envelopes(raw_data))
    assert len(records) == 5000

    # Verify timestamps are consistently within Nov 2015 epoch (1.448e18 ns)
    min_ts = min(r.timestamp_ns for r in records)
    max_ts = max(r.timestamp_ns for r in records)
    assert 1_448_000_000_000_000_000 <= min_ts <= 1_449_000_000_000_000_000
    assert 1_448_000_000_000_000_000 <= max_ts <= 1_449_000_000_000_000_000


def test_err_ev_mode_msg_decoding(converted_log171):
    """Verify that ERR, EV, MSG, and MODE events are retained and decoded accurately."""
    out_fss, _ = converted_log171
    raw_data = out_fss.read_bytes()
    records = list(iter_envelopes(raw_data))

    schema_reg = load_schema_file(str(PROJECT_ROOT / "schemas" / "drone_v0.yaml"))
    decoded_events = decode_all(records, schema_reg)

    err_events = [e for e in decoded_events if e.event_type == 0x45]
    assert len(err_events) >= 7, f"Expected at least 7 ERR/EV events, found {len(err_events)}"

    # Find the active EKF failsafe event (Subsys 16, ECode 2)
    ekf_failsafe = next(
        (e for e in err_events if e.fields.get("subsystem_code") == 16 and e.fields.get("error_code") == 2),
        None,
    )
    assert ekf_failsafe is not None, "ERR (Subsys 16, ECode 2) active fault event missing!"

    # Find the EKF failsafe cleared event (Subsys 16, ECode 0)
    ekf_cleared = next(
        (e for e in err_events if e.fields.get("subsystem_code") == 16 and e.fields.get("error_code") == 0),
        None,
    )
    assert ekf_cleared is not None, "ERR (Subsys 16, ECode 0) recovery event missing!"
    assert ekf_cleared.fields.get("error_code") == 0, "error_code 0 was incorrectly replaced by None!"

    # Verify EV event IDs are present in subsystem_code
    subsystem_codes = {e.fields.get("subsystem_code") for e in err_events}
    for expected_ev_id in (8, 10, 15, 25, 28):
        assert expected_ev_id in subsystem_codes, f"Event ID {expected_ev_id} missing from decoded events!"


def test_ardupilot_error_manual_matching(converted_log171):
    """Verify that decoded ERROR_EVENT records match ArduPilot error catalog with zero LIDAR_TIMEOUT citations."""
    out_fss, _ = converted_log171
    raw_data = out_fss.read_bytes()
    records = list(iter_envelopes(raw_data))

    schema_reg = load_schema_file(str(PROJECT_ROOT / "schemas" / "drone_v0.yaml"))
    decoded_events = decode_all(records, schema_reg)
    ev_dicts = [e.to_dict() for e in decoded_events]

    catalog = load_builtin_manual("ardupilot")
    assert len(catalog) > 0, "Failed to load ArduPilot builtin manual"

    matched_rules = enrich_evidence_with_manual({"events": ev_dicts}, catalog)
    assert len(matched_rules) > 0, "Expected > 0 matched error-manual rules for log171.bin!"

    matched_codes = {m["error_code"] for m in matched_rules}
    assert "SUBSYS_16_ECODE_2" in matched_codes, "Failed to match SUBSYS_16_ECODE_2 (EKF Failsafe)"
    assert "SUBSYS_16_ECODE_0" in matched_codes, "Failed to match SUBSYS_16_ECODE_0 (EKF Cleared)"
    assert "EV_28" in matched_codes or "SUBSYS_28_ECODE_0" in matched_codes

    # Ensure zero spurious LIDAR_TIMEOUT citations for ArduPilot flight
    matched_titles = " ".join(m.get("title", "") for m in matched_rules).upper()
    assert "LIDAR_TIMEOUT" not in matched_titles, "Spurious LIDAR_TIMEOUT matched in ArduPilot catalog!"


def test_num_ctx_16384_sane_compaction():
    """Verify that a 1,671-event timeline retains 100-220 events at num_ctx=16384 (never crushing to 7)."""
    events = []
    base_ts = 1448149430000000000
    for i in range(1671):
        ts = base_ts + (i * 100_000_000)
        is_fault = i in (50, 150, 500, 1000, 1600)
        events.append({
            "event_id": f"ev-{i:06x}",
            "source_id": 1,
            "timestamp_ns": ts,
            "event_type": 0x45 if is_fault else 0x40,
            "event_name": "ERROR_EVENT" if is_fault else "ATTITUDE",
            "decode_status": "SUCCESS",
            "fields": {"subsystem_code": 16, "error_code": 2} if is_fault else {"roll": 0.0, "pitch": 0.0, "yaw": 0.0},
        })

    evidence = {
        "events": events,
        "retrieval": {"returned": 1671},
        "sources": [1],
    }
    query = "Diagnose EKF and flight control anomalies in ArduPilot log"
    knowledge = []

    compacted_evidence, was_compacted, warning_msg = preflight_budget_guard(
        evidence,
        query,
        knowledge,
        effective_num_ctx=16384,
        enable_thinking=False,
    )

    assert was_compacted is True
    compacted_count = len(compacted_evidence["events"])

    # Compaction target: typically 100–220 events, NEVER crushing 1671 events down to 7
    assert 100 <= compacted_count <= 220, (
        f"Compacted event count {compacted_count} outside expected range [100, 220]!"
    )

    # 100% of all 5 fault events MUST be preserved
    compacted_ids = {e["event_id"] for e in compacted_evidence["events"]}
    for fault_idx in (50, 150, 500, 1000, 1600):
        expected_eid = f"ev-{fault_idx:06x}"
        assert expected_eid in compacted_ids, f"Fault event {expected_eid} was dropped by compaction!"

    # Verify context fits within prompt token budget
    context = build_llm_context(
        compacted_evidence,
        query,
        knowledge,
        effective_num_ctx=16384,
        _skip_budget_guard=True,
    )
    tokens = estimate_tokens(context)
    available_prompt_budget = 16384 - 1200
    assert tokens <= available_prompt_budget, f"Context tokens {tokens} exceeds available budget {available_prompt_budget}"
