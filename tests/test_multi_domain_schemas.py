"""Unit and integration tests for Multi-Domain Schema Packs & Vendor Error-Code Manuals.

Tests compliance with:
- Schema loading & validation (fss_py.schema.load_schema_file)
- Deterministic decoding for multi-domain schemas (little-endian and D4 big-endian)
- Enum resolution to documented strings and UNKNOWN fallback for undefined codes
- Trailing opaque bytes quarantine (DecodeStatus.PARTIALLY_DECODED)
- Truncated / malformed payloads & invalid bools (DecodeStatus.MALFORMED_PAYLOAD)
- Generalisation rules G01-G04 (identical decoder code across domains)
- Domain manual parsing for all 5 technical error manuals
"""

from __future__ import annotations

import struct
from pathlib import Path
import pytest

from bridge.error_catalog import (
    VendorErrorEntry,
    extract_error_codes_from_events,
    parse_error_manual,
)
from fss_py.decoder import decode, decode_all
from fss_py.model import DecodeStatus, EventRecord
from fss_py.schema import SchemaRegistry, load_schema_file

WORKSPACE_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = WORKSPACE_ROOT / "schemas"
MANUALS_DIR = WORKSPACE_ROOT / "domain_manuals"


# ==============================================================================
# 1. Schema Loading & Validation
# ==============================================================================

@pytest.fixture
def microservices_schema() -> SchemaRegistry:
    return load_schema_file(SCHEMAS_DIR / "microservices_v0.yaml")


@pytest.fixture
def industrial_plc_schema() -> SchemaRegistry:
    return load_schema_file(SCHEMAS_DIR / "industrial_plc_v0.yaml")


@pytest.fixture
def autonomous_vehicle_schema() -> SchemaRegistry:
    return load_schema_file(SCHEMAS_DIR / "autonomous_vehicle_v0.yaml")


@pytest.fixture
def financial_hft_schema() -> SchemaRegistry:
    return load_schema_file(SCHEMAS_DIR / "financial_hft_v0.yaml")


@pytest.fixture
def warehouse_robots_schema() -> SchemaRegistry:
    return load_schema_file(SCHEMAS_DIR / "warehouse_robots_v0.yaml")


@pytest.fixture
def mavlink_common_schema() -> SchemaRegistry:
    return load_schema_file(SCHEMAS_DIR / "mavlink_common_v0.yaml")


def test_schema_loading_all(
    microservices_schema: SchemaRegistry,
    industrial_plc_schema: SchemaRegistry,
    autonomous_vehicle_schema: SchemaRegistry,
    financial_hft_schema: SchemaRegistry,
    warehouse_robots_schema: SchemaRegistry,
    mavlink_common_schema: SchemaRegistry,
):
    """Verify that all 6 multi-domain schemas load cleanly and register expected event types."""
    assert microservices_schema.schema_set == "d2_microservices"
    assert len(microservices_schema) == 3
    assert 0x0201 in microservices_schema
    assert 0x0202 in microservices_schema
    assert 0x0203 in microservices_schema

    assert industrial_plc_schema.schema_set == "d3_industrial_plc"
    assert len(industrial_plc_schema) == 3
    assert 0x0301 in industrial_plc_schema
    assert 0x0302 in industrial_plc_schema
    assert 0x0303 in industrial_plc_schema

    assert autonomous_vehicle_schema.schema_set == "d4_autonomous_vehicle"
    assert len(autonomous_vehicle_schema) == 4
    assert 0x0401 in autonomous_vehicle_schema
    assert 0x0402 in autonomous_vehicle_schema
    assert 0x0403 in autonomous_vehicle_schema
    assert 0x0404 in autonomous_vehicle_schema

    assert financial_hft_schema.schema_set == "financial_hft"
    assert len(financial_hft_schema) == 3
    assert 0x0501 in financial_hft_schema
    assert 0x0502 in financial_hft_schema
    assert 0x0503 in financial_hft_schema

    assert warehouse_robots_schema.schema_set == "d5_warehouse_robots"
    assert len(warehouse_robots_schema) == 4
    assert 0x0601 in warehouse_robots_schema
    assert 0x0602 in warehouse_robots_schema
    assert 0x0603 in warehouse_robots_schema
    assert 0x0604 in warehouse_robots_schema

    assert mavlink_common_schema.schema_set == "mavlink_common"
    assert len(mavlink_common_schema) == 6
    assert 0 in mavlink_common_schema
    assert 1 in mavlink_common_schema
    assert 24 in mavlink_common_schema
    assert 30 in mavlink_common_schema
    assert 33 in mavlink_common_schema
    assert 253 in mavlink_common_schema


