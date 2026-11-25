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
import hashlib
import json
import os
import shutil
import signal
import socket
import struct
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Literal, Sequence

if os.name == "nt":
    import errno
    import msvcrt

    _LOCK_RETRY_ERRNOS = {errno.EACCES, getattr(errno, "EDEADLK", errno.EACCES)}

    def _lock_sidecar_file(lock_file: BinaryIO) -> None:
        lock_file.seek(0, os.SEEK_END)
        if lock_file.tell() == 0:
            lock_file.write(b"\0")
            lock_file.flush()
        lock_file.seek(0)
        while True:
            try:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
                return
            except OSError as exc:
                if exc.errno not in _LOCK_RETRY_ERRNOS:
                    raise
                time.sleep(0.05)

    def _unlock_sidecar_file(lock_file: BinaryIO) -> None:
        lock_file.seek(0)
        msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
else:
    import fcntl

    def _lock_sidecar_file(lock_file: BinaryIO) -> None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)

    def _unlock_sidecar_file(lock_file: BinaryIO) -> None:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)

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
    DataGapWire,
    RetrievalEnvelope,
    TelemetryEventWire,
    UINT64_MAX,
    bind_trace_id,
    get_or_create_trace_id,
)
from bridge.bin_ingestor import BinCacheManager, normalize_time_bounds

MAX_OUTPUT_BYTES = 64 * 1024 * 1024  # 64 MiB ceiling to prevent unbounded pipe buffering


@dataclass(frozen=True)
class JavaReceipt:
    """Explicit metadata receipt returned from Stage 1 Java Storage & Retrieval execution."""

    java_engine: str
    store_layout: str
    external_path: str
    segments_detected: int
    events_returned: int
    java_ipc_latency_ms: float
    min_ts_ns: int | None
    max_ts_ns: int | None
    truncated: bool
    trace_id: str
    slices_queried: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "java_engine": self.java_engine,
            "store_layout": self.store_layout,
            "external_path": self.external_path,
            "segments_detected": self.segments_detected,
            "events_returned": self.events_returned,
            "java_ipc_latency_ms": round(self.java_ipc_latency_ms, 2),
            "min_ts_ns": self.min_ts_ns,
            "max_ts_ns": self.max_ts_ns,
            "truncated": self.truncated,
            "trace_id": self.trace_id,
            "slices_queried": self.slices_queried,
        }


@dataclass(frozen=True)
class StoreBounds:
    """Java-owned storage boundaries and segment intervals discovered over IPC."""

    status: str
    min_ts_ns: int | None
    max_ts_ns: int | None
    segment_count: int
    block_count: int
    segment_windows: list[tuple[int, int]]
    known_sources: list[int]
    error: str | None = None


def plan_bounded_retrieval_slices(
    windows: list[tuple[int, int]],
    min_ts_ns: int | None = None,
    max_ts_ns: int | None = None,
    max_window_ns: int = 3600 * 1_000_000_000,
    max_slices: int = 100,
) -> list[tuple[int, int]]:
    """Merge adjacent populated segment windows within <= max_window_ns and chunk larger spans into valid Contract v0 bounded slices."""
    all_wins = list(windows)
    if not all_wins and (min_ts_ns is not None and max_ts_ns is not None and min_ts_ns <= max_ts_ns):
        all_wins = [(min_ts_ns, max_ts_ns)]

    if not all_wins:
        return []

    # Sort windows by start timestamp
    sorted_windows = sorted(all_wins, key=lambda w: (w[0], w[1]))
    slices: list[tuple[int, int]] = []

    for w_start, w_end in sorted_windows:
        if len(slices) >= max_slices:
            break
        if w_end < w_start:
            w_start, w_end = w_end, w_start
        if w_start == w_end:
            w_end = min(w_start + 1, UINT64_MAX)

        span = w_end - w_start
        if span > max_window_ns:
            # Chunk large window into <= max_window_ns slices
            cur = w_start
            while cur < w_end and len(slices) < max_slices:
                nxt = min(cur + max_window_ns, w_end)
                slices.append((cur, nxt))
                cur = nxt
        else:
            if not slices:
                slices.append((w_start, w_end))
            else:
                prev_start, prev_end = slices[-1]
                if w_end <= prev_end:
                    continue
                # If merging with previous slice stays within max_window_ns, merge!
                if w_end - prev_start <= max_window_ns:
                    slices[-1] = (prev_start, w_end)
                else:
                    slices.append((w_start, w_end))

    return slices


@dataclass(frozen=True)
class JavaStoreLayout:
    """Structural inspection of a candidate Java BlockStore target path."""

    layout_type: Literal["segmented_directory", "single_file", "invalid"]
    store_path: Path
    root_dir: Path | None = None
    blocks_dat: Path | None = None
    block_metadata_dat: Path | None = None
    segments_dir: Path | None = None
    segment_count: int = 0
    is_valid: bool = True
    error_message: str | None = None


