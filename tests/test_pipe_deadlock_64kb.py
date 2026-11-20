"""Test Suite 2: 64KB Kernel Pipe Buffer Deadlock Elimination.

Spec requirement:
  Trigger retrieval returning >256KB on stdout while Java concurrently writes >128KB of logs to stderr;
  completes cleanly under 5s with zero hang and zero JSON corruption.

Background:
  Linux kernel pipe buffers hold ~64KB per file descriptor. If child stdout or stderr exceeds
  64KB while the parent blocks on wait(), both processes deadlock.
  JavaRetriever's run_subprocess_safe uses communicate() with concurrent stream draining
  and strict timeouts to ensure zero hangs.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from bridge.java_retriever import resolve_java_bin, run_subprocess_safe
from bridge.schemas import RETRIEVAL_ENVELOPE_ADAPTER

ROOT_DIR = Path(__file__).resolve().parent.parent


class TestPipeDeadlock64KB(unittest.TestCase):

    def test_pipe_deadlock_concurrent_large_buffers(self):
        """Java process concurrently writing >256KB to stdout and >128KB to stderr completes in < 5s."""
        java_bin = resolve_java_bin()

        # Java class that concurrently writes >128KB to stderr and >256KB NDJSON to stdout
        java_src = r"""
package test;

public class PipeDeadlockEmitter {
    public static void main(String[] args) throws Exception {
        // Concurrently stream >128KB to stderr
        Thread stderrThread = new Thread(() -> {
            for (int i = 0; i < 2200; i++) {
                System.err.println("STDERR_LOG_LINE_DRAIN_PIPE_TEST_" + i + "_" + "x".repeat(64));
            }
        });
        stderrThread.start();

        // Concurrently stream >256KB valid NDJSON envelope to stdout
        StringBuilder sb = new StringBuilder(300_000);
        sb.append("{\"status\":\"OK\",\"trace_id\":\"a1b2c3d4e5f67890a1b2c3d4e5f67890\",\"truncated\":false,\"count\":3000,\"events\":[");
        for (int i = 0; i < 3000; i++) {
            if (i > 0) sb.append(",");
            sb.append("{\"source_id\":474,\"timestamp_ns\":\"").append(1791454021200000000L + i)
              .append("\",\"event_type\":16,\"payload_b64\":\"AQIDBA==\"}");
        }
        sb.append("],\"data_gaps\":[]}\n");

        byte[] outBytes = sb.toString().getBytes("UTF-8");
        System.out.write(outBytes);
        System.out.flush();

        stderrThread.join();
    }
}
"""
        with tempfile.TemporaryDirectory(prefix="test_deadlock_") as tmpdir:
            src_pkg_dir = Path(tmpdir) / "test"
            src_pkg_dir.mkdir(parents=True, exist_ok=True)
            java_file = src_pkg_dir / "PipeDeadlockEmitter.java"
            java_file.write_text(java_src, encoding="utf-8")

            # Compile
            javac_bin = str(Path(java_bin).parent / "javac")
            if not Path(javac_bin).exists():
                javac_bin = "javac"

            compile_res = subprocess.run(
                [javac_bin, "-d", tmpdir, str(java_file)],
                capture_output=True,
                text=True,
            )
            self.assertEqual(compile_res.returncode, 0, f"Compilation failed: {compile_res.stderr}")

            cmd = [java_bin, "-cp", tmpdir, "test.PipeDeadlockEmitter"]

            start_t = time.monotonic()
            rc, stdout, stderr = run_subprocess_safe(cmd, timeout=5.0)
            duration = time.monotonic() - start_t

            # Verify performance criterion: completes cleanly under 5.0 seconds
            self.assertLess(duration, 5.0, f"Execution took {duration:.2f}s (must be < 5.0s)")
            self.assertEqual(rc, 0, f"Process exited with non-zero code {rc}")

            # Verify byte volume: >256KB on stdout, >128KB on stderr
            self.assertGreater(
                len(stdout),
                256 * 1024,
                f"Stdout size {len(stdout)} bytes is not > 256KB",
            )
            self.assertGreater(
                len(stderr),
                128 * 1024,
                f"Stderr size {len(stderr)} bytes is not > 128KB",
            )

            # Verify zero JSON corruption: Pydantic v2 TypeAdapter parses cleanly
            envelope = RETRIEVAL_ENVELOPE_ADAPTER.validate_json(stdout.strip())
            self.assertEqual(envelope.status, "OK")
            self.assertEqual(envelope.count, 3000)
            self.assertEqual(len(envelope.events), 3000)
            self.assertEqual(envelope.events[0].source_id, 474)
            self.assertEqual(envelope.events[0].timestamp_ns, 1791454021200000000)
            self.assertEqual(envelope.events[2999].timestamp_ns, 1791454021200000000 + 2999)