# ==============================================================================
# 2. Decoding & Endianness (G03)
# ==============================================================================

def test_decode_microservices(microservices_schema: SchemaRegistry):
    """Test decoding little-endian D2 Microservices telemetry events."""
    # 0x0201 DB_POOL_STATUS: pool_id(u16), active_conns(u16), max_conns(u16), state_code(u8)
    payload_pool = struct.pack("<HHHB", 1, 50, 50, 3)
    rec_pool = EventRecord(source_id=10, timestamp_ns=1_000_000, event_type=0x0201, payload_bytes=payload_pool)
    ev_pool = decode(rec_pool, microservices_schema)
    assert ev_pool.decode_status == DecodeStatus.SUCCESS
    assert ev_pool.event_name == "DB_POOL_STATUS"
    assert ev_pool.fields["pool_id"] == 1
    assert ev_pool.fields["active_conns"] == 50
    assert ev_pool.fields["max_conns"] == 50
    assert ev_pool.fields["state_code"] == 3
    assert ev_pool.fields["state"] == "POOL_EXHAUSTED"

    # 0x0202 REQUEST_METRIC: service_id(u16), latency_ms(u32), status_code(u16)
    payload_metric = struct.pack("<HIH", 42, 1250, 504)
    rec_metric = EventRecord(source_id=10, timestamp_ns=1_000_100, event_type=0x0202, payload_bytes=payload_metric)
    ev_metric = decode(rec_metric, microservices_schema)
    assert ev_metric.decode_status == DecodeStatus.SUCCESS
    assert ev_metric.fields["service_id"] == 42
    assert ev_metric.fields["latency_ms"] == 1250
    assert ev_metric.fields["status_code"] == 504
    assert ev_metric.fields["status_name"] == "GATEWAY_TIMEOUT"

    # 0x0203 HTTP_ERROR: service_id(u16), error_code(u16), downstream_id(u16)
    payload_err = struct.pack("<HHH", 42, 504, 99)
    rec_err = EventRecord(source_id=10, timestamp_ns=1_000_200, event_type=0x0203, payload_bytes=payload_err)
    ev_err = decode(rec_err, microservices_schema)
    assert ev_err.decode_status == DecodeStatus.SUCCESS
    assert ev_err.fields["service_id"] == 42
    assert ev_err.fields["error_code"] == 504
    assert ev_err.fields["downstream_id"] == 99


def test_decode_industrial_plc(industrial_plc_schema: SchemaRegistry):
    """Test decoding little-endian D3 Industrial PLC telemetry events."""
    # 0x0301 TEMP_STATUS: sensor_id(u16), temperature_c(f32), drift_detected(bool)
    payload_temp = struct.pack("<HfB", 5, 87.5, 1)
    rec_temp = EventRecord(source_id=20, timestamp_ns=2_000_000, event_type=0x0301, payload_bytes=payload_temp)
    ev_temp = decode(rec_temp, industrial_plc_schema)
    assert ev_temp.decode_status == DecodeStatus.SUCCESS
    assert ev_temp.fields["sensor_id"] == 5
    assert ev_temp.fields["temperature_c"] == 87.5
    assert ev_temp.fields["drift_detected"] is True

    # 0x0302 INTERLOCK: interlock_id(u16), tripped(bool), reason_code(u16)
    payload_lock = struct.pack("<HBH", 12, 1, 1)
    rec_lock = EventRecord(source_id=20, timestamp_ns=2_000_100, event_type=0x0302, payload_bytes=payload_lock)
    ev_lock = decode(rec_lock, industrial_plc_schema)
    assert ev_lock.decode_status == DecodeStatus.SUCCESS
    assert ev_lock.fields["interlock_id"] == 12
    assert ev_lock.fields["tripped"] is True
    assert ev_lock.fields["reason_code"] == 1
    assert ev_lock.fields["reason"] == "OVERHEAT"

    # 0x0303 CONVEYOR_STATE: conveyor_id(u16), state_code(u8)
    payload_conv = struct.pack("<HB", 3, 1)
    rec_conv = EventRecord(source_id=20, timestamp_ns=2_000_200, event_type=0x0303, payload_bytes=payload_conv)
    ev_conv = decode(rec_conv, industrial_plc_schema)
    assert ev_conv.decode_status == DecodeStatus.SUCCESS
    assert ev_conv.fields["conveyor_id"] == 3
    assert ev_conv.fields["state_code"] == 1
    assert ev_conv.fields["state"] == "STARVED"


