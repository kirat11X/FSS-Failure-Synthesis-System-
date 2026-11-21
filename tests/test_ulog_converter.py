"""Tests for PX4 ULog to FSS binary converter (scripts/convert_ulog_to_fss.py)."""

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
from scripts.convert_ulog_to_fss import (
    EVENT_ATTITUDE,
    EVENT_BATTERY,
    EVENT_GPS,
    convert_ulog_to_fss,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schemas" / "drone_v0.yaml"


def create_synthetic_ulog(path: Path, num_records: int = 10) -> None:
    """Generate a minimal valid synthetic PX4 ULog file."""
    base_us = 1718000000000000  # Microseconds epoch

    # 1. Header (16 bytes)
    header = b"ULog\x01\x12\x35\x00" + struct.pack("<Q", base_us)

    # 2. Formats ('F')
    # vehicle_attitude
    fmt_att = "vehicle_attitude:uint64_t timestamp;float roll;float pitch;float yaw;"
    msg_f1 = struct.pack("<HB", len(fmt_att), ord("F")) + fmt_att.encode("ascii")

    # vehicle_gps_position
    fmt_gps = "vehicle_gps_position:uint64_t timestamp;int32_t lat;int32_t lon;int32_t alt;uint8_t fix_type;uint8_t satellites_used;uint16_t hdop;"
    msg_f2 = struct.pack("<HB", len(fmt_gps), ord("F")) + fmt_gps.encode("ascii")

    # battery_status
    fmt_bat = "battery_status:uint64_t timestamp;float voltage_v;float current_a;float remaining;"
    msg_f3 = struct.pack("<HB", len(fmt_bat), ord("F")) + fmt_bat.encode("ascii")

    # 3. Subscriptions ('A')
    # msg_id 1 -> vehicle_attitude
    sub_att = struct.pack("<BH", 0, 1) + b"vehicle_attitude"
    msg_a1 = struct.pack("<HB", len(sub_att), ord("A")) + sub_att

    # msg_id 2 -> vehicle_gps_position
    sub_gps = struct.pack("<BH", 0, 2) + b"vehicle_gps_position"
    msg_a2 = struct.pack("<HB", len(sub_gps), ord("A")) + sub_gps

    # msg_id 3 -> battery_status
    sub_bat = struct.pack("<BH", 0, 3) + b"battery_status"
    msg_a3 = struct.pack("<HB", len(sub_bat), ord("A")) + sub_bat

    # 4. Data messages ('D')
    data_msgs = bytearray()
    for i in range(num_records):
        ts_us = base_us + i * 20_000  # +20ms

        # ATT
        att_body = struct.pack("<H", 1) + struct.pack("<Qfff", ts_us, 10.5 + i, -5.2, 180.0)
        data_msgs.extend(struct.pack("<HB", len(att_body), ord("D")) + att_body)

        # GPS
        gps_body = struct.pack("<H", 2) + struct.pack(
            "<QiiiBBH", ts_us, 377749000, -1224194000, 15000, 3, 14, 120
        )
        data_msgs.extend(struct.pack("<HB", len(gps_body), ord("D")) + gps_body)

        # BAT
        bat_body = struct.pack("<H", 3) + struct.pack("<Qfff", ts_us, 16.2 - i * 0.01, 12.5, 0.90)
        data_msgs.extend(struct.pack("<HB", len(bat_body), ord("D")) + bat_body)

    content = header + msg_f1 + msg_f2 + msg_f3 + msg_a1 + msg_a2 + msg_a3 + bytes(data_msgs)
    path.write_bytes(content)


def test_ulog_converter_direct(tmp_path: Path):
    in_ulg = tmp_path / "flight.ulg"
    out_fss = tmp_path / "flight.fss.bin"

    num_records = 5
    create_synthetic_ulog(in_ulg, num_records=num_records)

    summary = convert_ulog_to_fss(in_ulg, out_fss, source_id=42)

    assert out_fss.exists()
    assert summary["records_written"] == num_records * 3
    assert summary["source_id"] == 42
    assert "0x40" in summary["type_counts"]
    assert "0x41" in summary["type_counts"]
    assert "0x42" in summary["type_counts"]

    # Verify binary envelopes decode cleanly via fss_py.wire.iter_envelopes
    fss_bytes = out_fss.read_bytes()
    records = list(iter_envelopes(fss_bytes))
    assert len(records) == num_records * 3

    # Check first record (ATTITUDE)
    att_rec = records[0]
    assert att_rec.source_id == 42
    assert att_rec.event_type == EVENT_ATTITUDE
    assert att_rec.timestamp_ns == 1718000000000000 * 1000  # ns precision

    # Verify against schemas/drone_v0.yaml
    reg = load_schema_file(SCHEMA_PATH)
    decoded_att = decode(att_rec, reg)
    assert decoded_att.decode_status.value == "SUCCESS"
    assert decoded_att.event_name == "ATTITUDE"
    assert pytest.approx(decoded_att.fields["roll_deg"], 0.1) == 10.5
    assert pytest.approx(decoded_att.fields["pitch_deg"], 0.1) == -5.2
    assert pytest.approx(decoded_att.fields["yaw_deg"], 0.1) == 180.0

    # Check GPS record
    gps_rec = records[1]
    assert gps_rec.event_type == EVENT_GPS
    decoded_gps = decode(gps_rec, reg)
    assert decoded_gps.decode_status.value == "SUCCESS"
    assert decoded_gps.event_name == "GPS_STATUS"
    assert decoded_gps.fields["num_sats"] == 14
    assert decoded_gps.fields["lat_e7"] == 377749000

    # Check BATTERY record
    bat_rec = records[2]
    assert bat_rec.event_type == EVENT_BATTERY
    decoded_bat = decode(bat_rec, reg)
    assert decoded_bat.decode_status.value == "SUCCESS"
    assert decoded_bat.event_name == "BATTERY_STATUS"
    assert pytest.approx(decoded_bat.fields["voltage_v"], 0.1) == 16.2
    assert pytest.approx(decoded_bat.fields["remaining_pct"], 0.1) == 90.0


def test_ulog_converter_cli(tmp_path: Path):
    in_ulg = tmp_path / "cli_test.ulg"
    out_fss = tmp_path / "cli_test.fss.bin"
    create_synthetic_ulog(in_ulg, num_records=3)

    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "convert_ulog_to_fss.py"),
        str(in_ulg),
        "-o",
        str(out_fss),
        "--source-id",
        "99",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    out_json = json.loads(res.stdout)
    assert out_json["source_id"] == 99
    assert out_json["records_written"] == 9
    assert out_fss.exists()

    envelopes = list(iter_envelopes(out_fss.read_bytes()))
    assert len(envelopes) == 9
    assert all(e.source_id == 99 for e in envelopes)


def test_ulog_converter_corrupt_file(tmp_path: Path):
    corrupt = tmp_path / "corrupt.ulg"
    corrupt.write_bytes(b"NOT_A_ULOG_FILE_CONTENT_AT_ALL")
    out = tmp_path / "out.fss.bin"

    with pytest.raises(ValueError, match="Invalid ULog magic"):
        convert_ulog_to_fss(corrupt, out)
