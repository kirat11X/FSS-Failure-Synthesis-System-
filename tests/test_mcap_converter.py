"""Tests for Foxglove / ROS2 MCAP to FSS binary converter (scripts/convert_mcap_to_fss.py)."""

from __future__ import annotations

import json
import struct
import subprocess
import sys
from pathlib import Path

import pytest

from fss_py.decoder import decode
from fss_py.schema import load_schema_file
from fss_py.wire import iter_envelopes
from scripts.convert_mcap_to_fss import (
    EVENT_ATTITUDE,
    EVENT_AVOIDANCE,
    EVENT_BATTERY,
    EVENT_GPS,
    MCAP_MAGIC,
    OP_CHANNEL,
    OP_HEADER,
    OP_MESSAGE,
    OP_SCHEMA,
    convert_mcap_to_fss,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schemas" / "drone_v0.yaml"


def _p_str(s: str) -> bytes:
    encoded = s.encode("utf-8")
    return struct.pack("<I", len(encoded)) + encoded


def _p_bytes(b: bytes) -> bytes:
    return struct.pack("<I", len(b)) + b


def _record(opcode: int, body: bytes) -> bytes:
    return struct.pack("<BQ", opcode, len(body)) + body


def create_synthetic_mcap(path: Path, num_records: int = 5) -> None:
    """Generate a valid synthetic MCAP file with Foxglove JSON message records."""
    buf = bytearray()
    # 1. Header magic
    buf.extend(MCAP_MAGIC)

    # 2. Header record (0x01)
    hdr_body = _p_str("foxglove") + _p_str("fss-test-suite")
    buf.extend(_record(OP_HEADER, hdr_body))

    # 3. Schemas (0x03)
    schema_att = struct.pack("<H", 1) + _p_str("Attitude") + _p_str("jsonschema") + _p_bytes(b"{}")
    buf.extend(_record(OP_SCHEMA, schema_att))

    schema_bat = struct.pack("<H", 2) + _p_str("Battery") + _p_str("jsonschema") + _p_bytes(b"{}")
    buf.extend(_record(OP_SCHEMA, schema_bat))

    schema_avoid = struct.pack("<H", 3) + _p_str("Avoidance") + _p_str("jsonschema") + _p_bytes(b"{}")
    buf.extend(_record(OP_SCHEMA, schema_avoid))

    # 4. Channels (0x04)
    # Channel 1: /drone/attitude
    ch_att = struct.pack("<HH", 1, 1) + _p_str("/drone/attitude") + _p_str("json") + _p_bytes(b"")
    buf.extend(_record(OP_CHANNEL, ch_att))

    # Channel 2: /drone/battery
    ch_bat = struct.pack("<HH", 2, 2) + _p_str("/drone/battery") + _p_str("json") + _p_bytes(b"")
    buf.extend(_record(OP_CHANNEL, ch_bat))

    # Channel 3: /drone/avoidance
    ch_avoid = struct.pack("<HH", 3, 3) + _p_str("/drone/avoidance") + _p_str("json") + _p_bytes(b"")
    buf.extend(_record(OP_CHANNEL, ch_avoid))

    # 5. Messages (0x05)
    base_ns = 1_718_000_000_000_000_000
    for i in range(num_records):
        ts_ns = base_ns + i * 50_000_000  # +50ms

        # Message on channel 1 (Attitude)
        att_json = json.dumps({"roll_deg": 5.0 + i, "pitch_deg": -3.5, "yaw_deg": 90.0}).encode("utf-8")
        msg1_body = struct.pack("<HIQQ", 1, i, ts_ns, ts_ns) + att_json
        buf.extend(_record(OP_MESSAGE, msg1_body))

        # Message on channel 2 (Battery)
        bat_json = json.dumps({"voltage_v": 15.8, "current_a": 8.4, "remaining_pct": 82.5}).encode("utf-8")
        msg2_body = struct.pack("<HIQQ", 2, i, ts_ns + 1_000_000, ts_ns + 1_000_000) + bat_json
        buf.extend(_record(OP_MESSAGE, msg2_body))

        # Message on channel 3 (Avoidance)
        avoid_json = json.dumps({"reason_code": 1, "min_separation_m": 3.2, "heading_change_deg": -25}).encode("utf-8")
        msg3_body = struct.pack("<HIQQ", 3, i, ts_ns + 2_000_000, ts_ns + 2_000_000) + avoid_json
        buf.extend(_record(OP_MESSAGE, msg3_body))

    # 6. Footer magic
    buf.extend(MCAP_MAGIC)
    path.write_bytes(bytes(buf))


def test_mcap_converter_direct(tmp_path: Path):
    in_mcap = tmp_path / "stream.mcap"
    out_fss = tmp_path / "stream.fss.bin"

    num_records = 4
    create_synthetic_mcap(in_mcap, num_records=num_records)

    summary = convert_mcap_to_fss(in_mcap, out_fss, source_id=101)

    assert out_fss.exists()
    assert summary["records_written"] == num_records * 3
    assert summary["source_id"] == 101
    assert "0x40" in summary["type_counts"]
    assert "0x42" in summary["type_counts"]
    assert "0x20" in summary["type_counts"]

    # Verify wire envelopes via iter_envelopes
    records = list(iter_envelopes(out_fss.read_bytes()))
    assert len(records) == num_records * 3

    reg = load_schema_file(SCHEMA_PATH)

    # First record: ATTITUDE
    rec0 = records[0]
    assert rec0.source_id == 101
    assert rec0.event_type == EVENT_ATTITUDE
    dec0 = decode(rec0, reg)
    assert dec0.decode_status.value == "SUCCESS"
    assert pytest.approx(dec0.fields["roll_deg"], 0.1) == 5.0
    assert pytest.approx(dec0.fields["pitch_deg"], 0.1) == -3.5
    assert pytest.approx(dec0.fields["yaw_deg"], 0.1) == 90.0

    # Second record: BATTERY
    rec1 = records[1]
    assert rec1.event_type == EVENT_BATTERY
    dec1 = decode(rec1, reg)
    assert dec1.decode_status.value == "SUCCESS"
    assert pytest.approx(dec1.fields["voltage_v"], 0.1) == 15.8

    # Third record: AVOIDANCE
    rec2 = records[2]
    assert rec2.event_type == EVENT_AVOIDANCE
    dec2 = decode(rec2, reg)
    assert dec2.decode_status.value == "SUCCESS"
    assert dec2.fields["reason"] == "COLLISION_RISK"
    assert pytest.approx(dec2.fields["min_separation_m"], 0.1) == 3.2
    assert dec2.fields["heading_change_deg"] == -25


def test_mcap_converter_cli(tmp_path: Path):
    in_mcap = tmp_path / "cli_flight.mcap"
    out_fss = tmp_path / "cli_flight.fss.bin"
    create_synthetic_mcap(in_mcap, num_records=2)

    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "convert_mcap_to_fss.py"),
        str(in_mcap),
        "-o",
        str(out_fss),
        "--source-id",
        "777",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    out_json = json.loads(res.stdout)
    assert out_json["source_id"] == 777
    assert out_json["records_written"] == 6

    envelopes = list(iter_envelopes(out_fss.read_bytes()))
    assert len(envelopes) == 6
    assert all(e.source_id == 777 for e in envelopes)


def test_mcap_converter_corrupt(tmp_path: Path):
    corrupt = tmp_path / "bad.mcap"
    corrupt.write_bytes(b"INVALID_HEADER_MAGIC_BYTES_HERE")
    out = tmp_path / "bad.fss.bin"

    with pytest.raises(ValueError, match="Invalid MCAP header magic"):
        convert_mcap_to_fss(corrupt, out)
