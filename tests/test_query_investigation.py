from __future__ import annotations

from datetime import timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

import bridge.investigation as investigation
from bridge.investigation import (
    ANALYSIS_SCHEMA,
    LocalModelBackend,
    LocalModelUnavailable,
    PLAN_SCHEMA,
    REVIEW_SCHEMA,
    local_wall_time_to_epoch_ns,
    run_investigation,
)
from bridge.retrieval_ipc import JavaRetrievalRequest, JavaRetrievalResult
from fss_py.model import EventRecord
from fss_py.schema import load_schema_file

ROOT = Path(__file__).resolve().parents[1]
QUERY = "Find the root cause from 2024-01-01 10:00 to 10:01"
START = "2024-01-01T10:00:00"
END = "2024-01-01T10:01:00"


def test_local_time_conversion_preserves_nanoseconds_and_rejects_dst_gaps():
    value = local_wall_time_to_epoch_ns("2024-01-01T10:00:00.123456789", timezone.utc)
    expected = local_wall_time_to_epoch_ns("2024-01-01T10:00:00", timezone.utc) + 123_456_789
    assert value == expected

    zone = ZoneInfo("America/New_York")
    with pytest.raises(ValueError, match="ambiguous"):
        local_wall_time_to_epoch_ns("2024-11-03T01:30:00", zone)
    with pytest.raises(ValueError, match="does not exist"):
        local_wall_time_to_epoch_ns("2024-03-10T02:30:00", zone)


def _is_analysis_schema(json_schema):
    return set(json_schema.get("properties", {})) == set(ANALYSIS_SCHEMA["properties"])


def test_huggingface_backend_uses_resolved_model_path_and_reports_missing_runtime(monkeypatch):
    backend = LocalModelBackend("huggingface", model_path="C:/models/fss-model")
    requested_paths: list[str | None] = []

    def fake_inference(_system_prompt, _user_prompt, *, raise_errors, model_path):
        assert raise_errors is True
        requested_paths.append(model_path)
        return {"result": "ok"}

    monkeypatch.setattr(investigation, "query_local_hf", fake_inference)
    assert backend.generate("system", "user", {}) == {"result": "ok"}
    assert requested_paths == ["C:/models/fss-model"]

    def missing_runtime(*_args, **_kwargs):
        raise ModuleNotFoundError("transformers")

    monkeypatch.setattr(investigation, "query_local_hf", missing_runtime)
    with pytest.raises(LocalModelUnavailable, match="failed during local inference"):
        backend.generate("system", "user", {})


class _FakeModel:
    model_label = "Fake local model"

    def __init__(self, record: EventRecord, reviews: list[dict]):
        self.event_reference = {
            "source_id": record.source_id,
            "timestamp_ns": record.timestamp_ns,
            "event_type": record.event_type,
        }
        self.reviews = list(reviews)
        self.review_calls = 0

    def generate(self, system_prompt, user_prompt, json_schema):
        if json_schema is PLAN_SCHEMA:
            return {
                "goal": "Determine the cause of the reported event",
                "timeframe_basis": "2024-01-01 10:00 to 10:01",
                "timeframe_is_bounded": True,
                "start_local": START,
                "end_local": END,
                "source": "all",
                "event_type": None,
                "clarification": None,
            }
        if _is_analysis_schema(json_schema):
            return {
                "root_cause_hypothesis": {
                    "statement": "The retrieved event is consistent with an avoidance action.",
                    "citations": [self.event_reference],
                },
                "observed_facts": [
                    {
                        "statement": "Source 1 emitted an AVOIDANCE event.",
                        "citations": [self.event_reference],
                    }
                ],
                "inferences": [
                    {
                        "statement": "The event may be a response to a safety condition.",
                        "citations": [self.event_reference],
                        "confidence": "low",
                    }
                ],
                "uncertainties": [],
                "data_gaps": [],
            }
        if json_schema is REVIEW_SCHEMA:
            result = self.reviews[self.review_calls]
            self.review_calls += 1
            return result
        raise AssertionError("Unexpected JSON response schema")


class _FakeRetriever:
    def __init__(self, record: EventRecord):
        self.record = record
        self.requests: list[JavaRetrievalRequest] = []

    def retrieve(self, request: JavaRetrievalRequest) -> JavaRetrievalResult:
        self.requests.append(request)
        return JavaRetrievalResult(request, (self.record,), 1, False)


