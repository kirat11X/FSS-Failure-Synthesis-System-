"""Tests for persistent JEP-380 Unix Domain Socket daemon & UdsJavaRetriever (REQ-072).

Verifies:
  - AF_UNIX socket daemon lifecycle (start_daemon, ping, shutdown).
  - Both 4-byte BE length prefix framing and NDJSON acceptance.
  - Full retrieve and retrieve_full roundtrip on golden BlockStore data.
  - uint64 nanosecond precision preservation without loss or overflow.
  - Sub-100ms multi-query latency over persistent UDS connection.
  - Transparent fallback to JavaRetriever when UDS socket is unavailable.
  - Clean shutdown and socket file unlinking on daemon exit.
"""

from __future__ import annotations

import json
import os
import socket
import struct
import tempfile
import time
import unittest
from pathlib import Path

from bridge.java_retriever import JavaRetriever
from bridge.uds_retriever import UdsJavaRetriever
from fss_py.model import ALL, EventRecord
from fss_py.wire import encode_envelope

ROOT_DIR = Path(__file__).resolve().parent.parent
GOLDEN_FIXTURE = ROOT_DIR / "fixtures" / "golden" / "scenario_298_474.bin"


class TestUdsIpc(unittest.TestCase):

    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.socket_path = Path(self.tmp_dir.name) / f"test_bridge_{os.getpid()}_{int(time.time()*1000)}.sock"
        self.daemon_proc = None

    def tearDown(self):
        if self.daemon_proc is not None:
            try:
                self.daemon_proc.terminate()
                self.daemon_proc.wait(timeout=2.0)
            except Exception:
                try:
                    self.daemon_proc.kill()
                except Exception:
                    pass
            self.daemon_proc = None

        if self.socket_path.exists():
            try:
                self.socket_path.unlink()
            except Exception:
                pass
        self.tmp_dir.cleanup()

    def test_01_daemon_lifecycle_and_ping(self):
        """Test starting the UDS daemon, pinging via client, and pinging via raw NDJSON."""
        self.daemon_proc = UdsJavaRetriever.start_daemon(socket_path=self.socket_path, timeout=6.0)
        self.assertTrue(self.socket_path.exists(), "Socket file should exist after daemon start")

        retriever = UdsJavaRetriever(socket_path=self.socket_path, fallback_to_cli=False)
        self.assertTrue(retriever.ping(), "UdsJavaRetriever.ping() should return True")

        # Test raw NDJSON framing acceptance
        raw_sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        raw_sock.settimeout(2.0)
        raw_sock.connect(str(self.socket_path))
        try:
            raw_sock.sendall(b'{"action": "ping", "trace_id": "test_ndjson_01"}\n')
            with raw_sock.makefile("rb") as reader:
                line = reader.readline()
            resp = json.loads(line.decode("utf-8"))
            self.assertEqual(resp.get("status"), "OK")
            self.assertTrue(resp.get("pong"))
            self.assertEqual(resp.get("trace_id"), "test_ndjson_01")
        finally:
            raw_sock.close()

    def test_02_retrieve_roundtrip_golden_fixture(self):
        """Test retrieve and retrieve_full on golden fixture with 100% field integrity."""
        # 1. Ingest golden fixture into a temp BlockStore .dat file
        store_path = Path(self.tmp_dir.name) / "golden_store.dat"
        cli = JavaRetriever.from_fixtures(GOLDEN_FIXTURE, store_path=store_path)

        # 2. Start UDS daemon
        self.daemon_proc = UdsJavaRetriever.start_daemon(socket_path=self.socket_path, timeout=6.0)
        retriever = UdsJavaRetriever(store_path=store_path, socket_path=self.socket_path, fallback_to_cli=False)

        # 3. Query all sources over bounded range
        min_ts = 1791454020000000000
        max_ts = 1791454025000000000
        res = retriever.retrieve(store_path, ALL, min_ts, max_ts, max_events=100)

        self.assertFalse(res.truncated)
        self.assertEqual(len(res.events), 9)

        # Check source 298
        res_298 = retriever.retrieve(store_path, 298, min_ts, max_ts, max_events=100)
        self.assertEqual(len(res_298.events), 4)
        for ev in res_298.events:
            self.assertEqual(ev.source_id, 298)

        # 4. Test retrieve_full
        full_events = retriever.retrieve_full(store_path)
        self.assertEqual(len(full_events), 9)
        self.assertEqual(
            sorted((e.source_id, e.timestamp_ns) for e in full_events),
            sorted((e.source_id, e.timestamp_ns) for e in res.events),
        )

    def test_03_uint64_nanosecond_precision(self):
        """Test uint64 nanosecond timestamp precision preservation."""
        store_path = Path(self.tmp_dir.name) / "uint64_store.dat"
        # Large timestamps near 2^63
        large_ts = 1791454021234567890
        ev = EventRecord(
            source_id=300,
            timestamp_ns=large_ts,
            event_type=0x42,
            payload_bytes=b"high_precision_payload",
        )

        tmp_bin = Path(self.tmp_dir.name) / "fixture_u64.bin"
        tmp_bin.write_bytes(encode_envelope(ev))

        cli = JavaRetriever(store_path=store_path)
        cli.ingest(tmp_bin)

        self.daemon_proc = UdsJavaRetriever.start_daemon(socket_path=self.socket_path, timeout=6.0)
        retriever = UdsJavaRetriever(store_path=store_path, socket_path=self.socket_path, fallback_to_cli=False)

        res = retriever.retrieve(store_path, 300, large_ts - 1000, large_ts + 1000, max_events=10)
        self.assertEqual(len(res.events), 1)
        ret_ev = res.events[0]
        self.assertEqual(ret_ev.timestamp_ns, large_ts)
        self.assertEqual(ret_ev.source_id, 300)
        self.assertEqual(ret_ev.event_type, 0x42)
        self.assertEqual(ret_ev.payload_bytes, b"high_precision_payload")

    def test_04_sub_100ms_multi_query_latency(self):
        """Test sub-100ms multi-query latency over persistent UDS connection."""
        store_path = Path(self.tmp_dir.name) / "latency_store.dat"
        cli = JavaRetriever.from_fixtures(GOLDEN_FIXTURE, store_path=store_path)

        self.daemon_proc = UdsJavaRetriever.start_daemon(socket_path=self.socket_path, timeout=6.0)
        retriever = UdsJavaRetriever(store_path=store_path, socket_path=self.socket_path, fallback_to_cli=False)

        # Warm up
        retriever.retrieve(store_path, ALL, 1791454020000000000, 1791454025000000000, max_events=10)

        num_queries = 50
        start_time = time.perf_counter()
        for _ in range(num_queries):
            res = retriever.retrieve(store_path, ALL, 1791454020000000000, 1791454025000000000, max_events=10)
            self.assertEqual(len(res.events), 9)
        elapsed = time.perf_counter() - start_time

        avg_latency_ms = (elapsed / num_queries) * 1000.0
        # Multi-query latency over persistent socket (target < 25ms/query under test load)
        self.assertLess(
            avg_latency_ms,
            25.0,
            f"Average query latency {avg_latency_ms:.2f}ms exceeds target 25ms",
        )
        self.assertLess(
            elapsed,
            1.5,
            f"50 queries took {elapsed:.3f}s total (exceeds 1.5s limit)",
        )

    def test_05_graceful_fallback_when_socket_missing(self):
        """Test graceful fallback to JavaRetriever when UDS socket is unavailable."""
        store_path = Path(self.tmp_dir.name) / "fallback_store.dat"
        cli = JavaRetriever.from_fixtures(GOLDEN_FIXTURE, store_path=store_path)

        # Do NOT start daemon, use non-existent socket
        non_existent = Path(self.tmp_dir.name) / "non_existent_bridge.sock"
        retriever = UdsJavaRetriever(store_path=store_path, socket_path=non_existent, fallback_to_cli=True)

        self.assertFalse(retriever.ping())

        # Retrieve should fall back to CLI without raising connection error
        res = retriever.retrieve(store_path, ALL, 1791454020000000000, 1791454025000000000, max_events=50)
        self.assertEqual(len(res.events), 9)

        # Full retrieve should also fall back cleanly
        full_events = retriever.retrieve_full(store_path)
        self.assertEqual(len(full_events), 9)

    def test_06_clean_shutdown_and_socket_unlinking(self):
        """Test clean shutdown command and socket file deletion."""
        self.daemon_proc = UdsJavaRetriever.start_daemon(socket_path=self.socket_path, timeout=6.0)
        self.assertTrue(self.socket_path.exists())

        retriever = UdsJavaRetriever(socket_path=self.socket_path, fallback_to_cli=False)
        shutdown_ok = retriever.shutdown()
        self.assertTrue(shutdown_ok)

        # Wait for daemon process to terminate
        try:
            self.daemon_proc.wait(timeout=3.0)
        except subprocess.TimeoutExpired:
            self.daemon_proc.kill()
            self.fail("Daemon did not exit within 3 seconds of shutdown signal")

        # Socket file should be unlinked
        deadline = time.time() + 2.0
        while time.time() < deadline and self.socket_path.exists():
            time.sleep(0.05)

        self.assertFalse(self.socket_path.exists(), "Socket file must be unlinked after daemon shutdown")
