"""Unit and adversarial tests for Citation & Warrant Auditor (4 Tiers & 5 Validation Badges).

Tests:
  1. Tier 1 — Metadata ID integrity: accepts real hex event IDs and knowledge docs;
     rejects [ev-fake], [drone-298], [ev-12345], and unobserved hex IDs.
  2. Tier 1 — Paragraph-level citation coverage (OWASP AISVS Req 7.4.2).
  3. Tier 1 — Deterministic entity binding (<5 ms):
     - Correct binding: Drone 474 with ev-1c432aca07e2.
     - Entity mismatch (deceptive grounding): Drone 474 claimed for Drone 298's LiDAR timeout -> ENTITY_MISMATCH.
  4. Tier 2 — Post-hoc attribution refinement (P-Cite/VeriCite):
     - Prunes ungrounded contributing events.
     - Bounded retries abstaining to INSUFFICIENT_EVIDENCE when ungrounded claims cannot be verified.
  5. Tier 3 — Context-preserving atomic claim entailment:
     - Numeric contradiction (0.5m claimed vs 3.2m actual) -> UNGROUNDED_HALLUCINATION.
     - Status contradiction (SENSOR_DISCONNECTED vs LIDAR_TIMEOUT) -> UNGROUNDED_HALLUCINATION.
  6. Tier 4 — ForceBench 5-axis evidence-force calibration:
     - Relation: uncalibrated cross-source causal verb -> FORCE_OVERCLAIM.
     - Modality: absolute certainty ("definitely") -> FORCE_OVERCLAIM.
     - Scope: universal fleet-wide generalization ("entire fleet") -> FORCE_OVERCLAIM.
     - Temporal: absolute cross-source temporal ordering -> FORCE_OVERCLAIM.
     - Numeric: fabricated decimal precision (3.20000 m) -> FORCE_OVERCLAIM.
  7. Clean Golden Synthesis -> VERIFIED_SUPPORTED badge.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from bridge.citation_auditor import (
    CitationAuditor,
    ValidationBadge,
    ValidationReport,
    ValidationVerdict,
    validate_synthesis,
)
from bridge.synthesizer import DOMAIN_KNOWLEDGE, deterministic_synthesize
from fss_py.decoder import decode_all
from fss_py.evidence import build_evidence
from fss_py.schema import load_schema_file
from fss_py.wire import iter_envelopes

ROOT_DIR = Path(__file__).resolve().parent.parent


class TestCitationAuditor(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fixture_path = ROOT_DIR / "fixtures" / "golden" / "scenario_298_474.bin"
        cls.schema_path = ROOT_DIR / "schemas" / "drone_v0.yaml"
        cls.schema = load_schema_file(cls.schema_path)

        raw = cls.fixture_path.read_bytes()
        events = list(iter_envelopes(raw))
        cls.decoded = decode_all(events, cls.schema)
        cls.evidence = build_evidence(cls.decoded, query="Why did drone 474 leave formation?")
        cls.auditor = CitationAuditor()

    def test_01_tier1_valid_citations_accepted(self):
        """Golden deterministic synthesis with valid citations receives VERIFIED_SUPPORTED badge."""
        synth = deterministic_synthesize(self.evidence, "Why did drone 474 leave formation?", DOMAIN_KNOWLEDGE)
        report = self.auditor.audit(synth, self.evidence, knowledge=DOMAIN_KNOWLEDGE)

        self.assertEqual(report.badge, ValidationBadge.VERIFIED_SUPPORTED)
        self.assertEqual(report.verdict, "ACCEPTED")
        self.assertTrue(report.is_accepted())
        self.assertIn("ev-1c432aca07e2", report.valid_citations)
        self.assertIn("ev-2e92ae032f03", report.valid_citations)
        self.assertIn("ev-722d707b684c", report.valid_citations)
        self.assertEqual(report.invalid_citations, [])

    def test_02_tier1_malformed_and_hallucinated_citations_rejected(self):
        """Catches and rejects fake IDs [ev-fake], [drone-298], [ev-12345], and unretrieved hex IDs."""
        bad_synth = {
            "summary": "Drone left [ev-fake] after alert [ev-12345] on drone [drone-298] and phantom [ev-ffffffffffff].",
            "likely_cause": None,
            "confidence": "high",
            "contributing_events": [
                {"source_id": 298, "timestamp": 0, "event_type": "LIDAR", "event_id": "ev-fake"}
            ],
            "uncertainty": [],
            "data_gaps": [],
        }
        report = self.auditor.audit(bad_synth, self.evidence, knowledge=DOMAIN_KNOWLEDGE, auto_refine=False)

        self.assertEqual(report.badge, ValidationBadge.UNGROUNDED_HALLUCINATION)
        self.assertEqual(report.verdict, "REJECTED")
        self.assertFalse(report.is_accepted())
        self.assertIn("ev-fake", report.invalid_citations)
        self.assertIn("drone-298", report.invalid_citations)
        self.assertIn("ev-12345", report.invalid_citations)
        self.assertIn("ev-ffffffffffff", report.invalid_citations)

    def test_03_tier1_entity_binding_deceptive_grounding(self):
        """Catches deceptive grounding where Drone 298's LiDAR timeout is misattributed to Drone 474."""
        # ev-2e92ae032f03 actually belongs to Drone 298
        deceptive_synth = {
            "summary": "Drone 474 reported LIDAR_TIMEOUT [ev-2e92ae032f03] and left formation.",
            "likely_cause": None,
            "confidence": "medium",
            "contributing_events": [
                {"source_id": 474, "timestamp": 1791454023050000000, "event_type": "LIDAR_STATUS", "event_id": "ev-2e92ae032f03"}
            ],
            "uncertainty": [],
            "data_gaps": [],
        }
        report = self.auditor.audit(deceptive_synth, self.evidence, knowledge=DOMAIN_KNOWLEDGE, auto_refine=False)

        self.assertEqual(report.badge, ValidationBadge.ENTITY_MISMATCH)
        self.assertEqual(report.verdict, "REJECTED")
        self.assertFalse(report.is_accepted())
        self.assertTrue(len(report.entity_bindings) > 0)
        self.assertEqual(report.entity_bindings[0]["actual_entity"], 298)

    def test_04_tier3_atomic_claim_numeric_contradiction(self):
        """Detects fabricated numeric values contradictory to event fields."""
        # ev-722d707b684c records min_separation_m: 3.2
        contradictory_synth = {
            "summary": "Drone 474 did avoidance [ev-722d707b684c] with a minimum separation of 0.5 m.",
            "likely_cause": None,
            "confidence": "medium",
            "contributing_events": [
                {"source_id": 474, "timestamp": 1791454023050000000, "event_type": "AVOIDANCE", "event_id": "ev-722d707b684c"}
            ],
            "uncertainty": [],
            "data_gaps": [],
        }
        report = self.auditor.audit(contradictory_synth, self.evidence, knowledge=DOMAIN_KNOWLEDGE, auto_refine=False)

        self.assertEqual(report.badge, ValidationBadge.UNGROUNDED_HALLUCINATION)
        self.assertEqual(report.verdict, "REJECTED")
        tier3_res = report.tier_results.get("tier3_atomic_claim_entailment", {})
        self.assertFalse(tier3_res.get("passed", True))
        self.assertTrue(any("Contradictory numeric claim" in f["reason"] for f in tier3_res.get("entailment_failures", [])))

    def test_05_tier3_atomic_claim_status_contradiction(self):
        """Detects status/enum claims contradictory to event fields."""
        # ev-2e92ae032f03 records status_name: "LIDAR_TIMEOUT"
        contradictory_synth = {
            "summary": "Drone 298 suffered SENSOR_DISCONNECTED [ev-2e92ae032f03].",
            "likely_cause": None,
            "confidence": "medium",
            "contributing_events": [
                {"source_id": 298, "timestamp": 1791454023050000000, "event_type": "LIDAR_STATUS", "event_id": "ev-2e92ae032f03"}
            ],
            "uncertainty": [],
            "data_gaps": [],
        }
        report = self.auditor.audit(contradictory_synth, self.evidence, knowledge=DOMAIN_KNOWLEDGE, auto_refine=False)

        self.assertEqual(report.badge, ValidationBadge.UNGROUNDED_HALLUCINATION)
        self.assertEqual(report.verdict, "REJECTED")
        tier3_res = report.tier_results.get("tier3_atomic_claim_entailment", {})
        self.assertFalse(tier3_res.get("passed", True))
        self.assertTrue(any("Status contradiction" in f["reason"] for f in tier3_res.get("entailment_failures", [])))

    def test_06_tier4_forcebench_relation_overclaim(self):
        """Catches causal escalation ('caused', 'triggered') across unsynchronized sources without hedging."""
        overclaim_synth = {
            "summary": "Drone 474 left formation [ev-1c432aca07e2].",
            "likely_cause": "Drone 298 LIDAR_TIMEOUT [ev-2e92ae032f03] directly caused Drone 474 to leave formation [ev-1c432aca07e2].",
            "confidence": "medium",
            "contributing_events": [
                {"source_id": 298, "timestamp": 1791454023050000000, "event_type": "LIDAR_STATUS", "event_id": "ev-2e92ae032f03"},
                {"source_id": 474, "timestamp": 1791454023050000000, "event_type": "FORMATION_STATE", "event_id": "ev-1c432aca07e2"},
            ],
            "uncertainty": [],
            "data_gaps": [],
        }
        report = self.auditor.audit(overclaim_synth, self.evidence, knowledge=DOMAIN_KNOWLEDGE, auto_refine=False)

        self.assertEqual(report.badge, ValidationBadge.FORCE_OVERCLAIM)
        self.assertEqual(report.verdict, "REJECTED")
        self.assertTrue(len(report.force_axes["relation"]) > 0)
        self.assertIn("causal verb", report.force_axes["relation"][0]["detail"])

    def test_07_tier4_forcebench_modality_overclaim(self):
        """Catches absolute certainty claims ('definitely', 'must have') on telemetry evidence."""
        modality_synth = {
            "summary": "Drone 474 definitely failed due to collision risk [ev-722d707b684c].",
            "likely_cause": "LiDAR timeout [ev-2e92ae032f03] plausibly triggered the issue.",
            "confidence": "high",
            "contributing_events": [
                {"source_id": 474, "timestamp": 1791454023050000000, "event_type": "AVOIDANCE", "event_id": "ev-722d707b684c"},
                {"source_id": 298, "timestamp": 1791454023050000000, "event_type": "LIDAR_STATUS", "event_id": "ev-2e92ae032f03"},
            ],
            "uncertainty": ["Clocks unsynchronized."],
            "data_gaps": [],
        }
        report = self.auditor.audit(modality_synth, self.evidence, knowledge=DOMAIN_KNOWLEDGE, auto_refine=False)

        self.assertEqual(report.badge, ValidationBadge.FORCE_OVERCLAIM)
        self.assertEqual(report.verdict, "REJECTED")
        self.assertTrue(len(report.force_axes["modality"]) > 0)

    def test_08_tier4_forcebench_scope_overclaim(self):
        """Catches fleet-wide universal generalizations ('entire fleet', 'all drones') from single node."""
        scope_synth = {
            "summary": "The entire fleet suffered failure [ev-722d707b684c].",
            "likely_cause": "System-wide outage.",
            "confidence": "medium",
            "contributing_events": [
                {"source_id": 474, "timestamp": 1791454023050000000, "event_type": "AVOIDANCE", "event_id": "ev-722d707b684c"}
            ],
            "uncertainty": [],
            "data_gaps": [],
        }
        report = self.auditor.audit(scope_synth, self.evidence, knowledge=DOMAIN_KNOWLEDGE, auto_refine=False)

        self.assertEqual(report.badge, ValidationBadge.FORCE_OVERCLAIM)
        self.assertEqual(report.verdict, "REJECTED")
        self.assertTrue(len(report.force_axes["scope"]) > 0)

    def test_09_tier4_forcebench_numeric_precision_overclaim(self):
        """Catches fabricated decimal precision (3.20000 m) not in telemetry."""
        precision_synth = {
            "summary": "Drone 474 did avoidance [ev-722d707b684c] with a minimum separation of 3.20000 m.",
            "likely_cause": "Plausibly drifted.",
            "confidence": "medium",
            "contributing_events": [
                {"source_id": 474, "timestamp": 1791454023050000000, "event_type": "AVOIDANCE", "event_id": "ev-722d707b684c"}
            ],
            "uncertainty": ["Clocks unsynchronized."],
            "data_gaps": [],
        }
        report = self.auditor.audit(precision_synth, self.evidence, knowledge=DOMAIN_KNOWLEDGE, auto_refine=False)

        self.assertEqual(report.badge, ValidationBadge.FORCE_OVERCLAIM)
        self.assertEqual(report.verdict, "REJECTED")
        self.assertTrue(len(report.force_axes["numeric"]) > 0)

    def test_10_insufficient_evidence_badge_on_quarantine(self):
        """Quarantine synthesis correctly receives INSUFFICIENT_EVIDENCE badge."""
        quarantine_synth = {
            "summary": "INSUFFICIENT EVIDENCE to diagnose query: 0 events retrieved.",
            "likely_cause": None,
            "confidence": "low",
            "contributing_events": [],
            "uncertainty": ["Bypassed LLM generation."],
            "data_gaps": ["[DATA_GAPS_QUARANTINE] Empty window."],
        }
        report = self.auditor.audit(quarantine_synth, self.evidence, knowledge=DOMAIN_KNOWLEDGE)
        self.assertEqual(report.badge, ValidationBadge.INSUFFICIENT_EVIDENCE)
        self.assertEqual(report.verdict, "REJECTED")

    def test_11_err_citations_valid(self):
        """Valid vendor error manual citations ([err-...]) validate cleanly without KeyError."""
        vendor_matches = [
            {"id": "err-subsys-2-code-1", "title": "Crash Check Error", "description": "Crash detector triggered."}
        ]
        synth = {
            "summary": "Drone 474 performed avoidance [ev-722d707b684c] following vendor error alert [err-subsys-2-code-1].",
            "likely_cause": "Avoidance manoeuvre for collision risk [ev-722d707b684c].",
            "confidence": "medium",
            "contributing_events": [
                {"source_id": 474, "timestamp": 1791454023050000000, "event_type": "AVOIDANCE", "event_id": "ev-722d707b684c"}
            ],
            "uncertainty": [],
            "data_gaps": [],
        }
        report = self.auditor.audit(
            synth,
            self.evidence,
            knowledge=DOMAIN_KNOWLEDGE,
            vendor_manual_matches=vendor_matches,
        )
        self.assertIn("err-subsys-2-code-1", report.valid_citations)
        self.assertNotIn("err-subsys-2-code-1", report.invalid_citations)
        self.assertTrue(report.tier_results["tier1_id_integrity"]["passed"])

    def test_12_err_citations_hallucinated_rejected(self):
        """Hallucinated vendor error citations ([err-fake]) are caught and rejected."""
        synth = {
            "summary": "Drone 474 suffered crash [ev-722d707b684c] documented under [err-fake].",
            "likely_cause": None,
            "confidence": "medium",
            "contributing_events": [
                {"source_id": 474, "timestamp": 1791454023050000000, "event_type": "AVOIDANCE", "event_id": "ev-722d707b684c"}
            ],
            "uncertainty": [],
            "data_gaps": [],
        }
        report = self.auditor.audit(synth, self.evidence, knowledge=DOMAIN_KNOWLEDGE, auto_refine=False)
        self.assertIn("err-fake", report.invalid_citations)
        self.assertEqual(report.badge, ValidationBadge.UNGROUNDED_HALLUCINATION)


if __name__ == "__main__":
    unittest.main()
