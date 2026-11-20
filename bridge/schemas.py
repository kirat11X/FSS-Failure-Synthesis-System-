"""Pydantic v2 wire contract schemas, Draft 2020-12 JSON Schema, and uint64 integrity.

Provides:
  - Lossless uint64 parsing (to arbitrary-precision Python int) and base-10 string serialization.
  - Formatted ISO-8601 UTC nanosecond display representations.
  - Strict Pydantic v2 TypeAdapter parsing (TypeAdapter.validate_json).
  - Draft 2020-12 JSON Schema generation.
  - W3C 32-hex trace_id context propagation via structlog.contextvars.
  - Explicit typed retrieval envelopes (OK, EMPTY, ERROR).
"""

from __future__ import annotations

import base64
import datetime
import os
import re
import secrets
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_serializer, field_validator, model_validator

from fss_py.model import EventRecord

UINT32_MAX = 0xFFFFFFFF
UINT64_MAX = 0xFFFFFFFFFFFFFFFF

HEX_TRACE_ID_RE = re.compile(r"^[0-9a-fA-F]{32}$")


def get_or_create_trace_id() -> str:
    """Resolve active 32-hex W3C trace_id from structlog contextvars, FSS_TRACE_ID env, or generate a fresh one."""
    try:
        import structlog
        cv = structlog.contextvars.get_contextvars()
        if "trace_id" in cv and cv["trace_id"]:
            tid = str(cv["trace_id"]).strip().lower()
            if HEX_TRACE_ID_RE.match(tid):
                return tid
    except ImportError:
        pass

    env_tid = os.environ.get("FSS_TRACE_ID", "").strip().lower()
    if HEX_TRACE_ID_RE.match(env_tid):
        return env_tid

    return secrets.token_hex(16)


def bind_trace_id(trace_id: str | None = None) -> str:
    """Bind a 32-hex trace_id into structlog contextvars and FSS_TRACE_ID env var."""
    tid = (trace_id or get_or_create_trace_id()).strip().lower()
    if not HEX_TRACE_ID_RE.match(tid):
        tid = secrets.token_hex(16)

    try:
        import structlog
        structlog.contextvars.bind_contextvars(trace_id=tid)
    except ImportError:
        pass

    os.environ["FSS_TRACE_ID"] = tid
    return tid


def format_iso8601_ns(ts_ns: int) -> str:
    """Losslessly format an unsigned 64-bit nanosecond timestamp into ISO-8601 UTC with nanoseconds."""
    sec = ts_ns // 1_000_000_000
    nsec = ts_ns % 1_000_000_000
    try:
        dt = datetime.datetime.fromtimestamp(sec, tz=datetime.timezone.utc)
        return dt.strftime("%Y-%m-%dT%H:%M:%S") + f".{nsec:09d}Z"
    except (OverflowError, OSError, ValueError):
        return f"{ts_ns} ns (synthetic out-of-range epoch)"


class TelemetryEventWire(BaseModel):
    """Wire representation of an ingested or retrieved telemetry event.

    Timestamps are transported over JSON as base-10 strings to prevent JavaScript
    IEEE 754 float64 (53-bit) truncation, and parsed into Python arbitrary-precision int.
    """
    model_config = ConfigDict(extra="ignore")

    source_id: int = Field(ge=0, le=UINT32_MAX, description="Unsigned 32-bit producer identifier")
    timestamp_ns: int = Field(ge=0, le=UINT64_MAX, description="Unsigned 64-bit producer timestamp (nanoseconds)")
    event_type: int = Field(ge=0, le=UINT32_MAX, description="Unsigned 32-bit schema event type code")
    payload_b64: str = Field(description="Standard Base64-encoded binary payload bytes")

    @field_validator("timestamp_ns", mode="before")
    @classmethod
    def _parse_uint64_ts(cls, v: Any) -> int:
        if isinstance(v, str):
            v = int(v.strip())
        elif not isinstance(v, int):
            raise ValueError(f"timestamp_ns must be int or base-10 str, got {type(v).__name__}")
        if not (0 <= v <= UINT64_MAX):
            raise ValueError(f"timestamp_ns {v} out of uint64 range [0, 2^64-1]")
        return v

    @field_serializer("timestamp_ns")
    def _serialize_uint64_ts(self, v: int) -> str:
        return str(v)

    @property
    def payload_bytes(self) -> bytes:
        return base64.b64decode(self.payload_b64)

    @property
    def timestamp_ns_str(self) -> str:
        return str(self.timestamp_ns)

    @property
    def iso8601(self) -> str:
        return format_iso8601_ns(self.timestamp_ns)

    @property
    def display_ns(self) -> str:
        return f"{self.timestamp_ns} ns"

    def to_event_record(self) -> EventRecord:
        return EventRecord(
            source_id=self.source_id,
            timestamp_ns=self.timestamp_ns,
            event_type=self.event_type,
            payload_bytes=self.payload_bytes,
        )


