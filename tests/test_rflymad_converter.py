"""Tests for RflyMAD dataset converter (scripts/convert_rflymad_to_fss.py)."""

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
from scripts.convert_rflymad_to_fss import (
    EVENT_ATTITUDE,
    EVENT_BATTERY,
    EVENT_ERROR,
    EVENT_GPS,
    EVENT_VIBRATION,
    RFLYMAD_11_FAULTS,
    convert_rflymad_to_fss,
)

ROOT = Path(__file__).resolve().parent.parent
SCHEMA_PATH = ROOT / "schemas" / "drone_v0.yaml"


def create_synthetic_rflymad_csv(path: Path, num_rows: int = 15) -> None:
    """Create a synthetic RflyMAD CSV with multirotor telemetry and fault injections."""
    fieldnames = [
        "timestamp",
        "roll",
        "pitch",
        "yaw",
        "lat",
        "lon",
        "alt",
        "voltage",
        "current",
        "remaining",
        "vibe_x",
        "vibe_y",
        "vibe_z",
        "fault_id",
    ]

    rows = []
    base_t = 50.0  # seconds

    for i in range(num_rows):
        t = base_t + i * 0.05
        # Rows 4..7: Fault 1 (Motor LOE); Rows 10..12: Fault 6 (Compass Failure); others nominal (0)
        if 4 <= i <= 7:
            fid = 1
        elif 10 <= i <= 12:
            fid = 6
        else:
            fid = 0

        rows.append({
            "timestamp": f"{t:.3f}",
            "roll": "1.2" if fid == 0 else "15.4",
            "pitch": "-0.8" if fid == 0 else "-12.1",
            "yaw": "45.0" if fid != 6 else "185.0",
            "lat": "37.7749",
            "lon": "-122.4194",
            "alt": "25.0",
            "voltage": "16.1",
            "current": "14.5",
            "remaining": "85.0",
            "vibe_x": "0.5" if fid != 1 else "18.5",
            "vibe_y": "0.6" if fid != 1 else "22.1",
            "vibe_z": "9.8" if fid != 1 else "35.4",
            "fault_id": str(fid),
        })

    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)


def test_rflymad_converter_direct(tmp_path: Path):
    in_csv = tmp_path / "rflymad_test.csv"
    out_fss = tmp_path / "rflymad_test.fss.bin"
    meta_out = tmp_path / "rflymad_test.ground_truth.json"

    create_synthetic_rflymad_csv(in_csv, num_rows=15)

    res = convert_rflymad_to_fss(in_csv, out_fss, meta_out_path=meta_out, source_id=505)

    assert out_fss.exists()
    assert meta_out.exists()
    assert res["records_written"] > 0
    assert res["failures_detected"] == 2  # Fault 1 and Fault 6

    # Verify binary envelopes decode cleanly
    records = list(iter_envelopes(out_fss.read_bytes()))
    assert len(records) == res["records_written"]
    assert all(r.source_id == 505 for r in records)

    # Check decoded fields with schema
    reg = load_schema_file(SCHEMA_PATH)

    # ATTITUDE
    att_recs = [r for r in records if r.event_type == EVENT_ATTITUDE]
    assert len(att_recs) == 15
    dec_att = decode(att_recs[0], reg)
    assert dec_att.decode_status.value == "SUCCESS"
    assert pytest.approx(dec_att.fields["roll_deg"], 0.1) == 1.2

    # GPS
    gps_recs = [r for r in records if r.event_type == EVENT_GPS]
    assert len(gps_recs) == 15
    dec_gps = decode(gps_recs[0], reg)
    assert dec_gps.decode_status.value == "SUCCESS"
    assert dec_gps.fields["lat_e7"] == 377749000

    # VIBRATION
    vibe_recs = [r for r in records if r.event_type == EVENT_VIBRATION]
    assert len(vibe_recs) == 15
    dec_vibe = decode(vibe_recs[0], reg)
    assert dec_vibe.decode_status.value == "SUCCESS"

    # ERROR_EVENT (0x45)
    err_recs = [r for r in records if r.event_type == EVENT_ERROR]
    assert len(err_recs) == 7  # 4 rows for fault 1 + 3 rows for fault 6
    dec_err1 = decode(err_recs[0], reg)
    assert dec_err1.fields["subsystem_code"] == 1
    assert dec_err1.fields["error_code"] == 10

    dec_err2 = decode(err_recs[4], reg)
    assert dec_err2.fields["subsystem_code"] == 6
    assert dec_err2.fields["error_code"] == 60

    # Verify ground truth metadata
    meta = json.loads(meta_out.read_text(encoding="utf-8"))
    assert meta["dataset"] == "RflyMAD"
    assert meta["source_id"] == 505
    assert len(meta["faults_catalog"]) == 11
    assert len(meta["failures"]) == 2
    f1 = meta["failures"][0]
    assert f1["fault_id"] == 1
    assert "Motor Loss" in f1["fault_name"]
    f2 = meta["failures"][1]
    assert f2["fault_id"] == 6
    assert "Compass" in f2["fault_name"]


