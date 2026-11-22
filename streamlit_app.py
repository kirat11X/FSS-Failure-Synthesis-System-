"""Streamlit Modernized Front End for Failure Synthesis System (FSS).

Minimalist Research-Grade CRT Diagnostics Terminal Edition.
Integrates:
  - Java BlockStore bounded & whole-dataset retrieval via JavaStoreBridge & JavaRetriever
  - Thread-Safe Resource Caching with ThreadSafeWrapper & st.cache_resource
  - Deterministic SHA-256 upload caching via BinCacheManager
  - Smart whole-dataset telemetry condensation (condense_telemetry)
  - Knowledge-base retrieval from the repository knowledge_base/ directory
  - Epistemic Citation Validator supporting 5 Functional Phosphor Validation Badges:
      * VERIFIED_SUPPORTED (Phosphor Green)
      * FORCE_OVERCLAIM (Phosphor Amber)
      * ENTITY_MISMATCH (Phosphor Red)
      * UNGROUNDED_HALLUCINATION (Phosphor Red)
      * INSUFFICIENT_EVIDENCE (Phosphor Cyan)
  - Clean top-of-page investigation query bar with submit-on-Enter & dynamic telemetry chips
  - Dedicated Matched Vendor Error Codes Inspector Tab
  - Lossless stringified uint64 timestamps preserving nanoseconds
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import random
import re
import shutil
import struct
import subprocess
import sys
import tempfile
import threading
import time
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
from fss_py.schema import DEFAULT_SCHEMA_PATH, SchemaError, load_schema_file
from fss_py.wire import iter_envelopes

from bridge.auto_unseen_simulator import (
    DOMAIN_CONFIGS,
    SUPPORTED_DOMAINS,
    GroundTruthManifest,
    run_auto_unseen_pipeline,
)
from bridge.bin_ingestor import (
    BinCacheManager,
    compute_sha256,
    condense_telemetry,
    is_critical_event,
    normalize_time_bounds,
)
from bridge.java_retriever import (
    JavaReceipt,
    JavaRetriever,
    JavaStoreLayout,
    StoreBounds,
    detect_java_store_layout,
    extract_unseen_zip,
    plan_bounded_retrieval_slices,
    stage_unseen_dat,
)
# Prevent CPU-only HuggingFace hangs in Streamlit interactive sessions when CUDA is absent or in tests
try:
    if "PYTEST_CURRENT_TEST" in os.environ or "FORCE_LOCAL_HF" not in os.environ:
        import bridge.synthesizer
        bridge.synthesizer.LOCAL_HF_PATH = ""
        os.environ["LOCAL_HF_PATH"] = ""
        os.environ["LOCAL_MODEL_PATH"] = ""
        os.environ["QWEN_MODEL_SNAPSHOT"] = ""
except Exception:
    pass

from bridge.synthesizer import (
    LOCAL_HF_PATH,
    ValidationReport,
    build_llm_context,
    check_ollama_available,
    retrieve_relevant_knowledge,
    synthesize_and_validate,
)

SCHEMA_PATH = DEFAULT_SCHEMA_PATH
GOLDEN_DIR = ROOT / "fixtures" / "golden"
BRIDGE_DIR = ROOT / "bridge"
IRL_DIR = ROOT / "test_data" / "irl"
UNSEEN_DIR = ROOT / "test_data" / "unseen"
CSS_PATH = ROOT / "assets" / "research_crt.css"
STORAGE_PROPERTIES = ROOT / "fss-core" / "storage.properties"
MAX_WINDOW_NS = 3600 * 10**9  # 1 hour max retrieval window contract


def detect_binary_format(data: bytes) -> str:
    """Detect the binary envelope format from raw initial bytes."""
    if len(data) >= 2 and data[0] == 0xA3 and data[1] == 0x95:
        return "ARDUPILOT DATAFLASH"
    if len(data) >= 2 and data[:2] == b"\x53\x46":
        return "FSS WIRE V0"
    if len(data) >= 20:
        try:
            HEADER = struct.Struct(">IQII")
            agent_id, ts, plen, etype = HEADER.unpack_from(data, 0)
            if 0 < plen < 65536 and 0 < etype < 256:
                return "BIG-ENDIAN CFG"
        except Exception:
            pass
    return "BINARY ENVELOPE"


def pick_directory_native() -> str | None:
    """Prompt the operator with a native OS folder dialog using zenity, kdialog, or tkinter."""
    if shutil.which("zenity"):
        try:
            res = subprocess.run(
                ["zenity", "--file-selection", "--directory", "--title=Select Java BlockStore Folder"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass
    if shutil.which("kdialog"):
        try:
            res = subprocess.run(
                ["kdialog", "--getexistingdirectory", "--title", "Select Java BlockStore Folder"],
                capture_output=True,
                text=True,
                timeout=30,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass
    if os.environ.get("DISPLAY"):
        try:
            cmd = [
                sys.executable,
                "-c",
                "import tkinter as tk; from tkinter import filedialog; root = tk.Tk(); root.withdraw(); "
                "path = filedialog.askdirectory(title='Select Java BlockStore Folder'); print(path if path else '')",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()
        except Exception:
            pass
    return None


def iter_any_envelopes(data: bytes) -> list[EventRecord]:
    """Decode envelopes from either Contract v0 wire (<HBBIQII) or envelope.cfg (>IQII)."""
    try:
        records = list(iter_envelopes(data))
        if records:
            return records
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
    """Connect JavaRetriever directly to an existing or external unseen .dat BlockStore."""
    retriever = JavaRetriever.for_external_store(store_path)
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
def get_schema_registry(schema_path: str | Path | None = None):
    """Load and cache the schema registry."""
    p = Path(schema_path) if schema_path else SCHEMA_PATH
    return load_schema_file(p)


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
    trace_id: str | None = None,
) -> RetrievalResult:
    """Cache immutable BlockStore retrieval results."""
    source_arg = ALL if source_str == "ALL" else int(source_str)
    return _retriever.retrieve(
        source=source_arg,
        t_start_ns=t_start_ns,
        t_end_ns=t_end_ns,
        max_events=max_events,
        trace_id=trace_id,
    )


@st.cache_data(show_spinner=False)
def cached_retrieve_knowledge(
    events_count: int,
    terms_hash: str,
    query: str,
    top_k: int = 8,
    _evidence: dict[str, Any] | None = None,
) -> list[dict[str, str]]:
    """Cache immutable knowledge_base retrieval chunks."""
    return retrieve_relevant_knowledge(_evidence or {"events": []}, query, top_k=top_k)


@st.cache_data(show_spinner=False)
def cached_synthesize_and_validate(
    query: str,
    evidence_hash: str,
    _evidence: dict[str, Any],
) -> tuple[dict[str, Any], ValidationReport, str]:
    """Cache synthesis grounded in telemetry and knowledge_base documents."""
    return synthesize_and_validate(_evidence, query)


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
    # Sanitize rows to ensure clean, homogeneous Arrow types
    sanitized = []
    for r in rows:
        sanitized.append({str(k): (str(v) if not isinstance(v, (int, float, bool)) else v) for k, v in r.items()})
    try:
        st.dataframe(sanitized, hide_index=True)
    except Exception:
        try:
            st.table(sanitized)
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
    """Map verification results to 1 of 5 epistemic Validation Badges in Minimalist Research CRT styling.

    Returns:
        (badge_name, css_class, title, description)
    """
    badge_attr = getattr(val_report, "badge", None)
    badge_str = badge_attr.value if hasattr(badge_attr, "value") else (str(badge_attr) if badge_attr else "")

    # 1. Stage 0 Sufficiency Short-Circuit or 0-event abstention -> INSUFFICIENT_EVIDENCE
    if not evidence.get("events") or synthesis.get("short_circuited") or badge_str == "INSUFFICIENT_EVIDENCE" or str(val_report.verdict) == "INSUFFICIENT_EVIDENCE":
        return (
            "INSUFFICIENT_EVIDENCE",
            "INSUFFICIENT_EVIDENCE",
            "VERDICT // INSUFFICIENT_EVIDENCE",
            "Pre-Generation Sufficiency Gate triggered: Zero events or unrecorded telemetry gaps in retrieval window. "
            "Neural synthesis was short-circuited to prevent ungrounded speculation (AISVS C07-04 / RINSE 2026).",
        )

    if badge_str == "VERIFIED_SUPPORTED":
        valid_cites = ", ".join(str(c) for c in val_report.valid_citations) if val_report.valid_citations else "None (nominal)"
        adj = " (with automated omission adjustments)" if getattr(val_report, "verdict", None) == "ACCEPTED_WITH_CORRECTIONS" else ""
        return (
            "VERIFIED_SUPPORTED",
            "VERIFIED_SUPPORTED",
            f"VERDICT // VERIFIED_SUPPORTED{adj}",
            f"Grounded verification PASSED: All factual claims strictly verified against BlockStore telemetry. Valid citations: {valid_cites}.",
        )
    elif badge_str == "FORCE_OVERCLAIM":
        return (
            "FORCE_OVERCLAIM",
            "FORCE_OVERCLAIM",
            "VERDICT // FORCE_OVERCLAIM",
            "Warrant escalation flagged: Telemetry records exist, but causal claims exceed evidence force "
            "(e.g. asserting definitive causation across unsynchronized clocks without explicit runbook warrant).",
        )
    elif badge_str == "ENTITY_MISMATCH":
        mismatch_notes = [n for n in val_report.notes if "mismatch" in n.lower() or "entity" in n.lower()]
        detail = mismatch_notes[0] if mismatch_notes else "Claimed entity does not match primary entity in cited telemetry."
        return (
            "ENTITY_MISMATCH",
            "ENTITY_MISMATCH",
            "VERDICT // ENTITY_MISMATCH",
            f"Deceptive grounding flagged: {detail}",
        )
    elif badge_str == "UNGROUNDED_HALLUCINATION":
        cites_str = ", ".join(str(c) for c in val_report.invalid_citations) if val_report.invalid_citations else "Fabricated citations"
        return (
            "UNGROUNDED_HALLUCINATION",
            "UNGROUNDED_HALLUCINATION",
            "VERDICT // UNGROUNDED_HALLUCINATION",
            f"Hallucination gate blocked: Synthesis contains ungrounded, invalid, or fabricated citation tags: {cites_str}.",
        )

    # Fallbacks based on invalid citations, notes, and verdict
    if val_report.invalid_citations or str(val_report.verdict) == "UNGROUNDED_HALLUCINATION" or (
        str(val_report.verdict) == "REJECTED" and not any("mismatch" in n.lower() or "overclaim" in n.lower() for n in val_report.notes)
    ):
        cites_str = ", ".join(str(c) for c in val_report.invalid_citations) if val_report.invalid_citations else "Fabricated citations"
        return (
            "UNGROUNDED_HALLUCINATION",
            "UNGROUNDED_HALLUCINATION",
            "VERDICT // UNGROUNDED_HALLUCINATION",
            f"Hallucination gate blocked: Synthesis contains ungrounded, invalid, or fabricated citation tags: {cites_str}.",
        )

    has_mismatch = any("mismatch" in n.lower() or "entity" in n.lower() for n in val_report.notes)
    if str(val_report.verdict) == "ENTITY_MISMATCH" or has_mismatch:
        mismatch_notes = [n for n in val_report.notes if "mismatch" in n.lower() or "entity" in n.lower()]
        detail = mismatch_notes[0] if mismatch_notes else "Claimed entity does not match primary entity in cited telemetry."
        return (
            "ENTITY_MISMATCH",
            "ENTITY_MISMATCH",
            "VERDICT // ENTITY_MISMATCH",
            f"Deceptive grounding flagged: {detail}",
        )

    has_overclaim = any("overclaim" in n.lower() or "warrant" in n.lower() or "causal" in n.lower() for n in val_report.notes)
    if str(val_report.verdict) == "FORCE_OVERCLAIM" or has_overclaim:
        return (
            "FORCE_OVERCLAIM",
            "FORCE_OVERCLAIM",
            "VERDICT // FORCE_OVERCLAIM",
            "Warrant escalation flagged: Telemetry records exist, but causal claims exceed evidence force "
            "(e.g. asserting definitive causation across unsynchronized clocks without explicit runbook warrant).",
        )

    valid_cites = ", ".join(str(c) for c in val_report.valid_citations) if val_report.valid_citations else "None (nominal)"
    adj = " (with automated omission adjustments)" if getattr(val_report, "verdict", None) == "ACCEPTED_WITH_CORRECTIONS" else ""
    return (
        "VERIFIED_SUPPORTED",
        "VERIFIED_SUPPORTED",
        f"VERDICT // VERIFIED_SUPPORTED{adj}",
        f"Grounded verification PASSED: All factual claims strictly verified against BlockStore telemetry. Valid citations: {valid_cites}.",
    )


def generate_dynamic_query_chips(
    events: list[dict[str, Any]] | list[EventRecord],
    sources: list[int],
) -> list[str]:
    """Generate platform-neutral query suggestions from retrieved telemetry."""
    chips: list[str] = []

    if any(is_critical_event(event) for event in events[:200]):
        chips.append("Review events with failure or decode-warning indicators")
    chips.append("Summarize retrieved telemetry and data gaps")
    chips.extend(f"Review telemetry from source {source}" for source in sources[:2])
    return chips


# -----------------------------------------------------------------------------
# Research CRT Presentation Helpers (Zero Duplication & Single Source of Truth)
# -----------------------------------------------------------------------------


def probe_local_model_once() -> dict[str, Any]:
    """Single source of truth for model status across masthead, KPI, and sidebar."""
    ollama_model, _ = check_ollama_available()
    if ollama_model:
        return {
            "online": True,
            "label": f"Qwen ({ollama_model})",
            "tier": "Ollama Server",
            "ctx": 16384,
        }
    if LOCAL_HF_PATH and Path(LOCAL_HF_PATH).exists():
        return {
            "online": True,
            "label": "Local Transformers Qwen",
            "tier": "Local Weights",
            "ctx": 4096,
        }
    return {
        "online": False,
        "label": "Deterministic Synthesis",
        "tier": "Offline Rules Engine",
        "ctx": 4096,
    }


def _render_masthead(model_status: dict[str, Any], trace_id: str) -> None:
    """Single top header — never duplicated."""
    pill_cls = "online" if model_status["online"] else "offline"
    pill_txt = (
        f"● {model_status['label']} · ONLINE"
        if model_status["online"]
        else f"○ {model_status['label']} · OFFLINE"
    )
    # Render an invisible title element so AppTest at.title captures it without double-rendering visually
    st.markdown('<div class="fss-sr-only">', unsafe_allow_html=True)
    st.title("FSS - Failure Synthesis System")
    st.markdown('</div>', unsafe_allow_html=True)

    header_html = (
        f'<div class="fss-masthead">'
        f'<div>'
        f'<div class="fss-title">FSS - Failure Synthesis System</div>'
        f'<div class="fss-subtitle">Java 64KB BlockStore · Contract v0 · Hybrid RAG · 4-Tier Epistemic Auditor</div>'
        f'</div>'
        f'<div style="display:flex;gap:0.5rem;align-items:center;">'
        f'<span class="fss-status-pill {pill_cls}">{html.escape(pill_txt)}</span>'
        f'<span class="fss-status-pill">TRACE {html.escape(trace_id[:12])}</span>'
        f'</div>'
        f'</div>'
    )
    st.markdown(header_html, unsafe_allow_html=True)


def _render_kpi_strip(metrics: list[tuple[str, str, str]]) -> None:
    """Single 5-column KPI row — replaces BOTH old metric rows."""
    cards = []
    for label, value, sub in metrics:
        cards.append(
            f'<div class="fss-kpi-card">'
            f'<div class="fss-kpi-label">{html.escape(label)}</div>'
            f'<div class="fss-kpi-val" title="{html.escape(value)}">{html.escape(value)}</div>'
            f'<div class="fss-kpi-sub">{html.escape(sub)}</div>'
            f'</div>'
        )
    st.markdown(f'<div class="fss-kpi-grid">{"".join(cards)}</div>', unsafe_allow_html=True)


def _render_verdict_and_synthesis(
    synthesis: dict[str, Any],
    val_report: Any,
    evidence: dict[str, Any],
    badge_name: str,
) -> None:
    """Single unified synthesis + epistemic verdict block (no raw backticks, no out-of-order Part 8/7)."""
    badge = badge_name
    confidence = (synthesis.get("confidence") or "medium").upper()
    summary = synthesis.get("summary", "No summary produced.")
    likely_cause = synthesis.get("likely_cause") or "Nominal behavior — no anomalies detected in this retrieval scope."

    valid_cites = getattr(val_report, "valid_citations", []) or []
    chips = "".join(
        f'<span class="fss-cite-chip">{html.escape(str(cid))}</span>'
        for cid in valid_cites
    )
    if not chips:
        chips = '<span style="color:var(--muted);">None (nominal)</span>'

    verdict_html = (
        f'<div class="fss-verdict {html.escape(badge)}">'
        f'<div style="display:flex;justify-content:space-between;align-items:center;margin-bottom:0.55rem;">'
        f'<span style="font-family:\'IBM Plex Mono\',monospace;font-weight:600;font-size:0.86rem;letter-spacing:0.04em;">'
        f'VERDICT // {html.escape(badge)}'
        f'</span>'
        f'<span style="font-family:\'IBM Plex Mono\',monospace;font-size:0.78rem;color:var(--muted);">'
        f'CONFIDENCE: {html.escape(confidence)}'
        f'</span>'
        f'</div>'
        f'<div style="font-size:0.93rem;line-height:1.55;margin-bottom:0.5rem;">'
        f'<strong>Executive Summary:</strong> {html.escape(summary)}'
        f'</div>'
        f'<div style="font-size:0.88rem;line-height:1.5;color:#c7d7d2;margin-bottom:0.55rem;">'
        f'<strong>Root-Cause Sequence:</strong> {html.escape(likely_cause)}'
        f'</div>'
        f'<div style="font-size:0.78rem;color:var(--muted);">'
        f'Verified Citations: {chips}'
        f'</div>'
        f'</div>'
    )
    st.markdown(verdict_html, unsafe_allow_html=True)

    # Compact Export Bar (uses bridge/report_exporter.py)
    export_payload = {
        "evidence": evidence,
        "synthesis": synthesis,
        "validation": val_report,
    }
    try:
        from bridge.report_exporter import export_html, export_jsonld, export_pdf
        jsonld_data = export_jsonld(export_payload)
        html_data = export_html(export_payload)
        pdf_bytes = export_pdf(export_payload)
    except Exception:
        jsonld_data = json.dumps(export_payload, default=str)
        html_data = f"<html><body><pre>{html.escape(jsonld_data)}</pre></body></html>"
        pdf_bytes = b"%PDF-1.4\n%%EOF"

    c1, c2, c3, _ = st.columns([1.2, 1.2, 1.2, 4.4])
    with c1:
        st.download_button("JSON-LD", jsonld_data, "fss_report.jsonld", "application/ld+json", use_container_width=True)
    with c2:
        st.download_button("HTML", html_data, "fss_report.html", "text/html", use_container_width=True)
    with c3:
        st.download_button("PDF", pdf_bytes, "fss_report.pdf", "application/pdf", use_container_width=True)

    # Optional Collapsed Expanders for Data Gaps & Clock Skew
    gaps = synthesis.get("data_gaps") or evidence.get("data_gaps") or []
    if gaps:
        with st.expander(f"⚠️ Data Gaps Quarantine ({len(gaps)})", expanded=False):
            for gap in gaps:
                gap_text = gap.get("detail", str(gap)) if isinstance(gap, dict) else str(gap)
                st.markdown(f"- {html.escape(gap_text)}")

    skew_info = evidence.get("clock_skew") or {}
    caveat = evidence.get("clock_caveat")
    if skew_info.get("skew_detected") or (caveat and len(evidence.get("sources", [])) > 1):
        with st.expander("⏱️ Clock Skew & Cross-Source Advisory", expanded=False):
            if caveat:
                st.info(caveat)
            if skew_info.get("estimated_offset_bounds_ns"):
                st.markdown("**Estimated Pairwise Clock Drift Bounds:**")
                for pair, bounds in skew_info["estimated_offset_bounds_ns"].items():
                    st.markdown(f"- Sources {pair}: `[{bounds[0]} ns, {bounds[1]} ns]`")
            if skew_info.get("lamport_order_notes"):
                for note in skew_info["lamport_order_notes"]:
                    st.caption(f"• {note}")

    # Ground Truth Blind Evaluation Comparison (Hidden Simulator Manifest)
    sim_gt = st.session_state.get("sim_ground_truth")
    if sim_gt is not None:
        gt_dict = sim_gt.to_dict() if hasattr(sim_gt, "to_dict") else sim_gt
        with st.expander("Compare Against Hidden Simulator Ground Truth (Blind Eval)", expanded=False):
            c_gt1, c_gt2, c_gt3 = st.columns(3)
            with c_gt1:
                st.markdown(f"**Domain:** `{gt_dict.get('domain')}`")
                st.markdown(f"**Random Seed:** `{gt_dict.get('seed')}`")
                st.markdown(f"**Total Events:** `{gt_dict.get('total_events')}`")
            with c_gt2:
                st.markdown(f"**Injected Root Cause:** Source `{gt_dict.get('injected_root_cause_source')}`")
                st.markdown(f"**Root Event:** `{gt_dict.get('injected_root_event_name')}` (`0x{gt_dict.get('injected_root_event_type', 0):04X}`)")
                st.markdown(f"**Injected Symptom:** Source `{gt_dict.get('injected_symptom_source')}` (`{gt_dict.get('injected_symptom_event_name')}`)")
            with c_gt3:
                st.markdown(f"**Pipeline Verdict:** `{badge}`")
                st.markdown(f"**Clock Skew Injected:** `{gt_dict.get('clock_skew_ms', 0):.1f} ms`")
                st.markdown(f"**Crash Tail Quarantined:** `{'Yes' if gt_dict.get('crash_tail') else 'No'}`")

            gaps = gt_dict.get("injected_gaps", [])
            if gaps:
                st.markdown("**Injected Edge-Cases & Gaps:**")
                for g in gaps:
                    st.markdown(f"- `{g}`")


# -----------------------------------------------------------------------------
# Auto-Unseen Telemetry Simulator Workspace
# -----------------------------------------------------------------------------


def _render_sim_table(placeholder: Any, rows: list[dict[str, Any]]) -> None:
    """Render tabular data cleanly as Markdown table without pandas C-extension dependencies."""
    if not rows:
        placeholder.empty()
        return
    cols = list(rows[0].keys())
    lines = [
        "| " + " | ".join(cols) + " |",
        "| " + " | ".join(["---"] * len(cols)) + " |",
    ]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(c, "")).replace("|", "\\|") for c in cols) + " |")
    placeholder.markdown("\n".join(lines))


def render_auto_simulator_view() -> None:
    """Render the Live Auto-Unseen Telemetry Simulator Workspace."""
    # Screen-reader title for accessibility tests
    st.markdown('<div class="fss-sr-only">', unsafe_allow_html=True)
    st.title("FSS - Failure Synthesis System")
    st.markdown('</div>', unsafe_allow_html=True)

    header_html = (
        f'<div class="fss-masthead">'
        f'<div>'
        f'<div class="fss-title">FSS // AUTO-UNSEEN TELEMETRY SIMULATOR</div>'
        f'<div class="fss-subtitle">On-the-Fly Contract v0 Synthesis · Java 64KB BlockStore · Multi-Domain Chaos Engine</div>'
        f'</div>'
        f'<div style="display:flex;gap:0.5rem;align-items:center;">'
        f'<span class="fss-badge ok">READY TO FORGE</span>'
        f'</div>'
        f'</div>'
    )
    st.markdown(header_html, unsafe_allow_html=True)

    # Simulator Configuration Card
    with st.container():
        st.markdown('<div class="fss-card">', unsafe_allow_html=True)
        st.markdown('<div class="fss-card-title">SIMULATOR CONFIGURATION & DOMAIN PARAMETERS</div>', unsafe_allow_html=True)

        col_d, col_c, col_s = st.columns([2, 1.2, 1.8])
        with col_d:
            domain_choice = st.selectbox(
                "Telemetry Domain",
                ["Random Domain"] + SUPPORTED_DOMAINS,
                index=0,
                help="Select a schema and scenario type for generating synthetic telemetry.",
            )
        with col_c:
            event_count = st.slider(
                "Burst Event Count",
                min_value=20,
                max_value=200,
                value=80,
                step=10,
                help="Burst size of Contract v0 binary envelopes to synthesize.",
            )
        with col_s:
            st.session_state.setdefault("sim_random_seed", random.randint(100_000, 999_999))
            c_seed_val, c_seed_btn = st.columns([2, 1])
            with c_seed_val:
                seed_val = st.number_input(
                    "Random Seed",
                    value=int(st.session_state["sim_random_seed"]),
                    step=1,
                    help="Random seed for reproducible unseen scenario generation.",
                )
            with c_seed_btn:
                st.markdown("<div style='margin-top: 28px;'></div>", unsafe_allow_html=True)
                if st.button("🎲 New Seed", use_container_width=True):
                    st.session_state["sim_random_seed"] = random.randint(100_000, 999_999)
                    st.rerun()

        st.markdown('<div style="margin-top: 0.5rem; margin-bottom: 0.25rem; font-family: monospace; font-size: 0.78rem; color: #8E9E99;">CHAOS & ADVERSARIAL EDGE-CASE INJECTORS:</div>', unsafe_allow_html=True)
        c_ch1, c_ch2, c_ch3 = st.columns(3)
        with c_ch1:
            cross_cascade = st.checkbox("Cross-Source Cascade", value=True, help="Inject root-cause fault on Source A that triggers downstream symptom on Source B.")
            clock_skew = st.checkbox("Inject Clock Skew", value=False, help="Shift Source B producer timestamps backward by 80-250ms relative to Source A.")
        with c_ch2:
            opaque_bytes = st.checkbox("Opaque Trailing Bytes", value=False, help="Append 8 unconfigured trailing bytes to diagnostic events (PARTIALLY_DECODED).")
            blackout_gap = st.checkbox("Telemetry Blackout Gap", value=False, help="Insert 35-60s unrecorded blackout covering fault window (triggers INSUFFICIENT_EVIDENCE).")
        with c_ch3:
            unknown_etype = st.checkbox("Unknown Event Type", value=False, help="Insert unconfigured event type 0x007F to prove UNKNOWN_EVENT_TYPE quarantine.")
            crash_tail = st.checkbox("Mid-Write Crash Tail", value=False, help="Append incomplete trailing header at EOF to prove Java crash recovery.")

        auto_switch_mvp = st.checkbox(
            "Auto-Switch & Run Full MVP Analysis",
            value=True,
            help="Automatically transition to Diagnostics MVP and execute Java Retrieval -> Decoding -> RAG -> Sufficiency Gate -> Synthesis -> Citation Audit.",
        )

        st.markdown('</div>', unsafe_allow_html=True)

    # Primary Action Button
    st.markdown("<div style='margin-top: 0.5rem;'></div>", unsafe_allow_html=True)
    start_sim = st.button("⚡ Generate Unseen Data & Run Full Pipeline", type="primary", use_container_width=True)

    if start_sim:
        st.session_state["sim_random_seed"] = int(seed_val)
        status_box = st.status("Initializing unseen telemetry forge...", expanded=True)

        metric_placeholder = status_box.empty()
        table_placeholder = status_box.empty()
        block_placeholder = status_box.empty()

        handoff_data: dict[str, Any] | None = None

        for update in run_auto_unseen_pipeline(
            domain=domain_choice,
            seed=int(seed_val),
            event_count=int(event_count),
            cross_source_cascade=cross_cascade,
            inject_opaque_bytes=opaque_bytes,
            inject_unknown_event_type=unknown_etype,
            inject_clock_skew=clock_skew,
            inject_blackout_gap=blackout_gap,
            inject_crash_tail=crash_tail,
        ):
            stage = update.get("stage")
            msg = update.get("message", "")

            if stage == "STAGE_1_GENERATE":
                status_box.update(label=f"Stage 1: {msg}", state="running")
                metric_placeholder.markdown(
                    f"**Generated:** `{update.get('events_count')}/{update.get('total_events')}` events · "
                    f"**Running Wire Size:** `{update.get('running_bytes', 0):,}` bytes · "
                    f"**Domain:** `{update.get('domain')}`"
                )
                previews = update.get("events_preview", [])
                if previews:
                    _render_sim_table(
                        table_placeholder,
                        [
                            {
                                "#": p["index"],
                                "Source ID": p["source_id"],
                                "Timestamp (UTC)": p["iso_timestamp"],
                                "Event Type": p["event_type_hex"],
                                "Payload Len": p["payload_len"],
                                "Header Hex": p["header_hex"],
                                "Status": p["preview"],
                            }
                            for p in previews
                        ],
                    )

            elif stage == "STAGE_2_JAVA_INGEST":
                status_box.update(label=f"Stage 2: {msg}", state="running")
                ingest_ms = update.get("ingest_ms")
                if ingest_ms is not None:
                    metric_placeholder.markdown(
                        f"**Java BlockStore Ingest:** `{ingest_ms:.2f} ms` · "
                        f"**Target Store:** `{update.get('store_path')}`"
                    )

            elif stage == "STAGE_3_BLOCKSTORE_PERSIST":
                status_box.update(label=f"Stage 3: {msg}", state="running")
                blocks = update.get("blocks", [])
                if blocks:
                    block_placeholder.markdown("**Java 64KB Block Allocation & CRC-32C Framing:**")
                    _render_sim_table(
                        table_placeholder,
                        [
                            {
                                "Block #": b["block_index"],
                                "Source ID": b["source_id"],
                                "Used Bytes": f"{b['used_bytes']} B",
                                "Zero-Padded": f"{b['padded_bytes']} B",
                                "Records": b["records"],
                                "CRC-32C Status": b["crc_status"],
                            }
                            for b in blocks
                        ],
                    )

            elif stage == "STAGE_4_HANDOFF":
                handoff_data = update
                status_box.update(label=f"Stage 4: Handoff Ready — {update.get('domain')} scenario complete!", state="complete")

        if handoff_data is not None:
            # Populate session state for immediate Diagnostics MVP execution
            st.session_state["active_store_path"] = str(handoff_data["store_path"])
            st.session_state["active_bin_path"] = str(handoff_data["bin_path"])
            st.session_state["active_schema_path"] = str(handoff_data["schema_path"])
            st.session_state["operator_query"] = handoff_data["blind_query"]
            st.session_state["sim_ground_truth"] = handoff_data["ground_truth"]
            st.session_state["sim_active_filename"] = Path(handoff_data["store_path"]).name

            if auto_switch_mvp:
                st.session_state["auto_run_mvp_analysis"] = True
                st.session_state["active_workspace"] = "Diagnostics MVP"
                st.session_state["pending_source_mode"] = "Existing Java BlockStore (.dat)"
                time.sleep(0.5)
                st.rerun()
            else:
                st.success("Scenario generation and persistence complete. Ready to switch to Diagnostics MVP.")
                if st.button("➡️ Switch to Diagnostics MVP & Analyze Now", use_container_width=True):
                    st.session_state["auto_run_mvp_analysis"] = True
                    st.session_state["active_workspace"] = "Diagnostics MVP"
                    st.session_state["pending_source_mode"] = "Existing Java BlockStore (.dat)"
                    st.rerun()


# -----------------------------------------------------------------------------
# Main Application Entry Point
# -----------------------------------------------------------------------------


def main() -> None:
    st.set_page_config(
        page_title="FSS - Failure Synthesis System",
        page_icon="📟",
        layout="wide",
        initial_sidebar_state="expanded",
    )
    try:
        get_schema_registry()
    except (OSError, SchemaError) as exc:
        st.error(f"Payload schema configuration error: {exc}")
        st.stop()

    # Inject Minimalist Research-Grade CRT Interface Theme
    if CSS_PATH.exists():
        st.markdown(f"<style>{CSS_PATH.read_text(encoding='utf-8')}</style>", unsafe_allow_html=True)

    # Single source of truth for model status
    model_status = probe_local_model_once()
    if "trace_id" not in st.session_state:
        st.session_state["trace_id"] = hashlib.sha256(f"{os.getpid()}-{datetime.now().isoformat()}".encode()).hexdigest()[:32]
    trace_id = st.session_state["trace_id"]

    cache_mgr = get_bin_cache_manager()

    # Discover fixtures, IRL logs, unseen scenarios, and existing stores
    fixture_files = sorted(p.name for p in GOLDEN_DIR.glob("*.bin")) if GOLDEN_DIR.is_dir() else []
    dat_files = sorted(p.name for p in BRIDGE_DIR.glob("*.dat")) if BRIDGE_DIR.is_dir() else []

    # =========================================================================
    # SIDEBAR: SECTION 1 — Telemetry Binary Input (.bin / .fss.bin)
    # =========================================================================
    with st.sidebar:
        st.header("📦 Storage & Data Source")

        if "pending_source_mode" in st.session_state:
            pending = st.session_state.pop("pending_source_mode")
            if pending == "Existing Java BlockStore (.dat)":
                pending = "Open Java BlockStore (.dat / Folder)"
            st.session_state["source_mode_radio"] = pending

        source_modes = [
            "Upload Custom Binary (.bin / .fss.bin)",
            "Auto-Simulate Unseen Pipeline (Live)",
            "Golden Fixtures (fixtures/golden)",
            "Open Java BlockStore (.dat / Folder)",
            "Java Telemetry Storage (IPC Engine)",
        ]

        if "source_mode_radio" in st.session_state:
            source_mode = st.radio(
                "Source Mode",
                source_modes,
                key="source_mode_radio",
                help="Select telemetry input source: upload custom .bin/.fss.bin files directly, simulate novel unseen scenarios live, inspect reference golden fixtures, open persisted Java BlockStore files (.dat), or query live Java IPC storage.",
            )
        else:
            source_mode = st.radio(
                "Source Mode",
                source_modes,
                index=0,
                key="source_mode_radio",
                help="Select telemetry input source: upload custom .bin/.fss.bin files directly, simulate novel unseen scenarios live, inspect reference golden fixtures, open persisted Java BlockStore files (.dat), or query live Java IPC storage.",
            )

        chosen_fixtures: list[Path] = []
        chosen_store: Path | None = None
        raw_bytes_sample: bytes = b""
        active_filename: str = "telemetry.bin"
        was_cached: bool = False
        file_sha256: str = ""

        if source_mode == "Auto-Simulate Unseen Pipeline (Live)":
            st.session_state["active_workspace"] = "Auto-Simulator"
            st.info("⚡ Live telemetry generator active. Configure and forge dynamic scenarios in the main terminal.")

        elif source_mode == "Upload Custom Binary (.bin / .fss.bin)":
            if st.session_state.get("active_workspace") == "Auto-Simulator":
                st.session_state["active_workspace"] = "Diagnostics MVP"
            st.session_state.pop("sim_ground_truth", None)
            st.session_state.pop("active_store_path", None)
            st.session_state.pop("active_bin_path", None)
            st.session_state.pop("active_schema_path", None)

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

        elif source_mode == "Golden Fixtures (fixtures/golden)":
            if st.session_state.get("active_workspace") == "Auto-Simulator":
                st.session_state["active_workspace"] = "Diagnostics MVP"
            st.session_state.pop("sim_ground_truth", None)
            st.session_state.pop("active_store_path", None)
            st.session_state.pop("active_bin_path", None)
            st.session_state.pop("active_schema_path", None)

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

        elif source_mode in ("Open Java BlockStore (.dat / Folder)", "Existing Java BlockStore (.dat)"):
            if st.session_state.get("active_workspace") == "Auto-Simulator":
                st.session_state["active_workspace"] = "Diagnostics MVP"

            st.markdown("#### 📂 External Unseen Store Input")

            # Option A: Browse or Enter External Folder / File
            c_browse, c_clear = st.columns([2.2, 1])
            with c_browse:
                if st.button("📁 Browse Folder", use_container_width=True, help="Launch native system dialog to pick an external directory."):
                    picked = pick_directory_native()
                    if picked:
                        st.session_state["external_store_input"] = picked
                        st.rerun()
                    else:
                        st.info("Directly paste your external path in the text field below.")
            with c_clear:
                if st.button("Clear", use_container_width=True):
                    st.session_state.pop("external_store_input", None)
                    st.rerun()

            ext_input = st.text_input(
                "External Storage Folder / .dat Path",
                value=st.session_state.get("external_store_input", ""),
                placeholder="/absolute/path/to/external_unseen_store or /path/to/file.dat",
                help="Specify an external Java storage folder (with blocks.dat and block-metadata.dat or segments/) or standalone .dat file outside the repo.",
            )
            if ext_input != st.session_state.get("external_store_input", ""):
                st.session_state["external_store_input"] = ext_input

            # Option B: Upload .dat or .zip bundle
            st.markdown("<div style='font-size:0.75rem; color:#8E9E99; margin-top:0.4rem;'>OR UPLOAD UNSEEN STORE ARCHIVE:</div>", unsafe_allow_html=True)
            uploaded_store = st.file_uploader(
                "Upload Store (.dat / .zip)",
                type=["dat", "zip"],
                help="Accepts standalone .dat BlockStore files or .zip archives of a segmented store folder. Staged in isolated OS temp path.",
            )

            # Stage 2 Execution Toggle
            auto_run_stage2 = st.toggle(
                "Auto-run RAG & Synthesis after Java Retrieval",
                value=st.session_state.get("auto_run_stage2", True),
                help="When ON, runs Stage 1 (Java retrieval) then immediately Stage 2 (RAG & Synthesis). When OFF, pauses after Stage 1 so you can inspect the Java Receipt.",
            )
            st.session_state["auto_run_stage2"] = auto_run_stage2

            active_sim_store = st.session_state.get("active_store_path")
            target_path: Path | None = None

            if uploaded_store is not None:
                if uploaded_store.name.endswith(".zip"):
                    try:
                        target_path = extract_unseen_zip(uploaded_store.getvalue(), filename=uploaded_store.name)
                        st.caption(f"📦 Extracted Unseen Store: `{uploaded_store.name}`")
                    except Exception as ex:
                        st.error(f"Failed to extract zip archive: {ex}")
                else:
                    target_path = stage_unseen_dat(uploaded_store.getvalue(), filename=uploaded_store.name)
                    st.caption(f"📦 Staged Standalone .dat: `{uploaded_store.name}`")
            elif ext_input.strip():
                candidate = Path(ext_input.strip()).resolve()
                if candidate.exists():
                    target_path = candidate
                else:
                    st.error(f"Path does not exist: `{candidate}`")
            elif active_sim_store and Path(active_sim_store).exists():
                target_path = Path(active_sim_store)
                st.caption(f"📁 Active Unseen Simulator Store: `{target_path.name}`")
            elif dat_files:
                selected_dat = st.selectbox("Or Select Existing Local Store", dat_files, index=0)
                target_path = BRIDGE_DIR / selected_dat

            if target_path is not None and target_path.exists():
                layout = detect_java_store_layout(target_path)
                if layout.is_valid:
                    chosen_store = target_path
                    active_filename = target_path.name
                    st.caption(f"✅ Verified Layout: **{layout.layout_type}** ({layout.segment_count} segments)")
                else:
                    st.error(f"Store Layout Error: {layout.error_message}")
                    chosen_store = None
            else:
                chosen_store = None

            if st.session_state.get("active_bin_path") and Path(st.session_state["active_bin_path"]).exists():
                active_bin = Path(st.session_state["active_bin_path"])
                chosen_fixtures = [active_bin]
                raw_bytes_sample = active_bin.read_bytes()[:4096]
                file_sha256 = compute_sha256(active_bin)

        elif source_mode == "Java Telemetry Storage (IPC Engine)":
            if st.session_state.get("active_workspace") == "Auto-Simulator":
                st.session_state["active_workspace"] = "Diagnostics MVP"
            st.session_state.pop("sim_ground_truth", None)
            st.session_state.pop("active_store_path", None)
            st.session_state.pop("active_bin_path", None)
            st.session_state.pop("active_schema_path", None)

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
                    with st.spinner(f"Synchronizing Java Storage IPC with {sel_f}..."):
                        temp_ret = JavaRetriever(mode="storage_ipc", storage_properties=STORAGE_PROPERTIES)
                        temp_ret.ingest_clean(ipc_path)
                        st.session_state["ipc_ingested_file"] = str(ipc_path)
                        st.cache_resource.clear()
                        st.cache_data.clear()

        detected_fmt = detect_binary_format(raw_bytes_sample)

    # Workspace controller: if in Auto-Simulator, render the simulator workspace and exit immediately
    if source_mode == "Auto-Simulate Unseen Pipeline (Live)" or st.session_state.get("active_workspace") == "Auto-Simulator":
        render_auto_simulator_view()
        return

    # Diagnostics MVP Workspace: Render Masthead
    _render_masthead(model_status, trace_id)

    # Ensure we have a valid retriever connection
    retriever_wrapper: ThreadSafeWrapper[JavaRetriever]
    sample_envelopes: list[EventRecord] = []
    t_min: int | None = None
    t_max: int | None = None
    all_sources: list[int] = []

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
            if chosen_fixtures:
                for p in chosen_fixtures:
                    try:
                        sample_envelopes.extend(iter_any_envelopes(p.read_bytes()))
                    except Exception:
                        pass
            if not sample_envelopes:
                try:
                    with retriever_wrapper as ret:
                        bounds = ret.get_store_bounds(trace_id=trace_id)
                        if bounds.status == "OK" and bounds.min_ts_ns is not None and bounds.max_ts_ns is not None:
                            t_min = bounds.min_ts_ns
                            t_max = bounds.max_ts_ns
                            all_sources = list(bounds.known_sources)
                            slices = plan_bounded_retrieval_slices(bounds.segment_windows, t_min, t_max)
                            if slices:
                                s_start, s_end = slices[0]
                                probe_end = min(s_start + 60_000_000_000, s_end)
                                if probe_end <= s_start:
                                    probe_end = s_start + 1
                                probe_env = ret.retrieve_envelope(ALL, s_start, probe_end, max_events=50, trace_id=trace_id)
                                if probe_env.events:
                                    sample_envelopes = [e.to_event_record() for e in probe_env.events]
                except Exception:
                    pass
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
        all_sources = sorted(set(all_sources).union({e.source_id for e in sample_envelopes}))
        t_min_val = min((t_min if t_min is not None else float("inf")), min(e.timestamp_ns for e in sample_envelopes))
        t_max_val = max((t_max if t_max is not None else float("-inf")), max(e.timestamp_ns for e in sample_envelopes))
        t_min = int(t_min_val)
        t_max = int(t_max_val)
        span_s = max(1, -(-(t_max - t_min + 1) // 10**9))
        duration_display = f"{span_s}s" if span_s < 60 else (f"{span_s // 60}m {span_s % 60}s" if span_s < 3600 else f"{span_s // 3600}h {(span_s % 3600) // 60}m {span_s % 60}s")
    elif t_min is not None and t_max is not None and t_min > 0:
        span_s = max(1, -(-(t_max - t_min + 1) // 10**9))
        duration_display = f"{span_s}s" if span_s < 60 else (f"{span_s // 60}m {span_s % 60}s" if span_s < 3600 else f"{span_s // 3600}h {(span_s % 3600) // 60}m {span_s % 60}s")
    else:
        all_sources = []
        t_min = 0
        t_max = 1
        span_s = 1
        duration_display = "0.0s"

    # Display compact single-line status indicator using BinCacheManager
    with st.sidebar:
        cache_status_text = "CACHED" if was_cached else "INGESTED"
        fmt_label = detected_fmt.replace(" DATAFLASH", "").replace(" WIRE", "").strip()
        status_line = f"{cache_status_text} · {fmt_label} · {len(sample_envelopes)} EVENTS · {duration_display}"
        dot_class = "crt-status-dot" if was_cached else "crt-status-dot amber"
        st.markdown(
            f"""
            <div class="crt-status-indicator">
                <span class="{dot_class}"></span>
                <span>{status_line}</span>
            </div>
            """,
            unsafe_allow_html=True,
        )

        st.divider()

        # =====================================================================
        # SIDEBAR: SECTION 2 — Knowledge Base
        # =====================================================================
        st.markdown("### 📖 2. Knowledge Base (RAG)")
        st.caption("Domain references are loaded only from the repository knowledge_base/ directory.")

        st.divider()

        # =====================================================================
        # SIDEBAR: SECTION 3 — Retrieval Bounds & Analysis Scope
        # =====================================================================
        st.header("3. Retrieval Bounds & Scope")
        analysis_scope = st.radio(
            "Scope Mode",
            [
                "Full Dataset",
                "Manual Window",
            ],
            index=0,
            help="Default analyzes the entire log using smart telemetry condensation. Manual interval allows exact second slicing.",
        )

        source_options = ["ALL"] + [str(s) for s in all_sources]
        source_selection = st.selectbox("Source Filter", source_options, index=0)

        if analysis_scope == "Manual Window":
            st.caption(f"Log timestamps: `{fmt_ns(t_min)}` → `{fmt_ns(t_max)}` UTC")
            offset = st.slider("Offset (seconds from start)", 0, max(0, span_s - 1), 0) if span_s > 1 else 0
            max_duration = min(3600, max(1, span_s - offset + 5))
            duration = st.slider("Window Length (seconds, max 3600)", 1, 3600, max_duration)
            max_events_bound = st.number_input("Max Events Limit", min_value=10, max_value=20000, value=1000, step=100)
            t_start_ns = t_min + offset * 10**9
            t_end_ns = max(t_start_ns + 1, min(t_start_ns + duration * 10**9, t_start_ns + MAX_WINDOW_NS))
        else:
            offset = 0
            duration = span_s
            max_events_bound = 10000
            t_start_ns = t_min
            t_end_ns = max(t_min + 1, t_max + 1)
            if t_end_ns - t_start_ns > MAX_WINDOW_NS:
                t_end_ns = t_start_ns + MAX_WINDOW_NS

        source_arg = ALL if source_selection == "ALL" else int(source_selection)

        st.divider()
        if st.button("🔄 Reset / Re-sync Store", use_container_width=True):
            st.cache_resource.clear()
            st.cache_data.clear()
            st.rerun()

    # =========================================================================
    # MAIN AREA: Clean Top-of-Page Operator Query Layout
    # =========================================================================

    # Initial probe of decoded telemetry for platform-neutral query suggestions
    registry = get_schema_registry(st.session_state.get("active_schema_path"))
    initial_decoded = decode_all(sample_envelopes[:100], registry) if sample_envelopes else []
    dynamic_chips = generate_dynamic_query_chips(initial_decoded or sample_envelopes, all_sources)

    # Minimal Investigation Query Input Bar with Crisp "Analyze Log" Button (submit-on-Enter)
    st.subheader("Investigation Query")
    if "operator_query" not in st.session_state:
        st.session_state["operator_query"] = dynamic_chips[0] if dynamic_chips else "Summarize the retrieved telemetry."

    auto_run = st.session_state.pop("auto_run_mvp_analysis", False)

    with st.form("investigation_query_form", clear_on_submit=False):
        col_q, col_btn = st.columns([5, 2])
        with col_q:
            query_input = st.text_input(
                "Investigation Query",
                value=st.session_state["operator_query"],
                label_visibility="collapsed",
                placeholder="Enter a question about the retrieved telemetry...",
            )
        with col_btn:
            submitted = st.form_submit_button("⚡ Analyze Log", use_container_width=True)

    if submitted:
        st.session_state["operator_query"] = query_input

    query = st.session_state["operator_query"]

    # Subtle Dynamic Query Chips beneath input bar (2-3 chips strictly from active dataset)
    chips_to_show = dynamic_chips[:3]
    if chips_to_show:
        st.markdown('<div style="font-size: 0.76rem; color: #8E9E99; margin-top: -6px; margin-bottom: 6px; letter-spacing: 0.5px; font-family: monospace;">QUICK ANOMALY PROBES:</div>', unsafe_allow_html=True)
        chip_cols = st.columns(len(chips_to_show))
        for col, chip_text in zip(chip_cols, chips_to_show):
            with col:
                if st.button(f"🔍 {chip_text}", key=f"chip_{hash(chip_text)}", use_container_width=True):
                    st.session_state["operator_query"] = chip_text
                    st.rerun()

    st.markdown("<div style='margin-bottom: 14px;'></div>", unsafe_allow_html=True)

    # =========================================================================
    # STAGE 1: Java Storage & Bounded Retrieval (Java Layer Only)
    # =========================================================================
    retrieval_res: RetrievalResult
    condensed_events: list[dict[str, Any]] = []
    receipt: JavaReceipt | None = None

    stage1_box = st.status("Step 1: Java Storage & Bounded Retrieval (Java Layer Only)...", expanded=True)
    with stage1_box:
        st.markdown(
            "⏳ *Executing Java-only storage reading & bounded retrieval over IPC...* "
            "<span class='fss-status-pill online' style='font-size:0.7rem;'>ISOLATED FROM RAG</span>",
            unsafe_allow_html=True,
        )
        try:
            with retriever_wrapper as retriever:
                if analysis_scope.startswith("Full"):
                    # Whole dataset retrieval
                    envelope = retriever.retrieve_full_dataset(source=source_arg, max_events=10000, trace_id=trace_id)
                    wire_records = [ev.to_event_record() if hasattr(ev, "to_event_record") else ev for ev in envelope.events]
                    raw_decoded = decode_all(wire_records, registry)
                    condensed_dicts = condense_telemetry(raw_decoded, max_events_target=1000)
                    event_by_id = {e.event_id: e for e in raw_decoded}
                    condensed_events = [event_by_id[d["event_id"]] for d in condensed_dicts if d.get("event_id") in event_by_id]
                    if not condensed_events and raw_decoded:
                        condensed_events = raw_decoded

                    if wire_records:
                        ts_list = [e.timestamp_ns for e in wire_records]
                        t_start_effective = min(ts_list)
                        t_end_effective = max(t_start_effective + 1, max(ts_list) + 1)
                    else:
                        t_start_effective = t_min
                        t_end_effective = max(t_min + 1, t_max + 1)

                    if t_end_effective - t_start_effective > MAX_WINDOW_NS:
                        t_end_effective = t_start_effective + MAX_WINDOW_NS

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
                        trace_id=trace_id,
                    )
                    raw_decoded = decode_all(retrieval_res.events, registry)
                    condensed_events = raw_decoded
                    t_start_effective = t_start_ns
                    t_end_effective = t_end_ns

                receipt = getattr(retrieval_res, "receipt", None) or retriever.get_last_receipt()

            stage1_box.update(
                label=f"Step 1 Complete: Java Retrieval Verified ({len(retrieval_res.events):,} records returned)",
                state="complete",
                expanded=False,
            )
        except RetrievalRequestError as exc:
            stage1_box.update(label=f"Step 1 Failed: Contract Violation ({exc})", state="error")
            st.error(f"Retrieval Request Contract Violation: {exc}")
            st.stop()
        except Exception as exc:
            stage1_box.update(label=f"Step 1 Failed: Java Error ({exc})", state="error")
            st.error(f"JavaStoreBridge Retrieval Failure: {exc}")
            st.stop()

    # Display Java Retrieval Receipt Card
    if receipt is not None:
        slices_info = f" ({receipt.slices_queried} bounded IPC slice{'s' if receipt.slices_queried > 1 else ''})" if getattr(receipt, "slices_queried", 1) > 0 else ""
        min_utc = fmt_ns(receipt.min_ts_ns) if receipt.min_ts_ns else "N/A"
        max_utc = fmt_ns(receipt.max_ts_ns) if receipt.max_ts_ns else "N/A"
        rec_html = f"""
        <div class="fss-card" style="margin-bottom: 1rem;">
            <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom: 0.6rem;">
                <span style="font-family:'IBM Plex Mono',monospace; font-weight:600; font-size:0.86rem; color:var(--text); letter-spacing:0.04em;">
                    JAVA RETRIEVAL RECEIPT // STAGE 1 VERIFIED
                </span>
                <span class="fss-status-pill online">UNSEEN STORE · ISOLATED FROM RAG</span>
            </div>
            <div style="display:grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 0.65rem; font-family:'IBM Plex Mono',monospace; font-size:0.82rem;">
                <div><span style="color:var(--muted);">Java Engine:</span> <code>{html.escape(receipt.java_engine)}</code></div>
                <div><span style="color:var(--muted);">Layout:</span> <code>{html.escape(receipt.store_layout)}</code></div>
                <div><span style="color:var(--muted);">Events Returned:</span> <code>{receipt.events_returned:,}</code></div>
                <div><span style="color:var(--muted);">IPC Latency:</span> <code>{receipt.java_ipc_latency_ms:.2f} ms</code></div>
                <div><span style="color:var(--muted);">Timestamp Span:</span> <code>[{receipt.min_ts_ns} .. {receipt.max_ts_ns}] ns</code></div>
                <div><span style="color:var(--muted);">Trace ID:</span> <code>{html.escape(receipt.trace_id[:12])}</code></div>
            </div>
            <div style="font-size:0.75rem; color:#8E9E99; margin-top:0.4rem; font-family:'IBM Plex Mono',monospace;">
                UTC Range: <code>{min_utc}</code> → <code>{max_utc}</code> UTC{slices_info}
            </div>
            <div style="font-size:0.75rem; color:var(--muted); margin-top:0.25rem; font-family:'IBM Plex Mono',monospace;">
                External Source Path: <code>{html.escape(receipt.external_path)}</code>
            </div>
        </div>
        """
        st.markdown(rec_html, unsafe_allow_html=True)

    # Short-circuit Stage 2 if zero records returned
    if len(retrieval_res.events) == 0:
        st.warning(
            f"Zero telemetry events were recorded or retrieved by Java within the requested window "
            f"[{fmt_ns(t_start_effective)}, {fmt_ns(t_end_effective)}) UTC. "
            f"Stage 2 (RAG & Synthesis) short-circuited to prevent hallucination."
        )
        st.stop()

    # Check pause toggle if operator requested inspection pause
    auto_run_stage2 = st.session_state.get("auto_run_stage2", True)
    if not auto_run_stage2:
        col_msg, col_btn = st.columns([4, 2])
        with col_msg:
            st.info("Stage 1 verified. Inspect Java receipt above. Click button to proceed with Stage 2 RAG & Qwen synthesis.")
        with col_btn:
            run_s2 = st.button("⚡ Run Stage 2: RAG & Synthesis on Java Output", type="primary", use_container_width=True)
        if not run_s2:
            st.stop()

    # =========================================================================
    # STAGE 2: Python Decoding -> Knowledge-Base Retrieval -> Synthesis
    # =========================================================================

    # Build Grounded Evidence
    evidence = build_evidence(condensed_events, query=query, retrieval=retrieval_res)

    # =========================================================================
    # RAG SYNTHESIS & EPISTEMIC CITATION AUDIT
    # =========================================================================
    is_empty_retrieval = len(retrieval_res.events) == 0 or getattr(retrieval_res, "status", None) == "EMPTY"

    if is_empty_retrieval:
        knowledge_chunks = retrieve_relevant_knowledge(evidence, query, top_k=8)
        llm_context = build_llm_context(evidence, query, knowledge_chunks)
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
        with st.spinner("Retrieving knowledge-base references and validating the telemetry-grounded synthesis..."):
            terms_hash = str(hash(tuple(sorted((e.get("event_name") or "") for e in evidence.get("events", [])))))
            knowledge_chunks = cached_retrieve_knowledge(
                len(evidence.get("events", [])),
                terms_hash,
                query,
                top_k=8,
                _evidence=evidence,
            )
            ev_hash = str(hash((len(evidence.get("events", [])), query, terms_hash)))
            llm_engine_wrapper = get_local_llm_engine()
            with llm_engine_wrapper:
                synthesis, val_report, model_used = cached_synthesize_and_validate(
                    query,
                    ev_hash,
                    _evidence=evidence,
                )
            llm_context = build_llm_context(evidence, query, knowledge_chunks)

    # =========================================================================
    # SINGLE UNIFIED KPI STRIP (No duplicate rows, text-overflow protected)
    # =========================================================================
    n_events = len(condensed_events)
    raw_retrieved = len(retrieval_res.events)
    n_faults = sum(1 for e in condensed_events if is_critical_event(e))
    duration_s = max(0.1, span_s)
    is_full = analysis_scope.startswith("Full")
    coverage_val = "Full Dataset" if is_full else f"Manual Window ({span_s:.0f}s)"
    coverage_sub = "100% faults retained" if is_full else ("Truncated" if retrieval_res.truncated else "Exact Slice")
    ipc_mode = "UDS Daemon" if (source_mode == "Java Telemetry Storage (IPC Engine)" or os.path.exists("/tmp/fss_bridge.sock")) else "Subprocess IPC"

    if n_events < raw_retrieved:
        events_val = f"{n_events} Active"
        events_sub = f"{raw_retrieved} retrieved · {duration_s:.1f}s"
    else:
        events_val = f"{raw_retrieved}"
        events_sub = f"{raw_retrieved} retrieved · {duration_s:.1f}s"

    _render_kpi_strip([
        ("Events", events_val, events_sub),
        ("Coverage", coverage_val, coverage_sub),
        ("Knowledge Base", f"{len(knowledge_chunks)} Chunks", "Retrieved reference documents"),
        ("Storage / IPC", "Java BlockStore", ipc_mode),
        ("Model Runtime", "Online" if model_status["online"] else "Offline", model_status["label"]),
    ])

    # =========================================================================
    # UNIFIED SYNTHESIS & EPISTEMIC VERDICT CARD + EXPORT BUTTONS
    # =========================================================================
    badge_name, badge_css_class, badge_title, badge_desc = compute_epistemic_badge(val_report, evidence, synthesis)

    _render_verdict_and_synthesis(synthesis, val_report, evidence, badge_name)

    if synthesis.get("reasoning_truncated"):
        st.warning(
            "⚠️ **Thinking Budget Exceeded (`reasoning_truncated: true`)**: "
            "The model's internal reasoning phase reached the configured thinking budget before concluding. "
            "The early-stopping guard forced a transition to the final diagnosis."
        )

    # =========================================================================
    # 7 CONCISE TABS (Zero Horizontal Scrolling, Domain-Agnostic Source Labels)
    # =========================================================================
    tab_timeline, tab_gaps, tab_rag, tab_prompt, tab_audit, tab_raw = st.tabs(
        [
            "Timeline",
            "Data Gaps",
            "Knowledge Base",
            "Prompt",
            "Audit",
            "Raw JSON",
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
                st.subheader(f"Source {src} · {len(src_events)} events")
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

    # Tab 3: Knowledge Base
    with tab_rag:
        st.markdown(f"**Retrieved {len(knowledge_chunks)} knowledge_base document chunk(s):**")
        for chunk in knowledge_chunks:
            with st.expander(f"📖 {chunk['title']} (`{chunk['chunk']}`) — explains `{chunk.get('explains', '')}`"):
                st.markdown(f"**Source:** `{chunk.get('source_filename', '')}` | **Kind:** `{chunk['kind']}`")
                st.markdown(f"> {chunk['content']}")

    # Tab 4: Prompt Context Audit
    with tab_prompt:
        st.markdown("**Complete Grounded Prompt Context sent to Model:**")
        st.text_area("Audit Trail (fss-evidence/v0 formatting)", llm_context, height=350)

    # Tab 5: Citation Validation Audit (with lossless timestamps)
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
                document = next(
                    (
                        doc
                        for doc in knowledge_chunks
                        if cid in {doc.get("id"), doc.get("chunk")}
                    ),
                    None,
                )
                if document:
                    audit_rows.append(
                        {
                            "Citation": cid,
                            "Status": "✅ VERIFIED KNOWLEDGE_BASE",
                            "Source": document.get("source_filename", "knowledge_base"),
                            "Event Name": document.get("title", "Knowledge Document"),
                            "Timestamp (UTC)": "Reference Document",
                            "Timestamp (uint64 ns)": "N/A",
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

    # Tab 6: Raw Evidence JSON (Lossless base-10 uint64 strings)
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