class DataGapWire(BaseModel):
    """Representation of an unrecorded interval, corrupt block, or truncated retrieval gap."""
    model_config = ConfigDict(extra="ignore")

    source_id: int = Field(ge=0, le=UINT32_MAX)
    gap_start_ns: int = Field(ge=0, le=UINT64_MAX)
    gap_end_ns: int = Field(ge=0, le=UINT64_MAX)
    reason: str

    @field_validator("gap_start_ns", "gap_end_ns", mode="before")
    @classmethod
    def _parse_uint64(cls, v: Any) -> int:
        if isinstance(v, str):
            v = int(v.strip())
        elif not isinstance(v, int):
            raise ValueError(f"timestamp must be int or base-10 str, got {type(v).__name__}")
        if not (0 <= v <= UINT64_MAX):
            raise ValueError(f"timestamp {v} out of uint64 range [0, 2^64-1]")
        return v

    @field_serializer("gap_start_ns", "gap_end_ns")
    def _serialize_uint64(self, v: int) -> str:
        return str(v)

    @model_validator(mode="after")
    def _enforce_ordered_gap_bounds(self) -> DataGapWire:
        if self.gap_start_ns > self.gap_end_ns:
            self.gap_start_ns, self.gap_end_ns = self.gap_end_ns, self.gap_start_ns
        return self

    @property
    def gap_start_ns_str(self) -> str:
        return str(self.gap_start_ns)

    @property
    def gap_end_ns_str(self) -> str:
        return str(self.gap_end_ns)


class RetrievalEnvelope(BaseModel):
    """NDJSON response frame returned by JavaStoreBridge or JavaBridgeWorker.

    Models:
      - status: Literal['OK', 'EMPTY', 'ERROR']
      - 0-event retrieval as explicit status='EMPTY'
      - lossless uint64 boundaries and events
    """
    model_config = ConfigDict(extra="ignore")

    status: Literal["OK", "EMPTY", "ERROR"] = Field(description="Retrieval status envelope")
    trace_id: str = Field(pattern=r"^[0-9a-fA-F]{32}$", description="W3C 32-hex correlation trace identifier")
    truncated: bool = Field(default=False, description="True iff matching events exceeded max_events limit")
    count: int = Field(default=0, ge=0, description="Count of returned events in this frame")
    events: list[TelemetryEventWire] = Field(default_factory=list, description="Retrieved telemetry events")
    data_gaps: list[DataGapWire] = Field(default_factory=list, description="Detected or quarantine data gaps")
    error_message: str | None = Field(default=None, description="Detailed diagnostic error message on failure")
    min_ts_ns: int | None = Field(default=None, description="Earliest recorded timestamp in store")
    max_ts_ns: int | None = Field(default=None, description="Latest recorded timestamp in store")
    scope: Literal["full_dataset", "interval"] | None = Field(default=None, description="Query scope")

    @field_validator("min_ts_ns", "max_ts_ns", mode="before")
    @classmethod
    def _parse_optional_uint64(cls, v: Any) -> int | None:
        if v is None:
            return None
        if isinstance(v, str):
            v = int(v.strip())
        return int(v)

    @field_serializer("min_ts_ns", "max_ts_ns")
    def _serialize_optional_uint64(self, v: int | None) -> str | None:
        return str(v) if v is not None else None

    @model_validator(mode="after")
    def _enforce_ordered_envelope_bounds(self) -> RetrievalEnvelope:
        if self.min_ts_ns is not None and self.max_ts_ns is not None:
            if self.min_ts_ns > self.max_ts_ns:
                self.min_ts_ns, self.max_ts_ns = self.max_ts_ns, self.min_ts_ns
        return self


class IngestRequest(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["ingest"] = "ingest"
    store_path: str
    fixtures: list[str]
    trace_id: str = Field(pattern=r"^[0-9a-fA-F]{32}$")


class IngestResponse(BaseModel):
    model_config = ConfigDict(extra="ignore")

    status: Literal["OK", "ERROR"]
    trace_id: str
    ingested_count: int = 0
    error: str | None = None


class RetrieveRequestWire(BaseModel):
    model_config = ConfigDict(extra="ignore")

    action: Literal["retrieve"] = "retrieve"
    store_path: str
    source: str = "ALL"
    start_ns: str = "0"
    end_ns: str = "0"
    max_events: int = 100
    event_type: int | None = None
    scope: Literal["full_dataset", "interval"] = "full_dataset"
    trace_id: str = Field(pattern=r"^[0-9a-fA-F]{32}$")

    @model_validator(mode="after")
    def _enforce_ordered_request_bounds(self) -> RetrieveRequestWire:
        try:
            s = int(self.start_ns)
            e = int(self.end_ns)
            if s > e:
                self.start_ns = str(e)
                self.end_ns = str(s)
        except (ValueError, TypeError):
            pass
        return self


# TypeAdapters for fast compiled pydantic-core validation
RETRIEVAL_ENVELOPE_ADAPTER: TypeAdapter[RetrievalEnvelope] = TypeAdapter(RetrievalEnvelope)
INGEST_RESPONSE_ADAPTER: TypeAdapter[IngestResponse] = TypeAdapter(IngestResponse)
INGEST_REQUEST_ADAPTER: TypeAdapter[IngestRequest] = TypeAdapter(IngestRequest)
RETRIEVE_REQUEST_ADAPTER: TypeAdapter[RetrieveRequestWire] = TypeAdapter(RetrieveRequestWire)


def get_retrieval_envelope_schema() -> dict[str, Any]:
    """Generate Draft 2020-12 JSON Schema for the RetrievalEnvelope wire contract."""
    schema = RetrievalEnvelope.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return schema


RETRIEVAL_ENVELOPE_SCHEMA: dict[str, Any] = get_retrieval_envelope_schema()