def test_rflymad_converter_cli(tmp_path: Path):
    in_csv = tmp_path / "cli_rflymad.csv"
    out_fss = tmp_path / "cli_rflymad.fss.bin"
    meta_out = tmp_path / "cli_rflymad.meta.json"
    create_synthetic_rflymad_csv(in_csv, num_rows=10)

    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "convert_rflymad_to_fss.py"),
        str(in_csv),
        "-o",
        str(out_fss),
        "--meta-out",
        str(meta_out),
        "--source-id",
        "606",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    out_json = json.loads(res.stdout)
    assert out_json["records_written"] > 0
    assert out_fss.exists()
    assert meta_out.exists()

    envelopes = list(iter_envelopes(out_fss.read_bytes()))
    assert len(envelopes) == out_json["records_written"]
    assert all(e.source_id == 606 for e in envelopes)


def test_rflymad_directory_input(tmp_path: Path):
    test_dir = tmp_path / "rflymad_dir"
    test_dir.mkdir()
    create_synthetic_rflymad_csv(test_dir / "flight_a.csv", num_rows=5)
    create_synthetic_rflymad_csv(test_dir / "flight_b.csv", num_rows=5)

    out_fss = tmp_path / "dir_out.fss.bin"
    res = convert_rflymad_to_fss(test_dir, out_fss, source_id=707)

    assert out_fss.exists()
    records = list(iter_envelopes(out_fss.read_bytes()))
    assert len(records) > 0
    ts_list = [r.timestamp_ns for r in records]
    assert ts_list == sorted(ts_list)


def test_simulate_drone_swarm_faults_all_scenarios(tmp_path: Path):
    from scripts.simulate_drone_swarm_faults import SCENARIOS, simulate_scenario

    for sname in SCENARIOS:
        bin_out = tmp_path / f"{sname}.fss.bin"
        res = simulate_scenario(sname, bin_out, seed=123)
        assert bin_out.exists()
        manifest_file = bin_out.with_name(f"{bin_out.stem}.manifest.json")
        assert manifest_file.exists()

        records = list(iter_envelopes(bin_out.read_bytes()))
        assert len(records) == res["events_written"]
        manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        assert manifest["scenario"] == sname


def test_simulate_drone_swarm_faults_cli(tmp_path: Path):
    out_bin = tmp_path / "sim_sc1.fss.bin"
    cmd = [
        sys.executable,
        str(ROOT / "scripts" / "simulate_drone_swarm_faults.py"),
        "--scenario",
        "scenario_298_474",
        "--output",
        str(out_bin),
        "--seed",
        "99",
    ]
    res = subprocess.run(cmd, capture_output=True, text=True, check=True)
    out_dict = json.loads(res.stdout)
    assert out_dict["scenario"] == "scenario_298_474"
    assert out_bin.exists()

    envelopes = list(iter_envelopes(out_bin.read_bytes()))
    assert len(envelopes) == out_dict["events_written"]
    # Verify drone 298 fault and drone 474 avoidance
    sources = {e.source_id for e in envelopes}
    assert 298 in sources and 474 in sources
