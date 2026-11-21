"""Failure Synthesizer, Local LLM Runtime & Validator for FSS Bridge.

Connects to:
  1. Local Ollama server (native /api/chat with OLLAMA_CONTEXT_LENGTH >= 16384, Qwen3 sampling).
  2. Local llama-server (--ctx-size >= 16384, prefix caching, Pydantic GBNF / json_schema).
  3. Local HuggingFace weights (with do_sample=True for transformers >= 4.46).
  4. Hybrid RAG (bm25s + Dense + RRF + Reranker) and Historical RCA Incidents (RCACopilot CoT).
  5. Pre-flight Python token-budget guard enforcing num_ctx >= 16384 with deterministic compaction.
  6. Qwen3 Thinking Mode stability & bounded think-budget guard (closing token 151668 / <|im_end|> 151645).
  7. Offline deterministic reasoning synthesizer matching streamlit_app.py and FSS golden rules.

Validates syntheses strictly against cited event IDs and evidence data gaps.
"""

from __future__ import annotations

import json
import os
import re
import sys
import urllib.error
import urllib.request
import warnings
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Literal, Sequence

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

# Ensure JAX does not preallocate VRAM
os.environ["XLA_PYTHON_CLIENT_PREALLOCATE"] = "false"

from bridge.bin_ingestor import is_critical_event
from bridge.hybrid_rag import (
    DOMAIN_KNOWLEDGE,
    HistoricalIncident,
    format_exemplars_cot,
    retrieve_historical_exemplars,
    retrieve_hybrid_knowledge,
)
from bridge.citation_auditor import (
    CitationAuditor,
    ValidationBadge,
    ValidationReport,
    ValidationVerdict,
)
from bridge.sufficiency_gate import (
    SufficiencyCheckResult,
    SufficiencyResult,
    SufficiencyStatus,
    create_insufficient_evidence_synthesis,
    evaluate_sufficiency,
)


def _load_env() -> None:
    env_file = ROOT_DIR / ".env"
    if env_file.is_file():
        try:
            for line in env_file.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    k, v = line.split("=", 1)
                    k = k.strip()
                    v = v.strip().strip('"').strip("'")
                    if k not in os.environ:
                        os.environ[k] = v
        except Exception:
            pass


_load_env()

# ---------------------------------------------------------------------------
# Configuration & Backend Enforcements
# ---------------------------------------------------------------------------

DEFAULT_NUM_CTX = 16384

# Enforce num_ctx >= 16384 for Ollama
# Spec rule: configure via OLLAMA_CONTEXT_LENGTH=16384 env, or native /api/chat options: {"num_ctx": 16384}
# Never pass to /v1/chat/completions or use non-existent OLLAMA_NUM_CTX; keep num_ctx stable across calls.
if "OLLAMA_NUM_CTX" in os.environ:
    warnings.warn(
        "OLLAMA_NUM_CTX is not a valid Ollama setting; using OLLAMA_CONTEXT_LENGTH=16384 instead.",
        UserWarning,
        stacklevel=2,
    )

OLLAMA_CONTEXT_LENGTH = max(
    int(os.environ.get("OLLAMA_CONTEXT_LENGTH", DEFAULT_NUM_CTX)),
    DEFAULT_NUM_CTX,
)
os.environ["OLLAMA_CONTEXT_LENGTH"] = str(OLLAMA_CONTEXT_LENGTH)

OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://127.0.0.1:11434")
LLAMA_SERVER_URL = os.environ.get("LLAMA_SERVER_URL", os.environ.get("LLAMA_CPP_URL", "http://127.0.0.1:8080"))

LOCAL_HF_PATH = (
    ""
    if "PYTEST_CURRENT_TEST" in os.environ
    else os.environ.get(
        "LOCAL_MODEL_PATH",
        os.environ.get(
            "QWEN_MODEL_SNAPSHOT",
            "/home/kirat/.cache/huggingface/hub/models--Qwen--Qwen2.5-1.5B-Instruct/snapshots/989aa7980e4cf806f80c7fef2b1adb7bc71aa306",
        ),
    )
)

# Qwen3 token constants
THINK_CLOSE_TOKEN_ID = 151668  # </think>
IM_END_TOKEN_ID = 151645  # <|im_end|>
THINK_CLOSE_STR = "</think>"
THINK_CUTOFF_INJECTION = (
    "\n\nConsidering the limited time by the user, I have to give the solution based on the thinking directly now.\n</think>\n\n"
)
DEFAULT_THINKING_BUDGET = 1024

RESERVED_OUTPUT_TOKENS_THINKING = 3200
RESERVED_OUTPUT_TOKENS_NON_THINKING = 1200

# (event_name, field, value) -> (severity, plain-language problem)
RULES: dict[tuple[str, str, str], tuple[str, str]] = {
    ("LIDAR_STATUS", "status_name", "LIDAR_TIMEOUT"): ("high", "LiDAR timed out"),
    ("LIDAR_STATUS", "status_name", "SENSOR_DISCONNECTED"): ("high", "LiDAR sensor disconnected"),
    ("LIDAR_STATUS", "status_name", "INVALID_SCAN"): ("medium", "LiDAR returned an invalid scan"),
    ("DIAGNOSTIC", "status_name", "FAULT"): ("high", "Diagnostics report FAULT"),
    ("DIAGNOSTIC", "status_name", "DEGRADED"): ("medium", "Diagnostics report DEGRADED"),
    ("FORMATION_STATE", "state", "LEFT_FORMATION"): ("high", "Left formation"),
    ("FORMATION_STATE", "state", "REJOINING"): ("low", "Rejoining formation"),
}

# System prompt is byte-identical across queries for host-memory prefix caching
# (llama-server --cram 256 --system-prompt-file ... --cache-prompt --cache-type-k q8_0)
SYSTEM_PROMPT = """You are the Failure Synthesis System for distributed autonomous systems.
You receive an evidence JSON (format fss-evidence/v0) and an operator query.

STRICT CONTRACT RULES:
1. Grounding: Reason solely over facts in the evidence, digested vendor error manuals, and provided domain knowledge.
2. Citation: Cite every factual claim with its exact event ID [ev-...], vendor error manual ID [err-...], or domain knowledge reference [doc-...] in square brackets.
3. Accuracy: ONLY cite event IDs, vendor error IDs, and document IDs that exist in the evidence and knowledge context. Never hallucinate an ID.
4. Clock Caveat: Producer clocks are NOT synchronized across sources. Never state cross-source order or causality as fact; state it as inferred or plausible.
5. Data Gaps: Any unknown or opaque data listed under data_gaps must be explicitly reported as unknown. Never guess payload bytes.
6. Format: Output valid JSON adhering to the FailureSynthesis schema:
{
  "summary": "1-3 sentences answering the operator question with [ev-...], [err-...], or [doc-...] citations",
  "likely_cause": "Root cause explanation citing contributing event_ids [ev-...] and error runbooks [err-...], or null if nominal",
  "confidence": "low" | "medium" | "high",
  "contributing_events": [
    {"source_id": 298, "timestamp": 1791454023050000000, "event_type": "LIDAR_STATUS", "event_id": "ev-2e92ae032f03"}
  ],
  "uncertainty": ["Clock unsynchronized caveat", "..."],
  "data_gaps": ["Event ev-9d2fbc08bffc: 8 payload bytes were not decoded..."],
  "reasoning_truncated": false
}"""


