"""Unit tests for Vendor Error-Code Manual Parser, Ingestion & Dual-Source Hybrid RAG."""

from __future__ import annotations

import json
import pytest

from bridge.error_catalog import (
    VendorErrorEntry,
    extract_error_codes_from_events,
    load_builtin_manual,
    parse_error_manual,
)
from bridge.hybrid_rag import (
    DOMAIN_KNOWLEDGE,
    HybridRetriever,
    enrich_evidence_with_manual,
)


def test_vendor_error_entry_creation_and_to_doc():
    """Verify VendorErrorEntry defaults, chunk ID generation, and document conversion."""
    entry = VendorErrorEntry(
        vendor="ArduPilot",
        error_code="SUBSYS_14_ECODE_2",
        subsystem="EKF",
        severity="CRITICAL",
        title="EKF Bad Variance / Divergence",
        description="Compass and GPS disagreement, persistent filter divergence.",
        probable_causes=["Magnetic disturbance", "High vibration"],
        recommended_actions=["Land vehicle", "Recalibrate IMU"],
    )

    assert entry.doc_chunk_id == "err-ardupilot-SUBSYS_14_ECODE_2"
    doc = entry.to_doc()

    assert doc["id"] == "err-ardupilot-SUBSYS_14_ECODE_2"
    assert doc["chunk"] == "err-ardupilot-SUBSYS_14_ECODE_2#1"
    assert doc["kind"] == "vendor-error-code"
    assert doc["domain"] == "ArduPilot"
    assert "[ARDUPILOT] SUBSYS_14_ECODE_2" in doc["title"]
    assert "EKF Bad Variance" in doc["content"]
    assert "Magnetic disturbance" in doc["content"]
    assert "Land vehicle" in doc["content"]


def test_parse_json_manual_list_and_dict():
    """Verify parsing .json manuals supporting both list of objects and dict mapping."""
    # List format
    raw_list = json.dumps([
        {
            "error_code": "ERR-BAT-01",
            "vendor": "custom_vendor",
            "subsystem": "Battery",
            "severity": "CRITICAL",
            "title": "Low Voltage Cutoff",
            "description": "Cell voltage dropped below 3.0V.",
            "probable_causes": ["Pack exhausted", "High internal resistance"],
            "recommended_actions": ["Land immediately", "Disarm"],
        }
    ])
    entries_list = parse_error_manual(raw_list, "battery.json")
    assert len(entries_list) == 1
    assert entries_list[0].error_code == "ERR-BAT-01"
    assert entries_list[0].vendor == "custom_vendor"
    assert entries_list[0].severity == "CRITICAL"

    # Dict format
    raw_dict = json.dumps({
        "ERR-MOT-04": {
            "subsystem": "Propulsion",
            "severity": "ERROR",
            "title": "ESC Desync",
            "description": "Motor commutation lost.",
            "probable_causes": ["Rapid throttle ramp"],
            "recommended_actions": ["Update ESC firmware"],
        }
    })
    entries_dict = parse_error_manual(raw_dict, "motor.json", vendor="motors_inc")
    assert len(entries_dict) == 1
    assert entries_dict[0].error_code == "ERR-MOT-04"
    assert entries_dict[0].vendor == "motors_inc"


def test_parse_csv_manual():
    """Verify parsing .csv manuals with standard and flexible column names."""
    csv_text = """code,subsystem,severity,title,description,probable_causes,recommended_actions
0x10,Perception,ERROR,LiDAR Timeout,Scan missing,Stalled mirror;Unseated cable,Reset driver;Check 5V
0x20,Guidance,WARNING,Avoidance Trigger,Path override,Intruder drone,Hold slot
"""
    entries = parse_error_manual(csv_text, "telemetry_codes.csv", vendor="fss_native")
    assert len(entries) == 2
    assert entries[0].error_code == "0x10"
    assert len(entries[0].probable_causes) == 2
    assert entries[0].probable_causes[0] == "Stalled mirror"
    assert entries[1].error_code == "0x20"


