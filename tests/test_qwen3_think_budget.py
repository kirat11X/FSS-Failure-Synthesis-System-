"""Unit Tests for Qwen3 Decoding Stability and Think-Budget Guard.

Spec requirement:
  Simulate an unclosed <think> stream exceeding thinking_budget tokens without token 151668.
  Guard injects </think> close prompt, emits reasoning_truncated=True, and produces schema-valid JSON.
"""

from __future__ import annotations

import json
import pytest
from bridge.synthesizer import (
    DEFAULT_THINKING_BUDGET,
    IM_END_TOKEN_ID,
    THINK_CLOSE_TOKEN_ID,
    THINK_CUTOFF_INJECTION,
    FailureSynthesis,
    Qwen3ThinkBudgetGuard,
    get_qwen3_sampling_params,
    get_synthesis_json_schema,
    process_qwen3_thinking_output,
    validate_synthesis_schema,
)


def test_qwen3_sampling_params_thinking_vs_non_thinking():
    """Verify Qwen3 sampling parameters adhere to documented stability rules."""
    # Thinking mode must NEVER use greedy decoding
    think_params = get_qwen3_sampling_params(enable_thinking=True, thinking_budget=1024)
    assert think_params["enable_thinking"] is True
    assert think_params["do_sample"] is True
    assert think_params["temperature"] == 0.6
    assert think_params["top_p"] == 0.95
    assert think_params["top_k"] == 20
    assert think_params["min_p"] == 0.0
    assert think_params["presence_penalty"] == 1.5
    assert think_params["thinking_budget"] == 1024

    # Non-thinking mode
    non_think_params = get_qwen3_sampling_params(enable_thinking=False)
    assert non_think_params["enable_thinking"] is False
    assert non_think_params["do_sample"] is True
    assert non_think_params["temperature"] == 0.7
    assert non_think_params["top_p"] == 0.8
    assert non_think_params["top_k"] == 20
    assert non_think_params["min_p"] == 0.0


def test_think_budget_guard_unclosed_stream_simulation():
    """Simulate an unclosed token stream exceeding thinking budget (> 1024 tokens) without token 151668."""
    budget = 1024
    guard = Qwen3ThinkBudgetGuard(thinking_budget=budget)

    # 1. Start thinking tag
    t, stop = guard.feed_token(None, "<think>\n")
    assert guard.inside_think is True
    assert stop is False

    # 2. Feed 1023 thinking tokens without token 151668
    for i in range(budget - 1):
        t, stop = guard.feed_token(None, f"analyzing_step_{i} ")
        assert stop is False
        assert guard.reasoning_truncated is False

    # 3. Feed the 1024th token: must trigger cutoff injection and reasoning_truncated = True
    t_cutoff, stop = guard.feed_token(None, "overflowing_step ")
    assert stop is True
    assert guard.reasoning_truncated is True
    assert guard.think_closed is True
    assert guard.inside_think is False
    assert t_cutoff == THINK_CUTOFF_INJECTION
    assert "</think>" in t_cutoff


def test_think_budget_guard_normal_closing_token():
    """Verify that emitting token 151668 cleanly closes thinking without truncation."""
    guard = Qwen3ThinkBudgetGuard(thinking_budget=1024)
    guard.feed_token(None, "<think>\nAnalyzing facts...")
    assert guard.inside_think is True

    # Closing token 151668 emitted
    t, stop = guard.feed_token(THINK_CLOSE_TOKEN_ID, "</think>")
    assert stop is False
    assert guard.think_closed is True
    assert guard.inside_think is False
    assert guard.reasoning_truncated is False


def test_process_qwen3_thinking_output_with_unclosed_loop():
    """Verify that an unclosed raw output string exceeding budget gets guarded and parses valid JSON."""
    long_reasoning = "Analyzing telemetry events in depth and considering clock caveats. " * 150  # ~1500 tokens
    valid_json = {
        "summary": "Drone 474 left formation [ev-1c432aca07e2] due to collision risk [ev-722d707b684c].",
        "likely_cause": "LiDAR timeout on drone 298 [ev-2e92ae032f03].",
        "confidence": "medium",
        "contributing_events": [
            {"source_id": 298, "timestamp": 1791454023050000000, "event_type": "LIDAR_STATUS", "event_id": "ev-2e92ae032f03"},
            {"source_id": 474, "timestamp": 1791454023120000000, "event_type": "AVOIDANCE", "event_id": "ev-722d707b684c"},
        ],
        "uncertainty": ["Clocks unsynchronized"],
        "data_gaps": [],
    }

    raw_output = f"<think>\n{long_reasoning}\n\n{json.dumps(valid_json)}"

    # Process through guard
    parsed_json, reasoning_truncated = process_qwen3_thinking_output(raw_output, thinking_budget=1024)

    assert reasoning_truncated is True
    assert parsed_json is not None
    assert parsed_json["reasoning_truncated"] is True
    assert parsed_json["summary"] == valid_json["summary"]

    # Validate against Pydantic schema
    validated = validate_synthesis_schema(parsed_json)
    assert validated.confidence == "medium"
    assert validated.reasoning_truncated is True
    assert len(validated.contributing_events) == 2


def test_synthesis_json_schema_validity():
    """Verify decode-time JSON Schema generated for llama-server."""
    schema = get_synthesis_json_schema()
    assert schema["type"] == "object"
    assert "summary" in schema["properties"]
    assert "confidence" in schema["properties"]
    assert schema["properties"]["confidence"]["enum"] == ["low", "medium", "high"]
    assert "contributing_events" in schema["properties"]
    assert "reasoning_truncated" in schema["properties"]
