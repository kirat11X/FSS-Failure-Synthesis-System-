"""Persistent framed-JSON client for the Java RetrievalIpcServer."""

from __future__ import annotations

import base64
import json
import os
import queue
import re
import struct
import subprocess
import sys
import threading
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from fss_py.model import EventRecord

MAX_RECORDS = 5_000
MAX_WINDOW_NS = 3_600_000_000_000
MAX_IPC_FRAME_BYTES = 64 * 1024 * 1024
MAX_SIGNED_LONG = (1 << 63) - 1
MAX_U32 = (1 << 32) - 1


class RetrievalIpcError(RuntimeError):
    """Java IPC failed or returned a response outside the protocol contract."""


def _parse_total_matches(value: Any, records_returned: int, response_keys: list[str]) -> int | None:
    if value is None:
        return None
    if type(value) is int:
        total_matches = value
    elif isinstance(value, str) and re.fullmatch(r"\d+", value):
        total_matches = int(value)
    else:
        total_matches = None

    if total_matches is None or total_matches < records_returned:
        raise RetrievalIpcError(
            "Java returned an invalid totalMatches value: "
            f"field={value!r}, "
            f"type={type(value).__name__}, "
            f"records_returned={records_returned}, "
            f"response_keys={response_keys}"
        )
    return total_matches


def _parse_retrieval_metadata(
    response: dict[str, Any], records_returned: int
) -> tuple[int | None, bool | None]:
    raw_total = response.get("totalMatches", response.get("total_matches"))
    total_matches = _parse_total_matches(
        raw_total, records_returned, sorted(response.keys())
    )
    raw_truncated = response.get("truncated")
    if raw_truncated is not None and type(raw_truncated) is not bool:
        raise RetrievalIpcError(
            "Java returned an invalid truncated value: "
            f"field={raw_truncated!r}, "
            f"type={type(raw_truncated).__name__}, "
            f"response_keys={sorted(response.keys())}"
        )

    if total_matches is not None:
        expected_truncated = total_matches > records_returned
        if raw_truncated is not None and raw_truncated != expected_truncated:
            raise RetrievalIpcError(
                "Java returned inconsistent retrieval metadata: "
                f"totalMatches={total_matches}, records_returned={records_returned}, "
                f"truncated={raw_truncated!r}"
            )
        return total_matches, expected_truncated

    return None, raw_truncated


@dataclass(frozen=True, slots=True)
class JavaRetrievalRequest:
    source: str | int
    start_time_ns: int
    end_time_ns: int
    event_type: int | None = None
    max_records: int = MAX_RECORDS

    def __post_init__(self) -> None:
        if (isinstance(self.source, str) and self.source.lower() == "all") or repr(self.source) == "ALL":
            object.__setattr__(self, "source", "all")
        elif type(self.source) is not int or not 0 <= self.source <= MAX_U32:
            raise ValueError("source must be 'all' or an unsigned 32-bit agent ID")
        for field_name in ("start_time_ns", "end_time_ns"):
            value = getattr(self, field_name)
            if type(value) is not int or not 0 <= value <= MAX_SIGNED_LONG:
                raise ValueError(f"{field_name} must be a non-negative signed 64-bit integer")
        if self.end_time_ns < self.start_time_ns:
            raise ValueError("end_time_ns must be greater than or equal to start_time_ns")
        if self.end_time_ns - self.start_time_ns > MAX_WINDOW_NS:
            raise ValueError("retrieval window exceeds one hour")
        if self.event_type is not None and (
            type(self.event_type) is not int or not 0 <= self.event_type <= MAX_U32
        ):
            raise ValueError("event_type must be an unsigned 32-bit integer or None")
        if type(self.max_records) is not int or not 1 <= self.max_records <= MAX_RECORDS:
            raise ValueError(f"max_records must be between 1 and {MAX_RECORDS}")

    def to_json(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "start_time": self.start_time_ns,
            "end_time": self.end_time_ns,
            "event_type": self.event_type,
            "max_records": self.max_records,
        }


@dataclass(frozen=True, slots=True)
class JavaRetrievalResult:
    request: JavaRetrievalRequest
    events: tuple[EventRecord, ...]
    total_matches: int | None
    truncated: bool | None


