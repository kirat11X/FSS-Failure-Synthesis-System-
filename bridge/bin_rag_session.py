"""Isolated session workspace manager for arbitrary .bin Java ingestion + Company Manual RAG."""

from __future__ import annotations

import os
import re
import shutil
import struct
import subprocess
import tempfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from bridge.bin_ingestor import safe_filename
from bridge.hybrid_rag import HybridRetriever
from bridge.investigation import format_epoch_ns_local
from bridge.java_retriever import convert_contract_v0_to_fss_bin, resolve_java_bin
from bridge.knowledge_base import SUPPORTED_SUFFIXES, KnowledgeBaseError
from bridge.retrieval_ipc import JavaRetrievalRequest, RetrievalIpcClient
from fss_py.schema import DEFAULT_SCHEMA_PATH, SchemaRegistry, load_schema_file
from fss_py.wire import EnvelopeError, iter_envelopes

ROOT_DIR = Path(__file__).resolve().parent.parent
ONE_HOUR_NS = 3_600_000_000_000
_BE_ENVELOPE_HEADER = struct.Struct(">IQII")


def discover_bin_files(root: Path = ROOT_DIR) -> list[Path]:
    """Discover existing .bin / .fss.bin telemetry files inside repo data folders."""
    candidates: list[Path] = []
    search_dirs = (
        root / "test_data_and_manuals",
        root / "fixtures",
        root / "test_data",
    )
    for folder in search_dirs:
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*.bin")):
            if not path.is_file() or path.stat().st_size == 0:
                continue
            # Avoid cluttering dropdown with cached auto-converted duplicates
            if path.parent.name == "uploads" and re.match(r"^[0-9a-f]{12,64}_", path.name):
                continue
            candidates.append(path)
    return candidates


def discover_manual_files(root: Path = ROOT_DIR) -> list[Path]:
    """Discover existing reference manuals inside test_data_and_manuals/ and knowledge_base/."""
    allowed_exts = {".pdf", ".md", ".txt", ".docx"}
    manuals: list[Path] = []
    for folder in (root / "test_data_and_manuals", root / "knowledge_base"):
        if not folder.is_dir():
            continue
        for path in sorted(folder.rglob("*")):
            if not path.is_file() or path.suffix.lower() not in allowed_exts:
                continue
            if path.name in {"README.md", "TEST_GUIDE.txt"}:
                continue
            manuals.append(path)
    return manuals


def discover_schema_files(root: Path = ROOT_DIR) -> list[Path]:
    """Return available YAML schema definitions (default first, then schemas/)."""
    files = []
    if DEFAULT_SCHEMA_PATH.is_file():
        files.append(DEFAULT_SCHEMA_PATH)
    schemas_dir = root / "schemas"
    if schemas_dir.is_dir():
        for path in sorted(schemas_dir.glob("*.yaml")):
            if path != DEFAULT_SCHEMA_PATH:
                files.append(path)
    return files


def select_matching_schema(
    event_types: set[int],
    root: Path = ROOT_DIR,
    override_path: Path | None = None,
) -> tuple[Path, SchemaRegistry]:
    """Select the best matching SchemaRegistry for the observed binary event types."""
    if override_path is not None and override_path.is_file():
        return override_path, load_schema_file(override_path)

    best_path = DEFAULT_SCHEMA_PATH
    best_registry = load_schema_file(DEFAULT_SCHEMA_PATH)
    if not event_types:
        return best_path, best_registry

    best_score = sum(1 for et in event_types if best_registry.get(et) is not None)
    for schema_path in discover_schema_files(root):
        try:
            candidate = load_schema_file(schema_path)
        except Exception:
            continue
        score = sum(1 for et in event_types if candidate.get(et) is not None)
        if score > best_score:
            best_score = score
            best_path = schema_path
            best_registry = candidate
    return best_path, best_registry


