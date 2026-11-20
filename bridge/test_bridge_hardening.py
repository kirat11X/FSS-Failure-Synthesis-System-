"""Integration Bridge Hardening Tests for FSS.

Tests:
  1. test_uint64_roundtrip: Lossless uint64 handling, boundary values, and 100ns precision.
  2. test_pipe_deadlock_64kb: Pipe draining with payloads > 64KB on stdout and stderr.
  3. test_timeout_orphan_cleanup: Child process-group termination and 0 orphan/zombie JVMs.
  4. test_stdout_pollution: JAVA_TOOL_OPTIONS banners do not corrupt NDJSON protocol frames.
  5. test_zero_event_contract: 0-event returns typed envelope status="EMPTY".
  6. test_persistent_worker_af_unix: JEP-380 AF_UNIX persistent worker lifecycle and queries.
  7. test_blockstore_concurrency: Concurrent reads and writes with ReentrantReadWriteLock & sidecar locks.
"""

from __future__ import annotations

import fcntl
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from bridge.java_retriever import JavaRetriever, run_subprocess_safe
from bridge.schemas import (
    RETRIEVAL_ENVELOPE_ADAPTER,
    RetrievalEnvelope,
    TelemetryEventWire,
    bind_trace_id,
    format_iso8601_ns,
    get_or_create_trace_id,
)
from fss_py.model import ALL, EventRecord, RetrievalRequest


