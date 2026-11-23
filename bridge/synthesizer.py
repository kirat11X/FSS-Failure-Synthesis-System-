"""Failure Synthesizer, Local LLM Runtime & Validator for FSS Bridge.

Connects to:
  1. Local Ollama server (native /api/chat with OLLAMA_CONTEXT_LENGTH >= 16384, Qwen3 sampling).
  2. Local llama-server (--ctx-size >= 16384, prefix caching, Pydantic GBNF / json_schema).
  3. Local HuggingFace weights (with do_sample=True for transformers >= 4.46).
  4. Knowledge-base retrieval (BM25S + dense embeddings + reciprocal-rank fusion).
  5. Pre-flight Python token-budget guard enforcing num_ctx >= 16384 with deterministic compaction.
  6. Qwen3 Thinking Mode stability & bounded think-budget guard (closing token 151668 / <|im_end|> 151645).
  7. Offline deterministic synthesis that reports telemetry without inventing causal explanations.

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
from bridge.hybrid_rag import retrieve_hybrid_knowledge
from bridge.schemas import format_iso8601_ns
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

DEFAULT_NUM_CTX = 1000000

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


def _inference_timeout_seconds(env_name: str, default: int | None = None) -> float | None:
    """Return a urllib timeout; set the environment value to 0 or none for no timeout."""
    raw_value = os.environ.get(env_name)
    if raw_value is None:
        return None if default is None else float(default)
    normalized = raw_value.strip().lower()
    if normalized in {"", "0", "none", "unlimited", "infinite"}:
        return None
    try:
        value = float(raw_value)
    except ValueError as exc:
        raise ValueError(f"{env_name} must be seconds, 0, or none") from exc
    if value < 0:
        raise ValueError(f"{env_name} cannot be negative")
    return value or None

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

# System prompt is byte-identical across queries for host-memory prefix caching
# (llama-server --cram 256 --system-prompt-file ... --cache-prompt --cache-type-k q8_0)
SYSTEM_PROMPT = """You are a telemetry investigation assistant.
You receive an operator query, compact one-line telemetry records, and retrieved knowledge-base
documents. Each telemetry timestamp is already formatted as local ISO-8601; use it as supplied and
never convert it or produce raw epoch values.

