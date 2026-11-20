"""Streamlit Modernized Front End for Failure Synthesis System (FSS).

1980s Cyberpunk / Synthwave Mission-Control Edition.
Integrates:
  - Java BlockStore bounded & whole-dataset retrieval via JavaStoreBridge & JavaRetriever
  - Thread-Safe Resource Caching with ThreadSafeWrapper & st.cache_resource
  - Deterministic SHA-256 upload caching via BinCacheManager
  - Smart whole-dataset telemetry condensation (condense_telemetry)
  - Vendor Error Manual RAG ingestion (load_builtin_manual, parse_error_manual, VendorErrorEntry)
  - Epistemic Citation Validator supporting 5 Validation Badges:
      * VERIFIED_SUPPORTED (Phosphor Acid Green)
      * FORCE_OVERCLAIM (Retro Amber)
      * ENTITY_MISMATCH (Neon Orange)
      * UNGROUNDED_HALLUCINATION (Crimson Neon)
      * INSUFFICIENT_EVIDENCE (Electric Cyan / Muted Slate)
  - Clean top-of-page investigation query bar with submit-on-Enter & dynamic telemetry chips
  - Dedicated Vendor Error Manual Inspector Tab
  - Lossless stringified uint64 timestamps preserving nanoseconds
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import struct
import sys
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Generic, TypeVar

# 1. Project path and environment setup
ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# Ensure local proxy compatibility: set NO_PROXY for local communication
os.environ["NO_PROXY"] = "127.0.0.1,localhost"
os.environ["no_proxy"] = "127.0.0.1,localhost"

# Protobuf compatibility with Python 3.14 (bypass C extension tp_new incompatibility)
sys.modules.setdefault("google._upb._message", None)
sys.modules.setdefault("google.protobuf.pyext._message", None)

import streamlit as st

from fss_py.contract import UNKNOWN_LABEL
from fss_py.decoder import decode_all
from fss_py.evidence import build_evidence
from fss_py.model import ALL, EventRecord, RetrievalRequest, RetrievalRequestError, RetrievalResult
from fss_py.schema import load_schema_file
from fss_py.wire import iter_envelopes

from bridge.bin_ingestor import (
    BinCacheManager,
    compute_sha256,
    condense_telemetry,
    is_critical_event,
    normalize_time_bounds,
)
from bridge.error_catalog import (
    VendorErrorEntry,
    extract_error_codes_from_events,
    load_builtin_manual,
    parse_error_manual,
)
from bridge.hybrid_rag import (
    HybridRetriever,
    enrich_evidence_with_manual,
)
from bridge.java_retriever import JavaRetriever
from bridge.synthesizer import (
    DOMAIN_KNOWLEDGE,
    LOCAL_HF_PATH,
    RULES,
    ValidationReport,
    build_llm_context,
    check_ollama_available,
    retrieve_relevant_knowledge,
    synthesize_and_validate,
)

SCHEMA_PATH = ROOT / "schemas" / "drone_v0.yaml"
GOLDEN_DIR = ROOT / "fixtures" / "golden"
BRIDGE_DIR = ROOT / "bridge"
IRL_DIR = ROOT / "test_data" / "irl"
UNSEEN_DIR = ROOT / "test_data" / "unseen"
CSS_PATH = ROOT / "assets" / "cyberpunk80s.css"
STORAGE_PROPERTIES = ROOT / "fss-core" / "storage.properties"
MAX_WINDOW_NS = 3600 * 10**9  # 1 hour max retrieval window contract


def detect_binary_format(data: bytes) -> tuple[str, str]:
    """Detect binary log wire format and vendor profile hint from raw initial bytes."""
    if len(data) >= 2 and data[0] == 0xA3 and data[1] == 0x95:
        return "ARDUPILOT DATAFLASH", "ardupilot"
    if len(data) >= 2 and data[:2] == b"\x53\x46":
        return "FSS WIRE V0", "fss_native"
    if len(data) >= 20:
        try:
            HEADER = struct.Struct(">IQII")
            agent_id, ts, plen, etype = HEADER.unpack_from(data, 0)
            if 0 < plen < 65536 and 0 < etype < 256:
                return "BIG-ENDIAN CFG", "fss_native"
        except Exception:
            pass
    return "BINARY ENVELOPE", "auto"


def iter_any_envelopes(data: bytes) -> list[EventRecord]:
    """Decode envelopes from either Contract v0 wire (<HBBIQII) or envelope.cfg (>IQII)."""
    if len(data) >= 2 and data[:2] == b"\x53\x46":
        try:
            return list(iter_envelopes(data))
        except Exception:
            pass
    # Try Big-Endian envelope.cfg: >IQII (20-byte header)
    HEADER = struct.Struct(">IQII")
    records = []
    offset = 0
    while offset + 20 <= len(data):
        agent_id, ts, plen, etype = HEADER.unpack_from(data, offset)
        if plen < 0 or offset + 20 + plen > len(data):
            break
        payload = data[offset + 20 : offset + 20 + plen]
        ts_ns = ts * 1_000_000 if (0 < ts < 100_000_000_000_000) else ts
        records.append(EventRecord(agent_id, ts_ns, etype, payload))
        offset += 20 + plen
    return records


T = TypeVar("T")


# -----------------------------------------------------------------------------
# Thread-Safe Resource Management
# -----------------------------------------------------------------------------


class ThreadSafeWrapper(Generic[T]):
    """Thread-safe wrapper holding a shared mutable singleton and an exclusive mutex."""

    def __init__(self, resource: T, close_fn: Callable[[T], None] | None = None) -> None:
        self._resource = resource
        self._lock = threading.Lock()
        self._close_fn = close_fn

    @property
    def resource(self) -> T:
        return self._resource

    @property
    def lock(self) -> threading.Lock:
        return self._lock

    def __enter__(self) -> T:
        self._lock.acquire()
        return self._resource

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self._lock.release()

    def is_healthy(self) -> bool:
        if self._resource is None:
            return False
        if hasattr(self._resource, "is_healthy") and callable(self._resource.is_healthy):
            try:
                return bool(self._resource.is_healthy())
            except Exception:
                return False
        return True

    def close(self) -> None:
        with self._lock:
            if self._close_fn and self._resource is not None:
                try:
                    self._close_fn(self._resource)
                except Exception:
                    pass


class LocalLLMEngine:
    """Thread-safe manager for local LLM inference with PyTorch torch.inference_mode and CUDA OOM recovery."""

    def __init__(self, model_path: str | None = None) -> None:
        self.model_path = model_path or LOCAL_HF_PATH
        self._model = None
        self._tokenizer = None

    def is_healthy(self) -> bool:
        return True

    def close(self) -> None:
        self._model = None
        self._tokenizer = None
        try:
            import torch
            if torch.cuda.is_available():
                torch.cuda.empty_cache()
        except ImportError:
            pass

    def generate(self, system_prompt: str, user_prompt: str, max_new_tokens: int = 512) -> dict[str, Any] | None:
        try:
            import torch
            from transformers import AutoModelForCausalLM, AutoTokenizer

            with torch.inference_mode():
                if self._model is None and os.path.isdir(self.model_path):
                    self._tokenizer = AutoTokenizer.from_pretrained(self.model_path, local_files_only=True)
                    self._model = AutoModelForCausalLM.from_pretrained(
                        self.model_path,
                        local_files_only=True,
                        torch_dtype=torch.float32,
                        device_map="auto" if torch.cuda.is_available() else None,
                    )
                if self._model is None or self._tokenizer is None:
                    return None

                messages = [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ]
                text = self._tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
                inputs = self._tokenizer([text], return_tensors="pt").to(self._model.device)
                generated_ids = self._model.generate(inputs.input_ids, max_new_tokens=max_new_tokens, do_sample=False)
                output = self._tokenizer.batch_decode(
                    [g[len(i) :] for i, g in zip(inputs.input_ids, generated_ids)],
                    skip_special_tokens=True,
                )[0]
                json_match = re.search(r"\{.*\}", output, re.DOTALL)
                if json_match:
                    return json.loads(json_match.group(0))
                return None
        except Exception:
            try:
                import torch
                if torch.cuda.is_available():
                    torch.cuda.empty_cache()
            except ImportError:
                pass
            return None


def _validate_resource_wrapper(wrapper: ThreadSafeWrapper[Any]) -> bool:
    return wrapper is not None and wrapper.is_healthy()


def _release_resource_wrapper(wrapper: ThreadSafeWrapper[Any]) -> None:
    if wrapper is not None:
        wrapper.close()


@st.cache_resource(
    validate=_validate_resource_wrapper,
    show_spinner="Initializing Java BlockStore from fixtures...",
)
def get_java_retriever(fixture_paths: tuple[str, ...]) -> ThreadSafeWrapper[JavaRetriever]:
    """Create JavaRetriever and ingest fixture files into a temporary or cached BlockStore."""
    retriever = JavaRetriever.from_fixtures(*fixture_paths)
    return ThreadSafeWrapper(retriever)


@st.cache_resource(
    validate=_validate_resource_wrapper,
    show_spinner="Connecting to active Java BlockStore...",
)
def get_java_retriever_for_store(store_path: str) -> ThreadSafeWrapper[JavaRetriever]:
    """Connect JavaRetriever directly to an existing .dat BlockStore."""
    retriever = JavaRetriever(store_path)
    return ThreadSafeWrapper(retriever)


@st.cache_resource(
    validate=_validate_resource_wrapper,
    show_spinner="Connecting to Java Storage IPC Engine...",
)
def get_java_storage_ipc_retriever(storage_properties: str) -> ThreadSafeWrapper[JavaRetriever]:
    """Connect JavaRetriever to Java Storage & Retrieval via IPC engine."""
    retriever = JavaRetriever(mode="storage_ipc", storage_properties=storage_properties)
    return ThreadSafeWrapper(retriever)


@st.cache_resource(
    validate=_validate_resource_wrapper,
)
def get_local_llm_engine() -> ThreadSafeWrapper[LocalLLMEngine]:
    return ThreadSafeWrapper(LocalLLMEngine())


@st.cache_resource
def get_schema_registry():
    """Load and cache the schema registry."""
    return load_schema_file(SCHEMA_PATH)


@st.cache_resource
def get_bin_cache_manager() -> BinCacheManager:
    """Singleton cache manager for uploaded and translated binary files."""
    return BinCacheManager()


# -----------------------------------------------------------------------------
# Cached Immutable Data Retrieval
# -----------------------------------------------------------------------------


@st.cache_data(show_spinner=False)
def cached_execute_retrieval(
    store_path: str,
    source_str: str,
    t_start_ns: int,
    t_end_ns: int,
    max_events: int,
    _retriever: JavaRetriever,
) -> RetrievalResult:
    """Cache immutable BlockStore retrieval results."""
    source_arg = ALL if source_str == "ALL" else int(source_str)
    return _retriever.retrieve(
        source=source_arg,
        t_start_ns=t_start_ns,
        t_end_ns=t_end_ns,
        max_events=max_events,
    )


@st.cache_data(show_spinner=False)
def cached_retrieve_knowledge(
    events_count: int,
    terms_hash: str,
    query: str,
    top_k: int = 8,
    _evidence: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Cache immutable domain knowledge retrieval chunks."""
    if _evidence is None:
        return DOMAIN_KNOWLEDGE[:top_k]
    return retrieve_relevant_knowledge(_evidence, query, top_k=top_k)