def _parse_be_envelope_stream(data: bytes) -> list[tuple[int, int, int]]:
    """Validate and extract (source_id, timestamp_ns, event_type) from envelope.cfg BE frames."""
    offset = 0
    records: list[tuple[int, int, int]] = []
    size = len(data)
    while offset + _BE_ENVELOPE_HEADER.size <= size:
        source_id, ts_ns, payload_len, event_type = _BE_ENVELOPE_HEADER.unpack_from(data, offset)
        if payload_len > 4096 or offset + _BE_ENVELOPE_HEADER.size + payload_len > size:
            raise ValueError(
                "Unsupported or invalid binary file: payload does not match ArduPilot, Contract v0, or Big-Endian envelope.cfg framing."
            )
        records.append((source_id, ts_ns, event_type))
        offset += _BE_ENVELOPE_HEADER.size + payload_len
    if offset != size or not records:
        raise ValueError("Unsupported or empty Big-Endian binary telemetry stream.")
    return records


def _java_classpath(root: Path) -> str:
    core_classes = root / "fss-core" / "target" / "classes"
    lib_dir = root / "fss-core" / "lib"
    jars = [str(p) for p in sorted(lib_dir.glob("*.jar"))] if lib_dir.exists() else []
    dep_file = root / "fss-core" / "target" / "dependency-classpath.txt"
    dep_jars: list[str] = []
    if dep_file.is_file():
        dep_text = dep_file.read_text(encoding="utf-8").strip()
        if dep_text:
            dep_jars = [entry for entry in dep_text.split(os.pathsep) if entry]
    return os.pathsep.join([str(core_classes)] + jars + dep_jars)


def _floor_local_iso(ns: int) -> str:
    seconds = ns // 1_000_000_000
    local = datetime.fromtimestamp(seconds, timezone.utc).astimezone()
    return f"{local:%Y-%m-%dT%H:%M:%S}"


def _ceil_local_iso(ns: int) -> str:
    seconds, rem = divmod(ns, 1_000_000_000)
    if rem > 0:
        seconds += 1
    local = datetime.fromtimestamp(seconds, timezone.utc).astimezone()
    return f"{local:%Y-%m-%dT%H:%M:%S}"


@dataclass
class BinRagSession:
    """Isolated Java BlockStore + Company Manual HybridRAG session workspace."""

    workspace_dir: Path
    storage_properties: Path
    manual_dir: Path
    source_bin_name: str
    total_events: int
    source_ids: list[int]
    event_types: list[dict[str, Any]]
    min_timestamp_ns: int
    max_timestamp_ns: int
    default_start_local: str
    default_end_local: str
    schema_path: Path
    registry: SchemaRegistry
    manual_filename: str | None = None
    manual_chunks: int = 0
    rag_retriever: HybridRetriever | None = None
    _ipc_client: RetrievalIpcClient | None = field(default=None, repr=False)

    def get_retriever(self, root: Path = ROOT_DIR) -> RetrievalIpcClient:
        """Return persistent session RetrievalIpcClient, launching it if needed."""
        if self._ipc_client is None:
            self._ipc_client = RetrievalIpcClient(self.storage_properties, root=root)
        return self._ipc_client

    def retrieve_knowledge(
        self, evidence: dict[str, Any], query: str, top_k: int = 8
    ) -> list[dict[str, Any]]:
        """Retrieve ranked Company Manual chunks strictly from this session's corpus."""
        if self.rag_retriever is None:
            return []
        return self.rag_retriever.retrieve_knowledge(query, evidence=evidence, top_k=top_k)

    def close(self) -> None:
        """Shut down session Java RetrievalMain child process and delete temp workspace."""
        if self._ipc_client is not None:
            try:
                self._ipc_client.close()
            except Exception:
                pass
            self._ipc_client = None
        if self.workspace_dir.exists():
            shutil.rmtree(self.workspace_dir, ignore_errors=True)