STRICT CONTRACT RULES:
1. Grounding: Use retrieved telemetry for situation-specific claims and only the retrieved knowledge_base documents for domain explanations. Do not rely on outside knowledge.
2. Citation: Identify telemetry records in contributing_events using source_id, timestamp_iso8601, and the displayed event_type. Cite retrieved knowledge document IDs [doc-...].
3. Accuracy: Only cite records and document IDs present in the context. Never invent identifiers, events, or field meanings.
4. Clock Caveat: Producer clocks may differ. Do not state cross-source order or causality as fact unless evidence establishes it.
5. Data Gaps: Report undecoded or opaque data as unknown. Never guess payload bytes.
6. Each contributing_events item must contain only source_id, timestamp_iso8601, and event_type. Do not output event IDs or raw epoch values.
7. Format: Output valid JSON adhering to the FailureSynthesis schema:
{
  "summary": "1-3 sentences answering the operator question with supported citations",
  "likely_cause": "Evidence-supported explanation or null when unsupported",
  "confidence": "low" | "medium" | "high",
  "contributing_events": [],
  "uncertainty": [],
  "data_gaps": [],
  "reasoning_truncated": false
}"""


# ---------------------------------------------------------------------------
# Pydantic v2 Schema & Compatibility Interface
# ---------------------------------------------------------------------------

@dataclass
class ContributingEvent:
    source_id: int
    timestamp_iso8601: str
    event_type: str
    event_id: str = ""


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
                            "timestamp_iso8601": {"type": "string"},
                            "event_type": {"type": "string"},
                        },
                        "required": ["source_id", "timestamp_iso8601", "event_type"],
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
                    source_id = e.get("source_id")
                    timestamp_iso8601 = e.get("timestamp_iso8601")
                    event_type = e.get("event_type")
                    if type(source_id) is not int:
                        raise ValueError("Contributing event source_id must be an integer")
                    if not isinstance(timestamp_iso8601, str) or not timestamp_iso8601:
                        raise ValueError("Contributing event timestamp_iso8601 is required")
                    if not isinstance(event_type, str) or not event_type:
                        raise ValueError("Contributing event event_type is required")
                    events.append(
                        ContributingEvent(
                            source_id=source_id,
                            timestamp_iso8601=timestamp_iso8601,
                            event_type=event_type,
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


def _event_type_label(event: dict[str, Any]) -> str:
    event_name = event.get("event_name")
    if event_name:
        return str(event_name)
    event_type = event.get("event_type")
    if isinstance(event_type, int):
        return f"0x{event_type:02X}"
    if isinstance(event_type, str):
        try:
            return f"0x{int(event_type, 0):02X}"
        except ValueError:
            return event_type
    return "UNKNOWN_EVENT_TYPE"


def _format_record_value(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        if re.fullmatch(r"[A-Za-z0-9_./:+-]+", value):
            return value
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    return str(value)


def _telemetry_reference_key(event: dict[str, Any]) -> tuple[int, str, str]:
    timestamp_ns = event.get("timestamp_ns")
    if timestamp_ns is None:
        timestamp_ns = event.get("timestamp")
    if type(timestamp_ns) is not int:
        raise ValueError("Telemetry record has an invalid timestamp")
    source_id = event.get("source_id")
    if type(source_id) is not int:
        raise ValueError("Telemetry record has an invalid source_id")
    return source_id, format_iso8601_ns(timestamp_ns), _event_type_label(event)


def _resolve_contributing_event_refs(
    synthesis: dict[str, Any],
    evidence: dict[str, Any],
) -> None:
    events_by_reference: dict[tuple[int, str, str], list[dict[str, Any]]] = {}
    for event in evidence.get("events", []):
        events_by_reference.setdefault(_telemetry_reference_key(event), []).append(event)

    cited_ids: list[str] = []
    for reference in synthesis.get("contributing_events", []):
        if not isinstance(reference, dict):
            continue
        key = (
            reference.get("source_id"),
            reference.get("timestamp_iso8601"),
            reference.get("event_type"),
        )
        matches = events_by_reference.get(key, [])
        if len(matches) != 1:
            continue
        event = matches[0]
        reference["event_id"] = event["event_id"]
        reference["timestamp"] = event["timestamp_ns"]
        reference["event_type"] = _event_type_label(event)
        if event["event_id"] not in cited_ids:
            cited_ids.append(event["event_id"])

    for field_name in ("summary", "likely_cause"):
        statement = synthesis.get(field_name)
        if not isinstance(statement, str):
            continue
        missing = [
            f"[{event_id}]"
            for event_id in cited_ids
            if f"[{event_id}]" not in statement
        ]
        if missing:
            synthesis[field_name] = f"{statement.rstrip()} {' '.join(missing)}".strip()


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
    include_event_ids: bool = True,
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
        effective_num_ctx=effective_num_ctx,
        _skip_budget_guard=True,
        include_event_ids=include_event_ids,
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

    # Preserve telemetry that the generic event classifier marks as noteworthy.
    anomalous_indices = set()
    for idx, ev in enumerate(all_events):
        if is_critical_event(ev):
            anomalous_indices.add(idx)

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
        effective_num_ctx=effective_num_ctx,
        _skip_budget_guard=True,
        include_event_ids=include_event_ids,
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
            effective_num_ctx=effective_num_ctx,
            _skip_budget_guard=True,
            include_event_ids=include_event_ids,
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
    effective_num_ctx: int = DEFAULT_NUM_CTX,
    enable_thinking: bool = False,
    _skip_budget_guard: bool = False,
    include_event_ids: bool = True,
) -> str:
    """Format records as compact local-time lines with separately retrieved KB context."""
    if not _skip_budget_guard:
        evidence, _, _ = preflight_budget_guard(
            evidence,
            query,
            knowledge,
            effective_num_ctx=effective_num_ctx,
            enable_thinking=enable_thinking,
            include_event_ids=include_event_ids,
        )

    all_events = evidence.get("events", [])

    lines = [
        "Query:",
        query,
        "",
        "Telemetry records (one source record per line; timestamps are local ISO-8601):",
    ]

    if all_events:
        for ev in all_events:
            timestamp_ns = ev.get("timestamp_ns")
            if timestamp_ns is None:
                timestamp_ns = ev.get("timestamp")
            if timestamp_ns is None:
                raise ValueError("Telemetry record is missing its numeric timestamp")
            source_id = ev.get("source_id")
            if type(source_id) is not int:
                raise ValueError("Telemetry record has an invalid source_id")
            fields = ev.get("fields") or {}
            if not isinstance(fields, dict):
                raise ValueError("Telemetry record fields must be an object")

            parts = [
                f"D{source_id}",
                format_iso8601_ns(int(timestamp_ns)),
                _event_type_label(ev),
            ]
            parts.extend(
                f"{name}={_format_record_value(value)}"
                for name, value in fields.items()
            )
            lines.append(" | ".join(parts))
    else:
        lines.append("(No telemetry records retrieved)")

    lines.append("")
    lines.append("Retrieved knowledge-base documents:")
    knowledge_chunks = knowledge[:8]
    if knowledge_chunks:
        for doc in knowledge_chunks:
            doc_id = doc.get("id") or doc.get("chunk")
            lines.append(f"[{doc_id}] {doc.get('title', '')}".rstrip())
            lines.extend(doc.get("content", "").splitlines())
    else:
        lines.append("(No knowledge-base documents matched)")

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
    json_schema: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Execute inference via Ollama native /api/chat with strict num_ctx enforcement."""
    sampling_opts = get_qwen3_sampling_params(enable_thinking=enable_thinking, thinking_budget=thinking_budget)

    effective_ctx = max(num_ctx, OLLAMA_CONTEXT_LENGTH)
    base_body: dict[str, Any] = {
        "model": model,
        "stream": False,
        "format": json_schema or "json",
        "think": False,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ],
    }

    ollama_timeout = _inference_timeout_seconds("OLLAMA_TIMEOUT")
    cpu_fallback_timeout = _inference_timeout_seconds(
        "OLLAMA_CPU_FALLBACK_TIMEOUT"
    )
    cpu_fallback_ctx = int(os.environ.get("OLLAMA_CPU_FALLBACK_CONTEXT", "2048"))
    attempts = [
        {
            "num_ctx": effective_ctx,
            "temperature": sampling_opts["temperature"],
            "top_p": sampling_opts["top_p"],
            "top_k": sampling_opts["top_k"],
            "min_p": sampling_opts["min_p"],
            "repeat_penalty": sampling_opts.get("presence_penalty", 1.1),
        }
    ]
    if os.environ.get("OLLAMA_DISABLE_CPU_FALLBACK") != "1":
        attempts.append(
            {
                "num_ctx": cpu_fallback_ctx,
                "num_gpu": 0,
                "temperature": sampling_opts["temperature"],
                "top_p": sampling_opts["top_p"],
                "top_k": sampling_opts["top_k"],
                "min_p": sampling_opts["min_p"],
                "repeat_penalty": sampling_opts.get("presence_penalty", 1.1),
            }
        )

    first_http_error: urllib.error.HTTPError | None = None
    for attempt_index, options in enumerate(attempts):
        body = dict(base_body)
        body["options"] = options
        req = urllib.request.Request(
            f"{OLLAMA_URL}/api/chat",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        attempt_timeout = (
            cpu_fallback_timeout
            if attempt_index == len(attempts) - 1
            else ollama_timeout
        )
        try:
            with urllib.request.urlopen(req, timeout=attempt_timeout) as resp:
                data = json.loads(resp.read().decode("utf-8"))
                content = data.get("message", {}).get("content", "")
                parsed, _ = process_qwen3_thinking_output(
                    content, thinking_budget=thinking_budget
                )
                return parsed
        except (TimeoutError, urllib.error.URLError) as exc:
            if isinstance(exc, urllib.error.URLError) and getattr(exc, "reason", None):
                reason = str(exc.reason)
            else:
                reason = str(exc)
            first_http_error = None
            if attempt_index == len(attempts) - 1:
                timeout_label = (
                    "no Python timeout"
                    if attempt_timeout is None
                    else f"{attempt_timeout:g}s"
                )
                raise TimeoutError(
                    f"Ollama exceeded {timeout_label} "
                    f"(GPU and CPU attempts): {reason}"
                ) from exc
            continue
        except urllib.error.HTTPError as exc:
            try:
                error_body = exc.read().decode("utf-8", errors="replace")
            finally:
                exc.close()
            memory_error = any(
                marker in error_body.lower()
                for marker in (
                    "cuda",
                    "out of memory",
                    "out-of-memory",
                    "kv cache",
                    "cuda malloc",
                    "unable to allocate",
                )
            )
            first_http_error = exc
            if attempt_index == len(attempts) - 1 or not memory_error:
                raise
            continue

    if first_http_error is not None:
        raise first_http_error
    raise RuntimeError("Ollama returned no inference result")


def query_llama_server(
    system_prompt: str,
    user_prompt: str,
    enable_thinking: bool = False,
    thinking_budget: int = DEFAULT_THINKING_BUDGET,
    ctx_size: int = DEFAULT_NUM_CTX,
    json_schema: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Execute inference via llama-server with host prefix-caching and Pydantic json_schema."""
    sampling_opts = get_qwen3_sampling_params(enable_thinking=enable_thinking, thinking_budget=thinking_budget)
    schema = json_schema or get_synthesis_json_schema()

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
    llama_timeout = _inference_timeout_seconds("LLAMA_SERVER_TIMEOUT")
    with urllib.request.urlopen(req, timeout=llama_timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
        content = data.get("content", "")
        parsed, _ = process_qwen3_thinking_output(content, thinking_budget=thinking_budget)
        return parsed


def query_local_hf(
    system_prompt: str,
    user_prompt: str,
    enable_thinking: bool = False,
    thinking_budget: int = DEFAULT_THINKING_BUDGET,
    raise_errors: bool = False,
    model_path: str | None = None,
) -> dict[str, Any] | None:
    """Execute inference using local HuggingFace weights if transformers is present."""
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer

        selected_model_path = model_path or LOCAL_HF_PATH
        sampling_opts = get_qwen3_sampling_params(enable_thinking=enable_thinking, thinking_budget=thinking_budget)

        tokenizer = AutoTokenizer.from_pretrained(selected_model_path, local_files_only=True)
        model = AutoModelForCausalLM.from_pretrained(
            selected_model_path,
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
        if raise_errors:
            raise
        return None


# ---------------------------------------------------------------------------
# Deterministic Fallback Synthesizer
# ---------------------------------------------------------------------------

def deterministic_synthesize(
    evidence: dict[str, Any],
    query: str,
    knowledge: list[dict[str, Any]],
) -> dict[str, Any]:
    """Produce a conservative summary without inferring domain-specific causes."""
    events = evidence.get("events", [])
    data_gap_summaries = []
    for gap in evidence.get("data_gaps", []):
        prefix = f"Event {gap['event_id']}: " if gap.get("event_id") else ""
        data_gap_summaries.append(f"{prefix}{gap.get('detail', '')}")

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

    notable_events = [event for event in events if is_critical_event(event)]
    selected_events = (notable_events or events)[:5]
    citations = " ".join(
        f"[{event['event_id']}]"
        for event in selected_events
        if event.get("event_id")
    )
    if notable_events:
        summary = (
            f"Retrieved {len(events)} telemetry event(s); "
            f"{len(notable_events)} meet generic failure or decode-warning indicators. {citations}"
        ).strip()
    else:
        summary = (
            f"Retrieved {len(events)} telemetry event(s); no generic failure "
            f"indicators were detected. {citations}"
        ).strip()

    contributing = [
        {
            "source_id": event.get("source_id", 0),
            "timestamp_iso8601": format_iso8601_ns(
                event.get("timestamp_ns", event.get("timestamp", 0))
            ),
            "event_type": _event_type_label(event),
        }
        for event in selected_events
    ]
    clock_caveat = evidence.get("clock_caveat")
    return {
        "summary": summary,
        "likely_cause": None,
        "confidence": "low",
        "contributing_events": contributing,
        "uncertainty": [clock_caveat] if clock_caveat else [],
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
    sufficiency_gate_enabled: bool = True,
    auditor: CitationAuditor | None = None,
) -> tuple[dict[str, Any], ValidationReport, str]:
    """Run synthesis using retrieved telemetry and knowledge_base documents only."""
    knowledge = retrieve_relevant_knowledge(evidence, query, top_k=8)
    audit_engine = auditor or CitationAuditor()

    if sufficiency_gate_enabled:
        sufficiency = evaluate_sufficiency(evidence, query, knowledge)
        if not sufficiency.is_sufficient:
            quarantine_synth = create_insufficient_evidence_synthesis(sufficiency, evidence, query)
            report = audit_engine.audit(
                quarantine_synth,
                evidence,
                knowledge=knowledge,
                query=query,
            )
            return report.corrected_synthesis, report, "Stage 0 Epistemic Sufficiency Gate (Quarantined)"

    guarded_evidence, was_compacted, _ = preflight_budget_guard(
        evidence,
        query,
        knowledge,
        effective_num_ctx=effective_num_ctx,
        enable_thinking=enable_thinking,
    )
    context_text = build_llm_context(
        guarded_evidence,
        query,
        knowledge,
        effective_num_ctx=effective_num_ctx,
        enable_thinking=enable_thinking,
        _skip_budget_guard=True,
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
        synthesis = deterministic_synthesize(guarded_evidence, query, knowledge)

    # Clean schema validation
    validated_obj = validate_synthesis_schema(synthesis)
    synthesis_dict = validated_obj.model_dump()
    _resolve_contributing_event_refs(synthesis_dict, guarded_evidence)

    # 7. Post-synthesis 4-Tier Citation & Warrant Audit
    report = audit_engine.audit(
        synthesis_dict,
        guarded_evidence,
        knowledge=knowledge,
        query=query,
    )
    return report.corrected_synthesis, report, model_used