class _BadCitationOnceModel(_FakeModel):
    def __init__(self, record: EventRecord, reviews: list[dict]):
        super().__init__(record, reviews)
        self.analysis_calls = 0
        self.analysis_schemas = []
        self.analysis_prompts = []
        self.review_prompts = []

    def generate(self, system_prompt, user_prompt, json_schema):
        if _is_analysis_schema(json_schema):
            self.analysis_calls += 1
            self.analysis_schemas.append(json_schema)
            self.analysis_prompts.append(user_prompt)
            result = super().generate(system_prompt, user_prompt, json_schema)
            if self.analysis_calls == 1:
                result["root_cause_hypothesis"]["citations"] = [
                    {"source_id": 2, "timestamp_ns": 1, "event_type": 2},
                    {"source_id": "2", "timestamp_ns": 1, "event_type": 2},
                ]
            return result
        if json_schema is REVIEW_SCHEMA:
            self.review_prompts.append(user_prompt)
        return super().generate(system_prompt, user_prompt, json_schema)


def _review(start: str, end: str) -> dict:
    return {
        "decision": "retrieve_more",
        "rationale": "The current events do not establish the upstream cause.",
        "missing_information": ["The upstream agent's state."],
        "clarification": None,
        "next_retrieval": {
            "start_local": start,
            "end_local": end,
            "source": "all",
            "event_type": None,
        },
    }


def _event() -> EventRecord:
    timestamp = local_wall_time_to_epoch_ns(START, timezone.utc)
    return EventRecord(1, timestamp, 0x20, bytes(7))


def _patch_context(monkeypatch):
    monkeypatch.setattr(investigation, "retrieve_relevant_knowledge", lambda *_args, **_kwargs: [])
    monkeypatch.setattr(investigation, "build_llm_context", lambda *_args, **_kwargs: "evidence")


def test_missing_timeframe_returns_clarification_without_java_call():
    class Planner:
        model_label = "Fake local model"

        def generate(self, system_prompt, user_prompt, json_schema):
            assert json_schema is PLAN_SCHEMA
            assert system_prompt
            assert user_prompt
            return {
                "goal": "Investigate the cause",
                "timeframe_basis": None,
                "timeframe_is_bounded": False,
                "start_local": None,
                "end_local": None,
                "source": "all",
                "event_type": None,
                "clarification": "Which event window should I inspect?",
            }

    result = run_investigation("Why did this happen?", model=Planner())
    assert result["status"] == "needs_clarification"
    assert result["retrieval_history"] == []


def test_empty_java_retrieval_stops_before_analysis_with_request_details():
    class Planner:
        model_label = "Fake local model"

        def generate(self, system_prompt, user_prompt, json_schema):
            assert json_schema is PLAN_SCHEMA
            return {
                "goal": "Determine the cause of the reported event",
                "timeframe_basis": "2024-01-01 10:00 to 10:01",
                "timeframe_is_bounded": True,
                "start_local": START,
                "end_local": END,
                "source": "all",
                "event_type": None,
                "clarification": None,
            }

    class EmptyRetriever:
        def __init__(self):
            self.requests = []

        def retrieve(self, request):
            self.requests.append(request)
            return JavaRetrievalResult(request, (), 0, False)

    retriever = EmptyRetriever()
    with pytest.raises(
        investigation.InvestigationError,
        match=r"returned no telemetry records.*total_matches=0",
    ):
        run_investigation(
            QUERY,
            model=Planner(),
            retriever=retriever,
            registry=load_schema_file(ROOT / "schemas" / "drone_v0.yaml"),
            local_zone=timezone.utc,
        )

    assert len(retriever.requests) == 1
    assert retriever.requests[0].source == "all"
    assert retriever.requests[0].start_time_ns == local_wall_time_to_epoch_ns(
        START, timezone.utc
    )


