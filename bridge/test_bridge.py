"""Comprehensive integration tests for the Java-to-Python Bridge.

Tests:
  1. JavaStoreBridge CLI compilation & help output.
  2. BlockStore ingestion of binary fixtures via Java.
  3. Bounded retrieval & envelope streaming with JSON metadata header.
  4. JavaRetriever Python protocol conformance (retrieval contract).
  5. Synthesizer reasoning, citation validation & hallucination detection.
  6. Pipeline runner 8-part report generation & JSON output.
  7. End-to-end Java -> Python ProcessBuilder pipeline invocation.
"""

from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from bridge.java_retriever import JavaRetriever
from bridge.synthesizer import (
    build_llm_context,
    deterministic_synthesize,
    retrieve_relevant_knowledge,
    synthesize_and_validate,
    validate_synthesis,
)
from fss_py.decoder import decode_all
from fss_py.evidence import build_evidence
from fss_py.model import ALL, RetrievalRequest, RetrievalRequestError
from fss_py.schema import load_schema_file
from fss_py.wire import iter_envelopes


class TestBridge(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.fixture_path = ROOT_DIR / "fixtures" / "golden" / "scenario_298_474.bin"
        cls.schema_path = ROOT_DIR / "schemas" / "drone_v0.yaml"
        cls.schema = load_schema_file(cls.schema_path)

        # Ensure classes compiled
        subprocess.run(["bash", "bridge/compile.sh"], cwd=ROOT_DIR, check=True)

    def test_01_java_store_bridge_help(self):
        """Verify JavaStoreBridge runs and displays CLI help."""
        cp = f"{ROOT_DIR}/fss-core/target/classes:{ROOT_DIR}/bridge/classes"
        proc = subprocess.run(
            ["java", "-cp", cp, "bridge.JavaStoreBridge", "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        self.assertIn("JavaStoreBridge: FSS Java Core & Python Pipeline Linkage", proc.stdout)
        self.assertIn("ingest", proc.stdout)
        self.assertIn("retrieve", proc.stdout)
        self.assertIn("pipeline", proc.stdout)

    def test_02_java_ingest_and_retrieve(self):
        """Verify JavaStoreBridge ingest and retrieve commands."""
        with tempfile.NamedTemporaryFile(prefix="test_store_", suffix=".dat", delete=False) as tmp:
            store_file = Path(tmp.name)

        try:
            cp = f"{ROOT_DIR}/fss-core/target/classes:{ROOT_DIR}/bridge/classes"

            # Ingest
            ingest_proc = subprocess.run(
                [
                    "java",
                    "-cp",
                    cp,
                    "bridge.JavaStoreBridge",
                    "ingest",
                    "--store",
                    str(store_file),
                    "--fixture",
                    str(self.fixture_path),
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=True,
            )
            self.assertIn("Successfully ingested 9 events", ingest_proc.stdout)

            # Retrieve
            ret_proc = subprocess.run(
                [
                    "java",
                    "-cp",
                    cp,
                    "bridge.JavaStoreBridge",
                    "retrieve",
                    "--store",
                    str(store_file),
                    "--source",
                    "ALL",
                    "--start",
                    "1791454020000000000",
                    "--end",
                    "1791454030000000000",
                    "--max-events",
                    "100",
                ],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=True,
            )
            raw = ret_proc.stdout
            nl = raw.find(b"\n")
            self.assertNotEqual(nl, -1)
            meta = json.loads(raw[:nl].decode("utf-8"))
            self.assertEqual(meta["count"], 9)
            self.assertFalse(meta["truncated"])

            envelopes = list(iter_envelopes(raw[nl + 1 :]))
            self.assertEqual(len(envelopes), 9)
        finally:
            if store_file.exists():
                store_file.unlink()

    def test_03_java_retriever_protocol(self):
        """Verify JavaRetriever conforms to Retriever protocol and enforces bounds."""
        retriever = JavaRetriever.from_fixtures(self.fixture_path)
        try:
            # Full retrieval
            res = retriever.retrieve(ALL, 1791454020000000000, 1791454030000000000, max_events=100)
            self.assertEqual(len(res.events), 9)
            self.assertFalse(res.truncated)

            # Filter by single source
            res_298 = retriever.retrieve(298, 1791454020000000000, 1791454030000000000, max_events=100)
            self.assertEqual(len(res_298.events), 4)
            for ev in res_298.events:
                self.assertEqual(ev.source_id, 298)

            # Filter by event_type
            res_avoid = retriever.retrieve(ALL, 1791454020000000000, 1791454030000000000, max_events=100, event_type=0x20)
            self.assertEqual(len(res_avoid.events), 1)
            self.assertEqual(res_avoid.events[0].event_type, 0x20)

            # Max events limit triggers truncation flag
            res_trunc = retriever.retrieve(ALL, 1791454020000000000, 1791454030000000000, max_events=2)
            self.assertEqual(len(res_trunc.events), 2)
            self.assertTrue(res_trunc.truncated)

            # Bounded contract violation: window > 1h raises RetrievalRequestError
            with self.assertRaises(RetrievalRequestError):
                retriever.retrieve(ALL, 0, 3600 * 10**9 + 1, max_events=100)

            # Bounded contract violation: t_end <= t_start
            with self.assertRaises(RetrievalRequestError):
                retriever.retrieve(ALL, 100, 50, max_events=100)
        finally:
            if retriever.store_path.exists():
                retriever.store_path.unlink()

    def test_04_synthesizer_and_validator(self):
        """Verify synthesis formatting, citations, and hallucination rejection."""
        raw_bytes = self.fixture_path.read_bytes()
        events = list(iter_envelopes(raw_bytes))
        decoded = decode_all(events, self.schema)
        evidence = build_evidence(decoded, query="Why did drone 474 leave formation?")

        # 1. Clean deterministic synthesis
        synth, val, model = synthesize_and_validate(evidence, "Why did drone 474 leave formation?")
        self.assertEqual(val.verdict, "ACCEPTED")
        self.assertTrue(val.is_accepted())
        self.assertIn("ev-1c432aca07e2", val.valid_citations)
        self.assertIn("ev-2e92ae032f03", val.valid_citations)
        self.assertIn("ev-722d707b684c", val.valid_citations)
        self.assertEqual(val.invalid_citations, [])

        # 2. Test Validator catches hallucinated event ID
        bad_synth = dict(synth)
        bad_synth["summary"] = "Hallucinated failure occurred [ev-ffffffffffff]."
        bad_synth["contributing_events"] = [{"source_id": 999, "timestamp": 0, "event_type": "FAKE", "event_id": "ev-ffffffffffff"}]
        bad_val = validate_synthesis(bad_synth, evidence)
        self.assertEqual(bad_val.verdict, "REJECTED")
        self.assertIn("ev-ffffffffffff", bad_val.invalid_citations)

    def test_05_pipeline_runner_json_output(self):
        """Verify pipeline_runner --json-out produces valid structured output."""
        proc = subprocess.run(
            [
                sys.executable,
                "bridge/pipeline_runner.py",
                "--file",
                str(self.fixture_path),
                "--query",
                "Why did drone 474 leave formation?",
                "--json-out",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        data = json.loads(proc.stdout)
        self.assertIn("evidence", data)
        self.assertIn("synthesis", data)
        self.assertIn("validation", data)
        self.assertEqual(data["validation"]["verdict"], "ACCEPTED")

    def test_06_java_end_to_end_pipeline(self):
        """Verify executing JavaStoreBridge pipeline runs the entire stack end-to-end."""
        cp = f"{ROOT_DIR}/fss-core/target/classes:{ROOT_DIR}/bridge/classes"
        proc = subprocess.run(
            [
                "java",
                "-cp",
                cp,
                "bridge.JavaStoreBridge",
                "pipeline",
                "--fixture",
                str(self.fixture_path),
                "--query",
                "Why did drone 474 leave formation?",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=True,
        )
        out = proc.stdout
        self.assertIn("[1] Question", out)
        self.assertIn("[2] Retrieval", out)
        self.assertIn("[3] Events", out)
        self.assertIn("[4] Data gaps", out)
        self.assertIn("[5] Knowledge", out)
        self.assertIn("[6] LLM context", out)
        self.assertIn("[7] Synthesis", out)
        self.assertIn("[8] Validator", out)
        self.assertIn("Verdict:** ACCEPTED", out)


if __name__ == "__main__":
    unittest.main()
