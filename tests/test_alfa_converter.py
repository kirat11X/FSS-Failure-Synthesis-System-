"""Tests for ALFA dataset converter (scripts/convert_alfa_to_fss.py)."""

from __future__ import annotations

import csv
import json
import subprocess
import sys
from pathlib import Path

import pytest

from fss_py.decoder import decode
from fss_py.schema import load_schema_file
from fss_py.wire import iter_envelopes
from scripts.convert_alfa_to_fss import (
    EVENT_ATTITUDE,
    EVENT_BATTERY,
    EVENT_ERROR,
    convert_alfa_to_fss,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schemas" / "drone_v0.yaml"


def create_synthetic_alfa_csv(path: Path, num_rows: int = 15) -> None:
    """Create a synthetic ALFA CSV file with nominal and fault intervals."""
    fieldnames = [
        "timestamp",
        "roll",
        "pitch",
        "yaw",
        "voltage",
        "current",
        "remaining",
        "thrust",
        "failure_mode",
    ]

    rows = []
    base_t = 100.0  # seconds

    for i in range(num_rows):
        t = base_t + i * 0.1
        # Rows 5..9 have motor_failure
        fault = "motor_failure" if 5 <= i <= 9 else "nominal"
        roll = 2.0 if fault == "nominal" else 25.4
        pitch = -1.5 if fault == "nominal" else -18.2
        thrust = 0.6 if fault == "nominal" else 0.1
        rows.append({
            "timestamp": f"{t:.3f}",
            "roll": f"{roll:.2f}",
            "pitch": f"{pitch:.2f}",
            "yaw": "120.0",
            "voltage": "15.4",
            "current": "12.0",
            "remaining": "78.0",
            "thrust": f"{thrust:.2f}",
            "failure_mode": fault,
        })

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_alfa_converter_direct(tmp_path: Path):
    in_csv = tmp_path / "alfa_flight.csv"
    out_fss = tmp_path / "alfa_flight.fss.bin"
    meta_out = tmp_path / "alfa_flight.ground_truth.json"

    create_synthetic_alfa_csv(in_csv, num_rows=15)

    res = convert_alfa_to_fss(in_csv, out_fss, meta_out_path=meta_out, source_id=202)

    assert out_fss.exists()
    assert meta_out.exists()
    assert res["records_written"] > 0
    assert res["failures_detected"] == 1

    # Verify envelopes
    records = list(iter_envelopes(out_fss.read_bytes()))
    assert len(records) == res["records_written"]
    assert all(r.source_id == 202 for r in records)

    # Verify decoder compatibility
    reg = load_schema_file(SCHEMA_PATH)
    att_records = [r for r in records if r.event_type == EVENT_ATTITUDE]
    assert len(att_records) == 15
    dec_att = decode(att_records[0], reg)
    assert dec_att.decode_status.value == "SUCCESS"
    assert pytest.approx(dec_att.fields["roll_deg"], 0.1) == 2.0
    assert pytest.approx(dec_att.fields["pitch_deg"], 0.1) == -1.5

    # Check error events (0x45)
    err_records = [r for r in records if r.event_type == EVENT_ERROR]
    assert len(err_records) == 5  # rows 5..9
    dec_err = decode(err_records[0], reg)
    assert dec_err.decode_status.value == "SUCCESS"
    assert dec_err.fields["subsystem_code"] == 1  # motor subsystem

    # Verify ground truth sidecar
    meta = json.loads(meta_out.read_text(encoding="utf-8"))
    assert meta["dataset"] == "ALFA"
    assert meta["source_id"] == 202
    assert meta["total_events"] == len(records)
    assert len(meta["failures"]) == 1
    fault_info = meta["failures"][0]
    assert fault_info["failure_type"] == "motor_failure"
    assert fault_info["subsystem_id"] == 1
    assert fault_info["start_time_ns"] < fault_info["end_time_ns"]


def test_alfa_converter_directory(tmp_path: Path):
    flight_dir = tmp_path / "alfa_dir"
    flight_dir.mkdir()
    create_synthetic_alfa_csv(flight_dir / "flight_01.csv", num_rows=5)
    create_synthetic_alfa_csv(flight_dir / "flight_02.csv", num_rows=5)

    out_fss = tmp_path / "combined.fss.bin"
    res = convert_alfa_to_fss(flight_dir, out_fss, source_id=303)

    assert out_fss.exists()
    records = list(iter_envelopes(out_fss.read_bytes()))
    assert len(records) > 0
    # Envelopes must be monotonically ordered in timestamp_ns
    timestamps = [r.timestamp_ns for r in records]
    assert timestamps == sorted(timestamps)


def test_alfa_converter_cli(tmp_path: Path):
    in_csv = tmp_path / "cli_alfa.csv"
    out_fss = tmp_path / "cli_alfa.fss.bin"
    meta_json = tmp_path / "cli_alfa.gt.json"
    create_synthetic_alfa_csv(in_csv, num_rows=8)

    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "convert_alfa_to_fss.py"),
        str(in_csv),
        "-o",
        str(out_fss),
        "--meta-out",
        str(meta_json),
        "--source-id",
        "404",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    out_dict = json.loads(res.stdout)
    assert out_dict["records_written"] > 0
    assert out_fss.exists()
    assert meta_json.exists()

    envelopes = list(iter_envelopes(out_fss.read_bytes()))
    assert len(envelopes) == out_dict["records_written"]
    assert all(e.source_id == 404 for e in envelopes)