def test_decode_autonomous_vehicle_big_endian(autonomous_vehicle_schema: SchemaRegistry):
    """Rule G03: D4 Autonomous Vehicle uses BIG-ENDIAN for all multi-byte fields."""
    # Verify schema definition enforces big-endian
    for event_type, schema in autonomous_vehicle_schema._by_type.items():
        for field in schema.fields:
            if field.size > 1:
                assert field.endian == "big", f"Field {field.name} in {schema.name} must be big-endian"

    # 0x0401 WHEEL_SPEED: wheel_id(u8), speed_kmh(f32 big-endian)
    payload_wheel = struct.pack(">Bf", 2, 72.4)
    rec_wheel = EventRecord(source_id=30, timestamp_ns=3_000_000, event_type=0x0401, payload_bytes=payload_wheel)
    ev_wheel = decode(rec_wheel, autonomous_vehicle_schema)
    assert ev_wheel.decode_status == DecodeStatus.SUCCESS
    assert ev_wheel.fields["wheel_id"] == 2
    assert ev_wheel.fields["speed_kmh"] == 72.4

    # 0x0402 ECU_FAULT: ecu_id(u16 big-endian), fault_code(u16 big-endian)
    # 0x1234 packed big-endian is [0x12, 0x34]
    payload_ecu = struct.pack(">HH", 0x1234, 1)
    rec_ecu = EventRecord(source_id=30, timestamp_ns=3_000_100, event_type=0x0402, payload_bytes=payload_ecu)
    ev_ecu = decode(rec_ecu, autonomous_vehicle_schema)
    assert ev_ecu.decode_status == DecodeStatus.SUCCESS
    assert ev_ecu.fields["ecu_id"] == 0x1234
    assert ev_ecu.fields["fault_code"] == 1
    assert ev_ecu.fields["fault_name"] == "SENSOR_COMM_ERROR"

    # 0x0403 ABS_STATE: abs_state_code(u8), slip_ratio(f32 big-endian)
    payload_abs = struct.pack(">Bf", 2, 0.28)
    rec_abs = EventRecord(source_id=30, timestamp_ns=3_000_200, event_type=0x0403, payload_bytes=payload_abs)
    ev_abs = decode(rec_abs, autonomous_vehicle_schema)
    assert ev_abs.decode_status == DecodeStatus.SUCCESS
    assert ev_abs.fields["abs_state_code"] == 2
    assert ev_abs.fields["abs_state_name"] == "DISABLED"
    assert ev_abs.fields["slip_ratio"] == 0.28

    # 0x0404 DASH_WARNING: warning_id(u8), lamp_on(bool)
    payload_dash = struct.pack(">BB", 4, 1)
    rec_dash = EventRecord(source_id=30, timestamp_ns=3_000_300, event_type=0x0404, payload_bytes=payload_dash)
    ev_dash = decode(rec_dash, autonomous_vehicle_schema)
    assert ev_dash.decode_status == DecodeStatus.SUCCESS
    assert ev_dash.fields["warning_id"] == 4
    assert ev_dash.fields["lamp_on"] is True


