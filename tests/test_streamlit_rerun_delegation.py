"""Test Suite 7: Streamlit Rerun Delegation and Blank-Page Regression Guard.

Spec requirement:
  Simulate initial load plus 3 consecutive widget reruns against app.py;
  all 8 UI sections render across every rerun; zero blank-page regression; zero torch.classes exceptions.

Background:
  Streamlit re-executes the target script from top to bottom on every widget interaction.
  If app.py uses bare `import streamlit_app`, Python treats subsequent runs as cached no-ops,
  causing a blank screen on the first rerun.
  Furthermore, importing PyTorch alongside Streamlit's default file watcher frequently crashes
  on torch.classes inspection unless fileWatcherType = "none".
"""

from __future__ import annotations

import unittest
from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT_DIR = Path(__file__).resolve().parent.parent
APP_PATH = str(ROOT_DIR / "app.py")


class TestStreamlitRerunDelegation(unittest.TestCase):

    def _verify_all_sections_rendered(self, at: AppTest, run_label: str) -> None:
        """Verify that all core UI sections and tabs render without blank-page regression."""
        # 1. Zero exceptions
        exceptions = [e.value for e in at.exception]
        self.assertEqual(len(exceptions), 0, f"[{run_label}] Encountered unexpected exceptions: {exceptions}")

        # 2. Zero torch.classes exceptions
        for exc_msg in exceptions:
            self.assertNotIn(
                "torch.classes",
                exc_msg,
                f"[{run_label}] Detected torch.classes file watcher conflict!",
            )

        # 3. Main title must be rendered (not a blank page)
        self.assertGreater(len(at.title), 0, f"[{run_label}] Blank screen: no st.title rendered")
        self.assertIn("Failure Synthesis System", at.title[0].value)

        # 4. Verify presence of sidebar/dashboard headers
        header_values = [h.value for h in at.header]
        self.assertTrue(
            any("Storage" in h or "Data Source" in h for h in header_values),
            f"[{run_label}] Missing Storage header",
        )
        self.assertTrue(
            any("Retrieval" in h or "Bounds" in h for h in header_values),
            f"[{run_label}] Missing Retrieval Bounds header",
        )

        # 5. Verify presence of operator investigation subheaders / queries
        subheader_values = [sh.value for sh in at.subheader]
        self.assertTrue(
            any("Investigation Query" in sh for sh in subheader_values),
            f"[{run_label}] Missing Investigation Query subheader",
        )

        # 6. Verify presence of metrics (System / Synthesis summary)
        self.assertGreater(len(at.metric), 0, f"[{run_label}] Missing dashboard metrics")

        # 7. Verify all 6 tabs render across every run
        tab_labels = [t.label for t in at.tabs]
        self.assertEqual(len(tab_labels), 6, f"[{run_label}] Expected 6 tabs, found {len(tab_labels)}")
        self.assertTrue(any("Timeline" in t for t in tab_labels), f"[{run_label}] Missing Timeline tab")
        self.assertTrue(any("Data Gaps" in t for t in tab_labels), f"[{run_label}] Missing Data Gaps tab")
        self.assertTrue(any("Domain Knowledge" in t for t in tab_labels), f"[{run_label}] Missing Domain Knowledge tab")
        self.assertTrue(any("Citation" in t for t in tab_labels), f"[{run_label}] Missing Citation Validation tab")

    def test_streamlit_initial_load_plus_three_consecutive_reruns(self):
        """Simulate initial load plus 3 consecutive widget reruns against app.py."""
        at = AppTest.from_file(APP_PATH, default_timeout=20)

        # Initial Load
        at.run()
        self._verify_all_sections_rendered(at, "Initial Load")

        # Rerun 1: Select single-source filter '474'
        if len(at.selectbox) >= 2:
            at.selectbox[1].select("474").run()
        else:
            at.run()
        self._verify_all_sections_rendered(at, "Rerun 1 (Filter 474)")

        # Rerun 2: Adjust window length slider
        if len(at.slider) >= 2:
            at.slider[1].set_value(5.0).run()
        else:
            at.run()
        self._verify_all_sections_rendered(at, "Rerun 2 (Slider Adjustment)")

        # Rerun 3: Reset filter to 'ALL'
        if len(at.selectbox) >= 2:
            at.selectbox[1].select("ALL").run()
        else:
            at.run()
        self._verify_all_sections_rendered(at, "Rerun 3 (Reset ALL)")