def test_explicit_ampm_range_overrides_incorrect_planner_wall_times():
    query = (
        "Investigate why agent 2 left formation on Nov 22th 2026 "
        "between 3.32pm to 3.35pm."
    )
    parsed_without_year = investigation._parse_explicit_timeframe(
        query.replace(" 2026", ""), 2026
    )
    assert parsed_without_year is not None
    assert parsed_without_year["start_local"] == "2026-11-22T15:32:00"
    assert parsed_without_year["end_local"] == "2026-11-22T15:35:00"

    expected_start = local_wall_time_to_epoch_ns(
        "2026-11-22T15:32:00", timezone.utc
    )
    expected_end = local_wall_time_to_epoch_ns(
        "2026-11-22T15:35:00", timezone.utc
    )

    class Planner:
        model_label = "Fake local model"

        def generate(self, system_prompt, user_prompt, json_schema):
            assert json_schema is PLAN_SCHEMA
            return {
                "goal": "Determine why agent 2 left formation",
                "timeframe_basis": "Nov 22th 2026 between 3.32pm to 3.35pm",
                "timeframe_is_bounded": True,
                "start_local": "2026-11-22T21:32:00",
                "end_local": "2026-11-22T21:35:00",
                "source": "all",
                "event_type": None,
                "clarification": None,
            }

    class EmptyRetriever:
        def __init__(self):
            self.request = None

        def retrieve(self, request):
            self.request = request
            return JavaRetrievalResult(request, (), 0, False)

    retriever = EmptyRetriever()
    with pytest.raises(investigation.InvestigationError, match="no telemetry records"):
        run_investigation(
            query,
            model=Planner(),
            retriever=retriever,
            registry=load_schema_file(ROOT / "schemas" / "drone_v0.yaml"),
            local_zone=timezone.utc,
        )

    assert retriever.request is not None
    assert retriever.request.start_time_ns == expected_start
    assert retriever.request.end_time_ns == expected_end


def test_followup_budget_is_three_total_and_exact_duplicates_are_blocked(monkeypatch):
    _patch_context(monkeypatch)
    record = _event()
    model = _FakeModel(
        record,
        [
            _review("2024-01-01T10:01:00", "2024-01-01T10:02:00"),
            _review("2024-01-01T10:02:00", "2024-01-01T10:03:00"),
            _review("2024-01-01T10:03:00", "2024-01-01T10:04:00"),
        ],
    )
    retriever = _FakeRetriever(record)
    result = run_investigation(
        QUERY,
        model=model,
        retriever=retriever,
        registry=load_schema_file(ROOT / "schemas" / "drone_v0.yaml"),
        local_zone=timezone.utc,
    )
    assert result["status"] == "insufficient_evidence"
    assert len(retriever.requests) == 3

    repeated = _FakeModel(record, [_review(START, END)])
    repeated_retriever = _FakeRetriever(record)
    repeated_result = run_investigation(
        QUERY,
        model=repeated,
        retriever=repeated_retriever,
        registry=load_schema_file(ROOT / "schemas" / "drone_v0.yaml"),
        local_zone=timezone.utc,
    )
    assert repeated_result["status"] == "insufficient_evidence"
    assert len(repeated_retriever.requests) == 1
    assert "identical" in repeated_result["reviewer_rationale"]


def test_unknown_analyst_citation_is_removed_and_analysis_continues(monkeypatch):
    monkeypatch.setattr(
        investigation, "retrieve_relevant_knowledge", lambda *_args, **_kwargs: []
    )
    record = _event()
    model = _BadCitationOnceModel(
        record,
        [
            {
                "decision": "clarify",
                "rationale": "The cause needs operator confirmation.",
                "missing_information": ["The operational configuration."],
                "clarification": "Which configuration was active?",
                "next_retrieval": None,
            }
        ],
    )
    retriever = _FakeRetriever(record)
    result = run_investigation(
        QUERY,
        model=model,
        retriever=retriever,
        registry=load_schema_file(ROOT / "schemas" / "drone_v0.yaml"),
        local_zone=timezone.utc,
    )

    assert model.analysis_calls == 1
    citation_variants = model.analysis_schemas[0]["properties"]["root_cause_hypothesis"][
        "properties"
    ]["citations"]["items"]["oneOf"]
    assert citation_variants[0]["required"] == [
        "source_id",
        "timestamp_ns",
        "event_type",
    ]
    assert all("ev-" not in prompt for prompt in model.analysis_prompts)
    assert all("ev-" not in prompt for prompt in model.review_prompts)
    assert f'"timestamp_ns": {model.event_reference["timestamp_ns"]}' in model.analysis_prompts[0]
    assert '"event_type": 32' in model.analysis_prompts[0]
    assert result["root_cause_hypothesis"] is None
    assert result["observed_facts"]
    assert any("unknown telemetry reference discarded" in gap for gap in result["data_gaps"])
    assert any("malformed telemetry reference discarded" in gap for gap in result["data_gaps"])
    assert len(model.review_prompts) == 1
    assert all(not key.startswith("ev-") for key in result["references"])
    assert result["status"] == "needs_clarification"
    assert len(retriever.requests) == 1