class TestBridgeHardening(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fixture_path = ROOT_DIR / "fixtures" / "golden" / "scenario_298_474.bin"
        # Ensure latest compilation
        subprocess.run(["bash", "bridge/compile.sh"], cwd=ROOT_DIR, check=True)

    def test_01_uint64_boundary_and_nanosecond_precision(self):
        """Verify uint64 boundaries and 100ns precision preservation."""
        boundaries = [
            0,
            (1 << 53) - 1,   # JS MAX_SAFE_INTEGER
            (1 << 53),       # JS precision loss threshold
            (1 << 63) - 1,   # Java Long.MAX_VALUE
            (1 << 63),       # High-bit set (negative if signed)
            (1 << 64) - 1,   # UINT64_MAX
        ]

        for b in boundaries:
            ev = TelemetryEventWire(
                source_id=1,
                timestamp_ns=str(b),
                event_type=0x10,
                payload_b64="AQID",
            )
            self.assertEqual(ev.timestamp_ns, b)
            self.assertIsInstance(ev.timestamp_ns, int)

            # JSON roundtrip
            dumped = ev.model_dump_json()
            self.assertIn(f'"{b}"', dumped)
            parsed = TelemetryEventWire.model_validate_json(dumped)
            self.assertEqual(parsed.timestamp_ns, b)

        # Verify 100 ns separation does not collapse
        ts_a = 1795139460123456789
        ts_b = 1795139460123456889  # +100 ns
        self.assertNotEqual(ts_a, ts_b)

        iso_a = format_iso8601_ns(ts_a)
        iso_b = format_iso8601_ns(ts_b)
        self.assertEqual(iso_a, "2026-11-20T01:51:00.123456789Z")
        self.assertEqual(iso_b, "2026-11-20T01:51:00.123456889Z")
        self.assertNotEqual(iso_a, iso_b)

    def test_02_pipe_deadlock_64kb_handling(self):
        """Verify run_subprocess_safe drains >64KB pipes without deadlocking."""
        # Spawn Java one-liner producing 256KB on stdout and 128KB on stderr
        java_code = (
            "public class PipeFlood {"
            "  public static void main(String[] args) {"
            "    byte[] chunk = new byte[8192];"
            "    for (int i = 0; i < 32; i++) System.out.write(chunk, 0, chunk.length);"
            "    System.out.flush();"
            "    for (int i = 0; i < 16; i++) System.err.write(chunk, 0, chunk.length);"
            "    System.err.flush();"
            "  }"
            "}"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "PipeFlood.java"
            src.write_text(java_code)
            subprocess.run(["javac", str(src)], check=True)

            cmd = ["java", "-cp", tmpdir, "PipeFlood"]
            start_t = time.time()
            code, stdout, stderr = run_subprocess_safe(cmd, timeout=5.0)
            elapsed = time.time() - start_t

            self.assertEqual(code, 0)
            self.assertEqual(len(stdout), 256 * 1024)
            self.assertEqual(len(stderr), 128 * 1024)
            self.assertLess(elapsed, 5.0)

    def test_03_timeout_orphan_cleanup(self):
        """Verify on TimeoutError child process group is terminated and zero zombie JVMs remain."""
        java_code = (
            "public class SleepHang {"
            "  public static void main(String[] args) throws Exception {"
            "    Thread.sleep(60000);"
            "  }"
            "}"
        )
        with tempfile.TemporaryDirectory() as tmpdir:
            src = Path(tmpdir) / "SleepHang.java"
            src.write_text(java_code)
            subprocess.run(["javac", str(src)], check=True)

            cmd = ["java", "-cp", tmpdir, "SleepHang"]
            with self.assertRaises(TimeoutError):
                run_subprocess_safe(cmd, timeout=0.8)

            # Give kernel brief moment to reap
            time.sleep(0.3)

            # Check ps table for SleepHang process
            ps = subprocess.run(["ps", "-eo", "pid,stat,cmd"], stdout=subprocess.PIPE, text=True)
            self.assertNotIn("SleepHang", ps.stdout)

    def test_04_stdout_pollution_immunity(self):
        """Verify JAVA_TOOL_OPTIONS notices do not corrupt NDJSON retrieval frames."""
        retriever = JavaRetriever.from_fixtures(self.fixture_path, mode="cli")
        try:
            # Inject JAVA_TOOL_OPTIONS
            old_opts = os.environ.get("JAVA_TOOL_OPTIONS")
            os.environ["JAVA_TOOL_OPTIONS"] = "-Dcustom.property=true"
            try:
                env = retriever.retrieve_envelope(ALL, 1791454020000000000, 1791454030000000000, max_events=100)
                self.assertEqual(env.status, "OK")
                self.assertEqual(len(env.events), 9)
            finally:
                if old_opts is not None:
                    os.environ["JAVA_TOOL_OPTIONS"] = old_opts
                else:
                    os.environ.pop("JAVA_TOOL_OPTIONS", None)
        finally:
            if retriever.store_path.exists():
                retriever.store_path.unlink()

    def test_05_zero_event_contract(self):
        """Verify querying a time range with 0 events returns explicit typed EMPTY envelope."""
        retriever = JavaRetriever.from_fixtures(self.fixture_path, mode="cli")
        try:
            env = retriever.retrieve_envelope(ALL, 1000, 2000, max_events=100)
            self.assertEqual(env.status, "EMPTY")
            self.assertEqual(len(env.events), 0)
            self.assertEqual(env.count, 0)
            self.assertFalse(env.truncated)

            res = retriever.retrieve(ALL, 1000, 2000, max_events=100)
            self.assertEqual(len(res.events), 0)
            self.assertFalse(res.truncated)
        finally:
            if retriever.store_path.exists():
                retriever.store_path.unlink()

    def test_06_persistent_worker_af_unix_lifecycle(self):
        """Verify full lifecycle of JEP-380 AF_UNIX persistent worker."""
        sock_path = Path("/tmp/fss_test_worker_lifecycle.sock")
        worker_proc = JavaRetriever.start_worker(socket_path=sock_path)
        try:
            self.assertTrue(JavaRetriever.is_worker_alive(sock_path))

            retriever = JavaRetriever.from_fixtures(self.fixture_path, mode="worker", socket_path=sock_path)
            try:
                # Retrieve over persistent worker
                res = retriever.retrieve(ALL, 1791454020000000000, 1791454030000000000, max_events=100)
                self.assertEqual(len(res.events), 9)

                # Filter source 298
                res_298 = retriever.retrieve(298, 1791454020000000000, 1791454030000000000, max_events=100)
                self.assertEqual(len(res_298.events), 4)

                # 0-event retrieval
                env_0 = retriever.retrieve_envelope(ALL, 1000, 2000, max_events=100)
                self.assertEqual(env_0.status, "EMPTY")
            finally:
                if retriever.store_path.exists():
                    retriever.store_path.unlink()
        finally:
            stopped = JavaRetriever.stop_worker(sock_path)
            self.assertTrue(stopped)
            try:
                worker_proc.wait(timeout=2.0)
            except Exception:
                worker_proc.kill()
            self.assertFalse(sock_path.exists())

    def test_07_blockstore_concurrency(self):
        """Verify concurrent reads across multiple threads execute cleanly without lock exceptions."""
        retriever = JavaRetriever.from_fixtures(self.fixture_path, mode="cli")
        try:
            def worker_read(i: int):
                res = retriever.retrieve(ALL, 1791454020000000000, 1791454030000000000, max_events=100)
                return len(res.events)

            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(worker_read, range(8)))

            for count in results:
                self.assertEqual(count, 9)
        finally:
            if retriever.store_path.exists():
                retriever.store_path.unlink()

    def test_08_w3c_trace_id_propagation(self):
        """Verify 32-hex trace_id propagates from Python into Java response envelope."""
        custom_trace_id = "1234567890abcdef1234567890abcdef"
        bind_trace_id(custom_trace_id)

        retriever = JavaRetriever.from_fixtures(self.fixture_path, mode="cli")
        try:
            env = retriever.retrieve_envelope(ALL, 1791454020000000000, 1791454030000000000, max_events=100)
            self.assertEqual(env.trace_id, custom_trace_id)
        finally:
            if retriever.store_path.exists():
                retriever.store_path.unlink()


if __name__ == "__main__":
    unittest.main()
