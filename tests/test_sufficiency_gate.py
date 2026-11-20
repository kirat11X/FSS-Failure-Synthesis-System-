"""Unit and adversarial tests for Stage 0 Pre-Generation Sufficiency Gate.

Tests:
  1. Deterministic short-circuit on 0 events (status == "EMPTY").
  2. Deterministic short-circuit when all events have uninterpretable decode statuses.
  3. Deterministic short-circuit when unrecorded data gaps span the requested failure window.
  4. Content coverage: query with present entities (Drone 474) passes.
  5. Content coverage: query with completely unobserved entities (Drone 901) fails and quarantines.
  6. Candidate span detection: failure query on nominal-only events triggers low score.
  7. Candidate span detection: failure query with anomaly events triggers high score.
  8. Cross-passage latent sufficiency evaluates domain knowledge bridges.
  9. Quarantine payload generation and synthesis formatting.
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
        self.assertIn("0 events retrieved", res.reason)
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
        """Unrecorded data gap spanning the requested failure window triggers deterministic gate."""
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
        self.assertIn("spans the requested failure window", res.reason)

    def test_04_content_coverage_queried_entity_present(self):
        """Query asking about drone 474 with drone 474 in evidence sources passes coverage."""
        res = evaluate_sufficiency(self.evidence_scenario, "Why did drone 474 leave formation?")
        self.assertTrue(res.is_sufficient)
        self.assertEqual(res.status, SufficiencyStatus.SUFFICIENT)
        self.assertFalse(res.bypass_synthesis)
        self.assertGreaterEqual(res.score, 0.5)

        cov_check = next(c for c in res.checks if c.name == "question_content_coverage")
        self.assertTrue(cov_check.passed)
        self.assertEqual(cov_check.score, 1.0)

    def test_05_content_coverage_queried_entity_absent(self):
        """Query asking specifically about drone 999 (not in evidence) must fail sufficiency."""
        res = evaluate_sufficiency(self.evidence_scenario, "Why did drone 999 crash?")
        self.assertFalse(res.is_sufficient)
        self.assertEqual(res.status, SufficiencyStatus.INSUFFICIENT_EVIDENCE)
        self.assertTrue(res.bypass_synthesis)
        self.assertIn("999", res.reason)
        self.assertIn(999, res.quarantine_payload.get("missing_entities", []))

        # Quarantine synthesis test
        synth = create_insufficient_evidence_synthesis(res, self.evidence_scenario, "Why did drone 999 crash?")
        self.assertIn("INSUFFICIENT EVIDENCE", synth["summary"])
        self.assertIn("999", synth["summary"])
        self.assertEqual(synth["confidence"], "low")
        self.assertEqual(synth["contributing_events"], [])

    def test_06_candidate_spans_nominal_failure_query(self):
        """When user asks a causal failure query but 100% of telemetry is nominal, candidate spans fail."""
        nominal_evidence = {
            "format": "fss-evidence/v0",
            "query": "Why did drone 474 fail?",
            "sources": [474],
            "events": [
                {
                    "event_id": "ev-aaaaaaaaaaaa",
                    "source_id": 474,
                    "event_name": "HEARTBEAT",
                    "decode_status": "SUCCESS",
                    "fields": {"status_name": "NOMINAL"},
                }
            ],
            "data_gaps": [],
        }
        res = evaluate_sufficiency(nominal_evidence, "Why did drone 474 fail?")
        span_check = next(c for c in res.checks if c.name == "candidate_span_detection")
        self.assertFalse(span_check.passed)
        self.assertLess(span_check.score, 0.5)

    def test_07_candidate_spans_anomalies_detected(self):
        """When candidate anomaly spans exist (AVOIDANCE, LEFT_FORMATION, LIDAR_TIMEOUT), score is boosted."""
        res = evaluate_sufficiency(self.evidence_scenario, "Why did drone 474 leave formation?")
        span_check = next(c for c in res.checks if c.name == "candidate_span_detection")
        self.assertTrue(span_check.passed)
        self.assertGreaterEqual(span_check.score, 0.7)

    def test_08_no_false_lockout_on_general_queries(self):
        """General exploratory queries must not falsely lock out on logs containing telemetry."""
        general_queries = [
            "What went wrong in this log?",
            "Analyze all errors and root causes",
            "Why did altitude diverge?",
            "Summarize anomalies using the error code manual",
        ]
        for q in general_queries:
            res = evaluate_sufficiency(self.evidence_scenario, q)
            self.assertTrue(res.is_sufficient, f"False lockout on query: {q}")
            self.assertEqual(res.status, SufficiencyStatus.SUFFICIENT)

    def test_09_no_false_entity_extraction_on_numbers(self):
        """Numbers like 404, 500, 100, 171 must not be treated as required entity source IDs."""
        query = "Explain HTTP 404 or error 500 at altitude 100 in log 171"
        res = evaluate_sufficiency(self.evidence_scenario, query)
        cov_check = next(c for c in res.checks if c.name == "question_content_coverage")
        self.assertTrue(cov_check.passed)
        self.assertEqual(cov_check.score, 1.0)
        self.assertTrue(res.is_sufficient)

    def test_10_blackout_data_gap_short_circuit(self):
        """A genuine blackout gap (> 30s) across failure window short-circuits to INSUFFICIENT_EVIDENCE."""
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
            "data_gaps": [],
        }
        res = evaluate_sufficiency(blackout_evidence, "What caused the crash?")
        self.assertFalse(res.is_sufficient)
        self.assertEqual(res.status, SufficiencyStatus.INSUFFICIENT_EVIDENCE)
        self.assertTrue(res.bypass_synthesis)
        self.assertIn("blackout", res.reason.lower())

    def test_11_vendor_manual_matches_candidate_spans(self):
        """Vendor-manual error matches are recognized as candidate anomaly spans."""
        nominal_evidence = {
            "format": "fss-evidence/v0",
            "query": "Summarize anomalies using the error code manual",
            "sources": [1],
            "events": [
                {
                    "event_id": "ev-000000000001",
                    "source_id": 1,
                    "event_name": "HEARTBEAT",
                    "decode_status": "SUCCESS",
                    "fields": {"status_name": "NOMINAL"},
                }
            ],
            "data_gaps": [],
        }
        vendor_matches = [
            {"id": "err-subsys-2-code-1", "title": "Crash Check Error", "description": "Crash detector triggered."}
        ]
        res = evaluate_sufficiency(
            nominal_evidence,
            "Summarize anomalies using the error code manual",
            vendor_manual_matches=vendor_matches,
        )
        self.assertTrue(res.is_sufficient)
        span_check = next(c for c in res.checks if c.name == "candidate_span_detection")
        self.assertTrue(span_check.passed)


if __name__ == "__main__":
    unittest.main()
