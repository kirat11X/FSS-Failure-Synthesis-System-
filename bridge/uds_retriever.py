"""Persistent Unix Domain Socket Retriever (REQ-072).

Communicates with persistent JEP-380 Retrieval.UdsBridgeServer over AF_UNIX sockets
using a 4-byte Big-Endian length-prefixed frame protocol.

Features:
  - Sub-100ms multi-query latency via persistent UDS connection.
  - Lossless uint64 timestamp preservation.
  - Full daemon lifecycle management (start_daemon, ping, shutdown).
  - Graceful fallback to JavaRetriever subprocess when socket is unavailable.
"""

from __future__ import annotations

import base64
import json
import os
import signal
import socket
import struct
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from fss_py.model import (
    ALL,
    EventRecord,
    RetrievalRequest,
    RetrievalResult,
    _AllSources,
)
from bridge.java_retriever import JavaRetriever, resolve_java_bin

DEFAULT_SOCKET_PATH = "/tmp/fss_bridge.sock"
MAX_FRAME_SIZE = 16 * 1024 * 1024  # 16 MB limit


class UdsJavaRetriever:
    """Retriever client communicating with JEP-380 UdsBridgeServer over AF_UNIX sockets."""

    def __init__(
        self,
        store_path: str | Path | None = None,
        socket_path: str | Path = DEFAULT_SOCKET_PATH,
        *,
        classpath: str | None = None,
        java_bin: str = "java",
        timeout: float = 10.0,
        fallback_to_cli: bool = True,
    ) -> None:
        root = Path(__file__).resolve().parent.parent
        self.root = root
        self.store_path = Path(store_path).resolve() if store_path else None
        self.socket_path = Path(socket_path).resolve()
        self.java_bin = resolve_java_bin(java_bin)
        self.timeout = timeout
        self.fallback_to_cli = fallback_to_cli
        self._daemon_proc: subprocess.Popen | None = None
        self._sock: socket.socket | None = None

        if classpath is None:
            core_classes = root / "fss-core" / "target" / "classes"
            bridge_classes = root / "bridge" / "classes"
            lib_dir = root / "fss-core" / "lib"
            jars = [str(p) for p in sorted(lib_dir.glob("*.jar"))] if lib_dir.exists() else []
            self.classpath = ":".join([str(core_classes), str(bridge_classes)] + jars)
        else:
            self.classpath = classpath

    def __enter__(self) -> UdsJavaRetriever:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        self.close()

    def close(self) -> None:
        """Close active socket connection and cleanup resources."""
        self._close_socket()
        if self._daemon_proc is not None:
            try:
                self._daemon_proc.terminate()
                self._daemon_proc.wait(timeout=2.0)
            except Exception:
                try:
                    self._daemon_proc.kill()
                except Exception:
                    pass
            self._daemon_proc = None

    def _close_socket(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except Exception:
                pass
            self._sock = None

    def _get_socket(self) -> socket.socket:
        if self._sock is not None:
            return self._sock
        if not self.socket_path.exists():
            raise FileNotFoundError(f"UDS socket does not exist at {self.socket_path}")

        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self.timeout)
        sock.connect(str(self.socket_path))
        self._sock = sock
        return sock

    @staticmethod
    def _read_exact(sock: socket.socket, num_bytes: int) -> bytes:
        chunks = []
        bytes_read = 0
        while bytes_read < num_bytes:
            chunk = sock.recv(num_bytes - bytes_read)
            if not chunk:
                raise ConnectionError("Connection closed while reading frame")
            chunks.append(chunk)
            bytes_read += len(chunk)
        return b"".join(chunks)

    def _send_frame_over_socket(self, sock: socket.socket, req_dict: dict[str, Any]) -> dict[str, Any]:
        req_bytes = json.dumps(req_dict).encode("utf-8")
        if len(req_bytes) > MAX_FRAME_SIZE:
            raise ValueError(f"Request frame exceeds MAX_FRAME_SIZE: {len(req_bytes)}")

        length_prefix = struct.pack(">I", len(req_bytes))
        sock.sendall(length_prefix + req_bytes)

        resp_len_bytes = self._read_exact(sock, 4)
        resp_len = struct.unpack(">I", resp_len_bytes)[0]
        if resp_len <= 0 or resp_len > MAX_FRAME_SIZE:
            raise ValueError(f"Invalid response frame length from UdsBridgeServer: {resp_len}")

        resp_bytes = self._read_exact(sock, resp_len)
        return json.loads(resp_bytes.decode("utf-8"))

    def _send_request(self, req_dict: dict[str, Any]) -> dict[str, Any]:
        try:
            sock = self._get_socket()
            return self._send_frame_over_socket(sock, req_dict)
        except Exception:
            self._close_socket()
            raise

    def ping(self, timeout: float = 2.0) -> bool:
        """Check if UdsBridgeServer is responsive over AF_UNIX socket."""
        if not self.socket_path.exists():
            return False
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(timeout)
            try:
                sock.connect(str(self.socket_path))
                req = {"action": "ping", "trace_id": uuid.uuid4().hex}
                resp = self._send_frame_over_socket(sock, req)
                return resp.get("status") == "OK" and (resp.get("pong") is True or "pong" in resp)
            finally:
                sock.close()
        except Exception:
            return False

    @classmethod
    def start_daemon(
        cls,
        socket_path: str | Path = DEFAULT_SOCKET_PATH,
        *,
        java_cp: str | None = None,
        java_bin: str = "java",
        timeout: float = 5.0,
    ) -> subprocess.Popen:
        """Start Retrieval.UdsBridgeServer as a background daemon process."""
        sock_p = Path(socket_path).resolve()
        root = Path(__file__).resolve().parent.parent

        if java_cp is None:
            core_classes = root / "fss-core" / "target" / "classes"
            bridge_classes = root / "bridge" / "classes"
            lib_dir = root / "fss-core" / "lib"
            jars = [str(p) for p in sorted(lib_dir.glob("*.jar"))] if lib_dir.exists() else []
            java_cp = ":".join([str(core_classes), str(bridge_classes)] + jars)

        resolved_java = resolve_java_bin(java_bin)
        cmd = [
            resolved_java,
            "-Dfile.encoding=UTF-8",
            "-cp",
            java_cp,
            "Retrieval.UdsBridgeServer",
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

        deadline = time.time() + timeout
        probe_client = cls(socket_path=sock_p, fallback_to_cli=False)

        while time.time() < deadline:
            if sock_p.exists() and probe_client.ping():
                return proc
            time.sleep(0.05)

        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except Exception:
            pass
        raise TimeoutError(f"UdsBridgeServer failed to bind {sock_p} within {timeout}s")

    def retrieve(
        self,
        store_path: str | Path | None = None,
        source: int | _AllSources | str = ALL,
        t_start_ns: int = 0,
        t_end_ns: int = 18446744073709551615,
        event_type: int | None = None,
        max_events: int = 10000,
        trace_id: str | None = None,
    ) -> RetrievalResult:
        """Query events matching bounded criteria via UDS daemon with fallback to CLI."""
        target_store = Path(store_path).resolve() if store_path else self.store_path
        if target_store is None:
            raise ValueError("store_path must be specified")

        tid = trace_id or uuid.uuid4().hex

        # Normalize source
        if source is ALL or (isinstance(source, str) and source.strip().upper() == "ALL"):
            source_arg: str | int = "ALL"
            source_req: int | _AllSources = ALL
        elif isinstance(source, (int, str)):
            source_arg = int(source)
            source_req = int(source)
        else:
            source_arg = "ALL"
            source_req = ALL

        req_payload = {
            "action": "retrieve",
            "store_path": str(target_store),
            "source_id": source_arg,
            "t_start_ns": str(t_start_ns),
            "t_end_ns": str(t_end_ns),
            "event_type": event_type,
            "max_events": max_events,
            "trace_id": tid,
        }

        # Try UDS socket communication
        use_uds = self.socket_path.exists()
        if use_uds:
            try:
                resp = self._send_request(req_payload)
                if resp.get("status") == "OK":
                    events = []
                    for ev in resp.get("events", []):
                        src_id = int(ev["source_id"])
                        ts_ns = int(ev["timestamp_ns"])
                        ev_t = int(ev["event_type"])
                        if "payload_base64" in ev and ev["payload_base64"]:
                            payload = base64.b64decode(ev["payload_base64"])
                        elif "payload_b64" in ev and ev["payload_b64"]:
                            payload = base64.b64decode(ev["payload_b64"])
                        elif "payload_hex" in ev and ev["payload_hex"]:
                            payload = bytes.fromhex(ev["payload_hex"])
                        else:
                            payload = b""
                        events.append(EventRecord(src_id, ts_ns, ev_t, payload))

                    req_obj = RetrievalRequest(
                        source=source_req,
                        t_start_ns=t_start_ns,
                        t_end_ns=t_end_ns,
                        max_events=max_events,
                        event_type=event_type,
                    )
                    return RetrievalResult(request=req_obj, events=events, truncated=resp.get("truncated", False))
                elif not self.fallback_to_cli:
                    raise RuntimeError(f"UDS retrieve error: {resp.get('error')}")
            except Exception:
                if not self.fallback_to_cli:
                    raise

        # Fallback to subprocess JavaRetriever
        cli_retriever = JavaRetriever(
            store_path=target_store,
            classpath=self.classpath,
            java_bin=self.java_bin,
            mode="cli",
            timeout=self.timeout,
        )
        return cli_retriever.retrieve(
            source=source_req,
            t_start_ns=t_start_ns,
            t_end_ns=t_end_ns,
            max_events=max_events,
            event_type=event_type,
        )

    def retrieve_full(self, store_path: str | Path | None = None, trace_id: str | None = None) -> list[EventRecord]:
        """Retrieve all events present in the store file."""
        target_store = Path(store_path).resolve() if store_path else self.store_path
        if target_store is None:
            raise ValueError("store_path must be specified")

        tid = trace_id or uuid.uuid4().hex
        req_payload = {
            "action": "retrieve_full",
            "store_path": str(target_store),
            "trace_id": tid,
        }

        use_uds = self.socket_path.exists()
        if use_uds:
            try:
                resp = self._send_request(req_payload)
                if resp.get("status") == "OK":
                    events = []
                    for ev in resp.get("events", []):
                        src_id = int(ev["source_id"])
                        ts_ns = int(ev["timestamp_ns"])
                        ev_t = int(ev["event_type"])
                        if "payload_base64" in ev and ev["payload_base64"]:
                            payload = base64.b64decode(ev["payload_base64"])
                        elif "payload_b64" in ev and ev["payload_b64"]:
                            payload = base64.b64decode(ev["payload_b64"])
                        elif "payload_hex" in ev and ev["payload_hex"]:
                            payload = bytes.fromhex(ev["payload_hex"])
                        else:
                            payload = b""
                        events.append(EventRecord(src_id, ts_ns, ev_t, payload))
                    return events
                elif not self.fallback_to_cli:
                    raise RuntimeError(f"UDS retrieve_full error: {resp.get('error')}")
            except Exception:
                if not self.fallback_to_cli:
                    raise

        # Fallback to CLI
        cli_retriever = JavaRetriever(
            store_path=target_store,
            classpath=self.classpath,
            java_bin=self.java_bin,
            mode="cli",
            timeout=self.timeout,
        )
        env = cli_retriever.retrieve_full_dataset(source=ALL, max_events=100000, trace_id=tid)
        return [ev.to_event_record() for ev in env.events]

    def shutdown(self, timeout: float = 3.0) -> bool:
        """Send shutdown command to UdsBridgeServer daemon."""
        if not self.socket_path.exists():
            return True
        try:
            req = {"action": "shutdown", "trace_id": uuid.uuid4().hex}
            resp = self._send_request(req)
            self._close_socket()
            return resp.get("status") == "OK" and (resp.get("shutting_down") is True or "shutting_down" in resp)
        except Exception:
            self._close_socket()
            return False
