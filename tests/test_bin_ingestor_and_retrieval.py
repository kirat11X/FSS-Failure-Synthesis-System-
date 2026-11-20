"""Unit and integration tests for BinCacheManager, retrieve_full_dataset, and condense_telemetry."""

import hashlib
import io
import struct
import tempfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from bridge.bin_ingestor import (
    BinCacheManager,
    compute_sha256,
    condense_telemetry,
    is_critical_event,
    normalize_time_bounds,
    safe_filename,
)
from bridge.java_retriever import JavaRetriever
from bridge.schemas import (
    DataGapWire,
    RetrievalEnvelope,
    RetrieveRequestWire,
    TelemetryEventWire,
)
from fss_py.model import ALL, RetrievalRequestError


# ---------------------------------------------------------------------------
# 1. Deterministic Hash Computation & Safe Filename
# ---------------------------------------------------------------------------

def test_compute_sha256_bytes_and_stream():
    sample_data = b"FSS_TELEMETRY_PAYLOAD_TEST_BYTES"
    expected = hashlib.sha256(sample_data).hexdigest()

    assert compute_sha256(sample_data) == expected
    assert compute_sha256(io.BytesIO(sample_data)) == expected


def test_safe_filename():
    assert safe_filename("flight log (nov 20).bin") == "flight_log__nov_20_.bin"
    assert safe_filename("///evil/path/../test.fss.bin") == "test.fss.bin"
    assert safe_filename("") == "upload.bin"


# ---------------------------------------------------------------------------
# 2. BinCacheManager & Ingestion Reuse
# ---------------------------------------------------------------------------

def test_bin_cache_manager_reuse(tmp_path: Path):
    cache_mgr = BinCacheManager(tmp_path / "uploads")
    payload = b"\x01\x02\x03\x04\x05"
    h = hashlib.sha256(payload).hexdigest()

    # 1. First ingest - writes raw upload
    p1, cached1 = cache_mgr.get_or_ingest(payload, "sample_flight.bin")
    assert not cached1
    assert p1.exists()
    assert p1.name == f"{h}_sample_flight.bin"

    # 2. Second ingest with identical content - reuses cached file
    p2, cached2 = cache_mgr.get_or_ingest(payload, "sample_flight.bin")
    assert cached2
    assert p1 == p2


def test_bin_cache_manager_ardupilot_conversion(tmp_path: Path, monkeypatch):
    cache_mgr = BinCacheManager(tmp_path / "uploads")
    ardupilot_magic = b"\xA3\x95" + b"\x00" * 20

    # Mock convert_log to write dummy .fss.bin without needing full pymavlink
    def mock_convert_log(in_p, out_p, agent_id=1):
        out_p.write_bytes(b"\x00\x00\x00\x01FSS_CONVERTED")

    import sys
    mock_module = type(sys)("scripts.convert_ardupilot_bin_to_fss")
    mock_module.convert_log = mock_convert_log
    sys.modules["scripts.convert_ardupilot_bin_to_fss"] = mock_module

    p1, cached1 = cache_mgr.get_or_ingest(ardupilot_magic, "flight.bin")
    assert not cached1
    assert p1.suffix == ".bin"
    assert p1.name.endswith(".fss.bin")
    assert p1.read_bytes() == b"\x00\x00\x00\x01FSS_CONVERTED"

    # Second call should reuse the converted .fss.bin
    p2, cached2 = cache_mgr.get_or_ingest(ardupilot_magic, "flight.bin")
    assert cached2
    assert p1 == p2


# ---------------------------------------------------------------------------
# 3. uint64 Inverted Bounds Fix & Normalization
# ---------------------------------------------------------------------------

def test_normalize_time_bounds_out_of_order_events():
    # Out of order timestamps from events
    events = [
        {"timestamp_ns": 2_000_000_000, "source_id": 1},
        {"timestamp_ns": 1_000_000_000, "source_id": 1},
        {"timestamp_ns": 3_000_000_000, "source_id": 1},
    ]
    t_start, t_end = normalize_time_bounds(None, None, events=events)
    assert t_start == 1_000_000_000
    assert t_end == 3_000_000_000
    assert t_start <= t_end


def test_normalize_time_bounds_inverted_input():
    t_start, t_end = normalize_time_bounds(500, 100)
    assert t_start == 100
    assert t_end == 500

    # Equal bounds incremented to guarantee half-open interval validity
    t_s2, t_e2 = normalize_time_bounds(100, 100)
    assert t_s2 == 100
    assert t_e2 == 101


def test_retrieve_request_wire_schema_inverted_bounds():
    req = RetrieveRequestWire(
        store_path="/tmp/store.dat",
        start_ns="2000000000",
        end_ns="1000000000",
        trace_id="a" * 32,
    )
    assert req.start_ns == "1000000000"
    assert req.end_ns == "2000000000"
    assert req.scope == "full_dataset"


def test_data_gap_wire_inverted_bounds():
    gap = DataGapWire(
        source_id=1,
        gap_start_ns=900,
        gap_end_ns=300,
        reason="TRUNCATED",
    )
    assert gap.gap_start_ns == 300
    assert gap.gap_end_ns == 900


def test_retrieval_envelope_inverted_bounds():
    env = RetrievalEnvelope(
        status="OK",
        trace_id="b" * 32,
        min_ts_ns=5000,
        max_ts_ns=1000,
    )
    assert env.min_ts_ns == 1000
    assert env.max_ts_ns == 5000