def test_decode_financial_hft(financial_hft_schema: SchemaRegistry):
    """Test decoding little-endian Financial HFT telemetry events."""
    # 0x0501 ORDER_BOOK_DEPTH: symbol_id(u32), bid_px(f64), ask_px(f64), depth(u32)
    payload_depth = struct.pack("<IddI", 101, 150.25, 150.50, 10000)
    rec_depth = EventRecord(source_id=40, timestamp_ns=4_000_000, event_type=0x0501, payload_bytes=payload_depth)
    ev_depth = decode(rec_depth, financial_hft_schema)
    assert ev_depth.decode_status == DecodeStatus.SUCCESS
    assert ev_depth.fields["symbol_id"] == 101
    assert ev_depth.fields["bid_px"] == 150.25
    assert ev_depth.fields["ask_px"] == 150.50
    assert ev_depth.fields["depth"] == 10000

    # 0x0502 FIX_CANCEL_REJECT: cl_ord_id(u64), reject_reason(u16)
    payload_cxl = struct.pack("<QH", 888999111, 0)
    rec_cxl = EventRecord(source_id=40, timestamp_ns=4_000_100, event_type=0x0502, payload_bytes=payload_cxl)
    ev_cxl = decode(rec_cxl, financial_hft_schema)
    assert ev_cxl.decode_status == DecodeStatus.SUCCESS
    assert ev_cxl.fields["cl_ord_id"] == 888999111
    assert ev_cxl.fields["reject_reason"] == 0
    assert ev_cxl.fields["reject_name"] == "TOO_LATE_TO_CANCEL"

    # 0x0503 FIX_EXEC_REPORT: exec_id(u64), ord_status(u8)
    payload_exec = struct.pack("<QB", 777666555, 8)
    rec_exec = EventRecord(source_id=40, timestamp_ns=4_000_200, event_type=0x0503, payload_bytes=payload_exec)
    ev_exec = decode(rec_exec, financial_hft_schema)
    assert ev_exec.decode_status == DecodeStatus.SUCCESS
    assert ev_exec.fields["exec_id"] == 777666555
    assert ev_exec.fields["ord_status"] == 8
    assert ev_exec.fields["ord_status_name"] == "REJECTED"


def test_decode_warehouse_robots(warehouse_robots_schema: SchemaRegistry):
    """Test decoding little-endian D5 Warehouse Robots telemetry events."""
    # 0x0601 BATTERY: robot_id(u16), voltage_v(f32), charge_pct(u8), critical(bool)
    payload_batt = struct.pack("<HfBB", 12, 20.8, 14, 1)
    rec_batt = EventRecord(source_id=50, timestamp_ns=5_000_000, event_type=0x0601, payload_bytes=payload_batt)
    ev_batt = decode(rec_batt, warehouse_robots_schema)
    assert ev_batt.decode_status == DecodeStatus.SUCCESS
    assert ev_batt.fields["robot_id"] == 12
    assert ev_batt.fields["voltage_v"] == 20.8
    assert ev_batt.fields["charge_pct"] == 14
    assert ev_batt.fields["critical"] is True

    # 0x0602 NAV_STATE: robot_id(u16), x_mm(i32), y_mm(i32), blocked(bool)
    payload_nav = struct.pack("<HiiB", 12, -4500, 12000, 1)
    rec_nav = EventRecord(source_id=50, timestamp_ns=5_000_100, event_type=0x0602, payload_bytes=payload_nav)
    ev_nav = decode(rec_nav, warehouse_robots_schema)
    assert ev_nav.decode_status == DecodeStatus.SUCCESS
    assert ev_nav.fields["robot_id"] == 12
    assert ev_nav.fields["x_mm"] == -4500
    assert ev_nav.fields["y_mm"] == 12000
    assert ev_nav.fields["blocked"] is True

    # 0x0603 REPLAN: robot_id(u16), reason_code(u8)
    payload_replan = struct.pack("<HB", 12, 0)
    rec_replan = EventRecord(source_id=50, timestamp_ns=5_000_200, event_type=0x0603, payload_bytes=payload_replan)
    ev_replan = decode(rec_replan, warehouse_robots_schema)
    assert ev_replan.decode_status == DecodeStatus.SUCCESS
    assert ev_replan.fields["robot_id"] == 12
    assert ev_replan.fields["reason_code"] == 0
    assert ev_replan.fields["reason"] == "ROUTE_BLOCKED"

    # 0x0604 TASK_STATE: robot_id(u16), task_id(u32), state(u8)
    payload_task = struct.pack("<HIB", 12, 8042, 4)
    rec_task = EventRecord(source_id=50, timestamp_ns=5_000_300, event_type=0x0604, payload_bytes=payload_task)
    ev_task = decode(rec_task, warehouse_robots_schema)
    assert ev_task.decode_status == DecodeStatus.SUCCESS
    assert ev_task.fields["robot_id"] == 12
    assert ev_task.fields["task_id"] == 8042
    assert ev_task.fields["state"] == 4
    assert ev_task.fields["state_name"] == "DELAYED"


