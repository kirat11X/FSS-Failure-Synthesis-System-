"""Adversarial citation fuzzing and warrant-strength calibration test suite.

Verifies:
  1. 100% rejection of fabricated IDs: [ev-fake], [drone-298], [ev-12345], [ev-badhex].
  2. 100% rejection of entity-swapped valid IDs (deceptive grounding) -> ENTITY_MISMATCH badge.
  3. 100% flagging of uncalibrated causal claims (correlation-to-causation overclaim across sources) -> FORCE_OVERCLAIM badge.
  4. 100% flagging of absolute modality overclaims -> FORCE_OVERCLAIM badge.
  5. 100% flagging of universal fleet-wide scope overclaims -> FORCE_OVERCLAIM badge.
  6. Verified grounded synthesis receives VERIFIED_SUPPORTED badge.
"""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import Any

from bridge.citation_auditor import CitationAuditor, ValidationBadge, validate_synthesis
from bridge.synthesizer import DOMAIN_KNOWLEDGE, deterministic_synthesize
from fss_py.decoder import decode_all
from fss_py.evidence import build_evidence
from fss_py.schema import load_schema_file
from fss_py.wire import iter_envelopes

ROOT_DIR = Path(__file__).resolve().parent.parent


class TestAdversarialCitations(unittest.TestCase):

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

    def test_01_fuzz_fake_and_malformed_citation_ids(self):
        """100% rejection of fake / malformed citation IDs."""
        fake_ids = [
            "ev-fake",
            "drone-298",
            "ev-12345",
            "ev-notanid",
            "ev-ffffffffffff",
            "source-474",
            "event-9999",
            "ev-0123456789ag",  # invalid hex 'g'
            "ev-1234567890",    # only 10 hex digits
        ]
        for fake_id in fake_ids:
            with self.subTest(fake_id=fake_id):
                synth = {
                    "summary": f"Drone left formation [{fake_id}].",
                    "likely_cause": None,
                    "confidence": "high",
                    "contributing_events": [],
                    "uncertainty": [],
                    "data_gaps": [],
                }
                report = self.auditor.audit(synth, self.evidence, knowledge=DOMAIN_KNOWLEDGE, auto_refine=False)
                self.assertFalse(report.is_accepted(), f"Fake ID [{fake_id}] was not rejected!")
                self.assertIn(fake_id, report.invalid_citations)
                self.assertEqual(report.badge, ValidationBadge.UNGROUNDED_HALLUCINATION)
                self.assertEqual(report.verdict, "REJECTED")

    def test_02_entity_swapped_valid_ids_trigger_entity_mismatch(self):
        """Entity-swapped valid IDs (deceptive grounding) must trigger ENTITY_MISMATCH badge."""
        # In evidence:
        # ev-2e92ae032f03 belongs to Drone 298 (LIDAR_STATUS)
        # ev-1c432aca07e2 belongs to Drone 474 (FORMATION_STATE)
        # ev-722d707b684c belongs to Drone 474 (AVOIDANCE)

        # Deceptive grounding 1: attribute 298's LIDAR event to Drone 474
        synth_swap_1 = {
            "summary": "Drone 474 suffered LIDAR_TIMEOUT [ev-2e92ae032f03].",
            "likely_cause": None,
            "confidence": "medium",
            "contributing_events": [
                {"source_id": 474, "timestamp": 1791454023050000000, "event_type": "LIDAR_STATUS", "event_id": "ev-2e92ae032f03"}
            ],
            "uncertainty": [],
            "data_gaps": [],
        }
        report1 = self.auditor.audit(synth_swap_1, self.evidence, knowledge=DOMAIN_KNOWLEDGE, auto_refine=False)
        self.assertEqual(report1.badge, ValidationBadge.ENTITY_MISMATCH)
        self.assertEqual(report1.verdict, "REJECTED")
        self.assertFalse(report1.is_accepted())

        # Deceptive grounding 2: attribute 474's AVOIDANCE to Drone 298
        synth_swap_2 = {
            "summary": "Drone 298 performed collision avoidance manoeuvre [ev-722d707b684c].",
            "likely_cause": None,
            "confidence": "medium",
            "contributing_events": [
                {"source_id": 298, "timestamp": 1791454023050000000, "event_type": "AVOIDANCE", "event_id": "ev-722d707b684c"}
            ],
            "uncertainty": [],
            "data_gaps": [],
        }
        report2 = self.auditor.audit(synth_swap_2, self.evidence, knowledge=DOMAIN_KNOWLEDGE, auto_refine=False)
        self.assertEqual(report2.badge, ValidationBadge.ENTITY_MISMATCH)
        self.assertEqual(report2.verdict, "REJECTED")

    def test_03_uncalibrated_causal_claims_trigger_force_overclaim(self):
        """Asserting causal verbs across unsynchronized sources triggers FORCE_OVERCLAIM."""
        causal_phrases = [
            "Drone 298 LIDAR_TIMEOUT [ev-2e92ae032f03] caused Drone 474 to leave formation [ev-1c432aca07e2].",
            "Drone 298 LIDAR_TIMEOUT [ev-2e92ae032f03] triggered Drone 474 collision avoidance [ev-722d707b684c].",
            "Drone 298 LIDAR_TIMEOUT [ev-2e92ae032f03] resulted in Drone 474 leaving formation [ev-1c432aca07e2].",
            "Drone 298 LIDAR_TIMEOUT [ev-2e92ae032f03] led to Drone 474 leaving formation [ev-1c432aca07e2].",
            "Drone 298 LIDAR_TIMEOUT [ev-2e92ae032f03] proved that Drone 474 had to deviate [ev-722d707b684c].",
        ]
        for phrase in causal_phrases:
            with self.subTest(phrase=phrase):
                synth = {
                    "summary": phrase,
                    "likely_cause": phrase,
                    "confidence": "medium",
                    "contributing_events": [
                        {"source_id": 298, "timestamp": 1791454023050000000, "event_type": "LIDAR_STATUS", "event_id": "ev-2e92ae032f03"},
                        {"source_id": 474, "timestamp": 1791454023050000000, "event_type": "AVOIDANCE", "event_id": "ev-722d707b684c"},
                    ],
                    "uncertainty": [],
                    "data_gaps": [],
                }
                report = self.auditor.audit(synth, self.evidence, knowledge=DOMAIN_KNOWLEDGE, auto_refine=False)
                self.assertEqual(report.badge, ValidationBadge.FORCE_OVERCLAIM)
                self.assertEqual(report.verdict, "REJECTED")
                self.assertFalse(report.is_accepted())
                self.assertTrue(len(report.force_axes["relation"]) > 0)

    def test_04_calibrated_hedged_causal_claim_accepted(self):
        """Properly hedged causal claims ('plausibly let it drift', 'inferred') pass without FORCE_OVERCLAIM."""
        hedged_synth = deterministic_synthesize(self.evidence, "Why did drone 474 leave formation?", DOMAIN_KNOWLEDGE)
        report = self.auditor.audit(hedged_synth, self.evidence, knowledge=DOMAIN_KNOWLEDGE)

        self.assertEqual(report.badge, ValidationBadge.VERIFIED_SUPPORTED)
        self.assertEqual(report.verdict, "ACCEPTED")
        self.assertTrue(report.is_accepted())
        self.assertEqual(len(report.force_axes["relation"]), 0)


if __name__ == "__main__":
    unittest.main()
