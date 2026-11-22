"""Pre-generation sufficiency checks based on retrieved telemetry and data gaps."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from bridge.bin_ingestor import is_critical_event


class SufficiencyStatus(StrEnum):
    SUFFICIENT = "SUFFICIENT"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"


@dataclass(frozen=True)
class SufficiencyCheckResult:
    name: str
    passed: bool
    score: float
    details: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "passed": self.passed,
            "score": round(self.score, 4),
            "details": self.details,
        }


@dataclass
class SufficiencyResult:
    is_sufficient: bool
    status: SufficiencyStatus
    score: float
    reason: str
    checks: list[SufficiencyCheckResult] = field(default_factory=list)
    quarantine_payload: dict[str, Any] = field(default_factory=dict)
    bypass_synthesis: bool = True

    def to_dict(self) -> dict[str, Any]:
        return {
            "is_sufficient": self.is_sufficient,
            "status": str(self.status),
            "score": round(self.score, 4),
            "reason": self.reason,
            "bypass_synthesis": self.bypass_synthesis,
            "checks": [check.to_dict() for check in self.checks],
            "quarantine_payload": self.quarantine_payload,
        }


def _event_to_dict(event: Any) -> dict[str, Any]:
    if isinstance(event, dict):
        result = dict(event)
    elif hasattr(event, "model_dump"):
        result = event.model_dump()
    elif hasattr(event, "to_dict"):
        result = event.to_dict()
    elif hasattr(event, "__dict__"):
        result = dict(vars(event))
    else:
        result = {}
    if "timestamp" in result and "timestamp_ns" not in result:
        result["timestamp_ns"] = result["timestamp"]
    if "source" in result and "source_id" not in result:
        result["source_id"] = result["source"]
    return result


def _requested_sources(query: str) -> set[int]:
    entity_labels = r"source|node|device|entity|agent|host|service|component|unit|process"
    return {
        int(match.group(1))
        for match in re.finditer(
            rf"\b(?:{entity_labels})\s*#?\s*([0-9]{{1,10}})\b",
            query,
            re.IGNORECASE,
        )
    }


def _check_data_availability(evidence: dict[str, Any]) -> tuple[bool, str, dict[str, Any]]:
    events = evidence.get("events", [])
    retrieval = evidence.get("retrieval") or {}
    if not events or retrieval.get("returned") == 0 or retrieval.get("status") == "EMPTY":
        return False, "No telemetry events were retrieved for the requested window.", {
            "gate": "EMPTY_RETRIEVAL",
            "events_count": 0,
            "retrieval_status": retrieval.get("status", "EMPTY"),
            "recommendation": "Review the time window and retrieval filters.",
        }

    unusable = {"UNKNOWN_EVENT_TYPE", "MALFORMED_PAYLOAD"}
    statuses = [str(event.get("decode_status", "")).upper() for event in events]
    if statuses and all(status in unusable for status in statuses):
        return False, "All retrieved events are unknown or malformed and cannot be interpreted.", {
            "gate": "UNINTERPRETABLE_DATA",
            "events_count": len(events),
            "decode_statuses": sorted(set(statuses)),
            "recommendation": "Check the configured payload schema and raw transport data.",
        }

    for gap in evidence.get("data_gaps", []):
        kind = str(gap.get("kind", "")).upper()
        if "UNRECORDED" in kind or "BLACKOUT" in kind:
            return False, f"An explicit telemetry gap is reported: {gap.get('detail', kind)}", {
                "gate": "EXPLICIT_DATA_GAP",
                "gap_kind": kind,
                "recommendation": "Retrieve adjacent or alternate telemetry if available.",
            }
    return True, "Retrieved telemetry is available for analysis.", {}


def _check_source_coverage(
    evidence: dict[str, Any],
    query: str,
) -> tuple[bool, float, str, set[int]]:
    requested = _requested_sources(query)
    if not requested:
        return True, 1.0, "No explicit source identifier was requested.", set()
    available = {int(source) for source in evidence.get("sources", [])}
    available.update(
        int(event["source_id"])
        for event in evidence.get("events", [])
        if event.get("source_id") is not None
    )
    missing = requested - available
    if missing:
        return (
            False,
            0.0,
            f"Requested source identifiers {sorted(missing)} are absent from retrieved telemetry.",
            missing,
        )
    return True, 1.0, f"All requested source identifiers {sorted(requested)} are present.", set()


def evaluate_sufficiency(
    evidence: dict[str, Any],
    query: str,
    knowledge: list[dict[str, Any]] | None = None,
    threshold: float = 0.5,
) -> SufficiencyResult:
    """Gate synthesis only on data availability and explicit source coverage."""
    normalized = {
        **evidence,
        "events": [_event_to_dict(event) for event in evidence.get("events", [])],
    }
    available, availability_details, gate_metadata = _check_data_availability(normalized)
    availability_check = SufficiencyCheckResult(
        name="telemetry_availability",
        passed=available,
        score=1.0 if available else 0.0,
        details=availability_details,
    )
    checks = [availability_check]
    if not available:
        quarantine = {
            "status": "QUARANTINED",
            "reason": availability_details,
            **gate_metadata,
            "data_gaps": normalized.get("data_gaps", []),
        }
        return SufficiencyResult(
            is_sufficient=False,
            status=SufficiencyStatus.INSUFFICIENT_EVIDENCE,
            score=0.0,
            reason=availability_details,
            checks=checks,
            quarantine_payload=quarantine,
            bypass_synthesis=True,
        )

    coverage_passed, coverage_score, coverage_details, missing_sources = (
        _check_source_coverage(normalized, query)
    )
    checks.append(
        SufficiencyCheckResult(
            name="requested_source_coverage",
            passed=coverage_passed,
            score=coverage_score,
            details=coverage_details,
        )
    )
    notable_events = [
        event for event in normalized["events"] if is_critical_event(event)
    ]
    checks.append(
        SufficiencyCheckResult(
            name="telemetry_indicators",
            passed=True,
            score=1.0,
            details=f"Found {len(notable_events)} event(s) with generic failure or decode-warning indicators.",
        )
    )
    knowledge_count = len(knowledge or [])
    checks.append(
        SufficiencyCheckResult(
            name="knowledge_base_context",
            passed=True,
            score=1.0,
            details=f"Retrieved {knowledge_count} knowledge_base document(s); documents are optional.",
        )
    )

    score = 0.75 * coverage_score + 0.25
    is_sufficient = coverage_passed and score >= threshold
    reason = (
        "Retrieved telemetry is sufficient for a grounded analysis."
        if is_sufficient
        else coverage_details
    )
    quarantine = {}
    if not is_sufficient:
        quarantine = {
            "status": "QUARANTINED",
            "reason": reason,
            "sufficiency_score": round(score, 4),
            "missing_sources": sorted(missing_sources),
            "data_gaps": normalized.get("data_gaps", []),
            "recommendation": "Review the retrieval source filter or broaden the query scope.",
        }
    return SufficiencyResult(
        is_sufficient=is_sufficient,
        status=(
            SufficiencyStatus.SUFFICIENT
            if is_sufficient
            else SufficiencyStatus.INSUFFICIENT_EVIDENCE
        ),
        score=score,
        reason=reason,
        checks=checks,
        quarantine_payload=quarantine,
        bypass_synthesis=not is_sufficient,
    )


def create_insufficient_evidence_synthesis(
    sufficiency: SufficiencyResult,
    evidence: dict[str, Any],
    query: str,
) -> dict[str, Any]:
    """Create an explicit quarantine result without generating unsupported claims."""
    data_gap_summaries = []
    for gap in evidence.get("data_gaps", []):
        prefix = f"Event {gap['event_id']}: " if gap.get("event_id") else ""
        data_gap_summaries.append(f"{prefix}{gap.get('detail', '')}")
    if sufficiency.quarantine_payload:
        quarantine = sufficiency.quarantine_payload
        data_gap_summaries.append(
            f"[DATA_GAPS_QUARANTINE] {quarantine.get('reason', '')} "
            f"Recommendation: {quarantine.get('recommendation', '')}"
        )
    empty = not evidence.get("events")
    return {
        "summary": (
            "No events recorded or retrieved in the requested time window."
            if empty
            else f"INSUFFICIENT EVIDENCE for query '{query}': {sufficiency.reason}"
        ),
        "likely_cause": None,
        "confidence": "low" if not empty else "high",
        "contributing_events": [],
        "uncertainty": [sufficiency.reason],
        "data_gaps": data_gap_summaries,
        "quarantine_card": sufficiency.quarantine_payload,
    }


evaluate_evidence_sufficiency = evaluate_sufficiency