def test_decode_mavlink_common(mavlink_common_schema: SchemaRegistry):
    """Test decoding little-endian MAVLink Common v0 messages."""
    # 0 HEARTBEAT: custom_mode(u32), type(u8), autopilot(u8), base_mode(u8), system_status(u8), mavlink_version(u8)
    payload_hb = struct.pack("<IBBBBB", 0x1000, 2, 3, 128, 4, 3)
    rec_hb = EventRecord(source_id=60, timestamp_ns=6_000_000, event_type=0, payload_bytes=payload_hb)
    ev_hb = decode(rec_hb, mavlink_common_schema)
    assert ev_hb.decode_status == DecodeStatus.SUCCESS
    assert ev_hb.event_name == "HEARTBEAT"
    assert ev_hb.fields["custom_mode"] == 0x1000
    assert ev_hb.fields["type"] == 2
    assert ev_hb.fields["autopilot"] == 3
    assert ev_hb.fields["base_mode"] == 128
    assert ev_hb.fields["system_status"] == 4
    assert ev_hb.fields["mavlink_version"] == 3

    # 30 ATTITUDE: time_boot_ms(u32), roll(f32), pitch(f32), yaw(f32), rollspeed(f32), pitchspeed(f32), yawspeed(f32)
    payload_att = struct.pack("<Iffffff", 50000, 0.05, -0.02, 1.57, 0.001, -0.001, 0.002)
    rec_att = EventRecord(source_id=60, timestamp_ns=6_000_100, event_type=30, payload_bytes=payload_att)
    ev_att = decode(rec_att, mavlink_common_schema)
    assert ev_att.decode_status == DecodeStatus.SUCCESS
    assert ev_att.fields["time_boot_ms"] == 50000
    assert ev_att.fields["roll"] == 0.05
    assert ev_att.fields["pitch"] == -0.02
    assert ev_att.fields["yaw"] == 1.57

    # 253 STATUSTEXT: severity(u8), id(u16), chunk_seq(u8)
    payload_st = struct.pack("<BHB", 3, 101, 0)
    rec_st = EventRecord(source_id=60, timestamp_ns=6_000_200, event_type=253, payload_bytes=payload_st)
    ev_st = decode(rec_st, mavlink_common_schema)
    assert ev_st.decode_status == DecodeStatus.SUCCESS
    assert ev_st.fields["severity"] == 3
    assert ev_st.fields["id"] == 101
    assert ev_st.fields["chunk_seq"] == 0


# ==============================================================================
# 3. Enum Resolution & Fallback to UNKNOWN
# ==============================================================================

def test_enum_translation_and_unknown_fallback(
    microservices_schema: SchemaRegistry,
    autonomous_vehicle_schema: SchemaRegistry,
):
    """Verify mapped enum codes translate to labels and unmapped codes decode as UNKNOWN."""
    # Mapped code
    payload_valid = struct.pack("<HHHB", 1, 10, 50, 0)
    ev_valid = decode(
        EventRecord(source_id=1, timestamp_ns=100, event_type=0x0201, payload_bytes=payload_valid),
        microservices_schema,
    )
    assert ev_valid.fields["state_code"] == 0
    assert ev_valid.fields["state"] == "HEALTHY"
    assert len(ev_valid.warnings) == 0

    # Unmapped code -> UNKNOWN
    payload_unmapped = struct.pack("<HHHB", 1, 10, 50, 77)
    ev_unmapped = decode(
        EventRecord(source_id=1, timestamp_ns=200, event_type=0x0201, payload_bytes=payload_unmapped),
        microservices_schema,
    )
    assert ev_unmapped.decode_status == DecodeStatus.SUCCESS
    assert ev_unmapped.fields["state_code"] == 77
    assert ev_unmapped.fields["state"] == "UNKNOWN"
    assert len(ev_unmapped.warnings) == 1
    assert "state_code=77 has no mapping in enum 'pool_state'" in ev_unmapped.warnings[0]

    # Big-endian unmapped code -> UNKNOWN
    payload_ecu_unmapped = struct.pack(">HH", 0x1111, 999)
    ev_ecu = decode(
        EventRecord(source_id=1, timestamp_ns=300, event_type=0x0402, payload_bytes=payload_ecu_unmapped),
        autonomous_vehicle_schema,
    )
    assert ev_ecu.decode_status == DecodeStatus.SUCCESS
    assert ev_ecu.fields["fault_code"] == 999
    assert ev_ecu.fields["fault_name"] == "UNKNOWN"
    assert len(ev_ecu.warnings) == 1
    assert "fault_code=999 has no mapping in enum 'ecu_fault'" in ev_ecu.warnings[0]