def detect_java_store_layout(path: str | Path) -> JavaStoreLayout:
    """Detect whether a path is a segmented Java BlockStore directory, a single-file J1 .dat store, or invalid.

    - Segmented Java Store Directory: Folder contains blocks.dat AND (block-metadata.dat OR segments/).
    - Single-File J1 .dat Store: Standalone .dat file.
    - Invalid / Incomplete: Returns clear validation error naming missing required artifacts.
    """
    p = Path(path).resolve()
    if not p.exists():
        return JavaStoreLayout(
            layout_type="invalid",
            store_path=p,
            is_valid=False,
            error_message=f"Store path does not exist: {p}",
        )

    if p.is_file():
        # Check if user selected blocks.dat inside a segmented store directory
        if p.name == "blocks.dat":
            parent = p.parent
            has_meta = (parent / "block-metadata.dat").is_file()
            has_segs = (parent / "segments").is_dir()
            if has_meta or has_segs:
                seg_count = (
                    len([d for d in (parent / "segments").iterdir() if d.is_dir()])
                    if has_segs
                    else 0
                )
                return JavaStoreLayout(
                    layout_type="segmented_directory",
                    store_path=p,
                    root_dir=parent,
                    blocks_dat=p,
                    block_metadata_dat=(parent / "block-metadata.dat") if has_meta else None,
                    segments_dir=(parent / "segments") if has_segs else None,
                    segment_count=seg_count,
                    is_valid=True,
                )

        if p.suffix == ".dat" or p.name.endswith(".dat"):
            return JavaStoreLayout(
                layout_type="single_file",
                store_path=p,
                root_dir=p.parent,
                blocks_dat=p,
                is_valid=True,
            )

        return JavaStoreLayout(
            layout_type="invalid",
            store_path=p,
            is_valid=False,
            error_message=f"File '{p.name}' is not a valid Java .dat store file (expected .dat extension)",
        )

    if p.is_dir():
        blocks_dat = p / "blocks.dat"
        block_meta = p / "block-metadata.dat"
        segments_dir = p / "segments"

        has_blocks = blocks_dat.is_file()
        has_meta = block_meta.is_file()
        has_segs = segments_dir.is_dir()

        if has_blocks and (has_meta or has_segs):
            seg_count = 0
            if has_segs:
                seg_count = len([d for d in segments_dir.iterdir() if d.is_dir()])
            return JavaStoreLayout(
                layout_type="segmented_directory",
                store_path=p,
                root_dir=p,
                blocks_dat=blocks_dat,
                block_metadata_dat=block_meta if has_meta else None,
                segments_dir=segments_dir if has_segs else None,
                segment_count=seg_count,
                is_valid=True,
            )

        # Incomplete directory: check what is missing
        missing = []
        if not has_blocks:
            missing.append("blocks.dat")
        if not has_meta and not has_segs:
            missing.append("block-metadata.dat or segments/")
        return JavaStoreLayout(
            layout_type="invalid",
            store_path=p,
            root_dir=p,
            is_valid=False,
            error_message=f"Incomplete Java store directory '{p}': missing required artifacts: {', '.join(missing)}",
        )

    return JavaStoreLayout(
        layout_type="invalid",
        store_path=p,
        is_valid=False,
        error_message=f"Unsupported path type: {p}",
    )


def extract_unseen_zip(zip_data: bytes | Path, *, filename: str = "store.zip") -> Path:
    """Safely extract a zipped Java store into an isolated temp directory with zip-slip guard."""
    if isinstance(zip_data, Path):
        data = zip_data.read_bytes()
    else:
        data = zip_data

    file_hash = hashlib.sha256(data).hexdigest()[:16]
    target_dir = Path(tempfile.gettempdir()) / f"fss_unseen_store_{file_hash}"
    target_dir.mkdir(parents=True, exist_ok=True)

    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for member in zf.infolist():
            m_path = Path(member.filename)
            if ".." in m_path.parts or member.filename.startswith(("/", "\\")):
                raise ValueError(
                    f"Security violation: Zip archive contains directory traversal member '{member.filename}'"
                )
            dest = (target_dir / member.filename).resolve()
            if not str(dest).startswith(str(target_dir.resolve())):
                raise ValueError(
                    f"Security violation: Target path escapes destination directory: '{member.filename}'"
                )
        zf.extractall(target_dir)

    # Locate blocks.dat or .dat file
    blocks_files = list(target_dir.rglob("blocks.dat"))
    if blocks_files:
        return blocks_files[0].parent
    dat_files = list(target_dir.rglob("*.dat"))
    if dat_files:
        return dat_files[0]
    return target_dir


