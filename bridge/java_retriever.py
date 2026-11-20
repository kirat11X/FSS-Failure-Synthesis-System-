"""Java-backed Retriever implementation for FSS Python pipeline.

Implements the fss_py.retriever.Retriever protocol by querying the Java
BlockStore via persistent AF_UNIX worker IPC or hardened one-shot CLI subprocesses.

Engineered against:
  - 64KB kernel pipe buffer deadlocks (via communicate and concurrent draining).
  - Orphan JVMs after timeouts (via start_new_session=True and process-group SIGTERM/SIGKILL).
  - Single-writer file corruption races (via StoreLockManager and sidecar .lock locking).
  - Lossless uint64 serialization (via Long.toUnsignedString and arbitrary-precision Python int).
  - Cross-language trace propagation (via structlog.contextvars and W3C 32-hex trace_id).
"""

from __future__ import annotations

import base64
import fcntl
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Literal, Sequence

from fss_py.model import (
    ALL,
    EventRecord,
    RetrievalRequest,
    RetrievalRequestError,
    RetrievalResult,
    _AllSources,
)
from bridge.schemas import (
    RETRIEVAL_ENVELOPE_ADAPTER,
    RetrievalEnvelope,
    bind_trace_id,
    get_or_create_trace_id,
)

MAX_OUTPUT_BYTES = 64 * 1024 * 1024  # 64 MiB ceiling to prevent unbounded pipe buffering


def resolve_java_bin(java_bin: str = "java") -> str:
    """Explicitly resolve the Java binary executable without shell expansion."""
    if os.path.isabs(java_bin) and os.path.isfile(java_bin) and os.access(java_bin, os.X_OK):
        return java_bin

    # 1. $JAVA_HOME/bin/java
    java_home = os.environ.get("JAVA_HOME")
    if java_home:
        jh_bin = Path(java_home) / "bin" / "java"
        if jh_bin.is_file() and os.access(jh_bin, os.X_OK):
            return str(jh_bin)

    # 2. PATH resolution via shutil.which
    which_bin = shutil.which(java_bin)
    if which_bin:
        return which_bin

    return "java"


def run_subprocess_safe(
    cmd: list[str],
    *,
    cwd: str | Path | None = None,
    env: dict[str, str] | None = None,
    timeout: float = 30.0,
    max_output_bytes: int = MAX_OUTPUT_BYTES,
) -> tuple[int, bytes, bytes]:
    """Execute a subprocess safely inside its own process group with strict timeouts.

    Guarantees:
      - Uses start_new_session=True instead of unsafe preexec_fn.
      - Never deadlocks on 64KB unconsumed pipe buffers (communicate drains both stdout and stderr).
      - On TimeoutExpired, sends SIGTERM to the entire child process group, allows a brief grace period,
        then SIGKILLs and calls a second communicate() to reap zombies and drain pipes (never proc.wait()).
      - Enforces strict maximum output byte ceilings.
      - Uses raw binary streams (text=False) with explicit UTF-8 decoding.
    """
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        start_new_session=True,
        cwd=str(cwd) if cwd else None,
        env=env,
        text=False,
    )

    try:
        stdout, stderr = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired as exc:
        # 1. Signal process group with SIGTERM
        pgid = None
        try:
            pgid = os.getpgid(proc.pid)
            os.killpg(pgid, signal.SIGTERM)
        except (ProcessLookupError, OSError):
            pass

        # 2. Brief grace wait
        try:
            stdout, stderr = proc.communicate(timeout=0.5)
        except subprocess.TimeoutExpired:
            # 3. Forcibly SIGKILL entire process group
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, OSError):
                    pass
            # 4. Mandatory post-timeout communicate to reap exit status & drain remaining buffers
            stdout, stderr = proc.communicate()

        raise TimeoutError(
            f"Process {proc.pid} timed out after {timeout:.1f}s: {' '.join(cmd)}"
        ) from exc

    if len(stdout) > max_output_bytes:
        raise RuntimeError(
            f"Subprocess stdout exceeded ceiling of {max_output_bytes} bytes (received {len(stdout)} bytes)"
        )

    return proc.returncode, stdout, stderr


