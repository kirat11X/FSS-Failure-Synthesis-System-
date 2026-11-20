import struct
from pathlib import Path

import pytest

from bridge.java_retriever import JavaRetriever
from fss_py.model import ALL
from streamlit_app import iter_any_envelopes


def test_iter_any_envelopes_normalizes_timestamps():
    """Verify iter_any_envelopes converts millisecond timestamps (<10^14) to nanoseconds."""
    HEADER = struct.Struct(">IQII")
    ms_ts = 1718000000000  # Milliseconds epoch
    raw = HEADER.pack(42, ms_ts, 4, 1) + b"TEST"
    envelopes = iter_any_envelopes(raw)
    assert len(envelopes) == 1
    assert envelopes[0].source_id == 42
    assert envelopes[0].timestamp_ns == ms_ts * 1_000_000  # Must be promoted to nanoseconds


def test_custom_fss_bin_ingest_and_retrieval(tmp_path: Path):
    """Test end-to-end ingestion and retrieval of custom .fss.bin files."""
    test_fss = tmp_path / "custom_telemetry.fss.bin"
    HEADER = struct.Struct(">IQII")
    base_ts = 1718000000000
    data = bytearray()
    for i in range(20):
        ts = base_ts + i * 100
        data.extend(HEADER.pack(101, ts, 4, 1))
        data.extend(b"\x01\x02\x03\x04")
    test_fss.write_bytes(data)

    retriever = JavaRetriever.from_fixtures(test_fss)

    # 1. Query with nanoseconds window
    t_min_ns = base_ts * 1_000_000
    t_max_ns = (base_ts + 2000) * 1_000_000
    res_ns = retriever.retrieve(ALL, t_min_ns, t_max_ns, max_events=50)
    assert len(res_ns.events) == 20
    assert res_ns.events[0].source_id == 101

    # 2. Query with millisecond window (tolerant promotion in JavaStoreBridge)
    res_ms = retriever.retrieve(ALL, base_ts, base_ts + 2000, max_events=50)
    assert len(res_ms.events) == 20


def test_custom_contract_v0_bin_ingest_and_retrieval():
    """Test ingestion of standard Contract v0 golden binaries."""
    golden = Path("fixtures/golden/scenario_298_474.bin")
    retriever = JavaRetriever.from_fixtures(golden)
    res = retriever.retrieve(ALL, 1791454020000000000, 1791454025000000000, max_events=50)
    assert len(res.events) == 9


def test_custom_ardupilot_bin_upload_translation():
    """Test that a raw ArduPilot DataFlash .bin log is auto-detected and translated to .fss.bin."""
    raw_df = Path("test_data/irl/dataflash_sample_1.bin")
    if not raw_df.exists():
        pytest.skip("dataflash_sample_1.bin not found")
    retriever = JavaRetriever.from_fixtures(raw_df)
    # Query within the log's timeline (starts at 1718001470810000000 ns)
    t_start = 1718001470810000000
    res = retriever.retrieve(ALL, t_start, t_start + 60 * 10**9, max_events=100)
    assert len(res.events) > 0