# ==============================================================================
# 4. Trailing Bytes (PARTIALLY_DECODED) & Malformed Payloads (MALFORMED_PAYLOAD)
# ==============================================================================

def test_partially_decoded_trailing_bytes(industrial_plc_schema: SchemaRegistry):
    """Payload with trailing vendor/debug bytes must decode as PARTIALLY_DECODED."""
    base_payload = struct.pack("<HfB", 5, 87.5, 0)
    extra_vendor_bytes = b"\xde\xad\xbe\xef\xca\xfe"
    rec = EventRecord(
        source_id=20,
        timestamp_ns=2_000_000,
        event_type=0x0301,
        payload_bytes=base_payload + extra_vendor_bytes,
    )
    ev = decode(rec, industrial_plc_schema)
    assert ev.decode_status == DecodeStatus.PARTIALLY_DECODED
    assert ev.unknown_bytes == len(extra_vendor_bytes)
    assert ev.fields["sensor_id"] == 5
    assert ev.fields["temperature_c"] == 87.5
    assert ev.fields["drift_detected"] is False


def test_malformed_truncated_payload(warehouse_robots_schema: SchemaRegistry):
    """Truncated payload missing bytes must yield MALFORMED_PAYLOAD with fields discarded."""
    full_payload = struct.pack("<HfBB", 12, 20.8, 14, 1)  # 8 bytes
    truncated = full_payload[:5]  # only 5 bytes
    rec = EventRecord(source_id=50, timestamp_ns=5_000_000, event_type=0x0601, payload_bytes=truncated)
    ev = decode(rec, warehouse_robots_schema)
    assert ev.decode_status == DecodeStatus.MALFORMED_PAYLOAD
    assert ev.fields == {}
    assert ev.unknown_bytes == 5
    assert "field 'voltage_v' needs bytes 2..5 but payload has 5 bytes" in (ev.reason or "")


def test_malformed_invalid_bool_value(industrial_plc_schema: SchemaRegistry):
    """Non-canonical bool value (not 0 or 1) makes the entire payload MALFORMED_PAYLOAD."""
    # drift_detected is bool at offset 6; set raw byte to 2
    bad_payload = struct.pack("<HfB", 5, 87.5, 2)
    rec = EventRecord(source_id=20, timestamp_ns=2_000_000, event_type=0x0301, payload_bytes=bad_payload)
    ev = decode(rec, industrial_plc_schema)
    assert ev.decode_status == DecodeStatus.MALFORMED_PAYLOAD
    assert ev.fields == {}
    assert "field 'drift_detected' is bool but byte 6 has value 2 (expected 0 or 1)" in (ev.reason or "")


# ==============================================================================
# 5. Generalisation Rules (G01 - G04)
# ==============================================================================