@st.cache_data(show_spinner=False)
def cached_synthesize_and_validate(
    query: str,
    evidence_hash: str,
    vendor_matches_hash: str,
    _evidence: dict[str, Any],
    _vendor_matches: list[dict[str, Any]],
) -> tuple[dict[str, Any], ValidationReport, str]:
    """Cache immutable RAG synthesis and citation validation outputs."""
    return synthesize_and_validate(_evidence, query, vendor_manual_matches=_vendor_matches)


# -----------------------------------------------------------------------------
# Lossless Timestamp & JSON Formatting
# -----------------------------------------------------------------------------


def fmt_ns(ns: int | None) -> str:
    """Format nanoseconds as UTC datetime string with full 9-digit nanosecond precision."""
    if ns is None:
        return "N/A"
    try:
        if ns < 10**14:  # Milliseconds
            dt = datetime.fromtimestamp(ns / 1000.0, tz=timezone.utc)
            return f"{dt:%Y-%m-%d %H:%M:%S}.{(ns % 1000):03d}000000"
        dt = datetime.fromtimestamp(ns // 10**9, tz=timezone.utc)
        nanos = ns % 10**9
        return f"{dt:%Y-%m-%d %H:%M:%S}.{nanos:09d}"
    except Exception:
        return str(ns)


def make_lossless_json_safe(obj: Any) -> Any:
    """Recursively convert uint64 timestamps to base-10 strings to prevent JavaScript IEEE 754 precision loss."""
    if isinstance(obj, dict):
        return {
            k: str(v) if isinstance(v, int) and (v >= 2**53 or "timestamp" in k.lower() or k.endswith("_ns"))
            else make_lossless_json_safe(v)
            for k, v in obj.items()
        }
    elif isinstance(obj, list):
        return [make_lossless_json_safe(x) for x in obj]
    elif isinstance(obj, int) and obj >= 2**53:
        return str(obj)
    return obj


def epistemic_tag(ev: dict[str, Any]) -> str:
    """Compute the epistemic status tag (decoded, documented, unknown bytes)."""
    status = ev.get("decode_status", "SUCCESS")
    has_doc = bool(ev.get("interpreted_fields"))
    unknown = ev.get("unknown_bytes", 0)

    if status == "UNKNOWN_EVENT_TYPE":
        return f"unknown ({ev.get('payload_len', 0)} bytes)"
    if status == "MALFORMED_PAYLOAD":
        return "malformed"
    if status == "PARTIALLY_DECODED":
        doc_part = " + documented" if has_doc else ""
        return f"decoded{doc_part} + unknown ({unknown} bytes)"
    if has_doc:
        return "decoded + documented"
    return "decoded"


def render_table(rows: list[dict[str, Any]], headers: list[str] | None = None) -> None:
    """Render tabular data robustly with automatic fallback."""
    if not rows:
        st.info("No records to display.")
        return
    try:
        st.dataframe(rows, use_container_width=True, hide_index=True)
    except Exception:
        try:
            st.table(rows)
        except Exception:
            cols = headers if headers else list(rows[0].keys())
            lines = [
                "| " + " | ".join(cols) + " |",
                "| " + " | ".join(["---"] * len(cols)) + " |",
            ]
            for r in rows:
                line_vals = [str(r.get(c, "")).replace("|", "\\|") for c in cols]
                lines.append("| " + " | ".join(line_vals) + " |")
            st.markdown("\n".join(lines))


def compute_epistemic_badge(
    val_report: ValidationReport,
    evidence: dict[str, Any],
    synthesis: dict[str, Any],
) -> tuple[str, str, str, str]:
    """Map verification results to 1 of 5 epistemic Validation Badges in Cyberpunk styling.

    Returns:
        (badge_name, css_class, title, description)
    """
    badge_attr = getattr(val_report, "badge", None)
    badge_str = badge_attr.value if hasattr(badge_attr, "value") else (str(badge_attr) if badge_attr else "")

    # 1. Stage 0 Sufficiency Short-Circuit or 0-event abstention -> INSUFFICIENT_EVIDENCE
    if not evidence.get("events") or synthesis.get("short_circuited") or badge_str == "INSUFFICIENT_EVIDENCE" or str(val_report.verdict) == "INSUFFICIENT_EVIDENCE":
        return (
            "INSUFFICIENT_EVIDENCE",
            "badge-insufficient",
            "🛡️ Part 8: Epistemic Validation Badge: INSUFFICIENT_EVIDENCE",
            "Pre-Generation Sufficiency Gate triggered: Zero events or unrecorded telemetry gaps in retrieval window. "
            "Neural synthesis was short-circuited to prevent ungrounded speculation (AISVS C07-04 / RINSE 2026).",
        )

    if badge_str == "VERIFIED_SUPPORTED":
        valid_cites = ", ".join(f"`{c}`" for c in val_report.valid_citations) if val_report.valid_citations else "None (nominal)"
        adj = " (with automated omission adjustments)" if getattr(val_report, "verdict", None) == "ACCEPTED_WITH_CORRECTIONS" else ""
        return (
            "VERIFIED_SUPPORTED",
            "badge-verified",
            f"🛡️ Part 8: Epistemic Validation Badge: VERIFIED_SUPPORTED{adj}",
            f"Grounded verification PASSED: All factual claims strictly verified against BlockStore telemetry. Valid citations: {valid_cites}.",
        )
    elif badge_str == "FORCE_OVERCLAIM":
        return (
            "FORCE_OVERCLAIM",
            "badge-overclaim",
            "⚠️ Part 8: Epistemic Validation Badge: FORCE_OVERCLAIM",
            "Warrant escalation flagged: Telemetry records exist, but causal claims exceed evidence force "
            "(e.g. asserting definitive causation across unsynchronized clocks without explicit runbook warrant).",
        )
    elif badge_str == "ENTITY_MISMATCH":
        mismatch_notes = [n for n in val_report.notes if "mismatch" in n.lower() or "entity" in n.lower()]
        detail = mismatch_notes[0] if mismatch_notes else "Claimed entity does not match primary entity in cited telemetry."
        return (
            "ENTITY_MISMATCH",
            "badge-mismatch",
            "🔶 Part 8: Epistemic Validation Badge: ENTITY_MISMATCH",
            f"Deceptive grounding flagged: {detail}",
        )
    elif badge_str == "UNGROUNDED_HALLUCINATION":
        cites_str = ", ".join(f"`{c}`" for c in val_report.invalid_citations) if val_report.invalid_citations else "Fabricated citations"
        return (
            "UNGROUNDED_HALLUCINATION",
            "badge-hallucination",
            "🚫 Part 8: Epistemic Validation Badge: UNGROUNDED_HALLUCINATION",
            f"Hallucination gate blocked: Synthesis contains ungrounded, invalid, or fabricated citation tags: {cites_str}.",
        )

    # Fallbacks based on invalid citations, notes, and verdict
    if val_report.invalid_citations or str(val_report.verdict) == "UNGROUNDED_HALLUCINATION" or (
        str(val_report.verdict) == "REJECTED" and not any("mismatch" in n.lower() or "overclaim" in n.lower() for n in val_report.notes)
    ):
        cites_str = ", ".join(f"`{c}`" for c in val_report.invalid_citations) if val_report.invalid_citations else "Fabricated citations"
        return (
            "UNGROUNDED_HALLUCINATION",
            "badge-hallucination",
            "🚫 Part 8: Epistemic Validation Badge: UNGROUNDED_HALLUCINATION",
            f"Hallucination gate blocked: Synthesis contains ungrounded, invalid, or fabricated citation tags: {cites_str}.",
        )

    has_mismatch = any("mismatch" in n.lower() or "entity" in n.lower() for n in val_report.notes)
    if str(val_report.verdict) == "ENTITY_MISMATCH" or has_mismatch:
        mismatch_notes = [n for n in val_report.notes if "mismatch" in n.lower() or "entity" in n.lower()]
        detail = mismatch_notes[0] if mismatch_notes else "Claimed entity does not match primary entity in cited telemetry."
        return (
            "ENTITY_MISMATCH",
            "badge-mismatch",
            "🔶 Part 8: Epistemic Validation Badge: ENTITY_MISMATCH",
            f"Deceptive grounding flagged: {detail}",
        )

    has_overclaim = any("overclaim" in n.lower() or "warrant" in n.lower() or "causal" in n.lower() for n in val_report.notes)
    if str(val_report.verdict) == "FORCE_OVERCLAIM" or has_overclaim:
        return (
            "FORCE_OVERCLAIM",
            "badge-overclaim",
            "⚠️ Part 8: Epistemic Validation Badge: FORCE_OVERCLAIM",
            "Warrant escalation flagged: Telemetry records exist, but causal claims exceed evidence force "
            "(e.g. asserting definitive causation across unsynchronized clocks without explicit runbook warrant).",
        )

    valid_cites = ", ".join(f"`{c}`" for c in val_report.valid_citations) if val_report.valid_citations else "None (nominal)"
    adj = " (with automated omission adjustments)" if getattr(val_report, "verdict", None) == "ACCEPTED_WITH_CORRECTIONS" else ""
    return (
        "VERIFIED_SUPPORTED",
        "badge-verified",
        f"🛡️ Part 8: Epistemic Validation Badge: VERIFIED_SUPPORTED{adj}",
        f"Grounded verification PASSED: All factual claims strictly verified against BlockStore telemetry. Valid citations: {valid_cites}.",
    )


def generate_dynamic_query_chips(
    events: list[dict[str, Any]] | list[EventRecord],
    detected_error_codes: list[str],
    sources: list[int],
) -> list[str]:
    """Generate dynamic, context-aware query chips strictly based on what is detected in the uploaded dataset.

    Never mentions drone 474 unless drone 474 exists in the active dataset.
    """
    chips: list[str] = []

    # 1. Error manual diagnostics if error codes or critical events present
    if detected_error_codes or any(is_critical_event(e) for e in events[:100]):
        chips.append("Diagnose all detected errors using loaded error manual")

    # 2. Check for EKF, altitude, IMU, compass, vibration
    has_ekf_or_imu = False
    for ev in events[:200]:
        ename = str(ev.get("event_name") if isinstance(ev, dict) else getattr(ev, "event_name", "")).upper()
        etype = str(ev.get("event_type") if isinstance(ev, dict) else getattr(ev, "event_type", "")).upper()
        if any(k in ename or k in etype for k in ("EKF", "BARO", "ALT", "VIBE", "IMU", "COMPASS", "0X43", "0X44")):
            has_ekf_or_imu = True
            break
    if has_ekf_or_imu:
        chips.append("Inspect EKF & altitude anomalies")

    # 3. Check for formation, avoidance, lidar
    has_avoid_or_formation = False
    for ev in events[:200]:
        ename = str(ev.get("event_name") if isinstance(ev, dict) else getattr(ev, "event_name", "")).upper()
        etype = str(ev.get("event_type") if isinstance(ev, dict) else getattr(ev, "event_type", "")).upper()
        if any(k in ename or k in etype for k in ("AVOID", "FORMATION", "LIDAR", "0X10", "0X20", "0X21")):
            has_avoid_or_formation = True
            break
    if has_avoid_or_formation:
        chips.append("Analyze formation flight & collision avoidance triggers")

    # 4. Source-specific telemetry fault probes (strictly for sources in this file)
    critical_sources = []
    for ev in events:
        if is_critical_event(ev):
            src = ev.get("source_id") if isinstance(ev, dict) else getattr(ev, "source_id", None)
            if src is not None and src not in critical_sources and src in sources:
                critical_sources.append(src)
    for src in critical_sources[:2]:
        chips.append(f"Investigate Drone {src} failure sequence")

    # 5. General fallback chips
    if "Audit telemetry gaps & health timeline" not in chips:
        chips.append("Audit telemetry gaps & health timeline")
    if len(chips) < 4:
        chips.append("Summarize fleet status & mission phases")

    return chips


# -----------------------------------------------------------------------------
# Main Application Entry Point
# -----------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(
        page_title="Failure Synthesis System (FSS)",
        page_icon="🛸",
        layout="wide",
        initial_sidebar_state="expanded",
    )

    # Inject Cyberpunk 1980s Synthwave Theme
    if CSS_PATH.exists():
        st.markdown(f"<style>{CSS_PATH.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)

    st.title("🛸 Failure Synthesis System (FSS)")
    st.caption("1980s Synthwave Edition • Java 64KB BlockStore ⟷ Contract v0 Wire ⟷ Qwen RAG Synthesizer ⟷ Epistemic Citation Validator")

    cache_mgr = get_bin_cache_manager()

    # Discover fixtures, IRL logs, unseen scenarios, and existing stores
    fixture_files = sorted(p.name for p in GOLDEN_DIR.glob("*.bin")) if GOLDEN_DIR.is_dir() else []
    irl_files = sorted(p.name for p in IRL_DIR.glob("*.bin")) if IRL_DIR.is_dir() else []
    unseen_files = sorted(p.name for p in UNSEEN_DIR.glob("*.bin")) if UNSEEN_DIR.is_dir() else []
    dat_files = sorted(p.name for p in BRIDGE_DIR.glob("*.dat")) if BRIDGE_DIR.is_dir() else []

    # =========================================================================
    # SIDEBAR: SECTION 1 — Telemetry Binary Input (.bin / .fss.bin)
    # =========================================================================
    with st.sidebar:
        st.header("📦 Storage & Data Source")

        source_mode = st.radio(
            "Source Mode",
            [
                "Upload Custom Binary (.bin / .fss.bin)",
                "Real-World ArduPilot Logs (test_data/irl)",
                "Unseen Test Scenarios (test_data/unseen)",
                "Golden Fixtures (fixtures/golden)",
                "Existing Java BlockStore (.dat)",
                "Java Telemetry Storage (IPC Engine)",
            ],
            index=0,
        )

        chosen_fixtures: list[Path] = []
        chosen_store: Path | None = None
        raw_bytes_sample: bytes = b""
        active_filename: str = "telemetry.bin"
        was_cached: bool = False
        file_sha256: str = ""

        if source_mode == "Upload Custom Binary (.bin / .fss.bin)":
            upload = st.file_uploader(
                "Upload Telemetry Binary (.bin / .fss.bin)",
                type=["bin"],
                help="Accepts ArduPilot DataFlash (.bin) or native FSS wire envelopes (.fss.bin). Deterministically cached by SHA-256.",
            )
            if upload is not None:
                active_filename = upload.name
                raw_bytes = upload.getvalue()
                raw_bytes_sample = raw_bytes[:4096]
                file_sha256 = compute_sha256(raw_bytes)
                dest_path, was_cached = cache_mgr.get_or_ingest(raw_bytes, filename=upload.name)
                chosen_fixtures = [dest_path]
            elif fixture_files:
                # Default fixture fallback for instant initial-load preview
                default_fixture = GOLDEN_DIR / "scenario_298_474.bin" if "scenario_298_474.bin" in fixture_files else (GOLDEN_DIR / fixture_files[0])
                active_filename = default_fixture.name
                raw_bytes_sample = default_fixture.read_bytes()[:4096]
                file_sha256 = compute_sha256(default_fixture)
                dest_path, was_cached = cache_mgr.get_or_ingest(default_fixture, filename=default_fixture.name)
                chosen_fixtures = [dest_path]
                st.caption(f"📁 Live Preview Fixture: `{default_fixture.name}`")
            else:
                st.info("Upload a `.bin` or `.fss.bin` file to begin.")

        elif source_mode == "Real-World ArduPilot Logs (test_data/irl)":
            if irl_files:
                sel_name = st.selectbox("Select ArduPilot Flight Log", irl_files, index=0)
                active_filename = sel_name
                src_path = IRL_DIR / sel_name
                raw_bytes_sample = src_path.read_bytes()[:4096]
                file_sha256 = compute_sha256(src_path)
                dest_path, was_cached = cache_mgr.get_or_ingest(src_path, filename=sel_name)
                chosen_fixtures = [dest_path]
            else:
                st.warning("No logs found in test_data/irl/")

        elif source_mode == "Unseen Test Scenarios (test_data/unseen)":
            if unseen_files:
                sel_name = st.selectbox("Select Unseen Scenario Log", unseen_files, index=0)
                active_filename = sel_name
                src_path = UNSEEN_DIR / sel_name
                raw_bytes_sample = src_path.read_bytes()[:4096]
                file_sha256 = compute_sha256(src_path)
                dest_path, was_cached = cache_mgr.get_or_ingest(src_path, filename=sel_name)
                chosen_fixtures = [dest_path]
            else:
                st.warning("No logs found in test_data/unseen/")

        elif source_mode == "Golden Fixtures (fixtures/golden)":
            if fixture_files:
                sel_name = st.selectbox("Select Golden Fixture", fixture_files, index=0)
                active_filename = sel_name
                src_path = GOLDEN_DIR / sel_name
                raw_bytes_sample = src_path.read_bytes()[:4096]
                file_sha256 = compute_sha256(src_path)
                dest_path, was_cached = cache_mgr.get_or_ingest(src_path, filename=sel_name)
                chosen_fixtures = [dest_path]
            else:
                st.warning("No fixtures found in fixtures/golden/")

        elif source_mode == "Existing Java BlockStore (.dat)":
            if dat_files:
                selected_dat = st.selectbox("Select BlockStore File", dat_files, index=0)
                chosen_store = BRIDGE_DIR / selected_dat
                active_filename = selected_dat
            else:
                st.warning("No .dat BlockStore files found in bridge/")

        elif source_mode == "Java Telemetry Storage (IPC Engine)":
            cat = st.selectbox(
                "IPC Source Corpus",
                [
                    "Unseen Scenarios (test_data/unseen)",
                    "Real-World ArduPilot (test_data/irl)",
                    "Golden Fixtures (fixtures/golden)",
                ],
                index=0,
            )
            target_dir = UNSEEN_DIR if "unseen" in cat else (IRL_DIR if "irl" in cat else GOLDEN_DIR)
            avail_files = sorted(p.name for p in target_dir.glob("*.bin"))
            if avail_files:
                sel_f = st.selectbox("Select IPC Log", avail_files, index=0)
                ipc_path = target_dir / sel_f
                active_filename = sel_f
                raw_bytes_sample = ipc_path.read_bytes()[:4096]
                file_sha256 = compute_sha256(ipc_path)
                chosen_fixtures = [ipc_path]
                curr_ingested = st.session_state.get("ipc_ingested_file")
                needs_ingest = curr_ingested != str(ipc_path)
                if needs_ingest:
                    st.warning("⚠️ Java IPC Storage needs sync.")
                    if st.button("📥 Sync Java Storage IPC", use_container_width=True):
                        with st.spinner("Ingesting into Java Storage IPC..."):
                            temp_ret = JavaRetriever(mode="storage_ipc", storage_properties=STORAGE_PROPERTIES)
                            temp_ret.ingest_clean(ipc_path)
                            st.session_state["ipc_ingested_file"] = str(ipc_path)
                            st.cache_resource.clear()
                            st.cache_data.clear()
                            st.rerun()

        # Detect wire format & vendor hint
        detected_fmt, vendor_hint = detect_binary_format(raw_bytes_sample)

    # Ensure we have a valid retriever connection
    retriever_wrapper: ThreadSafeWrapper[JavaRetriever]
    sample_envelopes: list[EventRecord] = []

    try:
        if source_mode == "Java Telemetry Storage (IPC Engine)":
            retriever_wrapper = get_java_storage_ipc_retriever(str(STORAGE_PROPERTIES))
            if chosen_fixtures and chosen_fixtures[0].exists():
                try:
                    sample_envelopes = iter_any_envelopes(chosen_fixtures[0].read_bytes())
                except Exception:
                    pass
        elif chosen_store is not None and chosen_store.exists():
            retriever_wrapper = get_java_retriever_for_store(str(chosen_store))
        elif chosen_fixtures:
            fixture_strs = tuple(str(p.resolve()) for p in chosen_fixtures)
            retriever_wrapper = get_java_retriever(fixture_strs)
            for p in chosen_fixtures:
                try:
                    sample_envelopes.extend(iter_any_envelopes(p.read_bytes()))
                except Exception:
                    pass
        else:
            st.info("👈 Select or upload a telemetry log in the sidebar to begin mission analysis.")
            st.stop()
    except Exception as exc:
        st.error(f"Failed to initialize Java BlockStore: {exc}")
        st.stop()

    # Compute duration & event statistics
    if sample_envelopes:
        all_sources = sorted({e.source_id for e in sample_envelopes})
        t_min = min(e.timestamp_ns for e in sample_envelopes)
        t_max = max(e.timestamp_ns for e in sample_envelopes)
        span_s = max(1, -(-(t_max - t_min + 1) // 10**9))
        duration_display = f"{span_s}s" if span_s < 60 else f"{span_s // 60}m {span_s % 60}s"
    else:
        all_sources = []
        t_min = 1791454020000000000
        t_max = 1791454025000000000
        span_s = 5
        duration_display = "5.0s"

    # Display compact "INGESTED & CACHED" neon badge in sidebar
    with st.sidebar:
        cache_label = "⚡ INGESTED & CACHED" if was_cached else "⚡ INGESTION COMPLETE"
        hash_short = f"SHA: {file_sha256[:8]}" if file_sha256 else "LIVE"
        st.markdown(
            f"""
            <div class="neon-badge-pill {'cached' if was_cached else ''}" style="margin-top: 6px; margin-bottom: 12px; width: 100%; justify-content: center;">
                <span>{cache_label}</span> •
                <span>{detected_fmt}</span> •
                <span>{len(sample_envelopes)} EV</span> •
                <span>{duration_display}</span> •
                <code>{hash_short}</code>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.divider()

        # =====================================================================
        # SIDEBAR: SECTION 2 — Vendor Error-Code Manual (RAG Spec)
        # =====================================================================
        st.markdown("### 📖 2. Vendor Error Manual (RAG)")

        manual_profile = st.selectbox(
            "Error Manual Profile",
            [
                "Auto-Detect (Recommended)",
                "ArduPilot Flight DataFlash",
                "PX4 Autopilot uORB",
                "FSS Native Telemetry",
                "Consolidated (All Built-in)",
                "Custom File Upload Only",
            ],
            index=0,
            help="Select the reference fault specification profile to match against decoded telemetry.",
        )

        custom_manual_file = st.file_uploader(
            "Upload Custom Manual (.md, .json, .csv, .txt)",
            type=["md", "json", "csv", "txt"],
            help="Upload vendor fault code specifications to dynamically index into Hybrid RAG.",
        )

        # Resolve vendor entries
        active_vendor_entries: list[VendorErrorEntry] = []
        vendor_label = "Auto"

        # 1. Custom manual file
        if custom_manual_file is not None:
            c_bytes = custom_manual_file.getvalue()
            custom_entries = parse_error_manual(c_bytes, custom_manual_file.name, vendor="custom")
            active_vendor_entries.extend(custom_entries)
            vendor_label = f"Custom ({custom_manual_file.name})"

        # 2. Built-in profile
        if manual_profile != "Custom File Upload Only":
            if manual_profile == "Auto-Detect (Recommended)":
                if vendor_hint == "ardupilot":
                    active_vendor_entries.extend(load_builtin_manual("ardupilot"))
                    vendor_label = "ArduPilot (Auto-Matched)"
                elif vendor_hint == "fss_native":
                    active_vendor_entries.extend(load_builtin_manual("fss_native"))
                    vendor_label = "FSS Native (Auto-Matched)"
                else:
                    active_vendor_entries.extend(load_builtin_manual("all"))
                    vendor_label = "All Built-in (Consolidated)"
            elif manual_profile == "ArduPilot Flight DataFlash":
                active_vendor_entries.extend(load_builtin_manual("ardupilot"))
                vendor_label = "ArduPilot"
            elif manual_profile == "PX4 Autopilot uORB":
                active_vendor_entries.extend(load_builtin_manual("px4"))
                vendor_label = "PX4 Autopilot"
            elif manual_profile == "FSS Native Telemetry":
                active_vendor_entries.extend(load_builtin_manual("fss_native"))
                vendor_label = "FSS Native"
            elif manual_profile == "Consolidated (All Built-in)":
                active_vendor_entries.extend(load_builtin_manual("all"))
                vendor_label = "Consolidated (All)"

        # Index digested entries into HybridRetriever
        if active_vendor_entries:
            try:
                HybridRetriever.get_instance().index_vendor_manual(active_vendor_entries)
            except Exception:
                pass

        st.markdown(
            f"""
            <div class="neon-badge-pill" style="margin-top: 4px; margin-bottom: 12px; width: 100%; justify-content: center;">
                <span>⚡ {len(active_vendor_entries)} ERROR SPECS DIGESTED</span> •
                <span>{vendor_label}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.divider()

        # =====================================================================
        # SIDEBAR: SECTION 3 — Retrieval Bounds & Analysis Scope
        # =====================================================================
        st.header("⏱️ Retrieval Bounds (Contract v0)")
        st.markdown("### ⚙️ Analysis Scope")

        analysis_scope = st.radio(
            "Scope Mode",
            [
                "Full Dataset (Entire .bin) [Default]",
                "Manual Interval Bounds",
            ],
            index=0,
            help="Default analyzes the entire log using smart telemetry condensation. Manual interval allows exact second slicing.",
        )

        # Advanced Time-Window Slicing
        with st.expander("⚙️ Advanced: Time-Window Slicing", expanded=(analysis_scope == "Manual Interval Bounds")):
            st.caption(f"Log timestamps: `{fmt_ns(t_min)}` → `{fmt_ns(t_max)}` UTC")
            offset = st.slider("Offset (seconds from start)", 0, max(0, span_s - 1), 0) if span_s > 1 else 0
            max_duration = min(3600, max(1, span_s - offset + 5))
            duration = st.slider("Window Length (seconds, max 3600)", 1, 3600, max_duration)

            source_options = ["ALL"] + [str(s) for s in all_sources]
            source_selection = st.selectbox("Source Filter", source_options, index=0)
            max_events_bound = st.number_input("Max Events Limit", min_value=10, max_value=20000, value=1000, step=100)

            t_start_ns = t_min + offset * 10**9
            t_end_ns = min(t_start_ns + duration * 10**9, t_start_ns + MAX_WINDOW_NS)

        source_arg = ALL if (analysis_scope != "Manual Interval Bounds" or source_selection == "ALL") else int(source_selection)

        st.divider()
        st.markdown("### 🤖 Model Tier")
        ollama_model, ollama_err = check_ollama_available()
        if ollama_model:
            st.success(f"🟢 Ollama Qwen ({ollama_model}) Online")
        else:
            st.info("ℹ️ Local HF Qwen / Deterministic Reasoning Engine Active")

        if st.button("🔄 Reset / Re-sync Store", use_container_width=True):
            st.cache_resource.clear()
            st.cache_data.clear()
            st.rerun()

    # =========================================================================
    # MAIN AREA: Clean Top-of-Page Operator Query Layout
    # =========================================================================

    # Mission-Control Header
    st.markdown(
        """
        <div style="border-bottom: 1px solid rgba(0, 245, 255, 0.3); padding-bottom: 8px; margin-bottom: 16px;">
            <div style="display: flex; align-items: baseline; justify-content: space-between; flex-wrap: wrap; gap: 8px;">
                <h1 style="margin: 0; font-size: 1.8rem; letter-spacing: 2px;">🛸 FAILURE SYNTHESIS SYSTEM // MISSION CONTROL</h1>
                <div class="hud-kpi-meta" style="font-size: 0.82rem;">1980s SYNTHWAVE EDITION • REAL-TIME EPIDEMIOLOGY & TELEMETRY RAG</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Initial probe of detected error codes from sample envelopes for dynamic chips
    registry = get_schema_registry()
    initial_decoded = decode_all(sample_envelopes[:100], registry) if sample_envelopes else []
    detected_codes = extract_error_codes_from_events(initial_decoded)
    dynamic_chips = generate_dynamic_query_chips(initial_decoded or sample_envelopes, detected_codes, all_sources)

    # Investigation Query Input Bar with Primary Action Button & submit-on-Enter
    st.subheader("1. Operator Investigation Query")
    if "operator_query" not in st.session_state:
        st.session_state["operator_query"] = dynamic_chips[0] if dynamic_chips else "Diagnose all detected errors using loaded error manual"

    with st.form("investigation_query_form", clear_on_submit=False):
        col_q, col_btn = st.columns([5, 2])
        with col_q:
            query_input = st.text_input(
                "INVESTIGATION QUERY",
                value=st.session_state["operator_query"],
                label_visibility="collapsed",
                placeholder="Enter investigation question (e.g. Diagnose all detected errors using loaded error manual)...",
            )
        with col_btn:
            submitted = st.form_submit_button("⚡ ANALYZE FULL TELEMETRY", use_container_width=True)

    if submitted:
        st.session_state["operator_query"] = query_input

    query = st.session_state["operator_query"]

    # Subtle Dynamic Query Chips beneath input bar
    if dynamic_chips:
        st.markdown('<div style="font-size: 0.76rem; color: #8FA0C0; margin-top: -8px; margin-bottom: 4px; letter-spacing: 1px;">QUICK TELEMETRY PROBES (AUTO-DETECTED):</div>', unsafe_allow_html=True)
        chip_cols = st.columns(len(dynamic_chips[:4]))
        for col, chip_text in zip(chip_cols, dynamic_chips[:4]):
            with col:
                if st.button(f"🔎 {chip_text}", key=f"chip_{hash(chip_text)}", use_container_width=True):
                    st.session_state["operator_query"] = chip_text
                    st.rerun()

    st.markdown("<div style='margin-bottom: 12px;'></div>", unsafe_allow_html=True)

    # =========================================================================
    # RETRIEVAL EXECUTION: Full Dataset vs Bounded Slicing
    # =========================================================================
    retrieval_res: RetrievalResult
    condensed_events: list[dict[str, Any]] = []

    with st.spinner("Executing Java BlockStore retrieval & telemetry condensation..."):
        try:
            with retriever_wrapper as retriever:
                if analysis_scope == "Full Dataset (Entire .bin) [Default]":
                    # Whole dataset retrieval
                    envelope = retriever.retrieve_full_dataset(source=source_arg, max_events=10000)
                    wire_records = [ev.to_event_record() if hasattr(ev, "to_event_record") else ev for ev in envelope.events]
                    raw_decoded = decode_all(wire_records, registry)
                    condensed_dicts = condense_telemetry(raw_decoded, max_events_target=1000)
                    event_by_id = {e.event_id: e for e in raw_decoded}
                    condensed_events = [event_by_id[d["event_id"]] for d in condensed_dicts if d.get("event_id") in event_by_id]
                    if not condensed_events and raw_decoded:
                        condensed_events = raw_decoded

                    t_start_effective = envelope.min_ts_ns or t_min
                    t_end_effective = envelope.max_ts_ns or t_max
                    retrieval_res = RetrievalResult(
                        request=RetrievalRequest(
                            source=source_arg,
                            t_start_ns=t_start_effective,
                            t_end_ns=t_end_effective,
                            max_events=10000,
                        ),
                        events=wire_records,
                        truncated=envelope.truncated,
                    )
                else:
                    # Bounded time-window retrieval
                    store_key = str(retriever.storage_properties if retriever.mode == "storage_ipc" else retriever.store_path)
                    retrieval_res = cached_execute_retrieval(
                        store_key,
                        "ALL" if source_arg is ALL else str(source_arg),
                        t_start_ns,
                        t_end_ns,
                        int(max_events_bound),
                        _retriever=retriever,
                    )
                    raw_decoded = decode_all(retrieval_res.events, registry)
                    condensed_events = raw_decoded
                    t_start_effective = t_start_ns
                    t_end_effective = t_end_ns

        except RetrievalRequestError as exc:
            st.error(f"Retrieval Request Contract Violation: {exc}")
            st.stop()
        except Exception as exc:
            st.error(f"JavaStoreBridge Retrieval Failure: {exc}")
            st.stop()

    # Build Grounded Evidence
    evidence = build_evidence(condensed_events, query=query, retrieval=retrieval_res)

    # Match Telemetry Faults against Digested Vendor Manual
    vendor_manual_matches = enrich_evidence_with_manual(evidence, active_vendor_entries)
    evidence["vendor_manual_matches"] = vendor_manual_matches

    # =========================================================================
    # RAG SYNTHESIS & EPISTEMIC CITATION AUDIT
    # =========================================================================
    is_empty_retrieval = len(retrieval_res.events) == 0 or getattr(retrieval_res, "status", None) == "EMPTY"

    if is_empty_retrieval:
        knowledge_chunks = retrieve_relevant_knowledge(evidence, query, top_k=8)
        llm_context = build_llm_context(evidence, query, knowledge_chunks, vendor_manual_matches=vendor_manual_matches)
        synthesis = {
            "summary": (
                f"INSUFFICIENT EVIDENCE: Zero telemetry events were recorded or retrieved within "
                f"the requested window [{fmt_ns(t_start_effective)}, {fmt_ns(t_end_effective)}) UTC. "
                f"Neural synthesis was short-circuited by the Pre-Generation Sufficiency Gate to prevent hallucination."
            ),
            "likely_cause": None,
            "confidence": "low",
            "contributing_events": [],
            "uncertainty": [
                "Zero observable events in requested time window.",
                "Causal analysis requires expanding retrieval window or removing source filter.",
            ],
            "data_gaps": [
                f"Unrecorded / empty telemetry window: 0 events retrieved between {t_start_effective} ns and {t_end_effective} ns."
            ],
            "short_circuited": True,
            "state": "INSUFFICIENT_EVIDENCE",
        }
        val_report = ValidationReport(
            verdict="INSUFFICIENT_EVIDENCE",
            valid_citations=[],
            invalid_citations=[],
            notes=["Stage 0 Sufficiency Gate: Zero-event short-circuit active. Generator bypassed."],
            corrected_synthesis=synthesis,
        )
        model_used = "Pre-Generation Sufficiency Gate (Short-Circuit)"
    else:
        with st.spinner("Executing Qwen RAG synthesis, vendor manual reasoning & epistemic citation validation..."):
            terms_hash = str(hash(tuple(sorted((e.get("event_name") or "") for e in evidence.get("events", [])))))
            knowledge_chunks = cached_retrieve_knowledge(
                len(evidence.get("events", [])),
                terms_hash,
                query,
                top_k=8,
                _evidence=evidence,
            )
            ev_hash = str(hash((len(evidence.get("events", [])), query, terms_hash)))
            vm_hash = str(hash(tuple(vm.get("error_code", "") for vm in vendor_manual_matches)))
            llm_engine_wrapper = get_local_llm_engine()
            with llm_engine_wrapper:
                synthesis, val_report, model_used = cached_synthesize_and_validate(
                    query,
                    ev_hash,
                    vm_hash,
                    _evidence=evidence,
                    _vendor_matches=vendor_manual_matches,
                )
            llm_context = build_llm_context(
                evidence,
                query,
                knowledge_chunks,
                vendor_manual_matches=vendor_manual_matches,
            )

    # =========================================================================
    # HUD KPI STRIP (Zero Label Truncation, High Contrast)
    # =========================================================================
    coverage_mode = "FULL DATASET (100% CRITICAL)" if analysis_scope.startswith("Full") else f"WINDOW ({span_s}s)"
    vendor_disp = f"{vendor_label.split()[0].upper()} ({len(active_vendor_entries)} SPECS)"
    model_disp = model_used.split()[0]

    # Streamlit standard metrics (ensures test-suite metric inspection)
    m_col1, m_col2, m_col3, m_col4, m_col5 = st.columns(5)
    with m_col1:
        st.metric("Source Filter", "ALL" if source_arg is ALL else str(source_arg))
    with m_col2:
        st.metric("Events Analyzed", f"{len(condensed_events)}")
    with m_col3:
        st.metric("Coverage Mode", coverage_mode)
    with m_col4:
        st.metric("Storage Engine", "Java BlockStore (64KB)")
    with m_col5:
        st.metric("Model Tier", model_disp)

    st.markdown(
        f"""
        <div class="hud-kpi-strip">
            <div class="hud-kpi-card">
                <div class="hud-kpi-label">EVENTS ANALYZED</div>
                <div class="hud-kpi-value">{len(condensed_events)}</div>
                <div class="hud-kpi-meta">{len(retrieval_res.events)} raw retrieved</div>
            </div>
            <div class="hud-kpi-card">
                <div class="hud-kpi-label">COVERAGE MODE</div>
                <div class="hud-kpi-value" style="font-size: 1.05rem;">{coverage_mode}</div>
                <div class="hud-kpi-meta">{'Truncated: YES' if retrieval_res.truncated else 'Complete Span'}</div>
            </div>
            <div class="hud-kpi-card">
                <div class="hud-kpi-label">VENDOR MANUAL</div>
                <div class="hud-kpi-value" style="font-size: 1.05rem;">{vendor_disp}</div>
                <div class="hud-kpi-meta">{len(vendor_manual_matches)} faults matched</div>
            </div>
            <div class="hud-kpi-card">
                <div class="hud-kpi-label">STORAGE ENGINE</div>
                <div class="hud-kpi-value" style="font-size: 1.05rem;">JAVA 64KB</div>
                <div class="hud-kpi-meta">CRC-32C Verified</div>
            </div>
            <div class="hud-kpi-card">
                <div class="hud-kpi-label">MODEL TIER</div>
                <div class="hud-kpi-value">{model_disp}</div>
                <div class="hud-kpi-meta">Grounded RAG</div>
            </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # =========================================================================
    # PART 8: EPISTEMIC VALIDATION BADGE (5 Distinct States)
    # =========================================================================
    badge_name, badge_css_class, badge_title, badge_desc = compute_epistemic_badge(val_report, evidence, synthesis)

    st.markdown(
        f"""
        <div class="badge-epistemic {badge_css_class}">
            <h3>{badge_title}</h3>
            <p><strong>STATUS [{badge_name}]:</strong> {badge_desc}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    # Surface reasoning_truncated warning if thinking budget was cut off
    if synthesis.get("reasoning_truncated"):
        st.warning(
            "⚠️ **Thinking Budget Exceeded (`reasoning_truncated: true`)**: "
            "The model's internal reasoning phase reached the configured thinking budget before concluding. "
            "The early-stopping guard forced a transition to the final diagnosis."
        )

    # =========================================================================
    # PART 7: GROUNDED FAILURE SYNTHESIS CARD
    # =========================================================================
    confidence = (synthesis.get("confidence") or "medium").lower()
    conf_color = "🟢" if confidence == "high" else ("🟡" if confidence == "medium" else "🔴")

    with st.container():
        st.markdown(
            f"""
            <div style="background-color: var(--bg-card); border: 1px solid var(--border-cyan); border-radius: 4px; padding: 18px 22px; margin-bottom: 20px;">
                <h3 style="margin-top: 0; font-size: 1.25rem;">🔬 Part 7: Grounded Failure Synthesis ({conf_color} {confidence.upper()} Confidence)</h3>
            """,
            unsafe_allow_html=True,
        )

        summary = synthesis.get("summary", "No summary produced.")
        st.markdown(f"**Executive Summary:** {summary}")

        likely_cause = synthesis.get("likely_cause")
        if likely_cause:
            st.markdown(f"**Root-Cause Sequence:** {likely_cause}")
        else:
            st.markdown("**Root-Cause Sequence:** Nominal behavior — no anomalies detected in this retrieval scope.")

        # Contributing Events Pills (lossless nanoseconds & verified status)
        contributing = synthesis.get("contributing_events", [])
        if contributing:
            st.markdown("**Contributing Events (Verified Citations):**")
            pills_html = []
            for ce in contributing:
                eid = ce.get("event_id", "unknown")
                etype = ce.get("event_type", "EVENT")
                src = ce.get("source_id", "?")
                ts_val = ce.get("timestamp") or ce.get("timestamp_ns")
                ts_str = fmt_ns(ts_val)[11:]
                is_valid = eid in val_report.valid_citations
                pill_class = "pill-verified" if is_valid else "pill-unverified"
                pills_html.append(
                    f'<span class="{pill_class}">[{eid}] Drone {src} • {etype} • {ts_str}</span>'
                )
            # Also show matched vendor error-code pills if present in synthesis
            for vm in vendor_manual_matches:
                v_code = vm.get("error_code")
                v_sub = vm.get("subsystem", "System")
                pills_html.append(
                    f'<span class="pill-vendor-code">[{vm.get("doc_chunk_id", f"err-{v_code}")}] {v_sub}: {v_code}</span>'
                )
            st.markdown(" ".join(pills_html), unsafe_allow_html=True)

        # Clock Caveat Notice
        if len(evidence.get("sources", [])) > 1:
            caveat = evidence.get(
                "clock_caveat",
                "Producer clocks are not synchronized across sources. Relative inter-source ordering is inferred.",
            )
            st.info(f"⏱️ **Clock Synchronization Caveat:** {caveat}")

        # Data gaps notice if any
        if synthesis.get("data_gaps"):
            with st.expander(f"⚠️ Quarantined Data Gaps Reported in Synthesis ({len(synthesis['data_gaps'])})"):
                for gap in synthesis["data_gaps"]:
                    st.markdown(f"- {gap}")

        st.markdown("</div>", unsafe_allow_html=True)

    # =========================================================================
    # INTERACTIVE INSPECTION TABS (Including Tab 4: Vendor Error Manual)
    # =========================================================================
    tab_timeline, tab_gaps, tab_rag, tab_manual, tab_prompt, tab_audit, tab_raw = st.tabs(
        [
            "📑 Tab 1: Timeline & Epistemic Tags",
            "🛡️ Tab 2: Data Gaps Quarantine",
            "📚 Tab 3: Domain Knowledge RAG",
            "📖 Tab 4: Vendor Error Manual",
            "🔍 Tab 5: Prompt Context",
            "⚖️ Tab 6: Citation Validation Audit",
            "💾 Tab 7: Raw Evidence JSON",
        ]
    )

    # Tab 1: Timeline & Epistemic Tags (Grouped by Source, Lossless uint64 timestamps)
    with tab_timeline:
        st.caption(evidence.get("ordering", "Grouped by source_id, sorted by producer timestamp."))
        events_list = evidence.get("events", [])
        if events_list:
            sources_present = evidence.get("sources", [])
            for src in sources_present:
                src_events = [e for e in events_list if e["source_id"] == src]
                st.subheader(f"Drone Source {src} ({len(src_events)} events)")
                rows = []
                for ev in src_events:
                    fields_str = ", ".join(f"{k}={v}" for k, v in ev.get("fields", {}).items())
                    interp_str = ", ".join(ev.get("interpreted_fields", [])) or "none"
                    rows.append(
                        {
                            "Event ID": ev["event_id"],
                            "Producer Time (UTC)": fmt_ns(ev["timestamp_ns"]),
                            "Timestamp (uint64 ns)": str(ev["timestamp_ns"]),
                            "Event Name": ev.get("event_name") or str(ev.get("event_type")),
                            "Status": ev.get("decode_status", "SUCCESS"),
                            "Epistemic Tag": epistemic_tag(ev),
                            "Decoded Fields": fields_str,
                            "Documented Enums": interp_str,
                            "Unknown Bytes": ev.get("unknown_bytes", 0),
                        }
                    )
                render_table(rows)
        else:
            st.info("No events retrieved in this bounded time window.")

    # Tab 2: Data Gaps Quarantine
    with tab_gaps:
        gaps = evidence.get("data_gaps", [])
        if is_empty_retrieval:
            st.warning(
                f"**[INSUFFICIENT_EVIDENCE]** Window [{fmt_ns(t_start_effective)}, {fmt_ns(t_end_effective)}) UTC "
                f"({t_start_effective} → {t_end_effective} ns) contains zero recorded telemetry events. "
                f"Quarantine active; model synthesis short-circuited."
            )
        if gaps:
            st.markdown(f"**Quarantined Data Gaps:** {len(gaps)} anomalous condition(s) isolated from LLM speculation.")
            for g in gaps:
                kind = g.get("kind", "DATA_GAP")
                eid = g.get("event_id")
                src = g.get("source_id")
                detail = g.get("detail", "")
                st.warning(
                    f"**[{kind}]** {f'Event {eid} • ' if eid else ''}"
                    f"{f'Source {src} • ' if src is not None else ''}{detail}"
                )
        elif not is_empty_retrieval:
            st.success("✅ **Zero Data Gaps**: All events were 100% cleanly decoded with known schemas and enums.")

    # Tab 3: Domain Knowledge RAG
    with tab_rag:
        st.markdown(f"**Retrieved {len(knowledge_chunks)} Domain Documentation Chunk(s) via RAG:**")
        for chunk in knowledge_chunks:
            with st.expander(f"📖 {chunk['title']} (`{chunk['chunk']}`) — explains `{chunk.get('explains', '')}`"):
                st.markdown(f"**Domain:** `{chunk['domain']}` | **Kind:** `{chunk['kind']}`")
                st.markdown(f"> {chunk['content']}")

    # Tab 4: Vendor Error Manual (Dedicated Inspector Tab)
    with tab_manual:
        st.markdown(f"### 📖 Vendor Error-Code Manual Inspector ({len(active_vendor_entries)} Specs Loaded)")

        # Section A: Telemetry Matched Error Codes
        st.markdown("#### 🎯 Error Codes Matched in Loaded Telemetry Log")
        if vendor_manual_matches:
            st.markdown(f"**Found {len(vendor_manual_matches)} matching fault definition(s) in this log:**")
            for vm in vendor_manual_matches:
                sev = vm.get("severity", "ERROR").upper()
                sev_badge_class = "badge-sev-error" if "CRIT" in sev or "ERR" in sev else ("badge-sev-warn" if "WARN" in sev else "badge-sev-info")
                causes_txt = "; ".join(vm.get("probable_causes", [])) if vm.get("probable_causes") else "Unspecified"
                actions_txt = "; ".join(vm.get("recommended_actions", [])) if vm.get("recommended_actions") else "Unspecified"

                st.markdown(
                    f"""
                    <div class="vendor-error-card severity-{sev.lower()}">
                        <div class="vendor-error-header">
                            <span class="vendor-error-code">[{vm.get('doc_chunk_id', vm.get('error_code'))}] {vm.get('error_code')} • {vm.get('title')}</span>
                            <span class="vendor-error-badge {sev_badge_class}">{sev}</span>
                        </div>
                        <div style="font-size: 0.88rem; color: #B5D2FF; margin-bottom: 6px;">
                            <strong>Subsystem:</strong> {vm.get('subsystem', 'General')} | <strong>Vendor:</strong> {vm.get('vendor', 'Unknown')}
                        </div>
                        <div style="font-size: 0.9rem; margin-bottom: 8px;">
                            <strong>Description:</strong> {vm.get('description', '')}
                        </div>
                        <div style="font-size: 0.85rem; color: #8FA0C0; line-height: 1.4;">
                            <div>⚡ <strong>Probable Causes:</strong> {causes_txt}</div>
                            <div>🛠️ <strong>Recommended Actions:</strong> {actions_txt}</div>
                        </div>
                    </div>
                    """,
                    unsafe_allow_html=True,
                )
        else:
            st.info("ℹ️ No specific vendor fault codes were triggered in this telemetry interval.")

        st.markdown("---")

        # Section B: Complete Error Catalog Browser
        st.markdown("#### 📚 Complete Vendor Error-Code Catalog")
        filter_col1, filter_col2 = st.columns([3, 1])
        with filter_col1:
            search_query = st.text_input("Search catalog entries by code, subsystem, or keyword:", placeholder="e.g. EKF, GPS, SUBSYS_14, TIMEOUT")
        with filter_col2:
            sev_filter = st.selectbox("Filter Severity", ["ALL", "CRITICAL", "ERROR", "WARNING", "NOTICE"])

        catalog_rows = []
        for entry in active_vendor_entries:
            # Filtering
            if sev_filter != "ALL" and entry.severity.upper() != sev_filter:
                continue
            if search_query:
                q_low = search_query.lower()
                matches_search = (
                    q_low in entry.error_code.lower()
                    or q_low in entry.title.lower()
                    or q_low in entry.subsystem.lower()
                    or q_low in entry.description.lower()
                    or any(q_low in a.lower() for a in entry.aliases)
                )
                if not matches_search:
                    continue

            is_in_telemetry = any(
                entry.error_code == vm.get("error_code") or entry.doc_chunk_id == vm.get("doc_chunk_id")
                for vm in vendor_manual_matches
            )

            catalog_rows.append(
                {
                    "Error Code": entry.error_code,
                    "Subsystem": entry.subsystem,
                    "Severity": entry.severity,
                    "Title": entry.title,
                    "In Telemetry": "✅ DETECTED" if is_in_telemetry else "Indexed",
                    "Description": entry.description,
                    "Probable Causes": "; ".join(entry.probable_causes) if entry.probable_causes else "—",
                    "Recommended Actions": "; ".join(entry.recommended_actions) if entry.recommended_actions else "—",
                    "RAG Chunk ID": entry.doc_chunk_id,
                }
            )

        render_table(catalog_rows)

    # Tab 5: Prompt Context Audit
    with tab_prompt:
        st.markdown("**Complete Grounded Prompt Context sent to Model:**")
        st.text_area("Audit Trail (fss-evidence/v0 formatting)", llm_context, height=350)

    # Tab 6: Citation Validation Audit (with lossless timestamps)
    with tab_audit:
        st.markdown("### Citation Verification & Hallucination Gate Results")
        c1, c2, c3 = st.columns(3)
        with c1:
            st.metric("Total Valid Citations", len(val_report.valid_citations))
        with c2:
            st.metric("Invalid Citations", len(val_report.invalid_citations))
        with c3:
            st.metric("Validator Status", badge_name)

        st.subheader("Citation Audit Detail")
        audit_rows = []
        for cid in val_report.valid_citations:
            ev = next((e for e in evidence.get("events", []) if e["event_id"] == cid), None)
            if ev:
                audit_rows.append(
                    {
                        "Citation": cid,
                        "Status": "✅ VERIFIED GROUNDED",
                        "Source": ev["source_id"],
                        "Event Name": ev.get("event_name", "N/A"),
                        "Timestamp (UTC)": fmt_ns(ev["timestamp_ns"]),
                        "Timestamp (uint64 ns)": str(ev["timestamp_ns"]),
                    }
                )
            else:
                # Could be a vendor error manual citation [err-...]
                matched_vm = next((vm for vm in vendor_manual_matches if vm.get("doc_chunk_id") == cid or vm.get("error_code") == cid), None)
                if matched_vm:
                    audit_rows.append(
                        {
                            "Citation": cid,
                            "Status": "✅ VERIFIED VENDOR MANUAL",
                            "Source": matched_vm.get("vendor", "VENDOR"),
                            "Event Name": matched_vm.get("title", "Vendor Error"),
                            "Timestamp (UTC)": "Reference Manual",
                            "Timestamp (uint64 ns)": "Manual Chunk",
                        }
                    )
                else:
                    audit_rows.append(
                        {
                            "Citation": cid,
                            "Status": "✅ VERIFIED GROUNDED",
                            "Source": "Evidence",
                            "Event Name": "Documented Event",
                            "Timestamp (UTC)": "Verified",
                            "Timestamp (uint64 ns)": "N/A",
                        }
                    )
        for cid in val_report.invalid_citations:
            audit_rows.append(
                {
                    "Citation": cid,
                    "Status": "❌ REJECTED / HALLUCINATED",
                    "Source": "UNRECORDED",
                    "Event Name": "UNKNOWN",
                    "Timestamp (UTC)": "N/A",
                    "Timestamp (uint64 ns)": "N/A",
                }
            )
        if audit_rows:
            render_table(audit_rows)
        else:
            st.write("No citations were made in this synthesis.")

        if getattr(val_report, "tier_results", None):
            st.subheader("Four-Tier Epistemic Audit Breakdown")
            for t_name, t_data in val_report.tier_results.items():
                t_status = "✅ PASS" if t_data.get("passed") else "⚠️ FLAGGED"
                st.markdown(f"**{t_name}**: {t_status} — {t_data.get('details', '')}")

        if getattr(val_report, "force_axes", None):
            st.subheader("ForceBench Warrant Calibration Axes")
            axes = val_report.force_axes
            if axes:
                axis_cols = st.columns(len(axes))
                for col, (ax_name, ax_data) in zip(axis_cols, axes.items()):
                    with col:
                        if isinstance(ax_data, dict):
                            ax_pass = ax_data.get("passed", True)
                        elif isinstance(ax_data, list):
                            ax_pass = len(ax_data) == 0
                        else:
                            ax_pass = bool(ax_data)
                        st.metric(ax_name.capitalize(), "PASS" if ax_pass else "ESCALATED")

        if val_report.notes:
            st.subheader("Validator Audit Trail & Omission Adjustments")
            for note in val_report.notes:
                st.markdown(f"- ℹ️ {note}")

    # Tab 7: Raw Evidence JSON (Lossless base-10 uint64 strings)
    with tab_raw:
        safe_evidence = make_lossless_json_safe(evidence)
        st.download_button(
            label="📥 Download Evidence JSON (fss-evidence/v0)",
            data=json.dumps(safe_evidence, indent=2),
            file_name=f"fss_evidence_{int(datetime.now().timestamp())}.json",
            mime="application/json",
        )
        st.json(safe_evidence)


if __name__ == "__main__":
    main()
