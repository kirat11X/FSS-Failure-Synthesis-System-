"""Test Suite 1: End-to-End Unsigned 64-Bit Timestamp Integrity.

Verifies:
  1. Hypothesis property testing @given(st.integers(0, (1<<64)-1)) for wire & schema roundtrip.
  2. Exact boundary seeds: 2^53-1, 2^53, 2^63-1, 2^63, 2^64-1, and 1795139460123456789 vs +100 ns
     persisted in Java BlockStore and retrieved.
  3. Exact bitwise equality in Python int (lossless, no sign inversion to negative).
  4. Monotonic findMinTs() behavior in Java BlockStore.
  5. Distinct stringified display timestamps for 100 ns pairs (preventing IEEE 754 float64 collisions).
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import pytest
from hypothesis import given, settings, example, strategies as st

from bridge.java_retriever import JavaRetriever
from bridge.schemas import (
    RETRIEVAL_ENVELOPE_ADAPTER,
    TelemetryEventWire,
    format_local_timestamp_ns,
)
from fss_py.model import ALL, EventRecord
from fss_py.wire import decode_envelope, encode_envelope


BOUNDARY_SEEDS = [
    0,
    (1 << 53) - 1,              # 9,007,199,254,740,991 (JS MAX_SAFE_INTEGER)
    1 << 53,                   # 9,007,199,254,740,992 (Precision loss boundary)
    (1 << 63) - 1,              # 9,223,372,036,854,775,807 (Java Long.MAX_VALUE)
    1 << 63,                   # 9,223,372,036,854,775,808 (High bit set, negative in signed long)
    (1 << 64) - 2,              # 18,446,744,073,709,551,614
    (1 << 64) - 1,              # 18,446,744,073,709,551,615 (UINT64_MAX, -1 in signed long)
    1795139460123456789,        # 2026 nanosecond scale epoch
    1795139460123456789 + 100,  # +100 ns pair (collides in IEEE 754 float64)
]


class TestUint64Roundtrip(unittest.TestCase):

    def test_01_boundary_seeds_wire_codec(self):
        """Wire envelope encodes and decodes all boundary seeds with exact bitwise equality."""
        for seed in BOUNDARY_SEEDS:
            with self.subTest(seed=seed):
                rec = EventRecord(source_id=474, timestamp_ns=seed, event_type=0x10, payload_bytes=b"\xaa\xbb")
                wire_bytes = encode_envelope(rec)
                decoded = decode_envelope(wire_bytes)
                self.assertEqual(decoded.timestamp_ns, seed)
                self.assertIsInstance(decoded.timestamp_ns, int)
                # Verify bitwise equality
                self.assertEqual(bin(decoded.timestamp_ns), bin(seed))

    def test_02_boundary_seeds_blockstore_persistence_and_retrieval(self):
        """Persist boundary timestamps into Java BlockStore and verify exact retrieved int."""
        # For retrieval in half-open window [tStart, tEnd), test seeds where tEnd > tStart can be formed
        retrievable_seeds = [s for s in BOUNDARY_SEEDS if s < (1 << 64) - 1]

        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f_bin:
            bin_path = Path(f_bin.name)
        with tempfile.NamedTemporaryFile(suffix=".dat", delete=False) as f_store:
            store_path = Path(f_store.name)

        try:
            # Write envelopes for each seed
            with open(bin_path, "wb") as bf:
                for seed in retrievable_seeds:
                    rec = EventRecord(source_id=474, timestamp_ns=seed, event_type=0x10, payload_bytes=b"\x01")
                    bf.write(encode_envelope(rec))

            # Ingest into Java BlockStore
            retriever = JavaRetriever.from_fixtures(bin_path, store_path=store_path)

            for seed in retrievable_seeds:
                with self.subTest(seed=seed):
                    t_start = max(0, seed - 1000)
                    t_end = min((1 << 64) - 1, seed + 1000)
                    if t_end <= t_start:
                        t_end = t_start + 1

                    env = retriever.retrieve_envelope(ALL, t_start, t_end, max_events=10)
                    self.assertEqual(env.status, "OK")
                    matching = [ev for ev in env.events if ev.timestamp_ns == seed]
                    self.assertEqual(len(matching), 1)
                    self.assertEqual(matching[0].timestamp_ns, seed)
                    self.assertIsInstance(matching[0].timestamp_ns, int)
                    # Verify no sign inversion (never negative)
                    self.assertGreaterEqual(matching[0].timestamp_ns, 0)
        finally:
            bin_path.unlink(missing_ok=True)
            store_path.unlink(missing_ok=True)

    def test_03_uint64_max_findmints(self):
        """Store containing (1<<64)-1 produces correct unsigned findMinTs() string without sign inversion."""
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f_bin:
            bin_path = Path(f_bin.name)
        with tempfile.NamedTemporaryFile(suffix=".dat", delete=False) as f_store:
            store_path = Path(f_store.name)

        try:
            rec = EventRecord(source_id=474, timestamp_ns=(1 << 64) - 1, event_type=0x10, payload_bytes=b"\x01")
            bin_path.write_bytes(encode_envelope(rec))

            retriever = JavaRetriever.from_fixtures(bin_path, store_path=store_path)
            # Retrieve without explicit window to trigger findMinTs()
            import subprocess
            cmd = [
                retriever.java_bin, "-cp", retriever.classpath, "bridge.JavaStoreBridge",
                "retrieve", "--store", str(store_path), "--format", "ndjson"
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(proc.stdout.strip())
            # min_ts_ns must be "18446744073709551615", NOT "-1"
            self.assertEqual(data["min_ts_ns"], str((1 << 64) - 1))
            self.assertNotEqual(data["min_ts_ns"], "-1")
            self.assertEqual(int(data["min_ts_ns"]), (1 << 64) - 1)
        finally:
            bin_path.unlink(missing_ok=True)
            store_path.unlink(missing_ok=True)

    def test_04_monotonic_findmints(self):
        """findMinTs() monotonically identifies the minimum unsigned timestamp across blocks."""
        t1 = 1000
        t2 = 1 << 63  # 9,223,372,036,854,775,808

        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as f_bin:
            bin_path = Path(f_bin.name)
        with tempfile.NamedTemporaryFile(suffix=".dat", delete=False) as f_store:
            store_path = Path(f_store.name)

        try:
            with open(bin_path, "wb") as bf:
                bf.write(encode_envelope(EventRecord(10, t1, 0x10, b"\x01")))
                bf.write(encode_envelope(EventRecord(20, t2, 0x10, b"\x02")))

            retriever = JavaRetriever.from_fixtures(bin_path, store_path=store_path)
            import subprocess
            cmd = [
                retriever.java_bin, "-cp", retriever.classpath, "bridge.JavaStoreBridge",
                "retrieve", "--store", str(store_path), "--format", "ndjson"
            ]
            proc = subprocess.run(cmd, capture_output=True, text=True, check=True)
            data = json.loads(proc.stdout.strip())
            # In unsigned comparison, 1000 < 2^63 (if signed, 2^63 would be negative and wrongly chosen as min)
            self.assertEqual(int(data["min_ts_ns"]), t1)
        finally:
            bin_path.unlink(missing_ok=True)
            store_path.unlink(missing_ok=True)

    def test_05_distinct_stringified_display_timestamps_for_100ns_pairs(self):
        """Verify 100 ns nanosecond pairs maintain distinct display strings despite IEEE 754 collision."""
        ts1 = 1795139460123456789
        ts2 = ts1 + 100  # 1795139460123456889

        # Confirm that IEEE 754 float64 COLLIDES due to 256 ns ULP at exponent 2^60
        f1 = float(ts1)
        f2 = float(ts2)
        self.assertEqual(f1, f2, "Expected IEEE 754 binary64 float collision at 2026 nanosecond scale")

        # Confirm that stringified representations and ISO-8601 displays are strictly distinct
        str1 = str(ts1)
        str2 = str(ts2)
        self.assertNotEqual(str1, str2)
        self.assertTrue(str1.endswith("789"))
        self.assertTrue(str2.endswith("889"))

        iso1 = format_local_timestamp_ns(ts1)
        iso2 = format_local_timestamp_ns(ts2)
        self.assertNotEqual(iso1, iso2)
        self.assertTrue(iso1.endswith(".123456789Z"))
        self.assertTrue(iso2.endswith(".123456889Z"))

        # Pydantic v2 TelemetryEventWire display properties
        ev1 = TelemetryEventWire(source_id=1, timestamp_ns=ts1, event_type=1, payload_b64="AA==")
        ev2 = TelemetryEventWire(source_id=1, timestamp_ns=ts2, event_type=1, payload_b64="AA==")
        self.assertNotEqual(ev1.timestamp_ns_str, ev2.timestamp_ns_str)
        self.assertNotEqual(ev1.display_ns, ev2.display_ns)
        self.assertNotEqual(ev1.iso8601, ev2.iso8601)


# Hypothesis property test
@settings(max_examples=50, deadline=None)
@given(ts=st.integers(0, (1 << 64) - 1))
@example(ts=0)
@example(ts=(1 << 53) - 1)
@example(ts=1 << 53)
@example(ts=(1 << 63) - 1)
@example(ts=1 << 63)
@example(ts=(1 << 64) - 1)
@example(ts=1795139460123456789)
def test_hypothesis_uint64_wire_and_schema_roundtrip(ts: int):
    """Property test: any uint64 in [0, 2^64-1] preserves bitwise equality across wire and schema."""
    rec = EventRecord(source_id=100, timestamp_ns=ts, event_type=0x20, payload_bytes=b"prop")
    wire_bytes = encode_envelope(rec)
    decoded = decode_envelope(wire_bytes)
    assert decoded.timestamp_ns == ts
    assert isinstance(decoded.timestamp_ns, int)

    # Wire schema JSON validation
    wire_model = TelemetryEventWire(source_id=100, timestamp_ns=ts, event_type=0x20, payload_b64="cHJvcA==")
    json_bytes = wire_model.model_dump_json().encode("utf-8")
    wire_parsed = TelemetryEventWire.model_validate_json(json_bytes)
    assert wire_parsed.timestamp_ns == ts
    assert isinstance(wire_parsed.timestamp_ns, int)