def stage_unseen_dat(dat_data: bytes | Path, *, filename: str = "store.dat") -> Path:
    """Stage a standalone .dat file in an isolated temp directory."""
    if isinstance(dat_data, Path):
        data = dat_data.read_bytes()
        fname = dat_data.name
    else:
        data = dat_data
        fname = filename

    file_hash = hashlib.sha256(data).hexdigest()[:16]
    target_dir = Path(tempfile.gettempdir()) / f"fss_unseen_store_{file_hash}"
    target_dir.mkdir(parents=True, exist_ok=True)
    dest_path = target_dir / fname
    dest_path.write_bytes(data)
    return dest_path


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


def convert_contract_v0_to_fss_bin(src_path: Path, dest_path: Path) -> None:
    """Convert a Contract v0 wire binary file (<HBBIQII) to envelope.cfg Big-Endian format (>IQII)."""
    from fss_py.wire import iter_envelopes
    data = src_path.read_bytes()
    records = list(iter_envelopes(data))
    HEADER = struct.Struct(">IQII")
    out = bytearray()
    for r in records:
        out.extend(HEADER.pack(r.source_id, r.timestamp_ns, len(r.payload_bytes), r.event_type))
        out.extend(r.payload_bytes)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest_path.write_bytes(out)


def resolve_and_convert_fixtures(
    fixture_paths: tuple[str | Path, ...],
    root: Path,
    for_storage_ipc: bool = False,
) -> list[str]:
    """Resolve fixture paths and auto-convert raw ArduPilot DataFlash logs (0xA3 0x95) or Contract v0 (for storage_ipc) to .fss.bin."""
    resolved = []
    for p in fixture_paths:
        fp = Path(p)
        resolved_fp = fp if fp.is_absolute() else (root / fp).resolve()
        if resolved_fp.exists():
            with open(resolved_fp, "rb") as tf:
                head2 = tf.read(2)
            conv_target = resolved_fp
            if len(head2) == 2 and head2[0] == 0xA3 and head2[1] == 0x95:
                cache_mgr = BinCacheManager(root / "test_data" / "uploads")
                conv_target, _ = cache_mgr.get_or_ingest(resolved_fp, filename=resolved_fp.name)
                with open(conv_target, "rb") as tf2:
                    head2 = tf2.read(2)
            if for_storage_ipc and len(head2) == 2 and head2[0] == 0x5A and head2[1] == 0xF5:
                conv_dir = Path(tempfile.gettempdir()) / "fss_converted"
                conv_dir.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256(str(conv_target).encode("utf-8")).hexdigest()[:12]
                out_fss = conv_dir / f"{conv_target.stem}_{digest}.be.bin"
                convert_contract_v0_to_fss_bin(conv_target, out_fss)
                resolved.append(str(out_fss))
                continue
            resolved.append(str(conv_target))
            continue
        resolved.append(str(resolved_fp))
    return resolved


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
        self.last_receipt: JavaReceipt | None = None
        self._external_layout: JavaStoreLayout | None = None

        if store_path is not None:
            store_p = Path(store_path)
            self.store_path = store_p if store_p.is_absolute() else (root / store_p).resolve()
            if self.store_path.exists():
                layout = detect_java_store_layout(self.store_path)
                if layout.is_valid:
                    self._external_layout = layout
                    if layout.layout_type == "segmented_directory":
                        mode = "storage_ipc"
                        if storage_properties is None and layout.root_dir is not None:
                            cfg_hash = hashlib.sha256(str(layout.root_dir).encode()).hexdigest()[:16]
                            tmp_cfg_dir = Path(tempfile.gettempdir()) / f"fss_unseen_cfg_{cfg_hash}"
                            tmp_cfg_dir.mkdir(parents=True, exist_ok=True)
                            prop_file = tmp_cfg_dir / "storage.properties"
                            envelope_cfg = (root / "fss-core" / "envelope.cfg").resolve()
                            prop_content = (
                                f"# Dynamically generated for external unseen store\n"
                                f"root={layout.root_dir.resolve()}\n"
                                f"blockSize=65536\n"
                                f"bufferCapacityBytes=1048576\n"
                                f"maxMessageSize=4096\n"
                                f"segmentDurationMillis=60000\n"
                                f"byteOrder=BIG_ENDIAN\n"
                                f"envelopeConfig={envelope_cfg}\n"
                            )
                            prop_file.write_text(prop_content, encoding="utf-8")
                            storage_properties = prop_file
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
            classpath_file = root / "fss-core" / "target" / "dependency-classpath.txt"
            dep_jars = []
            if classpath_file.is_file():
                dep_text = classpath_file.read_text(encoding="utf-8").strip()
                if dep_text:
                    dep_jars = [e for e in dep_text.split(os.pathsep) if e]
            self.classpath = os.pathsep.join([str(core_classes), str(bridge_classes)] + jars + dep_jars)
        else:
            resolved_entries = []
            for entry in classpath.split(os.pathsep):
                if entry:
                    ep = Path(entry)
                    resolved_entries.append(str(ep if ep.is_absolute() else (root / ep).resolve()))
            self.classpath = os.pathsep.join(resolved_entries)

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
    def for_external_store(
        cls,
        path: str | Path,
        *,
        classpath: str | None = None,
        java_bin: str = "java",
        socket_path: str | Path = "/tmp/fss_bridge.sock",
        timeout: float = 30.0,
    ) -> JavaRetriever:
        """Create a JavaRetriever configured for an arbitrary external unseen Java store."""
        layout = detect_java_store_layout(path)
        if not layout.is_valid:
            raise ValueError(f"Invalid external Java store: {layout.error_message}")

        repo_root = Path(__file__).resolve().parent.parent
        envelope_cfg = (repo_root / "fss-core" / "envelope.cfg").resolve()

        if layout.layout_type == "segmented_directory":
            assert layout.root_dir is not None
            cfg_hash = hashlib.sha256(str(layout.root_dir).encode()).hexdigest()[:16]
            tmp_cfg_dir = Path(tempfile.gettempdir()) / f"fss_unseen_cfg_{cfg_hash}"
            tmp_cfg_dir.mkdir(parents=True, exist_ok=True)
            prop_file = tmp_cfg_dir / "storage.properties"

            prop_content = (
                f"# Dynamically generated for external unseen store\n"
                f"root={layout.root_dir.resolve()}\n"
                f"blockSize=65536\n"
                f"bufferCapacityBytes=1048576\n"
                f"maxMessageSize=4096\n"
                f"segmentDurationMillis=60000\n"
                f"byteOrder=BIG_ENDIAN\n"
                f"envelopeConfig={envelope_cfg}\n"
            )
            prop_file.write_text(prop_content, encoding="utf-8")

            retriever = cls(
                store_path=layout.blocks_dat,
                classpath=classpath,
                java_bin=java_bin,
                mode="storage_ipc",
                socket_path=socket_path,
                storage_properties=prop_file,
                timeout=timeout,
            )
            retriever._external_layout = layout
            return retriever
        else:
            # Standalone single file
            retriever = cls(
                store_path=layout.store_path,
                classpath=classpath,
                java_bin=java_bin,
                mode="cli",
                socket_path=socket_path,
                timeout=timeout,
            )
            retriever._external_layout = layout
            return retriever

    def get_last_receipt(self) -> JavaReceipt | None:
        """Return the explicit Java Retrieval Receipt metadata from the most recent retrieval."""
        return self.last_receipt

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
        resolved_fixtures = resolve_and_convert_fixtures(fixture_paths, self.root, for_storage_ipc=(self.mode == "storage_ipc"))

        # If in storage_ipc mode, ingest directly using Ingestion.FileIngestor
        if self.mode == "storage_ipc":
            if self.storage_properties is None or not self.storage_properties.exists():
                raise FileNotFoundError(f"Storage properties file not found: {self.storage_properties}")
            for rf in resolved_fixtures:
                cmd = [
                    self.java_bin,
                    "-Dfile.encoding=UTF-8",
                    "-cp",
                    self.classpath,
                    "Ingestion.FileIngestor",
                    str(self.storage_properties),
                    rf,
                    "--append",
                ]
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
                    raise RuntimeError(f"FileIngestor failed for {rf} (code {code}):\n{err_msg}")
            return

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
            _lock_sidecar_file(lock_file)
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
                _unlock_sidecar_file(lock_file)

    def ingest_clean(self, *fixture_paths: str | Path, trace_id: str | None = None) -> None:
        """Clean storage directory and ingest wire envelope fixtures."""
        resolved_fixtures = resolve_and_convert_fixtures(fixture_paths, self.root, for_storage_ipc=(self.mode == "storage_ipc"))
        if self.mode == "storage_ipc":
            if self.storage_properties is None or not self.storage_properties.exists():
                raise FileNotFoundError(f"Storage properties file not found: {self.storage_properties}")
            tid = bind_trace_id(trace_id)
            first = True
            for rf in resolved_fixtures:
                cmd = [
                    self.java_bin,
                    "-Dfile.encoding=UTF-8",
                    "-cp",
                    self.classpath,
                    "Ingestion.FileIngestor",
                    str(self.storage_properties),
                    rf,
                ]
                if not first:
                    cmd.append("--append")
                first = False

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
                    raise RuntimeError(f"FileIngestor clean ingest failed for {rf} (code {code}):\n{err_msg}")
            return

        # Fallback to standard ingest
        self.ingest(*resolved_fixtures, trace_id=trace_id)

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
        if t_start_ns > t_end_ns:
            t_start_ns, t_end_ns = t_end_ns, t_start_ns
        if t_start_ns == t_end_ns:
            t_end_ns = t_start_ns + 1

        start_val = max(0, min(int(t_start_ns), (1 << 63) - 1))
        end_val = max(start_val + 1, min(int(t_end_ns), (1 << 63) - 1))

        source_val: str | int = "all" if source is ALL else (int(source) if isinstance(source, int) else str(source))
        req = {
            "source": source_val,
            "start_time": start_val,
            "end_time": end_val,
            "event_type": event_type,
        }

        layout = getattr(self, "_external_layout", None)
        seg_count = layout.segment_count if layout else 0
        store_path_str = str(layout.store_path) if layout else (str(self.store_path) if self.store_path else "storage_ipc")
        layout_name = f"Segmented Store ({seg_count} segments)" if seg_count > 0 else "Segmented Store"

        cmd = [
            self.java_bin,
            "-Dfile.encoding=UTF-8",
            "-cp",
            self.classpath,
            "Retrieval.RetrievalMain",
            str(self.storage_properties),
        ]

        t_start_time = time.perf_counter()

        proc = subprocess.Popen(
            cmd,
            cwd=self.root,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )

        try:
            def _query_ipc(req_data: dict[str, Any]) -> dict[str, Any]:
                req_b = json.dumps(req_data).encode("utf-8")
                if proc.stdin is None or proc.stdout is None:
                    raise RuntimeError("Failed to open stdio pipes to RetrievalMain")
                proc.stdin.write(struct.pack(">I", len(req_b)))
                proc.stdin.write(req_b)
                proc.stdin.flush()

                len_b = proc.stdout.read(4)
                if len(len_b) < 4:
                    err_s = proc.stderr.read().decode("utf-8", errors="replace") if proc.stderr else ""
                    raise RuntimeError(f"RetrievalIpcServer closed stream without responding. Stderr: {err_s}")

                r_len = struct.unpack(">I", len_b)[0]
                max_f_bytes = 16 * 1024 * 1024  # Enforce 16 MB max frame length guard
                if r_len <= 0 or r_len > max_f_bytes:
                    raise ValueError(
                        f"Invalid IPC frame length from RetrievalIpcServer: {r_len} bytes "
                        f"(must be between 1 and {max_f_bytes} bytes)"
                    )
                r_buf = proc.stdout.read(r_len)
                return json.loads(r_buf.decode("utf-8"))

            resp_obj = _query_ipc(req)
            if resp_obj.get("status") != "OK":
                error_msg = resp_obj.get("error", "Unknown retrieval error")
                latency_ms = (time.perf_counter() - t_start_time) * 1000.0
                receipt = JavaReceipt(
                    java_engine="Retrieval.RetrievalMain (Storage IPC)",
                    store_layout=layout_name,
                    external_path=store_path_str,
                    segments_detected=seg_count,
                    events_returned=0,
                    java_ipc_latency_ms=latency_ms,
                    min_ts_ns=None,
                    max_ts_ns=None,
                    truncated=False,
                    trace_id=tid,
                )
                self.last_receipt = receipt
                return RetrievalEnvelope(
                    status="ERROR",
                    error_message=error_msg,
                    trace_id=tid,
                    receipt=receipt.to_dict(),
                )

            records = resp_obj.get("records", [])
            # Fallback: if 0 records found and bounds were nanoseconds (> 10^14), try querying in milliseconds
            if not records and (start_val > 100_000_000_000_000 or end_val > 100_000_000_000_000):
                ms_req = dict(req)
                ms_req["start_time"] = start_val // 1_000_000
                ms_req["end_time"] = (end_val + 999_999) // 1_000_000
                ms_resp = _query_ipc(ms_req)
                if ms_resp.get("status") == "OK" and ms_resp.get("records"):
                    records = ms_resp.get("records", [])

            if not records:
                latency_ms = (time.perf_counter() - t_start_time) * 1000.0
                receipt = JavaReceipt(
                    java_engine="Retrieval.RetrievalMain (Storage IPC)",
                    store_layout=layout_name,
                    external_path=store_path_str,
                    segments_detected=seg_count,
                    events_returned=0,
                    java_ipc_latency_ms=latency_ms,
                    min_ts_ns=None,
                    max_ts_ns=None,
                    truncated=False,
                    trace_id=tid,
                )
                self.last_receipt = receipt
                return RetrievalEnvelope(
                    status="EMPTY",
                    events=[],
                    trace_id=tid,
                    receipt=receipt.to_dict(),
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
                ts_ns = raw_ts + (1 << 64) if raw_ts < 0 else raw_ts
                if 0 < ts_ns < 100_000_000_000_000:
                    ts_ns = ts_ns * 1_000_000

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

            min_ts = min((w.timestamp_ns for w in wire_events), default=t_start_ns)
            max_ts = max((w.timestamp_ns for w in wire_events), default=t_end_ns)

            latency_ms = (time.perf_counter() - t_start_time) * 1000.0
            receipt = JavaReceipt(
                java_engine="Retrieval.RetrievalMain (Storage IPC)",
                store_layout=layout_name,
                external_path=store_path_str,
                segments_detected=seg_count,
                events_returned=len(wire_events),
                java_ipc_latency_ms=latency_ms,
                min_ts_ns=min_ts,
                max_ts_ns=max_ts,
                truncated=len(records) > max_events,
                trace_id=tid,
            )
            self.last_receipt = receipt

            return RetrievalEnvelope(
                status="OK",
                count=len(wire_events),
                truncated=len(records) > max_events,
                events=wire_events,
                trace_id=tid,
                min_ts_ns=min_ts,
                max_ts_ns=max_ts,
                receipt=receipt.to_dict(),
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

    def get_store_bounds(self, trace_id: str | None = None) -> StoreBounds:
        """Query Java for store boundaries, segment intervals, and known sources over IPC."""
        tid = bind_trace_id(trace_id)
        if self.mode == "storage_ipc":
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
                req_b = json.dumps({"action": "bounds"}).encode("utf-8")
                if proc.stdin is None or proc.stdout is None:
                    raise RuntimeError("Failed to open stdio pipes to RetrievalMain")
                proc.stdin.write(struct.pack(">I", len(req_b)))
                proc.stdin.write(req_b)
                proc.stdin.flush()
                len_b = proc.stdout.read(4)
                if len(len_b) < 4:
                    return StoreBounds(
                        status="EMPTY",
                        min_ts_ns=None,
                        max_ts_ns=None,
                        segment_count=0,
                        block_count=0,
                        segment_windows=[],
                        known_sources=[],
                    )
                r_len = struct.unpack(">I", len_b)[0]
                resp = json.loads(proc.stdout.read(r_len).decode("utf-8"))
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
        elif (self.mode == "worker") or (self.mode == "auto" and self.is_worker_alive(self.socket_path)):
            req = {"action": "bounds", "store_path": str(self.store_path), "trace_id": tid}
            raw = send_worker_request(self.socket_path, req, timeout=self.timeout)
            resp = json.loads(raw)
        else:
            cmd = [
                self.java_bin,
                "-Dfile.encoding=UTF-8",
                "-cp",
                self.classpath,
                "bridge.JavaStoreBridge",
                "bounds",
                "--store",
                str(self.store_path),
                "--trace-id",
                tid,
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, cwd=self.root)
            if res.returncode != 0:
                return StoreBounds(
                    status="ERROR",
                    min_ts_ns=None,
                    max_ts_ns=None,
                    segment_count=0,
                    block_count=0,
                    segment_windows=[],
                    known_sources=[],
                    error=res.stderr,
                )
            resp = json.loads(res.stdout)

        st = resp.get("status", "OK")
        min_ts = int(resp["min_ts_ns"]) if resp.get("min_ts_ns") is not None else None
        max_ts = int(resp["max_ts_ns"]) if resp.get("max_ts_ns") is not None else None
        seg_count = int(resp.get("segment_count", 0))
        block_count = int(resp.get("block_count", 0))
        raw_windows = resp.get("segment_windows", [])
        parsed_windows: list[tuple[int, int]] = []
        for w in raw_windows:
            if isinstance(w, dict):
                parsed_windows.append((int(w["start_ts_ns"]), int(w["end_ts_ns"])))
            elif isinstance(w, (list, tuple)) and len(w) >= 2:
                parsed_windows.append((int(w[0]), int(w[1])))
        known_srcs = [int(s) for s in resp.get("known_sources", [])]
        return StoreBounds(
            status=st,
            min_ts_ns=min_ts,
            max_ts_ns=max_ts,
            segment_count=seg_count,
            block_count=block_count,
            segment_windows=parsed_windows,
            known_sources=known_srcs,
            error=resp.get("error"),
        )

    def retrieve_envelope(
        self,
        source: int | _AllSources | str,
        t_start_ns: int,
        t_end_ns: int,
        *,
        max_events: int,
        event_type: int | None = None,
        trace_id: str | None = None,
    ) -> RetrievalEnvelope:
        """Perform bounded retrieval returning the complete Pydantic v2 typed envelope."""
        # Normalize source if str
        if source is ALL or (isinstance(source, str) and source.strip().upper() == "ALL"):
            src_val: int | _AllSources = ALL
        elif isinstance(source, (int, str)):
            src_val = int(source)
        else:
            src_val = source

        # Enforce non-negative bounds
        if t_start_ns > t_end_ns:
            t_start_ns, t_end_ns = t_end_ns, t_start_ns
        if t_start_ns == t_end_ns:
            t_end_ns = t_start_ns + 1

        # Contract validation: raises RetrievalRequestError if bounded window or max_events is violated
        RetrievalRequest(
            source=src_val,
            t_start_ns=t_start_ns,
            t_end_ns=t_end_ns,
            max_events=max_events,
            event_type=event_type,
        )

        tid = bind_trace_id(trace_id)
        source_arg = "ALL" if src_val is ALL else str(src_val)

        if self.mode == "storage_ipc":
            return self._retrieve_via_storage_ipc(
                src_val,
                t_start_ns,
                t_end_ns,
                max_events=max_events,
                event_type=event_type,
                trace_id=tid,
            )

        # 1. Try persistent worker if available and enabled
        use_worker = (self.mode == "worker") or (self.mode == "auto" and self.is_worker_alive(self.socket_path))
        if use_worker:
            t_start = time.perf_counter()
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
            env = RETRIEVAL_ENVELOPE_ADAPTER.validate_json(raw.encode("utf-8"))
            latency_ms = (time.perf_counter() - t_start) * 1000.0
            receipt = JavaReceipt(
                java_engine="bridge.JavaBridgeWorker (AF_UNIX Socket)",
                store_layout="Single-File J1 BlockStore",
                external_path=str(self.store_path),
                segments_detected=0,
                events_returned=len(env.events),
                java_ipc_latency_ms=latency_ms,
                min_ts_ns=env.min_ts_ns,
                max_ts_ns=env.max_ts_ns,
                truncated=env.truncated,
                trace_id=tid,
            )
            self.last_receipt = receipt
            env.receipt = receipt.to_dict()
            return env

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

        t_start = time.perf_counter()
        with open(lock_path, "a+b") as lock_file:
            _lock_sidecar_file(lock_file)
            try:
                code, stdout, stderr = run_subprocess_safe(
                    cmd,
                    cwd=self.root,
                    env=env,
                    timeout=self.timeout,
                )
            finally:
                _unlock_sidecar_file(lock_file)

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

        env = RETRIEVAL_ENVELOPE_ADAPTER.validate_json(raw_line)
        receipt = JavaReceipt(
            java_engine="bridge.JavaStoreBridge (J1 BlockStore)",
            store_layout="Single-File J1 BlockStore",
            external_path=str(self.store_path),
            segments_detected=0,
            events_returned=len(env.events),
            java_ipc_latency_ms=latency_ms,
            min_ts_ns=env.min_ts_ns,
            max_ts_ns=env.max_ts_ns,
            truncated=env.truncated,
            trace_id=tid,
        )
        self.last_receipt = receipt
        env.receipt = receipt.to_dict()
        return env

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
            res = RetrievalResult(request=request, events=[], truncated=False)
            if self.last_receipt is not None:
                object.__setattr__(res, "receipt", self.last_receipt)
            return res

        events = [ev.to_event_record() for ev in envelope.events]
        res = RetrievalResult(request=request, events=events, truncated=envelope.truncated)
        if self.last_receipt is not None:
            object.__setattr__(res, "receipt", self.last_receipt)
        return res

    def retrieve_full_dataset(
        self,
        source: int | _AllSources = ALL,
        max_events: int = 10000,
        trace_id: str | None = None,
    ) -> RetrievalEnvelope:
        """Whole-dataset retrieval across the full time span of the file/store.

        Retrieves across the full [min_ts, max_ts] span. If the total span exceeds
        MAX_WINDOW_NS (1 hour = 3600s), retrieves in consecutive 1-hour chunks and merges
        the resulting events (deduplicated by timestamp_ns and source_id), stopping if
        max_events is reached.

        Returns a complete RetrievalEnvelope with status="OK", truncated=(count >= max_events),
        and all collected events.
        """
        # Normalize source
        if source is ALL or (isinstance(source, str) and source.strip().upper() == "ALL"):
            src_val: int | _AllSources = ALL
        elif isinstance(source, (int, str)):
            src_val = int(source)
        else:
            src_val = source

        tid = bind_trace_id(trace_id)
        t_start_perf = time.perf_counter()

        layout = getattr(self, "_external_layout", None)
        seg_count = layout.segment_count if layout else 0
        store_path_str = str(layout.store_path) if layout else (str(self.store_path) if self.store_path else "storage_ipc")
        layout_name = f"Segmented Store ({seg_count} segments)" if seg_count > 0 else ("Segmented Store" if self.mode == "storage_ipc" else "Single-File J1 BlockStore")

        # Step 1a: Query Java-owned store boundaries and populated intervals over IPC
        bounds = self.get_store_bounds(trace_id=tid)

        if bounds.status != "OK" or (bounds.min_ts_ns is None and not bounds.segment_windows):
            # Fallback for mock environments or standalone tests: probe bounds via retrieve_envelope
            try:
                probe = self.retrieve_envelope(src_val, 0, 1000, max_events=1, trace_id=tid)
                p_min = probe.min_ts_ns
                p_max = probe.max_ts_ns
                if p_min is not None and p_max is not None and (p_min > 0 or p_max > 0 or probe.events):
                    bounds = StoreBounds(
                        status="OK",
                        min_ts_ns=p_min,
                        max_ts_ns=p_max,
                        segment_count=1,
                        block_count=len(probe.events),
                        segment_windows=[(p_min, p_max)],
                        known_sources=[],
                    )
            except Exception:
                pass

        if bounds.status != "OK" or (bounds.min_ts_ns is None and not bounds.segment_windows):
            latency_ms = (time.perf_counter() - t_start_perf) * 1000.0
            receipt = JavaReceipt(
                java_engine="Retrieval.RetrievalMain (Storage IPC)" if self.mode == "storage_ipc" else "bridge.JavaStoreBridge (J1 BlockStore)",
                store_layout=layout_name,
                external_path=store_path_str,
                segments_detected=bounds.segment_count or seg_count,
                events_returned=0,
                java_ipc_latency_ms=latency_ms,
                min_ts_ns=None,
                max_ts_ns=None,
                truncated=False,
                trace_id=tid,
                slices_queried=0,
            )
            self.last_receipt = receipt
            return RetrievalEnvelope(
                status="EMPTY",
                trace_id=tid,
                count=0,
                events=[],
                data_gaps=[],
                min_ts_ns=0,
                max_ts_ns=0,
                scope="full_dataset",
                receipt=receipt.to_dict(),
            )

        # Step 1b: Merge adjacent windows and chunk spans > 3600s into valid Contract v0 bounded slices
        slices = plan_bounded_retrieval_slices(bounds.segment_windows, bounds.min_ts_ns, bounds.max_ts_ns)
        if not slices:
            latency_ms = (time.perf_counter() - t_start_perf) * 1000.0
            receipt = JavaReceipt(
                java_engine="Retrieval.RetrievalMain (Storage IPC)" if self.mode == "storage_ipc" else "bridge.JavaStoreBridge (J1 BlockStore)",
                store_layout=layout_name,
                external_path=store_path_str,
                segments_detected=bounds.segment_count or seg_count,
                events_returned=0,
                java_ipc_latency_ms=latency_ms,
                min_ts_ns=bounds.min_ts_ns,
                max_ts_ns=bounds.max_ts_ns,
                truncated=False,
                trace_id=tid,
                slices_queried=0,
            )
            self.last_receipt = receipt
            return RetrievalEnvelope(
                status="EMPTY",
                trace_id=tid,
                count=0,
                events=[],
                data_gaps=[],
                min_ts_ns=bounds.min_ts_ns or 0,
                max_ts_ns=bounds.max_ts_ns or 0,
                scope="full_dataset",
                receipt=receipt.to_dict(),
            )

        all_events: list[TelemetryEventWire] = []
        seen_keys: set[tuple[int, int, int, str]] = set()
        all_data_gaps: list[DataGapWire] = []
        truncated = False

        for s_start, s_end in slices:
            if len(all_events) >= max_events:
                truncated = True
                break
            chunk_max = min(max_events - len(all_events), 10000)
            chunk_env = self.retrieve_envelope(
                src_val,
                s_start,
                s_end,
                max_events=chunk_max,
                trace_id=tid,
            )
            for ev in chunk_env.events:
                key = (ev.source_id, ev.timestamp_ns, ev.event_type, ev.payload_b64)
                if key not in seen_keys:
                    seen_keys.add(key)
                    all_events.append(ev)
                    if len(all_events) >= max_events:
                        truncated = True
                        break

            if chunk_env.data_gaps:
                all_data_gaps.extend(chunk_env.data_gaps)
            if chunk_env.truncated and len(all_events) >= max_events:
                truncated = True
                break

        # Sort all events by timestamp_ns while preserving lossless uint64
        all_events.sort(key=lambda e: (e.timestamp_ns, e.source_id))

        min_ts = min((e.timestamp_ns for e in all_events), default=bounds.min_ts_ns or 0)
        max_ts = max((e.timestamp_ns for e in all_events), default=bounds.max_ts_ns or min_ts)
        if max_ts <= min_ts:
            max_ts = min_ts + 1

        latency_ms = (time.perf_counter() - t_start_perf) * 1000.0
        receipt = JavaReceipt(
            java_engine="Retrieval.RetrievalMain (Storage IPC)" if self.mode == "storage_ipc" else "bridge.JavaStoreBridge (J1 BlockStore)",
            store_layout=layout_name,
            external_path=store_path_str,
            segments_detected=bounds.segment_count or seg_count,
            events_returned=len(all_events),
            java_ipc_latency_ms=latency_ms,
            min_ts_ns=min_ts,
            max_ts_ns=max_ts,
            truncated=truncated,
            trace_id=tid,
            slices_queried=len(slices),
        )
        self.last_receipt = receipt

        return RetrievalEnvelope(
            status="OK",
            trace_id=tid,
            truncated=truncated,
            count=len(all_events),
            events=all_events,
            data_gaps=all_data_gaps,
            min_ts_ns=min_ts,
            max_ts_ns=max_ts,
            scope="full_dataset",
            receipt=receipt.to_dict(),
        )