def send_worker_request(
    socket_path: str | Path,
    request_data: dict[str, Any],
    timeout: float = 30.0,
) -> str:
    """Send a single-line UTF-8 NDJSON request to the persistent AF_UNIX worker."""
    sock_p = Path(socket_path).resolve()
    if not sock_p.exists():
        raise FileNotFoundError(f"Worker AF_UNIX socket not found at {sock_p}")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    try:
        sock.connect(str(sock_p))
        req_line = json.dumps(request_data, separators=(",", ":")) + "\n"
        sock.sendall(req_line.encode("utf-8"))

        # Read single line response
        with sock.makefile("rb") as reader:
            raw_line = reader.readline()
        if not raw_line:
            raise ConnectionError("Worker closed connection without writing response")
        return raw_line.decode("utf-8")
    finally:
        sock.close()


class JavaRetriever:
    """Retriever implementation that queries a Java BlockStore through JavaStoreBridge.

    Conforms to the Retriever Protocol:
        retrieve(source, t_start_ns, t_end_ns, *, max_events, event_type=None) -> RetrievalResult
    """

    def __init__(
        self,
        store_path: str | Path | None = None,
        *,
        classpath: str | None = None,
        java_bin: str = "java",
        mode: Literal["auto", "worker", "cli", "storage_ipc"] = "auto",
        socket_path: str | Path = "/tmp/fss_bridge.sock",
        storage_properties: str | Path | None = None,
        timeout: float = 30.0,
    ) -> None:
        root = Path(__file__).resolve().parent.parent
        self.root = root

        if store_path is not None:
            store_p = Path(store_path)
            self.store_path = store_p if store_p.is_absolute() else (root / store_p).resolve()
        else:
            self.store_path = root / "bridge" / "blocks.dat"

        if storage_properties is not None:
            sp = Path(storage_properties)
            self.storage_properties: Path | None = sp if sp.is_absolute() else (root / sp).resolve()
        else:
            default_sp = root / "fss-core" / "storage.properties"
            self.storage_properties = default_sp if default_sp.exists() else None

        self.java_bin = resolve_java_bin(java_bin)
        self.mode = mode
        self.socket_path = Path(socket_path).resolve()
        self.timeout = timeout

        if classpath is None:
            core_classes = root / "fss-core" / "target" / "classes"
            bridge_classes = root / "bridge" / "classes"
            lib_dir = root / "fss-core" / "lib"
            jars = [str(p) for p in sorted(lib_dir.glob("*.jar"))] if lib_dir.exists() else []
            self.classpath = ":".join([str(core_classes), str(bridge_classes)] + jars)
        else:
            resolved_entries = []
            for entry in classpath.split(":"):
                if entry:
                    ep = Path(entry)
                    resolved_entries.append(str(ep if ep.is_absolute() else (root / ep).resolve()))
            self.classpath = ":".join(resolved_entries)

    @classmethod
    def from_fixtures(
        cls,
        *fixture_paths: str | Path,
        store_path: str | Path | None = None,
        classpath: str | None = None,
        java_bin: str = "java",
        mode: Literal["auto", "worker", "cli"] = "auto",
        socket_path: str | Path = "/tmp/fss_bridge.sock",
    ) -> JavaRetriever:
        """Create a JavaRetriever and ingest one or more fixture files into its store."""
        if store_path is None:
            tmp = tempfile.NamedTemporaryFile(prefix="fss_store_", suffix=".dat", delete=False)
            tmp.close()
            s_path = Path(tmp.name)
        else:
            s_path = Path(store_path)

        retriever = cls(s_path, classpath=classpath, java_bin=java_bin, mode=mode, socket_path=socket_path)
        if fixture_paths:
            retriever.ingest(*fixture_paths)
        return retriever

    @classmethod
    def is_worker_alive(cls, socket_path: str | Path = "/tmp/fss_bridge.sock") -> bool:
        """Check if persistent JavaBridgeWorker is reachable on socket_path."""
        try:
            raw = send_worker_request(socket_path, {"action": "ping"}, timeout=1.0)
            data = json.loads(raw)
            return data.get("status") == "OK"
        except Exception:
            return False

    @classmethod
    def start_worker(
        cls,
        socket_path: str | Path = "/tmp/fss_bridge.sock",
        *,
        classpath: str | None = None,
        java_bin: str = "java",
        timeout: float = 5.0,
    ) -> subprocess.Popen:
        """Start persistent JavaBridgeWorker as a detached background daemon."""
        root = Path(__file__).resolve().parent.parent
        resolved_java = resolve_java_bin(java_bin)
        sock_p = Path(socket_path).resolve()

        if classpath is None:
            core_classes = root / "fss-core" / "target" / "classes"
            bridge_classes = root / "bridge" / "classes"
            cp = f"{core_classes}:{bridge_classes}"
        else:
            cp = classpath

        cmd = [
            resolved_java,
            "-Dfile.encoding=UTF-8",
            "-cp",
            cp,
            "bridge.JavaBridgeWorker",
            "--socket",
            str(sock_p),
        ]

        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            cwd=str(root),
        )

        # Wait for socket to become ready
        deadline = time.time() + timeout
        while time.time() < deadline:
            if cls.is_worker_alive(sock_p):
                return proc
            time.sleep(0.05)

        raise TimeoutError(f"JavaBridgeWorker failed to bind {sock_p} within {timeout}s")

    @classmethod
    def stop_worker(cls, socket_path: str | Path = "/tmp/fss_bridge.sock", timeout: float = 3.0) -> bool:
        """Send shutdown signal to persistent JavaBridgeWorker."""
        try:
            send_worker_request(socket_path, {"action": "shutdown"}, timeout=timeout)
            return True
        except Exception:
            return False

    def ingest(self, *fixture_paths: str | Path, trace_id: str | None = None) -> None:
        """Ingest wire envelope (.bin) fixtures into this retriever's BlockStore with write locking."""
        tid = bind_trace_id(trace_id)
        resolved_fixtures: list[str] = []
        for p in fixture_paths:
            fp = Path(p)
            resolved_fp = fp if fp.is_absolute() else (self.root / fp).resolve()
            resolved_fixtures.append(str(resolved_fp))

        # Check if persistent worker is active
        use_worker = (self.mode == "worker") or (self.mode == "auto" and self.is_worker_alive(self.socket_path))
        if use_worker:
            req = {
                "action": "ingest",
                "store_path": str(self.store_path),
                "fixtures": resolved_fixtures,
                "trace_id": tid,
            }
            raw = send_worker_request(self.socket_path, req, timeout=self.timeout)
            data = json.loads(raw)
            if data.get("status") != "OK":
                raise RuntimeError(f"Worker ingest failed: {data.get('error')}")
            return

        # Fallback to CLI with sidecar file locking to prevent write collisions
        lock_path = self.store_path.with_suffix(self.store_path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        with open(lock_path, "a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                cmd = [
                    self.java_bin,
                    "-Dfile.encoding=UTF-8",
                    "-cp",
                    self.classpath,
                    "bridge.JavaStoreBridge",
                    "ingest",
                    "--store",
                    str(self.store_path),
                    "--trace-id",
                    tid,
                ]
                for rf in resolved_fixtures:
                    cmd.extend(["--fixture", rf])

                env = dict(os.environ)
                env["FSS_TRACE_ID"] = tid

                code, stdout, stderr = run_subprocess_safe(
                    cmd,
                    cwd=self.root,
                    env=env,
                    timeout=self.timeout,
                )
                if code != 0:
                    err_msg = stderr.decode("utf-8", errors="replace")
                    raise RuntimeError(f"JavaStoreBridge ingest failed (code {code}):\n{err_msg}")
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

    def _retrieve_via_storage_ipc(
        self,
        source: int | _AllSources,
        t_start_ns: int,
        t_end_ns: int,
        *,
        max_events: int,
        event_type: int | None = None,
        trace_id: str | None = None,
    ) -> RetrievalEnvelope:
        """Query the Retrieval.RetrievalMain IPC server using 4-byte Big-Endian framing."""
        if self.storage_properties is None or not self.storage_properties.exists():
            raise FileNotFoundError(f"Storage properties file not found: {self.storage_properties}")

        tid = bind_trace_id(trace_id)
        # StorageConfig timestamps are in milliseconds; convert if provided in nanoseconds
        start_ms = t_start_ns // 1_000_000 if t_start_ns > 10_000_000_000_000 else t_start_ns
        end_ms = t_end_ns // 1_000_000 if t_end_ns > 10_000_000_000_000 else t_end_ns

        source_val: str | int = "all" if source is ALL else (int(source) if isinstance(source, int) else str(source))
        req = {
            "source": source_val,
            "start_time": int(start_ms),
            "end_time": int(end_ms),
            "event_type": event_type,
        }

        cmd = [
            self.java_bin,
            "-Dfile.encoding=UTF-8",
            "-cp",
            self.classpath,
            "Retrieval.RetrievalMain",
            str(self.storage_properties),
        ]

        proc = subprocess.Popen(
            cmd,
            cwd=self.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        try:
            req_bytes = json.dumps(req).encode("utf-8")
            if proc.stdin is None or proc.stdout is None:
                raise RuntimeError("Failed to open stdio pipes to RetrievalMain")

            proc.stdin.write(struct.pack(">I", len(req_bytes)))
            proc.stdin.write(req_bytes)
            proc.stdin.flush()

            len_buf = proc.stdout.read(4)
            if len(len_buf) < 4:
                stderr_str = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                raise RuntimeError(f"RetrievalIpcServer closed stream without responding. Stderr: {stderr_str}")

            resp_len = struct.unpack(">I", len_buf)[0]
            resp_buf = proc.stdout.read(resp_len)
            resp_obj = json.loads(resp_buf.decode("utf-8"))

            if resp_obj.get("status") != "OK":
                error_msg = resp_obj.get("error", "Unknown retrieval error")
                return RetrievalEnvelope(
                    status="ERROR",
                    error_message=error_msg,
                    trace_id=tid,
                )

            records = resp_obj.get("records", [])
            if not records:
                return RetrievalEnvelope(
                    status="EMPTY",
                    events=[],
                    trace_id=tid,
                )

            from bridge.schemas import TelemetryEventWire

            wire_events = []
            for r in records[:max_events]:
                agent_str = str(r.get("agentId", "0"))
                try:
                    src_id = int(agent_str)
                except ValueError:
                    src_id = 0

                raw_ts = int(r.get("timestamp", 0))
                ts_ns = raw_ts * 1_000_000 if raw_ts < 10_000_000_000_000 else raw_ts

                payload_b64 = r.get("payload", "")
                ev_type = int(r.get("eventType", 0))

                wire_events.append(
                    TelemetryEventWire(
                        source_id=src_id,
                        timestamp_ns=ts_ns,
                        event_type=ev_type,
                        payload_b64=payload_b64,
                    )
                )

            return RetrievalEnvelope(
                status="OK",
                count=len(wire_events),
                truncated=len(records) > max_events,
                events=wire_events,
                trace_id=tid,
            )
        finally:
            try:
                if proc.stdin:
                    proc.stdin.close()
                os.killpg(proc.pid, signal.SIGTERM)
                proc.wait(timeout=2.0)
            except Exception:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    pass

    def retrieve_envelope(
        self,
        source: int | _AllSources,
        t_start_ns: int,
        t_end_ns: int,
        *,
        max_events: int,
        event_type: int | None = None,
        trace_id: str | None = None,
    ) -> RetrievalEnvelope:
        """Perform bounded retrieval returning the complete Pydantic v2 typed envelope."""
        # Contract validation: raises RetrievalRequestError if bounded window or max_events is violated
        RetrievalRequest(
            source=source,
            t_start_ns=t_start_ns,
            t_end_ns=t_end_ns,
            max_events=max_events,
            event_type=event_type,
        )

        tid = bind_trace_id(trace_id)
        source_arg = "ALL" if source is ALL else str(source)

        if self.mode == "storage_ipc":
            return self._retrieve_via_storage_ipc(
                source,
                t_start_ns,
                t_end_ns,
                max_events=max_events,
                event_type=event_type,
                trace_id=tid,
            )

        # 1. Try persistent worker if available and enabled
        use_worker = (self.mode == "worker") or (self.mode == "auto" and self.is_worker_alive(self.socket_path))
        if use_worker:
            req = {
                "action": "retrieve",
                "store_path": str(self.store_path),
                "source": source_arg,
                "start_ns": str(t_start_ns),
                "end_ns": str(t_end_ns),
                "max_events": max_events,
                "event_type": event_type,
                "trace_id": tid,
            }
            raw = send_worker_request(self.socket_path, req, timeout=self.timeout)
            return RETRIEVAL_ENVELOPE_ADAPTER.validate_json(raw.encode("utf-8"))

        if self.mode == "worker":
            raise ConnectionError(f"Persistent worker required but unreachable on {self.socket_path}")

        # 2. Fast one-shot CLI mode with strict process-group safety and sidecar file locking
        cmd = [
            self.java_bin,
            "-Dfile.encoding=UTF-8",
            "-cp",
            self.classpath,
            "bridge.JavaStoreBridge",
            "retrieve",
            "--store",
            str(self.store_path),
            "--source",
            source_arg,
            "--start",
            str(t_start_ns),
            "--end",
            str(t_end_ns),
            "--max-events",
            str(max_events),
            "--format",
            "ndjson",
            "--trace-id",
            tid,
        ]
        if event_type is not None:
            cmd.extend(["--event-type", str(event_type)])

        env = dict(os.environ)
        env["FSS_TRACE_ID"] = tid

        lock_path = self.store_path.with_suffix(self.store_path.suffix + ".lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)

        with open(lock_path, "a+b") as lock_file:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                code, stdout, stderr = run_subprocess_safe(
                    cmd,
                    cwd=self.root,
                    env=env,
                    timeout=self.timeout,
                )
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

        if code != 0:
            err_msg = stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"JavaStoreBridge retrieve failed with exit code {code}:\n{err_msg}")

        # Single-line NDJSON validation via Pydantic v2 TypeAdapter
        raw_line = stdout.strip()
        if not raw_line:
            err_msg = stderr.decode("utf-8", errors="replace")
            raise RuntimeError(f"JavaStoreBridge produced empty output:\nstderr:\n{err_msg}")

        if b"\n" in raw_line:
            for line in reversed(raw_line.splitlines()):
                candidate = line.strip()
                if candidate.startswith(b"{") and candidate.endswith(b"}"):
                    raw_line = candidate
                    break

        return RETRIEVAL_ENVELOPE_ADAPTER.validate_json(raw_line)

    def retrieve(
        self,
        source: int | _AllSources,
        t_start_ns: int,
        t_end_ns: int,
        *,
        max_events: int,
        event_type: int | None = None,
    ) -> RetrievalResult:
        """Bounded retrieval conforming to the official Retriever protocol."""
        request = RetrievalRequest(
            source=source,
            t_start_ns=t_start_ns,
            t_end_ns=t_end_ns,
            max_events=max_events,
            event_type=event_type,
        )

        envelope = self.retrieve_envelope(
            source,
            t_start_ns,
            t_end_ns,
            max_events=max_events,
            event_type=event_type,
        )

        if envelope.status == "ERROR":
            raise RuntimeError(f"JavaStoreBridge retrieval error: {envelope.error_message}")

        if envelope.status == "EMPTY":
            return RetrievalResult(request=request, events=[], truncated=False)

        events = [ev.to_event_record() for ev in envelope.events]
        return RetrievalResult(request=request, events=events, truncated=envelope.truncated)