def test_parse_markdown_tables_and_sections():
    """Verify markdown table parsing and exact alphanumeric preservation."""
    md_text = """# Custom Protocol Error Codes

| Code | Subsystem | Severity | Meaning | Causes | Actions |
|---|---|---|---|---|---|
| `ERR_PAYLOAD_DROP_01` | Payload | CRITICAL | Cargo Latch Malfunction | Solenoid jam | Abort drop |
| `0x45_1` | Safety | ERROR | Recoverable Fault | Redundant channel fallback | Bench inspect |

### [ERR-GCS-99] SUBSYS 8 CODE 99 - GCS Keepalive Heartbeat Dropped
- **Subsystem**: Comms
- **Severity**: WARNING
- **Description**: Ground station serial stream missed 5 frames.
- **Probable Causes**: Antenna misalignment; Frequency congestion
- **Recommended Actions**: Re-aim ground tracker; Switch frequency
"""
    entries = parse_error_manual(md_text, "custom.md", vendor="flight_corp")
    assert len(entries) == 3

    codes = {e.error_code for e in entries}
    assert "ERR_PAYLOAD_DROP_01" in codes
    assert "0x45_1" in codes
    assert "ERR-GCS-99" in codes

    gcs_entry = next(e for e in entries if e.error_code == "ERR-GCS-99")
    assert gcs_entry.subsystem == "Comms"
    assert "Ground station serial stream" in gcs_entry.description
    assert "Antenna misalignment" in gcs_entry.probable_causes


def test_load_builtin_manuals():
    """Verify loading all built-in vendor manuals (ArduPilot, PX4, FSS native)."""
    # ArduPilot
    ardupilot_entries = load_builtin_manual("ardupilot")
    assert len(ardupilot_entries) >= 40
    ardupilot_codes = {e.error_code for e in ardupilot_entries}
    assert "SUBSYS_1_ECODE_1" in ardupilot_codes
    assert "SUBSYS_14_ECODE_2" in ardupilot_codes
    assert "SUBSYS_5_ECODE_2" in ardupilot_codes
    assert "EV_10" in ardupilot_codes
    assert "EV_28" in ardupilot_codes

    # PX4
    px4_entries = load_builtin_manual("px4")
    assert len(px4_entries) >= 25
    px4_codes = {e.error_code for e in px4_entries}
    assert "EST_FAULT_VEL_HORIZ" in px4_codes
    assert "EST_FAULT_MAG_HEADING" in px4_codes
    assert "FAILSAFE_RTL" in px4_codes
    assert "BATT_WARNING_CRITICAL" in px4_codes
    assert "ACTUATOR_MOTOR_FAIL" in px4_codes

    # FSS native
    fss_entries = load_builtin_manual("fss_native")
    assert len(fss_entries) >= 20
    fss_codes = {e.error_code for e in fss_entries}
    assert "LIDAR_TIMEOUT" in fss_codes
    assert "SENSOR_DISCONNECTED" in fss_codes
    assert "COLLISION_RISK" in fss_codes
    assert "LEFT_FORMATION" in fss_codes
    assert "ESC_DESYNC" in fss_codes

    # Auto loads consolidated catalog
    auto_entries = load_builtin_manual("auto")
    assert len(auto_entries) >= 80


def test_extract_error_codes_from_events():
    """Verify fault code extraction from decoded telemetry events."""
    events = [
        {
            "event_type": 16,
            "event_name": "LIDAR_STATUS",
            "fields": {"status_code": 17, "timeout_count": 4},
        },
        {
            "event_type": 32,
            "event_name": "AVOIDANCE",
            "fields": {"reason_code": 1, "min_separation_m": 1.8},
        },
        {
            "event_type": "ERR",
            "event_name": "ERR",
            "fields": {"subsys": 14, "ecode": 2},
        },
        {
            "event_type": "EV",
            "event_name": "EV",
            "fields": {"id": 28},
        },
        {
            "event_type": "vehicle_status",
            "event_name": "vehicle_status",
            "fields": {"nav_state": "FAILSAFE_RTL", "failsafe": True},
        },
    ]

    codes = extract_error_codes_from_events(events)

    # Verify exact alphanumeric tokens extracted without fragmentation
    assert "16" in codes
    assert "0x10" in codes
    assert "LIDAR_STATUS" in codes
    assert "17" in codes
    assert "LIDAR_TIMEOUT" in codes
    assert "COLLISION_RISK" in codes
    assert "SUBSYS_14_ECODE_2" in codes
    assert "ERR_14_2" in codes
    assert "EV_28" in codes
    assert "FAILSAFE_RTL" in codes