# ---------------------------------------------------------------------------
# 4. Telemetry Condensation
# ---------------------------------------------------------------------------

def test_condense_telemetry_keeps_100_percent_critical():
    events = []
    base_ts = 1_000_000_000

    # 1. Generate 2000 repetitive nominal attitude frames (0x40)
    for i in range(2000):
        events.append({
            "source_id": 1,
            "timestamp_ns": base_ts + i * 1_000_000,
            "event_type": 0x40,
            "event_name": "ATTITUDE",
            "fields": {"roll_deg": 0.1, "pitch_deg": 0.0, "yaw_deg": 45.0},
            "decode_status": "SUCCESS",
        })

    # 2. Inject critical errors, avoidance, faults, and breaches
    critical_injections = [
        {"source_id": 1, "timestamp_ns": base_ts + 50_000_000, "event_type": 0x45, "event_name": "ERROR_EVENT", "fields": {"subsystem_code": 1, "error_code": 99}},
        {"source_id": 2, "timestamp_ns": base_ts + 100_000_000, "event_type": 0x20, "event_name": "AVOIDANCE", "fields": {"reason_code": 1, "reason": "COLLISION_RISK"}},
        {"source_id": 1, "timestamp_ns": base_ts + 150_000_000, "event_type": 0x10, "event_name": "LIDAR_STATUS", "fields": {"status_code": 17, "timeout_count": 3}},
        {"source_id": 1, "timestamp_ns": base_ts + 200_000_000, "event_type": 0x30, "event_name": "DIAGNOSTIC", "fields": {"status_code": 2, "status_name": "FAULT"}},
        {"source_id": 1, "timestamp_ns": base_ts + 250_000_000, "event_type": 0x21, "event_name": "FORMATION_STATE", "fields": {"state_code": 1, "state": "LEFT_FORMATION"}},
        {"source_id": 1, "timestamp_ns": base_ts + 300_000_000, "event_type": 0x43, "event_name": "EKF_STATUS", "fields": {"compass_variance": 0.89}},
    ]
    events.extend(critical_injections)

    # Condense down to 500 events
    condensed = condense_telemetry(events, max_events_target=500)
    assert len(condensed) <= 500

    # Verify 100% of critical events were retained
    types_in_condensed = [c["event_type"] for c in condensed]
    for crit in critical_injections:
        assert crit["event_type"] in types_in_condensed
        # Verify specific matching event is present
        matching = [c for c in condensed if c["timestamp_ns"] == crit["timestamp_ns"] and c["event_type"] == crit["event_type"]]
        assert len(matching) == 1

    # Verify timestamps are in non-decreasing order
    ts_list = [c["timestamp_ns"] for c in condensed]
    assert ts_list == sorted(ts_list)


# ---------------------------------------------------------------------------
# 5. Whole-Dataset Retrieval Mode (retrieve_full_dataset)
# ---------------------------------------------------------------------------

def test_retrieve_full_dataset_golden(tmp_path: Path):
    golden = Path("fixtures/golden/scenario_298_474.bin")
    retriever = JavaRetriever.from_fixtures(golden)

    env = retriever.retrieve_full_dataset(source=ALL, max_events=100)
    assert env.status == "OK"
    assert env.count == 9
    assert len(env.events) == 9
    assert not env.truncated
    assert env.min_ts_ns is not None
    assert env.max_ts_ns is not None
    assert env.min_ts_ns <= env.max_ts_ns


def test_retrieve_full_dataset_chunking_mock():
    """Verify that retrieve_full_dataset chunks spans greater than 1 hour (3600s)."""
    retriever = JavaRetriever(Path("/tmp/dummy_store.dat"))

    # Span is 2.5 hours: min_ts = 0, max_ts = 9_000_000_000_000 ns (2.5h)
    chunk_calls = []

    def mock_retrieve_envelope(source, start_ns, end_ns, max_events=10000, event_type=None, trace_id=None):
        chunk_calls.append((start_ns, end_ns))
        # Initial probe
        if start_ns == 0 and end_ns == 1000:
            return RetrievalEnvelope(
                status="EMPTY",
                trace_id="c" * 32,
                min_ts_ns=0,
                max_ts_ns=9_000_000_000_000,
            )
        # Return dummy events for each chunk
        ev = TelemetryEventWire(
            source_id=1,
            timestamp_ns=start_ns + 100,
            event_type=0x10,
            payload_b64="AAAA",
        )
        return RetrievalEnvelope(
            status="OK",
            trace_id="c" * 32,
            count=1,
            events=[ev],
            min_ts_ns=0,
            max_ts_ns=9_000_000_000_000,
        )

    retriever.retrieve_envelope = mock_retrieve_envelope
    full_env = retriever.retrieve_full_dataset(source=ALL, max_events=50)

    assert full_env.status == "OK"
    assert full_env.count == 3  # 3 chunks for 2.5 hours: [0, 1h), [1h, 2h), [2h, 2.5h)
    # 1 probe + 3 chunks = 4 calls
    assert len(chunk_calls) == 4
    # Chunk 1: [0, 3600*10^9)
    assert chunk_calls[1] == (0, 3_600_000_000_000)
    # Chunk 2: [3600*10^9, 7200*10^9)
    assert chunk_calls[2] == (3_600_000_000_000, 7_200_000_000_000)
    # Chunk 3: [7200*10^9, 9000*10^9)
    assert chunk_calls[3] == (7_200_000_000_000, 9_000_000_000_000)
