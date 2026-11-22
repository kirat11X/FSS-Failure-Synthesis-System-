"""Tests for conservative cross-source clock caveats.

Verifies:
  1. Producer timestamps are never mutated.
  2. Cross-source timestamp ordering is not treated as causal evidence.
  3. Single-source and multi-source reports are handled consistently.
  4. DecodedEvent, EventRecord, and raw dictionaries are accepted.
"""

from __future__ import annotations

import copy
from pathlib import Path

import pytest

from bridge.clock_skew_compensator import (
    ClockSkewReport,
    analyze_clock_skew,
)
from fss_py.decoder import decode_all
from fss_py.model import DecodeStatus, DecodedEvent, EventRecord
from fss_py.schema import load_schema_file
from fss_py.wire import iter_envelopes

ROOT_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
def drone_registry():
    return load_schema_file(str(ROOT_DIR / "schemas" / "drone_v0.yaml"))


@pytest.fixture
def golden_clock_skew_events(drone_registry):
    bin_path = ROOT_DIR / "fixtures" / "golden" / "clock_skew_pair.bin"
    raw = bin_path.read_bytes()
    records = list(iter_envelopes(raw))
    return decode_all(records, drone_registry)


def test_01_golden_multi_source_events_only_report_clock_caveat(golden_clock_skew_events):
    """A multi-source fixture does not justify inferred skew or causal ordering."""
    # Create deep copies of timestamps to verify Hard Rule 5 (no mutation)
    original_timestamps = [e.timestamp_ns for e in golden_clock_skew_events]
    original_dicts = [copy.deepcopy(e.to_dict()) for e in golden_clock_skew_events]

    report = analyze_clock_skew(golden_clock_skew_events)

    assert report.skew_detected is False
    assert 115 in report.sources
    assert 220 in report.sources

    # 2. Hard Rule 5: Timestamps MUST NOT be mutated
    for idx, e in enumerate(golden_clock_skew_events):
        assert e.timestamp_ns == original_timestamps[idx], "Producer timestamp_ns was mutated!"
        assert e.to_dict() == original_dicts[idx], "DecodedEvent dict representation was mutated!"

    assert report.estimated_offset_bounds_ns == {}
    assert report.ambiguous_pairs == []
    assert report.lamport_order_notes == []
    assert "does not" in report.clock_caveat
    assert "causality" in report.clock_caveat


def test_02_hard_rule_5_producer_timestamp_never_mutated():
    """Verify under all representations that timestamp_ns is never mutated or replaced."""
    # Synthetic events with different producer timestamps.
    e_cause = DecodedEvent(
        event_id="ev-cause",
        source_id=1,
        timestamp_ns=2000_000_000,  # 2.0s
        event_type=0x10,
        event_name="EVENT_A",
        schema_version=1,
        decode_status=DecodeStatus.SUCCESS,
        fields={"value": 1},
        interpreted_fields=["status_name"],
        unknown_bytes=0,
        reason=None,
        warnings=[],
        payload_len=6,
    )
    e_react = DecodedEvent(
        event_id="ev-react",
        source_id=2,
        timestamp_ns=1000_000_000,  # 1.0s (appears earlier due to clock skew)
        event_type=0x20,
        event_name="EVENT_B",
        schema_version=1,
        decode_status=DecodeStatus.SUCCESS,
        fields={"value": 2},
        interpreted_fields=["reason"],
        unknown_bytes=0,
        reason=None,
        warnings=[],
        payload_len=6,
    )

    events = [e_cause, e_react]
    report = analyze_clock_skew(events)

    assert report.skew_detected is False
    assert report.estimated_offset_bounds_ns == {}
    # Strictly check timestamps
    assert e_cause.timestamp_ns == 2000_000_000
    assert e_react.timestamp_ns == 1000_000_000


def test_03_cross_source_values_do_not_create_causal_order():
    """Opaque event names and values cannot establish a cross-source causal order."""
    events = [
        {
            "event_id": "ev-source7-event",
            "source_id": 7,
            "timestamp_ns": 100_000_000,
            "event_name": "EVENT_A",
            "fields": {"value": "A"},
        },
        {
            "event_id": "ev-source12-event",
            "source_id": 12,
            "timestamp_ns": 250_000_000,
            "event_name": "EVENT_B",
            "fields": {"value": "B"},
        },
    ]

    report = analyze_clock_skew(events)
    assert report.skew_detected is False
    assert report.estimated_offset_bounds_ns == {}
    assert report.lamport_order_notes == []
    assert "causality" in report.clock_caveat


def test_04_single_source_no_skew_no_caveat():
    """Verify that single-source events produce no skew and no caveat."""
    events = [
        {"source_id": 10, "timestamp_ns": 1000, "event_name": "LIDAR_STATUS", "fields": {"status_code": 1}},
        {"source_id": 10, "timestamp_ns": 2000, "event_name": "AVOIDANCE", "fields": {"reason_code": 1}},
    ]
    report = analyze_clock_skew(events)
    assert report.skew_detected is False
    assert report.estimated_offset_bounds_ns == {}
    assert report.ambiguous_pairs == []
    assert report.clock_caveat == ""
    assert report.sources == [10]


def test_05_multi_source_monotonic_caveat():
    """Verify that multiple sources with normal monotonic order receive standard multi-source caveat."""
    events = [
        {"source_id": 1, "timestamp_ns": 1000, "event_name": "HEARTBEAT", "fields": {}},
        {"source_id": 2, "timestamp_ns": 2000, "event_name": "HEARTBEAT", "fields": {}},
    ]
    report = analyze_clock_skew(events)
    assert report.skew_detected is False
    assert report.estimated_offset_bounds_ns == {}
    assert "synchronization is unverified" in report.clock_caveat
    assert "does not establish event order or causality" in report.clock_caveat


def test_06_event_record_inputs():
    """Verify that analyze_clock_skew handles raw EventRecord instances without errors."""
    r1 = EventRecord(source_id=1, timestamp_ns=1000, event_type=0x10, payload_bytes=b"\x00" * 8)
    r2 = EventRecord(source_id=2, timestamp_ns=2000, event_type=0x20, payload_bytes=b"\x00" * 8)

    report = analyze_clock_skew([r1, r2])
    assert report.sources == [1, 2]
    # Timestamps intact
    assert r1.timestamp_ns == 1000
    assert r2.timestamp_ns == 2000


def test_07_clock_skew_report_to_dict():
    """Verify ClockSkewReport serialization."""
    rep = ClockSkewReport(
        skew_detected=True,
        estimated_offset_bounds_ns={(115, 220): (170_000_000, 1_170_000_000)},
        ambiguous_pairs=[{"pair": 1}],
        lamport_order_notes=["Note 1"],
        clock_caveat="Caveat text",
        sources=[115, 220],
    )
    d = rep.to_dict()
    assert d["skew_detected"] is True
    assert d["estimated_offset_bounds_ns"] == {"115->220": [170_000_000, 1_170_000_000]}
    assert d["sources"] == [115, 220]
    assert d["clock_caveat"] == "Caveat text"
