"""Test Suite 3: Subprocess Timeout and Orphan/Zombie JVM Cleanup.

Spec requirement:
  Invoke simulated hung JVM with timeout=1.0, inspect process tree for surviving or defunct PIDs;
  TimeoutExpired handled cleanly, 0 live or <defunct> child JVM processes remain.

Background:
  When proc.communicate(timeout=...) raises TimeoutExpired, Python does not kill the child process.
  Without process-group termination and a second communicate() call, orphaned JVMs and defunct
  zombie processes accumulate, exhausting OS process tables and file descriptors.
"""

from __future__ import annotations

import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path

from bridge.java_retriever import resolve_java_bin, run_subprocess_safe


class TestTimeoutOrphanCleanup(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.java_bin = resolve_java_bin()
        cls.javac_bin = str(Path(cls.java_bin).parent / "javac")
        if not Path(cls.javac_bin).exists():
            cls.javac_bin = "javac"

    def _is_pid_alive_or_defunct(self, pid: int) -> tuple[bool, bool]:
        """Check whether a PID is alive or defunct/zombie in /proc."""
        # 1. os.kill check
        try:
            os.kill(pid, 0)
            is_alive = True
        except ProcessLookupError:
            return False, False
        except PermissionError:
            is_alive = True

        # 2. Inspect /proc/<pid>/status
        proc_status = Path(f"/proc/{pid}/status")
        is_defunct = False
        if proc_status.exists():
            try:
                for line in proc_status.read_text().splitlines():
                    if line.startswith("State:"):
                        # 'Z (zombie)'
                        if "Z" in line or "zombie" in line.lower():
                            is_defunct = True
                        break
            except (OSError, IOError):
                return False, False

        return is_alive, is_defunct

    def test_01_timeout_expired_cleans_up_hung_jvm(self):
        """Invoke a simulated hung JVM sleeping forever; verify 0 live or defunct processes remain."""
        java_src = r"""
package test;

public class HungSleepJvm {
    public static void main(String[] args) throws Exception {
        System.out.println("READY");
        System.out.flush();
        // Sleep indefinitely to simulate an unresponsive JVM or hung BlockStore lock
        Thread.sleep(60_000);
    }
}
"""
        with tempfile.TemporaryDirectory(prefix="test_hung_") as tmpdir:
            src_pkg_dir = Path(tmpdir) / "test"
            src_pkg_dir.mkdir(parents=True, exist_ok=True)
            java_file = src_pkg_dir / "HungSleepJvm.java"
            java_file.write_text(java_src, encoding="utf-8")

            res = subprocess.run([self.javac_bin, "-d", tmpdir, str(java_file)], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"Compilation failed: {res.stderr}")

            cmd = [self.java_bin, "-cp", tmpdir, "test.HungSleepJvm"]

            start_t = time.monotonic()
            timed_out = False
            child_pid = None

            try:
                run_subprocess_safe(cmd, timeout=1.0)
            except TimeoutError as exc:
                timed_out = True
                # Parse PID from exception message: "Process <pid> timed out..."
                parts = str(exc).split()
                if len(parts) >= 2 and parts[1].isdigit():
                    child_pid = int(parts[1])

            duration = time.monotonic() - start_t

            self.assertTrue(timed_out, "Expected TimeoutError was not raised")
            self.assertIsNotNone(child_pid, "Failed to capture child PID from TimeoutError")
            # Should time out around 1.0s to 1.6s (1.0s timeout + up to 0.5s grace wait)
            self.assertLess(duration, 3.0, f"Teardown took too long: {duration:.2f}s")

            # Check process state in OS
            time.sleep(0.1)  # small pause for OS table update
            is_alive, is_defunct = self._is_pid_alive_or_defunct(child_pid)
            self.assertFalse(is_alive, f"Child PID {child_pid} is still alive in process table")
            self.assertFalse(is_defunct, f"Child PID {child_pid} remained as a <defunct> zombie process")

    def test_02_stubborn_jvm_sigkill_escalation(self):
        """Simulate a JVM that traps SIGTERM with a blocking shutdown hook; verify SIGKILL reaps it."""
        java_src = r"""
package test;

public class StubbornShutdownHookJvm {
    public static void main(String[] args) throws Exception {
        Runtime.getRuntime().addShutdownHook(new Thread(() -> {
            try {
                // Block SIGTERM graceful exit
                while (true) {
                    Thread.sleep(1000);
                }
            } catch (Exception ignored) {}
        }));

        while (true) {
            Thread.sleep(1000);
        }
    }
}
"""
        with tempfile.TemporaryDirectory(prefix="test_stubborn_") as tmpdir:
            src_pkg_dir = Path(tmpdir) / "test"
            src_pkg_dir.mkdir(parents=True, exist_ok=True)
            java_file = src_pkg_dir / "StubbornShutdownHookJvm.java"
            java_file.write_text(java_src, encoding="utf-8")

            res = subprocess.run([self.javac_bin, "-d", tmpdir, str(java_file)], capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"Compilation failed: {res.stderr}")

            cmd = [self.java_bin, "-cp", tmpdir, "test.StubbornShutdownHookJvm"]

            timed_out = False
            child_pid = None

            try:
                run_subprocess_safe(cmd, timeout=1.0)
            except TimeoutError as exc:
                timed_out = True
                parts = str(exc).split()
                if len(parts) >= 2 and parts[1].isdigit():
                    child_pid = int(parts[1])

            self.assertTrue(timed_out)
            self.assertIsNotNone(child_pid)

            time.sleep(0.1)
            is_alive, is_defunct = self._is_pid_alive_or_defunct(child_pid)
            self.assertFalse(is_alive, f"Stubborn child PID {child_pid} still alive after SIGKILL escalation")
            self.assertFalse(is_defunct, f"Stubborn child PID {child_pid} remained <defunct> in process table")