def test_hybrid_retriever_dual_source_indexing_and_retrieval():
    """Verify HybridRetriever indexes default DOMAIN_KNOWLEDGE + dynamic vendor manuals."""
    vendor_entries = [
        VendorErrorEntry(
            vendor="ArduPilot",
            error_code="SUBSYS_14_ECODE_2",
            subsystem="EKF",
            severity="CRITICAL",
            title="EKF Bad Variance / Divergence",
            description="Compass and GPS disagreement triggering failsafe.",
            probable_causes=["Magnetic disturbance", "High motor vibration"],
            recommended_actions=["Land vehicle", "Recalibrate compass"],
        ),
        VendorErrorEntry(
            vendor="fss_native",
            error_code="LIDAR_TIMEOUT",
            subsystem="Perception",
            severity="ERROR",
            title="LiDAR Scan Timeout",
            description="3+ consecutive missed sweeps, position filter drift.",
            probable_causes=["Driver crash", "Mirror motor stall"],
            recommended_actions=["Reset LiDAR driver"],
        ),
    ]

    # Initialize retriever and index vendor entries
    retriever = HybridRetriever()
    initial_len = len(retriever.corpus)
    assert initial_len == len(DOMAIN_KNOWLEDGE)

    retriever.index_vendor_manual(vendor_entries)
    assert len(retriever.corpus) == initial_len + len(vendor_entries)

    # Retrieval with exact code query and evidence
    evidence = {
        "events": [
            {
                "event_type": 16,
                "event_name": "LIDAR_STATUS",
                "fields": {"status_code": 17},
            }
        ]
    }
    results = retriever.retrieve_knowledge(
        query="Investigate LiDAR scan timeout anomaly",
        evidence=evidence,
        top_k=5,
    )

    doc_ids = [r["id"] for r in results]
    assert "err-fss_native-LIDAR_TIMEOUT" in doc_ids
    # Ensure chunk ID adheres to err-{vendor}-{code}
    assert any(did.startswith("err-fss_native-") for did in doc_ids)


def test_enrich_evidence_with_manual():
    """Verify enrich_evidence_with_manual matches events against catalog."""
    catalog = load_builtin_manual("auto")
    evidence = {
        "events": [
            {
                "event_type": 16,
                "event_name": "LIDAR_STATUS",
                "fields": {"status_code": 17},
            },
            {
                "event_type": "ERR",
                "event_name": "ERR",
                "fields": {"subsys": 14, "ecode": 2},
            },
            {
                "event_type": "vehicle_status",
                "event_name": "vehicle_status",
                "fields": {"nav_state": "FAILSAFE_RTL"},
            },
        ]
    }

    enriched = enrich_evidence_with_manual(evidence, catalog)
    assert len(enriched) >= 3

    matched_codes = {item["error_code"] for item in enriched}
    assert "LIDAR_TIMEOUT" in matched_codes
    assert "SUBSYS_14_ECODE_2" in matched_codes
    assert "FAILSAFE_RTL" in matched_codes

    for item in enriched:
        assert "error_code" in item
        assert "vendor" in item
        assert "subsystem" in item
        assert "severity" in item
        assert "title" in item
        assert "description" in item
        assert isinstance(item["probable_causes"], list)
        assert isinstance(item["recommended_actions"], list)
        assert item["doc_chunk_id"].startswith("err-")
