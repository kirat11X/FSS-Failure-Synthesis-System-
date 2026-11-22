"""Unit and adversarial tests for Stage 0 Pre-Generation Sufficiency Gate.

Tests:
  1. Empty, uninterpretable, and explicitly gapped retrievals are quarantined.
  2. Explicitly requested source coverage is checked without domain-specific entity labels.
  3. Available telemetry remains analyzable without hardcoded anomaly or cause rules.
  4. Knowledge-base documents are optional context.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from bridge.sufficiency_gate import (
    SufficiencyStatus,
    create_insufficient_evidence_synthesis,
    evaluate_sufficiency,
)
from fss_py.decoder import decode_all
from fss_py.evidence import build_evidence
from fss_py.model import ALL, RetrievalRequest, RetrievalResult
from fss_py.schema import load_schema_file
from fss_py.wire import iter_envelopes

ROOT_DIR = Path(__file__).resolve().parent.parent


class TestSufficiencyGate(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fixture_path = ROOT_DIR / "fixtures" / "golden" / "scenario_298_474.bin"
        cls.schema_path = ROOT_DIR / "schemas" / "drone_v0.yaml"
        cls.schema = load_schema_file(cls.schema_path)

        raw = cls.fixture_path.read_bytes()
        events = list(iter_envelopes(raw))
        cls.decoded = decode_all(events, cls.schema)
        cls.evidence_scenario = build_evidence(
            cls.decoded, query="Why did drone 474 leave formation?"
        )

    def test_01_empty_retrieval_deterministic_short_circuit(self):
        """0-event retrieval must deterministically short-circuit to INSUFFICIENT_EVIDENCE."""
        empty_evidence = {
            "format": "fss-evidence/v0",
            "query": "Why did drone 474 leave?",
            "retrieval": {"status": "EMPTY", "returned": 0},
            "sources": [],
            "events": [],
            "data_gaps": [],
        }
        res = evaluate_sufficiency(empty_evidence, "Why did drone 474 leave?")
        self.assertFalse(res.is_sufficient)
        self.assertEqual(res.status, SufficiencyStatus.INSUFFICIENT_EVIDENCE)
        self.assertEqual(res.score, 0.0)
        self.assertTrue(res.bypass_synthesis)
        self.assertIn("No telemetry events were retrieved", res.reason)
        self.assertEqual(res.quarantine_payload.get("status"), "QUARANTINED")

        # Verify quarantine synthesis
        synth = create_insufficient_evidence_synthesis(res, empty_evidence, "Why did drone 474 leave?")
        self.assertEqual(synth["summary"], "No events recorded or retrieved in the requested time window.")
        self.assertIsNone(synth["likely_cause"])
        self.assertEqual(synth["contributing_events"], [])

    def test_02_uninterpretable_data_gaps_short_circuit(self):
        """When all retrieved events have uninterpretable statuses, gate short-circuits."""
        uninterpretable_evidence = {
            "format": "fss-evidence/v0",
            "query": "Why did drone fail?",
            "sources": [474],
            "events": [
                {
                    "event_id": "ev-000000000001",
                    "source_id": 474,
                    "decode_status": "UNKNOWN_EVENT_TYPE",
                    "fields": {},
                },
                {
                    "event_id": "ev-000000000002",
                    "source_id": 474,
                    "decode_status": "MALFORMED_PAYLOAD",
                    "fields": {},
                },
            ],
            "data_gaps": [{"kind": "UNKNOWN_EVENT_TYPE", "detail": "Missing schema"}],
        }
        res = evaluate_sufficiency(uninterpretable_evidence, "Why did drone fail?")
        self.assertFalse(res.is_sufficient)
        self.assertEqual(res.status, SufficiencyStatus.INSUFFICIENT_EVIDENCE)
        self.assertTrue(res.bypass_synthesis)
        self.assertIn("uninterpretable", res.reason)

    def test_03_spanning_data_gap_short_circuit(self):
        """An explicitly reported unrecorded gap triggers the generic availability gate."""
        spanning_evidence = {
            "format": "fss-evidence/v0",
            "query": "Why did drone 474 leave formation?",
            "sources": [474],
            "events": [{"event_id": "ev-111111111111", "source_id": 474, "decode_status": "SUCCESS", "fields": {}}],
            "data_gaps": [
                {
                    "kind": "UNRECORDED_TIME_GAP",
                    "detail": "Unrecorded data gap spanning requested failure window between 1791454020 and 1791454025.",
                }
            ],
        }
        res = evaluate_sufficiency(spanning_evidence, "Why did drone 474 leave formation?")
        self.assertFalse(res.is_sufficient)
        self.assertEqual(res.status, SufficiencyStatus.INSUFFICIENT_EVIDENCE)
        self.assertIn("explicit telemetry gap", res.reason)

    def test_04_requested_source_present(self):
        """An explicitly named source identifier is checked against retrieved telemetry."""
        res = evaluate_sufficiency(self.evidence_scenario, "Why did source 474 stop?")
        self.assertTrue(res.is_sufficient)
        self.assertEqual(res.status, SufficiencyStatus.SUFFICIENT)
        self.assertFalse(res.bypass_synthesis)
        self.assertGreaterEqual(res.score, 0.5)

        source_check = next(c for c in res.checks if c.name == "requested_source_coverage")
        self.assertTrue(source_check.passed)
        self.assertEqual(source_check.score, 1.0)

    def test_05_requested_source_absent(self):
        """An explicitly requested but unavailable source must fail the coverage gate."""
        res = evaluate_sufficiency(self.evidence_scenario, "Why did source 999 stop?")
        self.assertFalse(res.is_sufficient)
        self.assertEqual(res.status, SufficiencyStatus.INSUFFICIENT_EVIDENCE)
        self.assertTrue(res.bypass_synthesis)
        self.assertIn("999", res.reason)
        self.assertIn(999, res.quarantine_payload.get("missing_sources", []))

        # Quarantine synthesis test
        synth = create_insufficient_evidence_synthesis(res, self.evidence_scenario, "Why did source 999 stop?")
        self.assertIsNotNone(synth["summary"])
        self.assertEqual(synth["confidence"], "low")
        self.assertEqual(synth["contributing_events"], [])

    def test_06_available_nominal_telemetry_is_not_rejected_by_domain_rules(self):
        """The generic gate does not require hardcoded anomaly labels to allow analysis."""
        nominal_evidence = {
            "format": "fss-evidence/v0",
            "query": "What is shown by source 474?",
            "sources": [474],
            "events": [
                {
                    "event_id": "ev-aaaaaaaaaaaa",
                    "source_id": 474,
                    "event_name": "EVENT",
                    "decode_status": "SUCCESS",
                    "fields": {"value": 1},
                }
            ],
            "data_gaps": [],
        }
        res = evaluate_sufficiency(nominal_evidence, "What is shown by source 474?")
        self.assertTrue(res.is_sufficient)
        self.assertTrue(res.checks[0].passed)

    def test_07_telemetry_indicators_are_descriptive_not_a_gate(self):
        res = evaluate_sufficiency(self.evidence_scenario, "Summarize the retrieved events.")
        indicator_check = next(c for c in res.checks if c.name == "telemetry_indicators")
        self.assertTrue(indicator_check.passed)

    def test_08_no_false_lockout_on_general_queries(self):
        """General exploratory queries must not falsely lock out on logs containing telemetry."""
        general_queries = [
            "What went wrong in this log?",
            "Analyze all errors and root causes",
            "Why did altitude diverge?",
            "Summarize retrieved reference context",
        ]
        for q in general_queries:
            res = evaluate_sufficiency(self.evidence_scenario, q)
            self.assertTrue(res.is_sufficient, f"False lockout on query: {q}")
            self.assertEqual(res.status, SufficiencyStatus.SUFFICIENT)

    def test_09_no_false_entity_extraction_on_numbers(self):
        """Numbers like 404, 500, 100, 171 must not be treated as required entity source IDs."""
        query = "Explain HTTP 404 or error 500 at altitude 100 in log 171"
        res = evaluate_sufficiency(self.evidence_scenario, query)
        self.assertTrue(res.is_sufficient)

    def test_10_blackout_data_gap_short_circuit(self):
        """An explicit blackout gap short-circuits without inferring a domain-specific threshold."""
        blackout_evidence = {
            "format": "fss-evidence/v0",
            "query": "What caused the crash?",
            "sources": [1],
            "events": [
                {
                    "event_id": "ev-000000000001",
                    "source_id": 1,
                    "timestamp_ns": 1719001000000000000,
                    "event_name": "GPS",
                    "decode_status": "SUCCESS",
                    "fields": {},
                },
                {
                    "event_id": "ev-000000000002",
                    "source_id": 1,
                    # 42-second blackout gap (42 * 10^9 ns)
                    "timestamp_ns": 1719001000000000000 + 42_000_000_000,
                    "event_name": "ERROR",
                    "decode_status": "SUCCESS",
                    "fields": {"subsys": 1, "ecode": 99},
                },
            ],
            "data_gaps": [
                {"kind": "BLACKOUT", "detail": "Telemetry was not recorded in this interval."}
            ],
        }
        res = evaluate_sufficiency(blackout_evidence, "What caused the crash?")
        self.assertFalse(res.is_sufficient)
        self.assertEqual(res.status, SufficiencyStatus.INSUFFICIENT_EVIDENCE)
        self.assertTrue(res.bypass_synthesis)
        self.assertIn("blackout", res.reason.lower())

    def test_11_knowledge_base_context_is_optional(self):
        """Sufficiency is determined by telemetry availability, not reference documents."""
        telemetry = {
            "format": "fss-evidence/v0",
            "query": "Summarize the retrieved events",
            "sources": [1],
            "events": [
                {
                    "event_id": "ev-000000000001",
                    "source_id": 1,
                    "event_name": "EVENT",
                    "decode_status": "SUCCESS",
                    "fields": {"value": 1},
                }
            ],
            "data_gaps": [],
        }
        document = {"id": "doc-kb-reference", "content": "Reference context"}

        without_docs = evaluate_sufficiency(telemetry, telemetry["query"])
        with_docs = evaluate_sufficiency(
            telemetry, telemetry["query"], knowledge=[document]
        )

        self.assertEqual(without_docs.status, with_docs.status)
        self.assertTrue(with_docs.is_sufficient)
        knowledge_check = next(
            check for check in with_docs.checks if check.name == "knowledge_base_context"
        )
        self.assertIn("1 knowledge_base document", knowledge_check.details)


if __name__ == "__main__":
    unittest.main()