def java_classpath(root: Path) -> str:
    """Read Maven's resolved runtime classpath and prepend the compiled Java classes."""
    classes = root / "fss-core" / "target" / "classes"
    classpath_file = root / "fss-core" / "target" / "dependency-classpath.txt"
    main_class = classes / "Retrieval" / "RetrievalMain.class"
    if not main_class.is_file():
        raise FileNotFoundError(
            f"Java RetrievalMain is not compiled: {main_class}. Run start.sh or "
            "bash bridge/compile.sh first."
        )
    if not classpath_file.is_file():
        raise FileNotFoundError(
            f"Maven runtime classpath is missing: {classpath_file}. "
            "Run start.sh or bash bridge/compile.sh first."
        )
    dependencies = classpath_file.read_text(encoding="utf-8").strip()
    entries = [str(classes)]
    if dependencies:
        entries.extend(entry for entry in dependencies.split(os.pathsep) if entry)
    return os.pathsep.join(entries)


class RetrievalIpcClient:
    """Own one persistent Java retrieval process and serialize its request stream."""

    def __init__(
        self,
        storage_properties: str | Path,
        *,
        root: str | Path | None = None,
        java_bin: str = "java",
        timeout: float = 90.0,
        startup_timeout: float = 20.0,
    ) -> None:
        self.root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
        self.root = self.root.resolve()
        properties = Path(storage_properties)
        self.storage_properties = (
            properties if properties.is_absolute() else self.root / properties
        ).resolve()
        if not self.storage_properties.is_file():
            raise FileNotFoundError(f"Storage properties file not found: {self.storage_properties}")

        self.timeout = timeout
        self._startup_timeout = startup_timeout
        self._lock = threading.Lock()
        self._responses: queue.Queue[dict[str, Any] | BaseException] = queue.Queue()
        self._stderr_lines: deque[str] = deque(maxlen=100)
        self._stderr_lock = threading.Lock()
        self._ready = threading.Event()
        command = [
            java_bin,
            "-Dfile.encoding=UTF-8",
            "-cp",
            java_classpath(self.root),
            "Retrieval.RetrievalMain",
            str(self.storage_properties),
        ]
        self._process = subprocess.Popen(
            command,
            cwd=self.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=0,
        )
        self._stdout_thread = threading.Thread(
            target=self._read_responses, name="fss-java-ipc-stdout", daemon=True
        )
        self._stderr_thread = threading.Thread(
            target=self._read_stderr, name="fss-java-ipc-stderr", daemon=True
        )
        self._stdout_thread.start()
        self._stderr_thread.start()
        if not self._ready.wait(startup_timeout):
            self.close()
            raise TimeoutError(
                f"RetrievalMain did not become ready in {startup_timeout:g}s. "
                f"Java stderr: {self._stderr_text()}"
            )
        if self._process.poll() is not None:
            raise RetrievalIpcError(
                f"RetrievalMain exited during startup. Java stderr: {self._stderr_text()}"
            )

    def retrieve(self, request: JavaRetrievalRequest) -> JavaRetrievalResult:
        print(
            "[FSS IPC] request "
            f"source={request.source} "
            f"start_time={request.start_time_ns} "
            f"end_time={request.end_time_ns} "
            f"event_type={request.event_type} "
            f"max_records={request.max_records}",
            flush=True,
        )
        request_bytes = json.dumps(
            request.to_json(), separators=(",", ":"), ensure_ascii=True
        ).encode("utf-8")
        if len(request_bytes) > MAX_IPC_FRAME_BYTES:
            raise ValueError("IPC request exceeds the 64 MiB frame limit")

        with self._lock:
            if self._process.poll() is not None:
                raise RetrievalIpcError(
                    f"RetrievalMain is not running. Java stderr: {self._stderr_text()}"
                )
            if self._process.stdin is None:
                raise RetrievalIpcError("RetrievalMain request pipe is unavailable")
            try:
                self._process.stdin.write(struct.pack(">I", len(request_bytes)))
                self._process.stdin.write(request_bytes)
                self._process.stdin.flush()
            except OSError as exc:
                raise RetrievalIpcError(
                    f"Could not write to RetrievalMain: {exc}. "
                    f"Java stderr: {self._stderr_text()}"
                ) from exc

            try:
                response = self._responses.get(timeout=self.timeout)
            except queue.Empty as exc:
                raise TimeoutError(
                    f"RetrievalMain did not answer within {self.timeout:g}s. "
                    f"Java stderr: {self._stderr_text()}"
                ) from exc
            if isinstance(response, BaseException):
                raise RetrievalIpcError(str(response)) from response

        if response.get("status") != "OK":
            raise RetrievalIpcError(
                "Java retrieval failed for "
                f"source={request.source}, "
                f"start_time={request.start_time_ns}, "
                f"end_time={request.end_time_ns}, "
                f"event_type={request.event_type}: "
                f"{response.get('error') or 'unspecified Java error'} "
                f"Java stderr: {self._stderr_text()}"
            )

        raw_records = response.get("records")
        if not isinstance(raw_records, list) or len(raw_records) > request.max_records:
            raise RetrievalIpcError("Java returned an invalid or over-limit records list")
        total_matches, truncated = _parse_retrieval_metadata(response, len(raw_records))
        if total_matches is None:
            print(
                "[FSS IPC] Java response omitted totalMatches; exact match count is unknown.",
                file=sys.stderr,
                flush=True,
            )
        if truncated is None:
            print(
                "[FSS IPC] Java response omitted truncated; truncation status is unknown.",
                file=sys.stderr,
                flush=True,
            )

        events = tuple(self._decode_record(record) for record in raw_records)
        return JavaRetrievalResult(
            request=request,
            events=events,
            total_matches=total_matches,
            truncated=truncated,
        )

    def close(self) -> None:
        process = getattr(self, "_process", None)
        if process is None or process.poll() is not None:
            return
        if process.stdin is not None:
            process.stdin.close()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.terminate()
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

    def _read_responses(self) -> None:
        stream = self._process.stdout
        if stream is None:
            self._responses.put(RetrievalIpcError("RetrievalMain response pipe is unavailable"))
            return
        try:
            while True:
                header = self._read_exact(stream, 4)
                if not header:
                    raise RetrievalIpcError(
                        f"RetrievalMain closed its response stream. Java stderr: "
                        f"{self._stderr_text()}"
                    )
                frame_size = struct.unpack(">I", header)[0]
                if frame_size <= 0 or frame_size > MAX_IPC_FRAME_BYTES:
                    raise RetrievalIpcError(
                        f"Invalid Java IPC response frame size: {frame_size}"
                    )
                payload = self._read_exact(stream, frame_size)
                if len(payload) != frame_size:
                    raise RetrievalIpcError("Java IPC response ended before the frame was complete")
                response = json.loads(payload.decode("utf-8"))
                if not isinstance(response, dict):
                    raise RetrievalIpcError("Java IPC response must be a JSON object")
                self._responses.put(response)
        except BaseException as exc:
            self._responses.put(exc)

    def _read_stderr(self) -> None:
        stream = self._process.stderr
        if stream is None:
            return
        for raw_line in iter(stream.readline, b""):
            line = raw_line.decode("utf-8", errors="replace").rstrip()
            with self._stderr_lock:
                self._stderr_lines.append(line)
            if "Retrieval IPC server ready" in line:
                self._ready.set()
        self._ready.set()

    @staticmethod
    def _read_exact(stream: Any, size: int) -> bytes:
        chunks: list[bytes] = []
        remaining = size
        while remaining:
            chunk = stream.read(remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)

    @staticmethod
    def _decode_record(raw: Any) -> EventRecord:
        if not isinstance(raw, dict):
            raise RetrievalIpcError("Java returned a non-object record")
        try:
            source_id = int(raw["agentId"])
            timestamp_ns = int(raw["timestamp"])
            event_type = int(raw["eventType"])
            payload_text = raw["payload"]
            if timestamp_ns < 0:
                raise ValueError("negative timestamps are outside the Python event model")
            if not isinstance(payload_text, str):
                raise ValueError("payload is not a Base64 string")
            payload = base64.b64decode(payload_text, validate=True)
            return EventRecord(
                source_id=source_id,
                timestamp_ns=timestamp_ns,
                event_type=event_type,
                payload_bytes=payload,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise RetrievalIpcError(f"Invalid Java retrieval record: {exc}") from exc

    def _stderr_text(self) -> str:
        with self._stderr_lock:
            return "\n".join(self._stderr_lines)

    def __enter__(self) -> RetrievalIpcClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