def prepare_bin_and_manual_session(
    bin_source: Path | tuple[str, bytes],
    manual_source: Path | tuple[str, bytes] | None = None,
    *,
    root: Path = ROOT_DIR,
    schema_override: Path | None = None,
) -> BinRagSession:
    """Ingest a .bin file into an isolated Java BlockStore and index an optional manual into RAG."""
    workspace_dir = Path(tempfile.mkdtemp(prefix="fss_bin_session_"))
    try:
        raw_dir = workspace_dir / "input"
        store_dir = workspace_dir / "store"
        manual_dir = workspace_dir / "manual"
        raw_dir.mkdir(parents=True, exist_ok=True)
        store_dir.mkdir(parents=True, exist_ok=True)
        manual_dir.mkdir(parents=True, exist_ok=True)

        if isinstance(bin_source, tuple):
            bin_name, bin_bytes = bin_source
            safe_bin = safe_filename(bin_name)
            raw_bin_path = raw_dir / safe_bin
            raw_bin_path.write_bytes(bin_bytes)
        else:
            raw_bin_path = Path(bin_source)
            bin_name = raw_bin_path.name
            bin_bytes = raw_bin_path.read_bytes()

        if len(bin_bytes) < 4:
            raise ValueError(f"Telemetry file '{bin_name}' is empty or too short to be a valid .bin file.")

        be_bin_path = workspace_dir / "ingest_frames.be.bin"
        meta_records: list[tuple[int, int, int]] = []

        # 1. Detect & convert telemetry format -> Big-Endian envelope.cfg binary
        if bin_bytes[:2] == b"\xa3\x95":
            from scripts.convert_ardupilot_bin_to_fss import convert_log

            contract_v0_path = workspace_dir / f"{raw_bin_path.stem}.v0.fss.bin"
            convert_log(raw_bin_path, contract_v0_path, agent_id=1)
            if not contract_v0_path.is_file() or contract_v0_path.stat().st_size == 0:
                raise ValueError(f"ArduPilot DataFlash log '{bin_name}' yielded no telemetry frames.")
            v0_bytes = contract_v0_path.read_bytes()
            meta_records = [(r.source_id, r.timestamp_ns, r.event_type) for r in iter_envelopes(v0_bytes)]
            convert_contract_v0_to_fss_bin(contract_v0_path, be_bin_path)
        elif bin_bytes[:2] == b"\x5a\xf5":
            try:
                envelopes = list(iter_envelopes(bin_bytes))
            except EnvelopeError as exc:
                raise ValueError(f"Invalid FSS Contract v0 binary '{bin_name}': {exc}") from exc
            if not envelopes:
                raise ValueError(f"FSS binary '{bin_name}' contains zero valid frames.")
            meta_records = [(r.source_id, r.timestamp_ns, r.event_type) for r in envelopes]
            v0_copy = workspace_dir / "input.v0.fss.bin"
            v0_copy.write_bytes(bin_bytes)
            convert_contract_v0_to_fss_bin(v0_copy, be_bin_path)
        else:
            # Try validating directly as native Big-Endian envelope.cfg format
            meta_records = _parse_be_envelope_stream(bin_bytes)
            be_bin_path.write_bytes(bin_bytes)

        # 2. Write isolated storage.properties (explicitly disabling repo fallback)
        envelope_cfg = (root / "envelope.cfg").resolve()
        if not envelope_cfg.is_file():
            envelope_cfg = (root / "fss-core" / "envelope.cfg").resolve()
        props_path = workspace_dir / "storage.properties"
        props_path.write_text(
            f"# Session-isolated Java BlockStore\n"
            f"root={store_dir.resolve()}\n"
            f"blockSize=65536\n"
            f"bufferCapacityBytes=1048576\n"
            f"maxMessageSize=4096\n"
            f"segmentDurationMillis=60000\n"
            f"byteOrder=BIG_ENDIAN\n"
            f"envelopeConfig={envelope_cfg}\n"
            f"fallbackToRepoDefaults=false\n",
            encoding="utf-8",
        )

        # 3. Ingest into isolated Java BlockStore via Ingestion.FileIngestor
        java_bin = resolve_java_bin("java")
        cp = _java_classpath(root)
        cmd = [
            java_bin,
            "-Dfile.encoding=UTF-8",
            "-cp",
            cp,
            "Ingestion.FileIngestor",
            str(props_path),
            str(be_bin_path),
        ]
        proc = subprocess.run(cmd, cwd=str(root), capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"Java FileIngestor failed for '{bin_name}': {detail}")

        # 4. Spin up RetrievalIpcClient and verify isolated BlockStore readability
        ipc_client = RetrievalIpcClient(props_path, root=root)
        min_ts = min(r[1] for r in meta_records)
        max_ts = max(r[1] for r in meta_records)
        if max_ts - min_ts > ONE_HOUR_NS:
            clamp_end_ts = min_ts + ONE_HOUR_NS
        else:
            clamp_end_ts = max(max_ts, min_ts + 1)

        # Verify Java IPC store actually answers from the newly ingested segments
        probe = ipc_client.retrieve(JavaRetrievalRequest("all", min_ts, clamp_end_ts, None, 5000))
        if not probe.events:
            raise RuntimeError(
                f"Java RetrievalMain returned 0 events from isolated store after ingesting '{bin_name}'."
            )

        observed_sources = sorted({r[0] for r in meta_records})
        observed_types = {r[2] for r in meta_records}
        schema_path, registry = select_matching_schema(observed_types, root=root, override_path=schema_override)
        event_type_summaries = []
        for et in sorted(observed_types):
            schema = registry.get(et)
            event_type_summaries.append({
                "id": et,
                "hex": f"0x{et:X}",
                "name": schema.name if schema is not None else f"0x{et:02X}",
            })

        # 5. Extract & index Company Manual into session-isolated HybridRetriever
        manual_filename: str | None = None
        manual_chunks = 0
        rag_retriever: HybridRetriever | None = None
        if manual_source is not None:
            if isinstance(manual_source, tuple):
                m_name, m_bytes = manual_source
                safe_m = safe_filename(m_name)
                if Path(safe_m).suffix.lower() not in SUPPORTED_SUFFIXES:
                    raise ValueError(f"Unsupported manual file format '{Path(safe_m).suffix}'.")
                manual_path = manual_dir / safe_m
                manual_path.write_bytes(m_bytes)
                manual_filename = m_name
            else:
                src_m = Path(manual_source)
                if src_m.suffix.lower() not in SUPPORTED_SUFFIXES:
                    raise ValueError(f"Unsupported manual file format '{src_m.suffix}'.")
                manual_path = manual_dir / safe_filename(src_m.name)
                shutil.copy2(src_m, manual_path)
                manual_filename = src_m.name

            try:
                rag_retriever = HybridRetriever(knowledge_base_dir=manual_dir)
                manual_chunks = len(rag_retriever.corpus)
            except KnowledgeBaseError as exc:
                raise ValueError(f"Could not extract/index manual '{manual_filename}': {exc}") from exc
            if manual_chunks == 0:
                raise ValueError(f"Manual '{manual_filename}' produced 0 indexable text chunks.")

        default_start_local = _floor_local_iso(min_ts)
        default_end_local = _ceil_local_iso(clamp_end_ts)
        # Guarantee strictly ordered, <= 1h window string
        if default_end_local <= default_start_local:
            default_end_local = format_epoch_ns_local(min_ts + 1_000_000_000).split(".")[0]

        return BinRagSession(
            workspace_dir=workspace_dir,
            storage_properties=props_path,
            manual_dir=manual_dir,
            source_bin_name=bin_name,
            total_events=len(meta_records),
            source_ids=observed_sources,
            event_types=event_type_summaries,
            min_timestamp_ns=min_ts,
            max_timestamp_ns=max_ts,
            default_start_local=default_start_local,
            default_end_local=default_end_local,
            schema_path=schema_path,
            registry=registry,
            manual_filename=manual_filename,
            manual_chunks=manual_chunks,
            rag_retriever=rag_retriever,
            _ipc_client=ipc_client,
        )
    except Exception:
        shutil.rmtree(workspace_dir, ignore_errors=True)
        raise
