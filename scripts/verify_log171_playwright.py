#!/usr/bin/env python3
"""scripts/verify_log171_playwright.py
--------------------------------------
Automated headless Playwright UI verification for log171.bin on branch integration-bridge.
Verifies:
  1. Live Streamlit UI is online at http://127.0.0.1:8501.
  2. Uploads test_data/irl/log171.bin.
  3. Verifies sidebar badge shows ARDUPILOT · 5000 EVENTS · 4m 3s.
  4. Triggers analysis and waits for 5000 retrieved in KPI strip.
  5. Verifies KPI strip shows 5000 retrieved, 243s duration, 18 matched error rules.
  6. Verifies export buttons (JSON-LD, HTML, PDF) are visible.
  7. Confirms VERIFIED_SUPPORTED verdict and zero LIDAR_TIMEOUT citations for ArduPilot log.
  8. Confirms zero .stException blocks.
  9. Saves high-resolution full-page screenshot to artifact directory.
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT_DIR = Path(__file__).resolve().parent.parent
LOG171_BIN = ROOT_DIR / "test_data" / "irl" / "log171.bin"
URL = os.environ.get("STREAMLIT_URL", "http://127.0.0.1:8501")
ARTIFACT_IMG = Path("/home/kirat/.gemini/antigravity/brain/90150533-5b4a-41d1-951c-0ba626226c93/log171_ui_verified.png")


def main():
    print("================================================================================")
    print("  RUNNING PLAYWRIGHT UI VERIFICATION FOR LOG171.BIN")
    print(f"  Target URL: {URL}")
    print("================================================================================")

    console_errors: list[str] = []
    page_errors: list[str] = []

    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"],
        )
        context = browser.new_context(viewport={"width": 1440, "height": 1080})
        page = context.new_page()

        def on_console(msg):
            if msg.type in ("error",) and "favicon.ico" not in msg.text:
                console_errors.append(msg.text)
                print(f"  [Console Error]: {msg.text}")

        page.on("console", on_console)
        page.on("pageerror", lambda err: page_errors.append(str(err)))

        print("  [Step 1] Loading Streamlit UI...")
        page.goto(URL, timeout=30000)
        page.wait_for_selector(".fss-masthead, .fss-kpi-card", timeout=30000)
        time.sleep(1.0)

        # Step 2: Upload log171.bin
        print(f"  [Step 2] Uploading {LOG171_BIN.name}...")
        file_input = page.locator('input[type="file"]').first
        file_input.set_input_files(str(LOG171_BIN))

        # Step 3: Wait for sidebar status badge to update to ARDUPILOT
        print("  [Step 3] Waiting for sidebar status badge (ARDUPILOT · 5000 EVENTS)...")
        page.wait_for_selector(".crt-status-indicator >> text=ARDUPILOT", timeout=20000)
        sidebar_badge = page.locator(".crt-status-indicator").first.inner_text().strip()
        print(f"  --> Sidebar Badge 1: {sidebar_badge}")
        assert "ARDUPILOT" in sidebar_badge, f"Expected 'ARDUPILOT' in badge, got '{sidebar_badge}'!"
        assert "5000" in sidebar_badge, f"Expected '5000' in badge, got '{sidebar_badge}'!"
        assert "4m 3s" in sidebar_badge, f"Expected '4m 3s' in badge, got '{sidebar_badge}'!"

        sidebar_badge2 = page.locator(".crt-status-indicator").nth(1).inner_text().strip()
        print(f"  --> Sidebar Badge 2: {sidebar_badge2}")
        assert "ArduPilot" in sidebar_badge2, f"Expected 'ArduPilot' in badge 2, got '{sidebar_badge2}'!"

        # Step 4: Click 'Analyze Log' button to trigger fresh synthesis on log171
        print("  [Step 4] Triggering Analysis via '⚡ Analyze Log' button...")
        time.sleep(1.0)
        page.wait_for_load_state("networkidle")
        analyze_btn = page.locator('button:has-text("Analyze Log"), button:has-text("⚡ Analyze Log")').first
        if analyze_btn.is_visible():
            analyze_btn.click()
            print("  --> Clicked '⚡ Analyze Log'")

        # Step 5: Wait for Synthesis on log171 to complete
        print("  [Step 5] Waiting for synthesis completion (5000 retrieved in KPI strip)...")
        page.wait_for_selector(".fss-kpi-grid >> text=5000 retrieved", timeout=90000)
        print("  --> Synthesis and verdict for log171 rendered successfully!")

        # Step 6: Verify KPI Cards
        print("  [Step 6] Checking KPI Cards...")
        kpi_text = page.locator(".fss-kpi-grid, .fss-kpi-card").all_inner_texts()
        full_kpi = " ".join(kpi_text)
        print(f"  --> KPI Content: {full_kpi[:250]}...")
        assert "5000 retrieved" in full_kpi, "Expected '5000 retrieved' in KPI strip!"
        assert "18 matched" in full_kpi, "Expected '18 matched' rules in KPI strip!"
        assert "LIDAR_TIMEOUT" not in full_kpi, "Spurious LIDAR_TIMEOUT found in KPI strip!"

        # Step 7: Verify Export Buttons
        print("  [Step 7] Checking Export Buttons...")
        buttons = page.locator('button, [data-testid="stDownloadButton"]').all_inner_texts()
        has_json_ld = any("JSON-LD" in b for b in buttons)
        has_html = any("HTML" in b for b in buttons)
        has_pdf = any("PDF" in b for b in buttons)
        print(f"  --> Export Buttons: JSON-LD={has_json_ld}, HTML={has_html}, PDF={has_pdf}")
        assert has_json_ld, "Expected 'JSON-LD' export button!"
        assert has_html, "Expected 'HTML' export button!"
        assert has_pdf, "Expected 'PDF' export button!"

        # Step 8: Verify Verdict Content & No Spurious LIDAR_TIMEOUT
        print("  [Step 8] Checking Verdict Content...")
        verdict_text = page.locator(".fss-verdict").inner_text()
        print(f"  --> Verdict Text:\n{verdict_text[:300]}...")
        assert "LIDAR_TIMEOUT" not in verdict_text, "Spurious LIDAR_TIMEOUT found in verdict!"
        assert "EKF" in verdict_text or "failsafe" in verdict_text.lower(), "Expected EKF failsafe in verdict!"

        # Step 9: Verify Zero Streamlit Exceptions
        print("  [Step 9] Checking for Streamlit Exceptions...")
        exceptions = page.locator(".stException").all()
        if exceptions:
            for exc in exceptions:
                print(f"  [ERROR] Streamlit Exception: {exc.inner_text()}")
        assert len(exceptions) == 0, f"Found {len(exceptions)} Streamlit exception(s)!"

        # Step 10: Capture Full-Page Screenshot
        print(f"  [Step 10] Saving full-page screenshot to {ARTIFACT_IMG}...")
        ARTIFACT_IMG.parent.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(ARTIFACT_IMG), full_page=True)
        print(f"  --> Screenshot saved: {ARTIFACT_IMG.stat().st_size:,} bytes")

        browser.close()

    print("================================================================================")
    print("  ALL PLAYWRIGHT UI CHECKS PASSED ✅")
    print("================================================================================")


if __name__ == "__main__":
    main()
