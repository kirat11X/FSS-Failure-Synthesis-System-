"""Verify that the dashboard is query-first and uses only the configured Java store."""

from __future__ import annotations

from pathlib import Path

from streamlit.testing.v1 import AppTest

ROOT_DIR = Path(__file__).resolve().parent.parent
APP_PATH = str(ROOT_DIR / "app.py")


def test_dashboard_has_query_input_without_source_mode_selector():
    at = AppTest.from_file(APP_PATH, default_timeout=120)
    at.run()

    assert not at.exception
    assert at.title[0].value == "Failure Synthesis System"
    assert len(at.chat_input) == 1
    assert not at.radio
    assert not at.selectbox
    assert any("configured Java store" in item.value for item in at.caption)