def test_document_only_hypothesis_is_omitted_and_review_continues(monkeypatch):
    record = _event()
    document = {"id": "doc-kb-01", "chunk": "doc-kb-01"}
    monkeypatch.setattr(
        investigation,
        "retrieve_relevant_knowledge",
        lambda *_args, **_kwargs: [document],
    )

    class _DocumentOnlyOnceModel(_FakeModel):
        def __init__(self, record, reviews):
            super().__init__(record, reviews)
            self.analysis_calls = 0
            self.analysis_prompts = []

        def generate(self, system_prompt, user_prompt, json_schema):
            if _is_analysis_schema(json_schema):
                self.analysis_calls += 1
                self.analysis_prompts.append(user_prompt)
                result = super().generate(system_prompt, user_prompt, json_schema)
                if self.analysis_calls == 1:
                    result["root_cause_hypothesis"]["citations"] = ["doc-kb-01"]
                return result
            return super().generate(system_prompt, user_prompt, json_schema)

    model = _DocumentOnlyOnceModel(
        record,
        [
            {
                "decision": "clarify",
                "rationale": "The cause needs operator confirmation.",
                "missing_information": ["The operational configuration."],
                "clarification": "Which configuration was active?",
                "next_retrieval": None,
            }
        ],
    )
    result = run_investigation(
        QUERY,
        model=model,
        retriever=_FakeRetriever(record),
        registry=load_schema_file(ROOT / "schemas" / "drone_v0.yaml"),
        local_zone=timezone.utc,
    )

    assert model.analysis_calls == 1
    assert f'"timestamp_ns": {model.event_reference["timestamp_ns"]}' in model.analysis_prompts[0]
    assert result["root_cause_hypothesis"] is None
    assert any("no valid telemetry citation" in gap for gap in result["data_gaps"])
    assert result["status"] == "needs_clarification"


def test_ambiguous_source_time_type_reference_is_discarded():
    duplicate_reference = {
        "source_id": 1,
        "timestamp_ns": 10,
        "event_type": "0x20",
    }
    evidence = {
        "events": [
            {"event_id": "ev-000000000001", **duplicate_reference},
            {"event_id": "ev-000000000002", **duplicate_reference},
        ]
    }
    raw_analysis = {
        "root_cause_hypothesis": {
            "statement": "The event indicates an avoidance action.",
            "citations": [
                {"source_id": 1, "timestamp_ns": 10, "event_type": 0x20}
            ],
        },
        "observed_facts": [],
        "inferences": [],
        "uncertainties": [],
        "data_gaps": [],
    }

    result = investigation._validate_analysis(raw_analysis, evidence, [])

    assert result["root_cause_hypothesis"] is None
    assert any(
        "ambiguous telemetry reference discarded" in gap
        for gap in result["data_gaps"]
    )


def test_retrieved_knowledge_and_provenance_reach_the_analysis_prompt(monkeypatch):
    record = _event()
    document = {
        "id": "doc-kb-calibration-01",
        "chunk": "doc-kb-calibration-01",
        "kind": "knowledge-document",
        "domain": "knowledge_base",
        "title": "Calibration Manual",
        "content": "The quasar sensor reports a drift warning after calibration.",
        "source_filename": "reference/calibration.md",
        "section": "Drift Warnings",
        "page_number": 4,
        "schema_version": "sensor-v3",
    }

    class RagAwareModel(_FakeModel):
        analysis_prompt = ""

        def generate(self, system_prompt, user_prompt, json_schema):
            if _is_analysis_schema(json_schema):
                self.analysis_prompt = user_prompt
            if json_schema is REVIEW_SCHEMA:
                return {
                    "decision": "clarify",
                    "rationale": "The cause needs operator confirmation.",
                    "missing_information": ["The operational configuration."],
                    "clarification": "Which sensor configuration was active?",
                    "next_retrieval": None,
                }
            return super().generate(system_prompt, user_prompt, json_schema)

    model = RagAwareModel(record, [])
    retriever = _FakeRetriever(record)
    monkeypatch.setattr(
        investigation,
        "retrieve_relevant_knowledge",
        lambda *_args, **_kwargs: [document],
    )
    result = run_investigation(
        QUERY,
        model=model,
        retriever=retriever,
        registry=load_schema_file(ROOT / "schemas" / "drone_v0.yaml"),
        local_zone=timezone.utc,
    )

    assert "quasar sensor reports a drift warning" in model.analysis_prompt
    assert 'source_filename="reference/calibration.md"' in model.analysis_prompt
    assert 'section="Drift Warnings"' in model.analysis_prompt
    assert 'page_number="4"' in model.analysis_prompt
    assert 'schema_version="sensor-v3"' in model.analysis_prompt
    assert result["knowledge_documents"] == [
        {
            "id": document["id"],
            "title": document["title"],
            "source_filename": document["source_filename"],
            "section": document["section"],
            "page_number": document["page_number"],
            "schema_version": document["schema_version"],
        }
    ]