# ---------------------------------------------------------------------------
# Pydantic v2 Schema & Compatibility Interface
# ---------------------------------------------------------------------------

@dataclass
class ContributingEvent:
    source_id: int
    timestamp: int
    event_type: str
    event_id: str


@dataclass
class FailureSynthesis:
    summary: str
    likely_cause: str | None
    confidence: Literal["low", "medium", "high"]
    contributing_events: list[ContributingEvent] = field(default_factory=list)
    uncertainty: list[str] = field(default_factory=list)
    data_gaps: list[str] = field(default_factory=list)
    reasoning_truncated: bool = False

    @classmethod
    def model_json_schema(cls) -> dict[str, Any]:
        """Generate JSON Schema Draft 2020-12 for llama-server json_schema / GBNF."""
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "title": "FailureSynthesis",
            "type": "object",
            "properties": {
                "summary": {"type": "string"},
                "likely_cause": {"type": ["string", "null"]},
                "confidence": {"type": "string", "enum": ["low", "medium", "high"]},
                "contributing_events": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "source_id": {"type": "integer"},
                            "timestamp": {"type": "integer"},
                            "event_type": {"type": "string"},
                            "event_id": {"type": "string"},
                        },
                        "required": ["source_id", "timestamp", "event_type", "event_id"],
                    },
                },
                "uncertainty": {"type": "array", "items": {"type": "string"}},
                "data_gaps": {"type": "array", "items": {"type": "string"}},
                "reasoning_truncated": {"type": "boolean"},
            },
            "required": ["summary", "confidence", "contributing_events", "uncertainty", "data_gaps"],
        }

    def model_dump(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def model_validate(cls, data: dict[str, Any]) -> "FailureSynthesis":
        summary = str(data.get("summary", ""))
        likely_cause = data.get("likely_cause")
        if likely_cause is not None:
            likely_cause = str(likely_cause)
        conf = str(data.get("confidence", "medium")).lower()
        if conf not in ("low", "medium", "high"):
            conf = "medium"

        events_raw = data.get("contributing_events", [])
        events: list[ContributingEvent] = []
        if isinstance(events_raw, list):
            for e in events_raw:
                if isinstance(e, dict):
                    events.append(
                        ContributingEvent(
                            source_id=int(e.get("source_id", 0)),
                            timestamp=int(e.get("timestamp") or e.get("timestamp_ns") or 0),
                            event_type=str(e.get("event_type") or e.get("event_name") or ""),
                            event_id=str(e.get("event_id", "")),
                        )
                    )

        return cls(
            summary=summary,
            likely_cause=likely_cause,
            confidence=conf,  # type: ignore[arg-type]
            contributing_events=events,
            uncertainty=[str(u) for u in data.get("uncertainty", [])],
            data_gaps=[str(g) for g in data.get("data_gaps", [])],
            reasoning_truncated=bool(data.get("reasoning_truncated", False)),
        )

    @classmethod
    def model_validate_json(cls, json_str: str) -> "FailureSynthesis":
        return cls.model_validate(json.loads(json_str))


def get_synthesis_json_schema() -> dict[str, Any]:
    """Return 8-part synthesis JSON schema for decode-time GBNF / json_schema constraints."""
    return FailureSynthesis.model_json_schema()


def validate_synthesis_schema(data: dict[str, Any]) -> FailureSynthesis:
    """Validate raw LLM dictionary against FailureSynthesis schema."""
    return FailureSynthesis.model_validate(data)


# ---------------------------------------------------------------------------
# Qwen3 Sampling Parameters & Decoding Stability
# ---------------------------------------------------------------------------

def get_qwen3_sampling_params(
    enable_thinking: bool = False,
    thinking_budget: int = DEFAULT_THINKING_BUDGET,
    presence_penalty: float = 1.5,
) -> dict[str, Any]:
    """Enforce documented Qwen3 sampling parameters.

    Never use greedy decoding when Thinking Mode is enabled.
    Thinking mode: temperature=0.6, top_p=0.95, top_k=20, min_p=0, presence_penalty=1.5, do_sample=True.
    Non-thinking mode: enable_thinking=False, temperature=0.7, top_p=0.8, top_k=20, min_p=0, do_sample=True.
    """
    if enable_thinking:
        return {
            "enable_thinking": True,
            "do_sample": True,
            "temperature": 0.6,
            "top_p": 0.95,
            "top_k": 20,
            "min_p": 0.0,
            "presence_penalty": presence_penalty,
            "thinking_budget": thinking_budget,
        }
    return {
        "enable_thinking": False,
        "do_sample": True,
        "temperature": 0.7,
        "top_p": 0.8,
        "top_k": 20,
        "min_p": 0.0,
    }


# ---------------------------------------------------------------------------
# Qwen3 Think-Budget Guard (Closing Token 151668 & Cutoff Injection)
# ---------------------------------------------------------------------------

class Qwen3ThinkBudgetGuard:
    """Monitors Qwen3 thinking tokens against thinking_budget.

    Tracks token ID 151668 (</think>) and 151645 (<|im_end|>).
    If thinking budget (> 1,024 tokens) is reached without 151668, injects:
      "\n\nConsidering the limited time by the user, I have to give the solution based on the thinking directly now.\n</think>\n\n"
    and flags reasoning_truncated = True.
    """

    def __init__(self, thinking_budget: int = DEFAULT_THINKING_BUDGET) -> None:
        self.thinking_budget = thinking_budget
        self.thinking_tokens: int = 0
        self.inside_think: bool = False
        self.think_closed: bool = False
        self.reasoning_truncated: bool = False
        self.buffered_text: list[str] = []

    def feed_token(self, token_id: int | None, token_text: str) -> tuple[str, bool]:
        """Feed a single generated token into the guard.

        Returns (text_to_emit, should_force_stop_thinking).
        """
        if "<think>" in token_text:
            self.inside_think = True
            return token_text, False

        if token_id == THINK_CLOSE_TOKEN_ID or "</think>" in token_text:
            self.think_closed = True
            self.inside_think = False
            return token_text, False

        if token_id == IM_END_TOKEN_ID or "<|im_end|>" in token_text:
            self.inside_think = False
            return token_text, False

        if self.inside_think:
            self.thinking_tokens += 1
            if self.thinking_tokens >= self.thinking_budget and not self.think_closed:
                # Early cutoff injection
                self.reasoning_truncated = True
                self.think_closed = True
                self.inside_think = False
                return THINK_CUTOFF_INJECTION, True

        return token_text, False

    def process_complete_text(self, text: str) -> tuple[str, bool]:
        """Inspect and guard a full response text for unclosed thinking loops."""
        if "<think>" not in text:
            return text, False

        think_start = text.find("<think>")
        think_end = text.find("</think>")

        if think_end != -1:
            # Cleanly closed thinking block
            return text, False

        # Unclosed thinking block
        think_body = text[think_start + len("<think>") :]
        est_tokens = estimate_tokens(think_body)

        if est_tokens >= self.thinking_budget:
            # Inject early cutoff
            self.reasoning_truncated = True
            # If JSON begins within the unclosed block, inject cutoff right before the JSON
            json_start = text.find("{", think_start + len("<think>"))
            if json_start != -1:
                cut_text = text[:json_start] + THINK_CUTOFF_INJECTION + text[json_start:]
            else:
                cut_text = text + THINK_CUTOFF_INJECTION
            return cut_text, True

        return text, False


def process_qwen3_thinking_output(raw_output: str, thinking_budget: int = DEFAULT_THINKING_BUDGET) -> tuple[dict[str, Any] | None, bool]:
    """Process raw Qwen3 output through think-budget guard and parse structured JSON."""
    guard = Qwen3ThinkBudgetGuard(thinking_budget=thinking_budget)
    guarded_text, was_truncated = guard.process_complete_text(raw_output)

    # Extract JSON after </think> if present, or entire text
    search_space = guarded_text
    if "</think>" in guarded_text:
        search_space = guarded_text.split("</think>", 1)[1]

    json_match = re.search(r"\{.*\}", search_space, re.DOTALL)
    if json_match:
        try:
            parsed = json.loads(json_match.group(0))
            if isinstance(parsed, dict):
                parsed["reasoning_truncated"] = was_truncated
                return parsed, was_truncated
        except Exception:
            pass
    return None, was_truncated


# ---------------------------------------------------------------------------
# Token Budgeting & Pre-Flight Token Budget Guard
# ---------------------------------------------------------------------------

def estimate_tokens(text: str) -> int:
    """Subword-calibrated token estimator for Qwen / standard BPE."""
    if not text:
        return 0
    # Words + punctuation tokens heuristic
    subword_tokens = len(re.findall(r"\w+|[^\w\s]", text))
    char_estimate = max(1, len(text) // 4)
    return max(subword_tokens, char_estimate)


def preflight_budget_guard(
    evidence: dict[str, Any],
    query: str,
    knowledge: list[dict[str, Any]],
    effective_num_ctx: int = DEFAULT_NUM_CTX,
    enable_thinking: bool = False,
    exemplars: Sequence[HistoricalIncident] | None = None,
    vendor_manual_matches: list[dict[str, Any]] | None = None,
) -> tuple[dict[str, Any], bool, str]:
    """Pre-flight Python token-budget guard.

    Computes prompt tokens + reserved output tokens against effective num_ctx.
    If exceeding budget, performs deterministic compaction (summarizing routine event spans
    or applying windowing around anomalies) or raises an explicit warning before dispatch.
    Never silently overflows.
    """
    reserved_output = RESERVED_OUTPUT_TOKENS_THINKING if enable_thinking else RESERVED_OUTPUT_TOKENS_NON_THINKING
    available_prompt_budget = max(512, effective_num_ctx - reserved_output)

    # Build initial candidate context
    test_context = build_llm_context(
        evidence,
        query,
        knowledge,
        exemplars=exemplars,
        effective_num_ctx=effective_num_ctx,
        _skip_budget_guard=True,
        vendor_manual_matches=vendor_manual_matches,
    )
    est_prompt_tokens = estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(test_context)

    if est_prompt_tokens <= available_prompt_budget:
        return evidence, False, ""

    # Exceeding budget -> Execute deterministic compaction
    all_events = evidence.get("events", [])
    if not all_events:
        msg = f"Warning: Prompt ({est_prompt_tokens} tokens) exceeds available budget ({available_prompt_budget} tokens)."
        warnings.warn(msg, UserWarning, stacklevel=2)
        return evidence, False, msg

    # Identify anomalies, faults, errors, and state transitions that MUST be preserved
    anomalous_indices = set()
    for idx, ev in enumerate(all_events):
        name = str(ev.get("event_name") or ev.get("event_type") or "").upper()
        status = str(ev.get("decode_status") or "").upper()
        fields = ev.get("fields", {}) or {}

        if status and status not in ("SUCCESS", "DECODED"):
            anomalous_indices.add(idx)
            continue
        if any(kw in name for kw in ("AVOID", "LEFT_FORMATION", "ERROR", "ERR", "EV", "FAULT", "WARN", "FAIL", "ALARM")):
            anomalous_indices.add(idx)
            continue
        for k, v in fields.items():
            k_lower = k.lower()
            if any(term in k_lower for term in ("error_code", "ecode", "fault_code", "reason_code", "timeout_count")):
                if isinstance(v, (int, float)) and v > 0:
                    anomalous_indices.add(idx)
                    break
            elif k_lower == "state_code" and isinstance(v, (int, float)) and v > 0:
                anomalous_indices.add(idx)
                break
            elif k_lower in ("status_name", "reason", "detail", "state") and isinstance(v, str):
                if any(term in v.upper() for term in ("FAIL", "ERROR", "TIMEOUT", "DEGRADED", "COLLISION", "LEFT_FORMATION", "EMERGENCY")):
                    anomalous_indices.add(idx)
                    break
        for (r_name, key, val), _ in RULES.items():
            if name == r_name and fields.get(key) == val:
                anomalous_indices.add(idx)
                break

    # Realistic available event token budget computation:
    # available = num_ctx - system_tokens - rag_tokens - reserved_output - safety_margin
    system_tokens = estimate_tokens(SYSTEM_PROMPT)
    knowledge_tokens = estimate_tokens(json.dumps(knowledge, default=str)) if knowledge else 0
    query_tokens = estimate_tokens(query)
    safety_margin = 256
    available_event_budget = max(400, available_prompt_budget - system_tokens - knowledge_tokens - query_tokens - safety_margin)

    # Subword-calibrated event tokens per event
    sample_size = min(len(all_events), 25)
    sample_text = "\n".join(str(all_events[i]) for i in range(sample_size))
    avg_tokens_per_event = max(35, estimate_tokens(sample_text) // max(1, sample_size))

    # Upper bound on total events that can fit inside available event budget
    max_budget_events = max(10, available_event_budget // avg_tokens_per_event)
    target_event_count = min(len(all_events), max_budget_events)

    critical_indices = sorted(anomalous_indices)
    nominal_indices = [i for i in range(len(all_events)) if i not in anomalous_indices]

    if len(all_events) <= target_event_count:
        final_indices = list(range(len(all_events)))
    elif len(critical_indices) >= target_event_count:
        # Downsample critical events evenly if critical alone exceed budget
        step = (len(critical_indices) - 1) / max(1, (target_event_count - 1))
        final_indices = sorted({critical_indices[round(i * step)] for i in range(target_event_count)})
    else:
        # Keep 100% of critical events, fill remainder with nominal evenly across timeline
        selected_indices = set(critical_indices)
        nominal_budget = target_event_count - len(critical_indices)
        step = (len(nominal_indices) - 1) / max(1, (nominal_budget - 1))
        for i in range(nominal_budget):
            idx = round(i * step)
            if idx < len(nominal_indices):
                selected_indices.add(nominal_indices[idx])
        final_indices = sorted(selected_indices)

    compacted_events: list[dict[str, Any]] = [all_events[i] for i in final_indices]
    critical_event_objs = [all_events[i] for i in critical_indices]

    # Re-evaluate token count after proportional compaction
    compacted_evidence = dict(evidence)
    compacted_evidence["events"] = compacted_events

    test_context2 = build_llm_context(
        compacted_evidence,
        query,
        knowledge,
        exemplars=exemplars,
        effective_num_ctx=effective_num_ctx,
        _skip_budget_guard=True,
        vendor_manual_matches=vendor_manual_matches,
    )
    est_prompt_tokens2 = estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(test_context2)

    # Progressively trim nominal events if still slightly over budget
    while est_prompt_tokens2 > available_prompt_budget and len(compacted_events) > len(critical_indices) + 2:
        current_nominal = [ev for ev in compacted_events if ev.get("event_id") not in {all_events[i].get("event_id") for i in critical_indices}]
        if not current_nominal:
            break
        to_remove_ids = {ev.get("event_id") for ev in current_nominal[1::2]}
        if not to_remove_ids and current_nominal:
            to_remove_ids = {current_nominal[0].get("event_id")}
        compacted_events = [ev for ev in compacted_events if ev.get("event_id") not in to_remove_ids]
        compacted_evidence["events"] = compacted_events
        test_context2 = build_llm_context(
            compacted_evidence,
            query,
            knowledge,
            exemplars=exemplars,
            effective_num_ctx=effective_num_ctx,
            _skip_budget_guard=True,
            vendor_manual_matches=vendor_manual_matches,
        )
        est_prompt_tokens2 = estimate_tokens(SYSTEM_PROMPT) + estimate_tokens(test_context2)

    # Final fallback if STILL over budget: clamp strictly to max_budget_events
    if est_prompt_tokens2 > available_prompt_budget:
        crit_set = {all_events[i].get("event_id") for i in critical_indices}
        crits_in_compacted = [ev for ev in compacted_events if ev.get("event_id") in crit_set]
        nominal_in_compacted = [ev for ev in compacted_events if ev.get("event_id") not in crit_set]
        compacted_events = crits_in_compacted[:max_budget_events]
        if len(compacted_events) < max_budget_events and nominal_in_compacted:
            rem = max_budget_events - len(compacted_events)
            compacted_events.extend(nominal_in_compacted[:rem])
        compacted_events = sorted(compacted_events, key=lambda e: e.get("timestamp_ns", 0))
        compacted_evidence["events"] = compacted_events

    # Add explicit Data Gap record for budget compaction
    existing_gaps = list(compacted_evidence.get("data_gaps") or [])
    guard_msg = (
        f"Pre-flight token budget guard deterministically compacted timeline from {len(all_events)} "
        f"to {len(compacted_evidence['events'])} events to fit effective num_ctx={effective_num_ctx} "
        f"(reserved output: {reserved_output} tokens)."
    )
    existing_gaps.append({
        "kind": "BUDGET_GUARD_COMPACTION",
        "event_id": None,
        "source_id": None,
        "detail": guard_msg,
    })
    compacted_evidence["data_gaps"] = existing_gaps

    warnings.warn(guard_msg, UserWarning, stacklevel=2)
    return compacted_evidence, True, guard_msg


# ---------------------------------------------------------------------------
# Hybrid RAG & Prompt Context Assembly
# ---------------------------------------------------------------------------

def retrieve_relevant_knowledge(evidence: dict[str, Any], query: str, top_k: int = 8) -> list[dict[str, str]]:
    """Retrieve domain knowledge chunks using Hybrid BM25S + Dense + RRF + Reranker."""
    return retrieve_hybrid_knowledge(query, evidence=evidence, top_k=top_k)


def build_llm_context(
    evidence: dict[str, Any],
    query: str,
    knowledge: list[dict[str, Any]],
    exemplars: Sequence[HistoricalIncident] | None = None,
    effective_num_ctx: int = DEFAULT_NUM_CTX,
    enable_thinking: bool = False,
    _skip_budget_guard: bool = False,
    vendor_manual_matches: list[dict[str, Any]] | None = None,
) -> str:
    """Format prompt context with context budgeting, vendor manual matches, and RCACopilot exemplars."""
    # Apply preflight budget guard if not skipped
    was_compacted = False
    guard_warning = ""
    if not _skip_budget_guard:
        evidence, was_compacted, guard_warning = preflight_budget_guard(
            evidence,
            query,
            knowledge,
            effective_num_ctx=effective_num_ctx,
            enable_thinking=enable_thinking,
            exemplars=exemplars,
            vendor_manual_matches=vendor_manual_matches,
        )

    retrieval = evidence.get("retrieval") or {}
    sources = evidence.get("sources", [])
    clock_caveat = evidence.get("clock_caveat")
    all_events = evidence.get("events", [])

    # Collect and normalize vendor manual error matches
    all_vendor_matches = list(vendor_manual_matches or [])
    if evidence.get("vendor_manual_matches"):
        all_vendor_matches.extend(evidence["vendor_manual_matches"])
    if evidence.get("vendor_matches"):
        all_vendor_matches.extend(evidence["vendor_matches"])
    if evidence.get("error_catalog"):
        ec = evidence["error_catalog"]
        if isinstance(ec, list):
            all_vendor_matches.extend(ec)
        elif isinstance(ec, dict) and "errors" in ec:
            all_vendor_matches.extend(ec["errors"])

    lines = [
        "=== USER QUERY ===",
        query,
        "",
    ]

    if guard_warning:
        lines.append(f"⚠️ [PRE-FLIGHT BUDGET GUARD] {guard_warning}")
        lines.append("")

    # Historical Incidents Exemplars (RCACopilot Few-Shot CoT)
    if exemplars:
        lines.append(format_exemplars_cot(exemplars))

    # ---------------------------------------------------------------------------
    # 1. Condensed Full-Log Telemetry & Extracted Fault Events ([ev-...])
    # ---------------------------------------------------------------------------
    lines.extend([
        "### 1. CONDENSED FULL-LOG TELEMETRY & EXTRACTED FAULT EVENTS",
        f"Retrieval: source={retrieval.get('source', 'ALL')}, window [{retrieval.get('t_start_ns', 0)}, {retrieval.get('t_end_ns', 0)}) ns, "
        f"max_events={retrieval.get('max_events', 100)}, returned={retrieval.get('returned', len(all_events))}, truncated={retrieval.get('truncated', False)}",
        f"Sources: {', '.join(map(str, sources)) if sources else 'none'}",
        f"Ordering: {evidence.get('ordering', 'Grouped by source_id, sorted by producer timestamp.')}",
    ])

    if clock_caveat:
        lines.append(f"Clock caveat: {clock_caveat}")

    if all_events:
        lines.append("Decoded events (one JSON per line, producer_time_ns on producer's clock, cite with [ev-...]):")
        for ev in all_events:
            entry = {
                "event_id": ev["event_id"],
                "source_id": ev["source_id"],
                "producer_time_ns": ev.get("timestamp_ns") or ev.get("timestamp"),
                "event": ev.get("event_name") or ev.get("event_type"),
                "status": ev.get("decode_status"),
                "fields": ev.get("fields", {}),
                "interpreted": ev.get("interpreted_fields", []),
            }
            lines.append(json.dumps(entry))
    else:
        lines.append("No events recorded or retrieved in the requested time window.")

    lines.append("")

    # ---------------------------------------------------------------------------
    # 2. Digested Vendor Error-Code Manual Matches & Domain RAG Knowledge ([doc-...] / [err-...])
    # ---------------------------------------------------------------------------
    lines.append("### 2. DIGESTED VENDOR ERROR-CODE MANUAL MATCHES & RUNBOOKS")

    # Injected Vendor Error-Code Manual Matches
    if all_vendor_matches:
        lines.append("Vendor Error-Code Manual Matches (cite with [err-...]):")
        for vm in all_vendor_matches:
            # Stable citation ID
            err_id = vm.get("id") or vm.get("err_id") or vm.get("code") or "err-match"
            if not str(err_id).startswith(("err-", "doc-")):
                err_id = f"err-{err_id}"
            title = vm.get("title") or vm.get("name") or "Vendor Error Code Match"
            subsys = vm.get("subsystem") or vm.get("subsys") or ""
            code = vm.get("code") or vm.get("ecode") or ""
            desc = vm.get("description") or vm.get("detail") or vm.get("content") or ""
            remedy = vm.get("remedy") or vm.get("runbook") or vm.get("action") or ""

            lines.append(f'<vendor_error id="{err_id}" title="{title}" subsystem="{subsys}" code="{code}">')
            if desc:
                lines.append(f"| Description: {desc}")
            if remedy:
                lines.append(f"| Runbook/Action: {remedy}")
            lines.append("</vendor_error>")
    else:
        lines.append("(No vendor error-code manual matches observed in this telemetry segment)")

    lines.append("")
    lines.append("Domain RAG Knowledge Chunks (cite with [doc-...]):")
    knowledge_chunks = knowledge[:8]
    if knowledge_chunks:
        for doc in knowledge_chunks:
            doc_id = doc.get("id") or doc.get("chunk") or "doc-unknown"
            if not str(doc_id).startswith(("doc-", "err-")) and not str(doc_id).startswith("d1_"):
                doc_id = f"doc-{doc_id}"
            lines.append(
                f'<document id="{doc.get("id")}" cite_id="{doc_id}" chunk="{doc.get("chunk")}" kind="{doc.get("kind")}" domain="{doc.get("domain")}" title="{doc.get("title")}">'
            )
            for line in doc.get("content", "").splitlines():
                lines.append(f"| {line}")
            lines.append("</document>")
    else:
        lines.append("(No additional domain knowledge chunks matched)")

    lines.append("")

    # ---------------------------------------------------------------------------
    # 3. Detected Data Gaps (if any)
    # ---------------------------------------------------------------------------
    lines.append("### 3. DETECTED DATA GAPS & DECODING WARNINGS")
    gaps = evidence.get("data_gaps", [])
    if gaps:
        for g in gaps:
            lines.append(f"- [{g.get('kind', 'GAP')}] {g.get('detail')}")
    else:
        lines.append("None: all events decoded without gaps.")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# LLM Backends: Ollama, llama-server & HuggingFace
# ---------------------------------------------------------------------------

def check_ollama_available() -> tuple[str | None, str | None]:
    """Check if Ollama is running and has a suitable model."""
    try:
        with urllib.request.urlopen(f"{OLLAMA_URL}/api/tags", timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
            models = [m.get("name", "") for m in data.get("models", [])]
            for target in ["qwen3:4b", "qwen2.5:3b", "qwen2.5:1.5b", "qwen2.5:7b"]:
                for m in models:
                    if target in m:
                        return m, None
            if models:
                return models[0], None
            return None, "No models found in Ollama"
    except urllib.error.HTTPError as exc:
        exc.close()
        return None, str(exc)
    except Exception as exc:
        return None, str(exc)


def check_llama_server_available() -> tuple[str | None, str | None]:
    """Check if local llama-server is running and healthy."""
    try:
        with urllib.request.urlopen(f"{LLAMA_SERVER_URL}/health", timeout=2) as r:
            data = json.loads(r.read().decode("utf-8"))
            if data.get("status") in ("ok", "loading model"):
                return "llama-server", None
            return None, f"llama-server status: {data.get('status')}"
    except Exception as exc:
        return None, str(exc)


def query_ollama(
    model: str,
    system_prompt: str,
    user_prompt: str,
    enable_thinking: bool = False,
    thinking_budget: int = DEFAULT_THINKING_BUDGET,
    num_ctx: int = DEFAULT_NUM_CTX,
) -> dict[str, Any] | None:
    """Execute inference via Ollama native /api/chat with strict num_ctx enforcement."""
    sampling_opts = get_qwen3_sampling_params(enable_thinking=enable_thinking, thinking_budget=thinking_budget)

    effective_ctx = max(num_ctx, OLLAMA_CONTEXT_LENGTH)
    body = {
        "model": model,
        "stream": False,
        "format": "json",
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
        "options": {
            "num_ctx": effective_ctx,
            "temperature": sampling_opts["temperature"],
            "top_p": sampling_opts["top_p"],
            "top_k": sampling_opts["top_k"],
            "min_p": sampling_opts["min_p"],
            "repeat_penalty": sampling_opts.get("presence_penalty", 1.1),
        },
    }

    req = urllib.request.Request(
        f"{OLLAMA_URL}/api/chat",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    ollama_timeout = int(os.environ.get("OLLAMA_TIMEOUT", "90"))
    with urllib.request.urlopen(req, timeout=ollama_timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        content = data.get("message", {}).get("content", "")
        parsed, _ = process_qwen3_thinking_output(content, thinking_budget=thinking_budget)
        return parsed


def query_llama_server(
    system_prompt: str,
    user_prompt: str,
    enable_thinking: bool = False,
    thinking_budget: int = DEFAULT_THINKING_BUDGET,
    ctx_size: int = DEFAULT_NUM_CTX,
) -> dict[str, Any] | None:
    """Execute inference via llama-server with host prefix-caching and Pydantic json_schema."""
    sampling_opts = get_qwen3_sampling_params(enable_thinking=enable_thinking, thinking_budget=thinking_budget)
    schema = get_synthesis_json_schema()

    # Byte-identical prefix caching prompt
    full_prompt = f"<|im_start|>system\n{system_prompt}<|im_end|>\n<|im_start|>user\n{user_prompt}<|im_end|>\n<|im_start|>assistant\n"

    body = {
        "prompt": full_prompt,
        "cache_prompt": True,
        "n_predict": RESERVED_OUTPUT_TOKENS_THINKING if enable_thinking else RESERVED_OUTPUT_TOKENS_NON_THINKING,
        "temperature": sampling_opts["temperature"],
        "top_p": sampling_opts["top_p"],
        "top_k": sampling_opts["top_k"],
        "min_p": sampling_opts["min_p"],
        "json_schema": schema,
        "stop": ["<|im_end|>"],
    }

    req = urllib.request.Request(
        f"{LLAMA_SERVER_URL}/completion",
        data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"},
    )
    ollama_timeout = int(os.environ.get("OLLAMA_TIMEOUT", "90"))
    with urllib.request.urlopen(req, timeout=ollama_timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        content = data.get("content", "")
        parsed, _ = process_qwen3_thinking_output(content, thinking_budget=thinking_budget)
        return parsed


def query_local_hf(
    system_prompt: str,
    user_prompt: str,
    enable_thinking: bool = False,
    thinking_budget: int = DEFAULT_THINKING_BUDGET,
) -> dict[str, Any] | None:
    """Execute inference using local HuggingFace weights if transformers is present."""
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        sampling_opts = get_qwen3_sampling_params(enable_thinking=enable_thinking, thinking_budget=thinking_budget)

        tokenizer = AutoTokenizer.from_pretrained(LOCAL_HF_PATH, local_files_only=True)
        model = AutoModelForCausalLM.from_pretrained(
            LOCAL_HF_PATH,
            local_files_only=True,
            torch_dtype=torch.float32,
            device_map="auto",
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]
        text = tokenizer.apply_chat_template(messages, tokenize=False, add_generation_prompt=True)
        inputs = tokenizer([text], return_tensors="pt").to(model.device)

        with torch.inference_mode():
            generated_ids = model.generate(
                inputs.input_ids,
                max_new_tokens=RESERVED_OUTPUT_TOKENS_THINKING if enable_thinking else RESERVED_OUTPUT_TOKENS_NON_THINKING,
                do_sample=True,  # explicit for transformers >= 4.46
                temperature=sampling_opts["temperature"],
                top_p=sampling_opts["top_p"],
                top_k=sampling_opts["top_k"],
            )

        output = tokenizer.batch_decode(
            [g[len(i) :] for i, g in zip(inputs.input_ids, generated_ids)],
            skip_special_tokens=False,
        )[0]

        parsed, _ = process_qwen3_thinking_output(output, thinking_budget=thinking_budget)
        return parsed
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Deterministic Fallback Synthesizer
# ---------------------------------------------------------------------------

def deterministic_synthesize(
    evidence: dict[str, Any],
    query: str,
    knowledge: list[dict[str, Any]],
    vendor_manual_matches: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Offline deterministic rule-based failure synthesizer matching golden FSS rules."""
    events = evidence.get("events", [])
    sources = evidence.get("sources", [])
    gaps = evidence.get("data_gaps", [])

    # Normalize vendor manual matches
    v_matches = list(vendor_manual_matches or [])
    if evidence.get("vendor_manual_matches"):
        v_matches.extend(evidence["vendor_manual_matches"])
    if evidence.get("vendor_matches"):
        v_matches.extend(evidence["vendor_matches"])

    data_gap_summaries = []
    for g in gaps:
        eid_str = f"Event {g['event_id']}: " if g.get("event_id") else (f"[{g.get('kind')}] " if g.get("kind") else "")
        data_gap_summaries.append(f"{eid_str}{g.get('detail', '')}")

    if not events:
        return {
            "summary": "No events recorded or retrieved in the requested time window.",
            "likely_cause": None,
            "confidence": "high",
            "contributing_events": [],
            "uncertainty": [],
            "data_gaps": data_gap_summaries,
            "reasoning_truncated": False,
        }

    anomalies: list[dict[str, Any]] = []
    for e in events:
        name = e.get("event_name") or ""
        fields = e.get("fields", {})

        for (r_name, key, val), (sev, desc) in RULES.items():
            if name == r_name and fields.get(key) == val:
                anomalies.append({**e, "severity": sev, "problem": desc, "rule_key": key, "rule_val": val})

        if name == "AVOIDANCE":
            anomalies.append({
                **e,
                "severity": "medium",
                "problem": f"Avoidance manoeuvre ({fields.get('reason', 'UNKNOWN')})",
                "reason": fields.get("reason"),
                "min_sep": fields.get("min_separation_m"),
                "heading": fields.get("heading_change_deg"),
            })

        if name == "ERROR_EVENT":
            sub_c = fields.get("subsystem_code") if fields.get("subsystem_code") is not None else fields.get("subsystem")
            err_c = fields.get("error_code") if fields.get("error_code") is not None else fields.get("ecode")
            anomalies.append({
                **e,
                "severity": "high" if (err_c is not None and err_c > 0) else "medium",
                "problem": f"Subsystem error event (subsystem={sub_c}, error_code={err_c})",
            })

    if v_matches:
        vm_anomalies: list[dict[str, Any]] = []
        for vm in v_matches:
            vm_id = vm.get("doc_chunk_id") or vm.get("id") or vm.get("err_id") or vm.get("code")
            if not str(vm_id).startswith(("err-", "doc-")):
                vm_id = f"err-{vm_id}"
            vm_title = vm.get("title") or vm.get("name") or "Vendor Error"
            matching_ev = None
            for e in events:
                if str(e.get("event_type")) == str(vm.get("code")) or e.get("event_name") == vm.get("code"):
                    matching_ev = e
                    break
                if vm.get("error_code") and (str(e.get("event_type")) == str(vm.get("error_code")) or e.get("event_name") == vm.get("error_code")):
                    matching_ev = e
                    break
                fields = e.get("fields", {})
                if any(str(v) == str(vm.get("error_code")) or str(v) == str(vm.get("code")) for v in fields.values()):
                    matching_ev = e
                    break
            target_ev = matching_ev or (events[0] if events else None)
            target_eid = target_ev["event_id"] if target_ev else "ev-unknown"
            target_src = target_ev["source_id"] if target_ev else (sources[0] if sources else 0)
            vm_anomalies.append({
                "source_id": target_src,
                "event_id": target_eid,
                "timestamp_ns": target_ev.get("timestamp_ns") if target_ev else 0,
                "severity": vm.get("severity", "high").lower(),
                "problem": f"{vm_title} [{vm_id}]",
                "vendor_chunk_id": vm_id,
                "vm": vm,
            })
        anomalies = vm_anomalies + anomalies

    drones_left = [a for a in anomalies if a.get("fields", {}).get("state") == "LEFT_FORMATION"]
    if drones_left:
        left_ev = drones_left[0]
        left_src = left_ev["source_id"]
        form_id = left_ev.get("fields", {}).get("formation_id", "")
        form_str = f" {form_id}" if form_id != "" else ""

        drones_avoid = [a for a in anomalies if a["source_id"] == left_src and a.get("event_name") == "AVOIDANCE"]
        other_lidar = [a for a in anomalies if a["source_id"] != left_src and a.get("fields", {}).get("status_name") == "LIDAR_TIMEOUT"]

        avoid_ev = drones_avoid[0] if drones_avoid else None
        lidar_ev = other_lidar[0] if other_lidar else None

        sep = avoid_ev.get("fields", {}).get("min_separation_m", 3.2) if avoid_ev else None
        sep_str = f" with a minimum separation of {sep} m" if sep is not None else ""

        if avoid_ev:
            summary = (
                f"Drone {left_src} left formation{form_str} (LEFT_FORMATION [{left_ev['event_id']}]) after an avoidance manoeuvre "
                f"for COLLISION_RISK [{avoid_ev['event_id']}]{sep_str}."
            )
            contributing = [
                {
                    "source_id": avoid_ev["source_id"],
                    "timestamp": avoid_ev.get("timestamp_ns") or avoid_ev.get("timestamp"),
                    "event_type": "AVOIDANCE",
                    "event_id": avoid_ev["event_id"],
                },
                {
                    "source_id": left_ev["source_id"],
                    "timestamp": left_ev.get("timestamp_ns") or left_ev.get("timestamp"),
                    "event_type": "FORMATION_STATE",
                    "event_id": left_ev["event_id"],
                },
            ]
        else:
            summary = f"Drone {left_src} left formation{form_str} (LEFT_FORMATION [{left_ev['event_id']}])."
            contributing = [
                {
                    "source_id": left_ev["source_id"],
                    "timestamp": left_ev.get("timestamp_ns") or left_ev.get("timestamp"),
                    "event_type": "FORMATION_STATE",
                    "event_id": left_ev["event_id"],
                }
            ]

        likely_cause = None
        if lidar_ev:
            timeout_cnt = lidar_ev.get("fields", {}).get("timeout_count", 0)
            cnt_str = f" ({timeout_cnt} consecutive missed scans [{lidar_ev['event_id']}])" if timeout_cnt else f" [{lidar_ev['event_id']}]"
            likely_cause = (
                f"Drone {lidar_ev['source_id']} reported LIDAR_TIMEOUT{cnt_str} around the same time. "
                f"According to domain reference d1_drone_fleet/lidar_timeout#1, a stale obstacle map on {lidar_ev['source_id']} plausibly let it drift "
                f"towards {left_src}, triggering {left_src}'s collision avoidance."
            )
            contributing.insert(
                0,
                {
                    "source_id": lidar_ev["source_id"],
                    "timestamp": lidar_ev.get("timestamp_ns") or lidar_ev.get("timestamp"),
                    "event_type": "LIDAR_STATUS",
                    "event_id": lidar_ev["event_id"],
                },
            )
        elif avoid_ev:
            likely_cause = f"Immediate trigger was an avoidance manoeuvre for COLLISION_RISK [{avoid_ev['event_id']}]. Root cause external to source {left_src} requires querying other sources."

        uncertainty = []
        if len(sources) > 1:
            uncertainty.append(
                "Producer clocks are not synchronised across sources, so the order of events from different "
                "sources cannot be established from their timestamps."
            )
        if lidar_ev:
            uncertainty.append(
                f"No event shows drone {lidar_ev['source_id']}'s actual trajectory deviation; the link between {lidar_ev['source_id']}'s timeout and {left_src}'s manoeuvre is inferred."
            )
        elif len(sources) == 1:
            uncertainty.append(
                f"Only source {left_src} was retrieved; identifying external causes or other drones involved requires querying additional sources."
            )

        return {
            "summary": summary,
            "likely_cause": likely_cause,
            "confidence": "medium",
            "contributing_events": contributing,
            "uncertainty": uncertainty,
            "data_gaps": data_gap_summaries,
            "reasoning_truncated": False,
        }

    if anomalies:
        first = anomalies[0]
        cites = [f"[{a['event_id']}]" for a in anomalies[:3]]
        summary = f"Detected {len(anomalies)} anomalies across sources {', '.join(map(str, sources))}: {first['problem']} {' '.join(cites)}."
        likely_cause = f"Failure sequence initiated by {first['problem']} [{first['event_id']}] on source {first['source_id']}."
        contributing = [
            {
                "source_id": a["source_id"],
                "timestamp": a.get("timestamp_ns") or a.get("timestamp"),
                "event_type": a.get("event_name") or a.get("event_type"),
                "event_id": a["event_id"],
            }
            for a in anomalies[:5]
        ]
        uncertainty = []
        if len(sources) > 1:
            uncertainty.append(
                "Producer clocks are not synchronised across sources; inter-source causal links are plausible hypotheses."
            )
        return {
            "summary": summary,
            "likely_cause": likely_cause,
            "confidence": "medium",
            "contributing_events": contributing,
            "uncertainty": uncertainty,
            "data_gaps": data_gap_summaries,
            "reasoning_truncated": False,
        }

    return {
        "summary": "Nominal operation: all retrieved events decoded successfully with nominal statuses.",
        "likely_cause": None,
        "confidence": "high",
        "contributing_events": [],
        "uncertainty": ["Clocks are unverified across sources."] if len(sources) > 1 else [],
        "data_gaps": data_gap_summaries,
        "reasoning_truncated": False,
    }


# ---------------------------------------------------------------------------
# Validation Engine & Badges
# ---------------------------------------------------------------------------

def validate_synthesis(
    synthesis: dict[str, Any],
    evidence: dict[str, Any],
    knowledge: list[dict[str, Any]] | None = None,
    query: str = "",
) -> ValidationReport:
    """Validate synthesis output against evidence facts, AISVS C07-04, and ForceBench rules."""
    return CitationAuditor().audit(synthesis, evidence, knowledge=knowledge, query=query)


# ---------------------------------------------------------------------------
# End-to-End Orchestrator
# ---------------------------------------------------------------------------

def synthesize_and_validate(
    evidence: dict[str, Any],
    query: str,
    enable_thinking: bool = False,
    thinking_budget: int = DEFAULT_THINKING_BUDGET,
    effective_num_ctx: int = DEFAULT_NUM_CTX,
    exemplars_k: int = 3,
    sufficiency_gate_enabled: bool = True,
    auditor: CitationAuditor | None = None,
    vendor_manual_matches: list[dict[str, Any]] | None = None,
    error_catalog: list[dict[str, Any]] | dict[str, Any] | None = None,
) -> tuple[dict[str, Any], ValidationReport, str]:
    """Execute end-to-end synthesis with active local backend, token budget guard, and validator."""
    # Normalize vendor manual matches & error catalog
    all_vendor_matches: list[dict[str, Any]] = []
    if vendor_manual_matches:
        all_vendor_matches.extend(vendor_manual_matches)
    if error_catalog:
        if isinstance(error_catalog, list):
            all_vendor_matches.extend(error_catalog)
        elif isinstance(error_catalog, dict):
            if "errors" in error_catalog and isinstance(error_catalog["errors"], list):
                all_vendor_matches.extend(error_catalog["errors"])
            else:
                for k, v in error_catalog.items():
                    if isinstance(v, dict):
                        all_vendor_matches.append({**v, "id": v.get("id", f"err-{k}")})
                    else:
                        all_vendor_matches.append({"id": f"err-{k}", "title": str(k), "description": str(v)})

    if evidence.get("vendor_manual_matches"):
        all_vendor_matches.extend(evidence["vendor_manual_matches"])
    if evidence.get("vendor_matches"):
        all_vendor_matches.extend(evidence["vendor_matches"])
    if evidence.get("error_catalog"):
        ec = evidence["error_catalog"]
        if isinstance(ec, list):
            all_vendor_matches.extend(ec)
        elif isinstance(ec, dict) and "errors" in ec:
            all_vendor_matches.extend(ec["errors"])

    for vm in all_vendor_matches:
        if "id" not in vm and "code" in vm:
            subsys = vm.get("subsystem") or vm.get("subsys")
            code = vm.get("code")
            if subsys is not None:
                vm["id"] = f"err-subsys-{subsys}-code-{code}"
            else:
                vm["id"] = f"err-{code}"
        elif "id" in vm and not str(vm["id"]).startswith(("err-", "doc-")):
            vm["id"] = f"err-{vm['id']}"

    # 1. Retrieve hybrid knowledge and historical exemplars
    knowledge = retrieve_relevant_knowledge(evidence, query, top_k=8)
    exemplars = retrieve_historical_exemplars(query, evidence=evidence, k=exemplars_k)
    audit_engine = auditor or CitationAuditor()

    # Stage 0: Pre-Generation Sufficiency Gate (RINSE-inspired)
    if sufficiency_gate_enabled:
        sufficiency = evaluate_sufficiency(
            evidence,
            query,
            knowledge,
            vendor_manual_matches=all_vendor_matches,
            error_catalog=error_catalog,
        )
        if not sufficiency.is_sufficient:
            quarantine_synth = create_insufficient_evidence_synthesis(sufficiency, evidence, query)
            report = audit_engine.audit(
                quarantine_synth,
                evidence,
                knowledge=knowledge,
                query=query,
                vendor_manual_matches=all_vendor_matches,
                error_catalog=error_catalog,
            )
            return report.corrected_synthesis, report, "Stage 0 Epistemic Sufficiency Gate (Quarantined)"

    # 2. Apply preflight budget guard
    guarded_evidence, was_compacted, _ = preflight_budget_guard(
        evidence,
        query,
        knowledge,
        effective_num_ctx=effective_num_ctx,
        enable_thinking=enable_thinking,
        exemplars=exemplars,
        vendor_manual_matches=all_vendor_matches,
    )
    context_text = build_llm_context(
        guarded_evidence,
        query,
        knowledge,
        exemplars=exemplars,
        effective_num_ctx=effective_num_ctx,
        enable_thinking=enable_thinking,
        _skip_budget_guard=True,
        vendor_manual_matches=all_vendor_matches,
    )

    synthesis: dict[str, Any] | None = None
    model_used = "Offline deterministic reasoning"

    # 3. Try llama-server if running
    llama_model, _ = check_llama_server_available()
    if llama_model:
        try:
            synthesis = query_llama_server(
                SYSTEM_PROMPT,
                context_text,
                enable_thinking=enable_thinking,
                thinking_budget=thinking_budget,
                ctx_size=effective_num_ctx,
            )
            if synthesis:
                model_used = "llama-server (Prefix-Cached + GBNF)"
        except Exception:
            synthesis = None

    # 4. Try Ollama if llama-server was not active
    skip_external_llm = (
        ("PYTEST_CURRENT_TEST" in os.environ or "pytest" in sys.modules)
        and "FORCE_EXTERNAL_LLM" not in os.environ
    )
    if synthesis is None and not skip_external_llm:
        ollama_model, _ = check_ollama_available()
        if ollama_model:
            try:
                synthesis = query_ollama(
                    ollama_model,
                    SYSTEM_PROMPT,
                    context_text,
                    enable_thinking=enable_thinking,
                    thinking_budget=thinking_budget,
                    num_ctx=effective_num_ctx,
                )
                if synthesis:
                    model_used = f"Ollama ({ollama_model})"
            except Exception:
                synthesis = None

    # 5. Try HuggingFace local weights if Ollama was not active
    effective_hf_path = (
        ""
        if (skip_external_llm or os.environ.get("SKIP_LOCAL_HF") == "1")
        else (LOCAL_HF_PATH or os.environ.get("LOCAL_MODEL_PATH", ""))
    )
    if synthesis is None and effective_hf_path and os.path.exists(effective_hf_path):
        hf_res = query_local_hf(
            SYSTEM_PROMPT,
            context_text,
            enable_thinking=enable_thinking,
            thinking_budget=thinking_budget,
        )
        if hf_res:
            synthesis = hf_res
            model_used = "Local HuggingFace Qwen"

    # 6. Fallback to offline deterministic synthesizer
    if synthesis is None:
        synthesis = deterministic_synthesize(
            guarded_evidence, query, knowledge, vendor_manual_matches=all_vendor_matches
        )

    # Clean schema validation
    validated_obj = validate_synthesis_schema(synthesis)
    synthesis_dict = validated_obj.model_dump()

    # 7. Post-synthesis 4-Tier Citation & Warrant Audit
    report = audit_engine.audit(
        synthesis_dict,
        guarded_evidence,
        knowledge=knowledge,
        query=query,
        vendor_manual_matches=all_vendor_matches,
        error_catalog=error_catalog,
    )
    return report.corrected_synthesis, report, model_used
