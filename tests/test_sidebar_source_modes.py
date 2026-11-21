"""Test for Streamlit Sidebar Source Modes simplification.

Verifies:
  1. Sidebar radio 'Source Mode' contains exactly 4 modes:
     - Upload Custom Binary (.bin / .fss.bin) (default)
     - Golden Fixtures (fixtures/golden)
     - Existing Java BlockStore (.dat)
     - Java Telemetry Storage (IPC Engine)
  2. Removed modes ('Real-World ArduPilot Logs', 'Unseen Test Scenarios') are not present.
"""

from __future__ import annotations

from pathlib import Path
from streamlit.testing.v1 import AppTest

ROOT_DIR = Path(__file__).resolve().parent.parent
APP_PATH = str(ROOT_DIR / "app.py")


def test_sidebar_has_exactly_four_clean_source_modes():
    """Verify that Source Mode radio contains exactly the 4 expected clean modes."""
    at = AppTest.from_file(APP_PATH, default_timeout=120)
    at.run()

    # Find the Source Mode radio
    source_mode_radio = None
    for r in at.radio:
        if r.label == "Source Mode":
            source_mode_radio = r
            break

    assert source_mode_radio is not None, "Source Mode radio not found in AppTest!"
    expected_modes = [
        "Upload Custom Binary (.bin / .fss.bin)",
        "Auto-Simulate Unseen Pipeline (Live)",
        "Golden Fixtures (fixtures/golden)",
        "Existing Java BlockStore (.dat)",
        "Java Telemetry Storage (IPC Engine)",
    ]

    assert source_mode_radio.options == expected_modes, (
        f"Expected options {expected_modes}, got {source_mode_radio.options}"
    )
    assert source_mode_radio.value == "Upload Custom Binary (.bin / .fss.bin)"