def test_g01_five_domains_through_same_pipeline(
    microservices_schema: SchemaRegistry,
    industrial_plc_schema: SchemaRegistry,
    autonomous_vehicle_schema: SchemaRegistry,
    financial_hft_schema: SchemaRegistry,
    warehouse_robots_schema: SchemaRegistry,
):
    """Rule G01: Events across all 5 domains are decoded through identical decoder code."""
    domains = [
        (microservices_schema, 0x0201, struct.pack("<HHHB", 1, 50, 50, 3)),
        (industrial_plc_schema, 0x0301, struct.pack("<HfB", 5, 88.0, 1)),
        (autonomous_vehicle_schema, 0x0401, struct.pack(">Bf", 1, 60.0)),
        (financial_hft_schema, 0x0501, struct.pack("<IddI", 1, 10.0, 10.5, 100)),
        (warehouse_robots_schema, 0x0601, struct.pack("<HfBB", 2, 24.0, 80, 0)),
    ]
    for schema, etype, payload in domains:
        rec = EventRecord(source_id=1, timestamp_ns=1_000_000_000, event_type=etype, payload_bytes=payload)
        ev = decode(rec, schema)
        assert ev.decode_status == DecodeStatus.SUCCESS
        assert len(ev.fields) > 0


def test_g02_no_domain_specific_code_in_decoder():
    """Rule G02: Check that core fss_py decoder and schema code contain no hardcoded domain terms."""
    decoder_src = (WORKSPACE_ROOT / "fss_py" / "decoder.py").read_text(encoding="utf-8").lower()
    schema_src = (WORKSPACE_ROOT / "fss_py" / "schema.py").read_text(encoding="utf-8").lower()
    for term in ("microservices", "industrial_plc", "autonomous_vehicle", "financial_hft", "warehouse_robots"):
        assert term not in decoder_src, f"Hardcoded domain term '{term}' found in decoder.py"
        assert term not in schema_src, f"Hardcoded domain term '{term}' found in schema.py"


def test_g03_big_endian_and_little_endian_coexistence(
    autonomous_vehicle_schema: SchemaRegistry,
    microservices_schema: SchemaRegistry,
):
    """Rule G03: Big-endian D4 pack and little-endian D2 pack decode correctly in the same runtime."""
    # Big-endian test: u16 value 0x0102 packed big-endian is [0x01, 0x02]
    d4_rec = EventRecord(source_id=1, timestamp_ns=100, event_type=0x0402, payload_bytes=struct.pack(">HH", 0x0102, 1))
    d4_ev = decode(d4_rec, autonomous_vehicle_schema)
    assert d4_ev.decode_status == DecodeStatus.SUCCESS
    assert d4_ev.fields["ecu_id"] == 0x0102
    assert d4_ev.fields["fault_code"] == 1

    # Little-endian test: u16 value 0x0102 packed little-endian is [0x02, 0x01]
    d2_rec = EventRecord(source_id=1, timestamp_ns=200, event_type=0x0201, payload_bytes=struct.pack("<HHHB", 0x0102, 10, 20, 0))
    d2_ev = decode(d2_rec, microservices_schema)
    assert d2_ev.decode_status == DecodeStatus.SUCCESS
    assert d2_ev.fields["pool_id"] == 0x0102


def test_g04_dynamic_schema_discovery():
    """Rule G04: Schemas can be loaded dynamically from arbitrary YAML files without code changes."""
    yaml_files = list(SCHEMAS_DIR.glob("*_v0.yaml"))
    assert len(yaml_files) >= 6
    registries = [load_schema_file(yf) for yf in yaml_files]
    assert all(isinstance(r, SchemaRegistry) for r in registries)
    assert all(len(r) > 0 for r in registries)


# ==============================================================================
# 6. Domain Manual Parsing (All 5 Domain Manuals)
# ==============================================================================

