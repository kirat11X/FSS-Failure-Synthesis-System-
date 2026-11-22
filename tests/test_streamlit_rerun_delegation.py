"""Regression coverage for query dashboard reruns and conversation rendering."""

from __future__ import annotations

import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT_DIR = Path(__file__).resolve().parent.parent
APP_PATH = str(ROOT_DIR / "app.py")


class TestStreamlitRerunDelegation(unittest.TestCase):

    def _verify_all_sections_rendered(self, at: AppTest, run_label: str) -> None:
        """Verify the query interface survives reruns without a blank page."""
        exceptions = [e.value for e in at.exception]
        self.assertEqual(len(exceptions), 0, f"[{run_label}] Encountered unexpected exceptions: {exceptions}")
        self.assertGreater(len(at.title), 0, f"[{run_label}] Blank screen: no st.title rendered")
        self.assertIn("Failure Synthesis System", at.title[0].value)
        self.assertEqual(len(at.chat_input), 1, f"[{run_label}] Missing investigation input")
        self.assertEqual(len(at.radio), 0, f"[{run_label}] Unexpected source-mode selector")
        self.assertEqual(len(at.selectbox), 0, f"[{run_label}] Unexpected source selector")

    def test_streamlit_initial_load_plus_three_consecutive_reruns(self):
        """Simulate initial load plus 3 consecutive widget reruns against app.py."""
        at = AppTest.from_file(APP_PATH, default_timeout=180)

        at.run()
        self._verify_all_sections_rendered(at, "Initial Load")

        for rerun in range(1, 4):
            at.run()
            self._verify_all_sections_rendered(at, f"Rerun {rerun}")

        at.session_state["investigations"] = [
            {
                "query": "Why did this happen?",
                "status": "needs_clarification",
                "goal": "Determine the cause",
                "clarification": "Which event window should I inspect?",
                "retrieval_history": [],
            }
        ]
        at.run()
        self._verify_all_sections_rendered(at, "Conversation rerun")
        self.assertTrue(
            any("Which event window should I inspect?" in item.value for item in at.markdown)
        )