@pytest.mark.parametrize(
    "manual_filename, min_entries, expected_codes",
    [
        (
            "microservices_error_codes.md",
            15,
            ["HEALTHY", "SATURATED", "POOL_EXHAUSTED", "GATEWAY_TIMEOUT", "SERVICE_UNAVAILABLE", "INTERNAL_SERVER_ERROR"],
        ),
        (
            "industrial_plc_error_codes.md",
            15,
            ["NONE", "OVERHEAT", "EMERGENCY_STOP", "PRESSURE_LOSS", "RUNNING", "STARVED", "BLOCKED"],
        ),
        (
            "can_bus_error_codes.md",
            15,
            ["NO_FAULT", "SENSOR_COMM_ERROR", "ACTUATOR_FAULT", "PLAUSIBILITY_CHECK_FAILED", "NORMAL", "DISABLED", "FAULT"],
        ),
        (
            "financial_fix_error_codes.md",
            15,
            ["TOO_LATE_TO_CANCEL", "UNKNOWN_ORDER", "NEW", "PARTIALLY_FILLED", "FILLED", "CANCELLED", "REJECTED"],
        ),
        (
            "warehouse_robots_error_codes.md",
            15,
            ["ROUTE_BLOCKED", "OBSTACLE_DETECTED", "LOW_BATTERY", "ASSIGNED", "IN_PROGRESS", "COMPLETED", "ABORTED", "DELAYED"],
        ),
    ],
)
def test_parse_domain_manuals(manual_filename: str, min_entries: int, expected_codes: list[str]):
    """Verify that all 5 domain technical error manuals parse into valid VendorErrorEntry objects."""
    path = MANUALS_DIR / manual_filename
    assert path.exists(), f"Manual {path} must exist"

    content = path.read_text(encoding="utf-8")
    entries = parse_error_manual(content, manual_filename)

    assert len(entries) >= min_entries, f"{manual_filename} produced {len(entries)} entries, expected >= {min_entries}"

    entry_codes = {e.error_code for e in entries}
    for expected in expected_codes:
        assert expected in entry_codes, f"Expected code '{expected}' missing from {manual_filename}"

    # Validate schema of each VendorErrorEntry
    for e in entries:
        assert isinstance(e, VendorErrorEntry)
        assert e.error_code
        assert e.subsystem
        assert e.severity in ("NOMINAL", "NOTICE", "INFO", "WARNING", "ERROR", "CRITICAL")
        assert e.title
        assert len(e.probable_causes) >= 1
        assert len(e.recommended_actions) >= 1
        assert e.doc_chunk_id.startswith("err-")

        # Verify to_doc() conversion for RAG indexing
        doc = e.to_doc()
        assert doc["kind"] == "vendor-error-code"
        assert e.error_code in doc["title"]
        assert doc["content"]


def test_extract_and_match_multi_domain_error_codes(
    microservices_schema: SchemaRegistry,
    industrial_plc_schema: SchemaRegistry,
):
    """Test extracting error codes from decoded multi-domain events and matching against manual entries."""
    # 1. Decode a DB pool exhausted event
    payload_pool = struct.pack("<HHHB", 1, 50, 50, 3)
    ev_pool = decode(
        EventRecord(source_id=1, timestamp_ns=1_000_000, event_type=0x0201, payload_bytes=payload_pool),
        microservices_schema,
    )
    # 2. Decode an industrial interlock overheat event
    payload_lock = struct.pack("<HBH", 12, 1, 1)
    ev_lock = decode(
        EventRecord(source_id=2, timestamp_ns=2_000_000, event_type=0x0302, payload_bytes=payload_lock),
        industrial_plc_schema,
    )

    events = [
        {
            "event_type": ev_pool.event_type,
            "event_name": ev_pool.event_name,
            "fields": ev_pool.fields,
            "interpreted_fields": ev_pool.interpreted_fields,
        },
        {
            "event_type": ev_lock.event_type,
            "event_name": ev_lock.event_name,
            "fields": ev_lock.fields,
            "interpreted_fields": ev_lock.interpreted_fields,
        },
    ]

    extracted_tokens = extract_error_codes_from_events(events)

    # Verify extracted tokens include both numeric and symbolic labels
    assert "POOL_EXHAUSTED" in extracted_tokens
    assert "OVERHEAT" in extracted_tokens
    assert "DB_POOL_STATUS" in extracted_tokens
    assert "INTERLOCK" in extracted_tokens

    # Verify matching with parsed manuals
    ms_entries = parse_error_manual((MANUALS_DIR / "microservices_error_codes.md").read_text(), "microservices.md")
    plc_entries = parse_error_manual((MANUALS_DIR / "industrial_plc_error_codes.md").read_text(), "industrial_plc.md")

    ms_codes = {e.error_code for e in ms_entries}
    plc_codes = {e.error_code for e in plc_entries}

    assert "POOL_EXHAUSTED" in ms_codes
    assert "OVERHEAT" in plc_codes
