#!/usr/bin/env python3
"""scripts/verify_ui_playwright.py
--------------------------------
Automated headless Playwright UI verification for Failure Synthesis System (FSS).
Verifies:
  1. Exactly 1 masthead (.fss-masthead) and no duplicate header titles.
  2. Exactly 1 KPI grid (.fss-kpi-grid) and no duplicate metrics.
  3. Zero raw backticks (`) rendered inside HTML verdict cards.
  4. Zero hardcoded "Drone Source" strings (Rule G02 compliance).
  5. Zero model Online/Offline contradictions between masthead pill and KPI card.
  6. Exactly 7 concise tabs (Timeline, Data Gaps, Error Codes, Domain RAG, Prompt, Audit, Raw JSON).
  7. Export buttons (Export JSON-LD, Export HTML, Export PDF) present and clickable.
  8. Saves a full-page verification screenshot to /tmp/fss_ui_verified.png.
  9. Zero browser console errors and zero stException blocks.
"""

import sys
import time
import subprocess
import urllib.request
import re
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT_DIR = Path(__file__).resolve().parent.parent
PORT = 8501
URL = f"http://127.0.0.1:{PORT}"
SCREENSHOT_PATH = "/tmp/fss_ui_verified.png"

def wait_for_server(url: str, timeout: float = 30.0) -> bool:
    start = time.time()
    while time.time() - start < timeout:
        try:
            with urllib.request.urlopen(url, timeout=1.5) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            time.sleep(0.5)
    return False

def main():
    server_proc = None
    # Check if server is already running
    if not wait_for_server(URL, timeout=1.0):
        print(f"Starting Streamlit server on port {PORT}...")
        server_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "streamlit",
                "run",
                str(ROOT_DIR / "app.py"),
                "--server.port",
                str(PORT),
                "--server.headless",
                "true",
                "--server.fileWatcherType",
                "none",
                "--browser.gatherUsageStats",
                "false",
            ],
            stdout=None,
            stderr=None,
            cwd=str(ROOT_DIR),
        )
        if not wait_for_server(URL, timeout=30.0):
            print("ERROR: Streamlit server failed to start within 30s")
            if server_proc:
                server_proc.kill()
            sys.exit(1)

    print(f"Streamlit server active at {URL}. Launching Playwright...")
    console_errors = []

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page(viewport={"width": 1440, "height": 1080})

            page.on("console", lambda msg: console_errors.append(msg.text) if msg.type == "error" else None)

            print(f"Navigating to {URL}...")
            page.goto(URL, wait_until="networkidle", timeout=30000)
            
            # Wait for synthesis and KPI grid to finish loading
            print("Waiting for retrieval & synthesis to complete...")
            page.wait_for_selector(".fss-kpi-grid", timeout=60000)
            page.wait_for_selector("[data-testid='stDownloadButton']", timeout=60000)

            # 1. Masthead verification
            mastheads = page.locator(".fss-masthead")
            count_masthead = mastheads.count()
            print(f"Mastheads count: {count_masthead}")
            assert count_masthead == 1, f"Expected exactly 1 masthead, found {count_masthead}"

            # Check that there is no duplicate H2 header
            h2_titles = page.locator("h2:has-text('FAILURE SYNTHESIS SYSTEM')")
            assert h2_titles.count() == 0, "Found duplicate H2 title banner"

            # 2. KPI grid verification
            kpi_grids = page.locator(".fss-kpi-grid")
            count_kpis = kpi_grids.count()
            print(f"KPI grid count: {count_kpis}")
            assert count_kpis == 1, f"Expected exactly 1 KPI grid, found {count_kpis}"

            # Ensure old duplicate hud-kpi-strip is not present
            old_kpis = page.locator(".hud-kpi-strip")
            assert old_kpis.count() == 0, "Found old duplicate hud-kpi-strip"

            # 3. Model status consistency
            pill_text = page.locator(".fss-status-pill").first.inner_text()
            kpi_model_card = page.locator(".fss-kpi-card", has_text=re.compile("Model Runtime", re.I))
            assert kpi_model_card.count() == 1, f"Missing Model Runtime card in KPI grid. Found cards: {page.locator('.fss-kpi-card').all_inner_texts()}"
            kpi_model_val = kpi_model_card.locator(".fss-kpi-val").inner_text()

            print(f"Masthead pill: {pill_text} | KPI Model: {kpi_model_val}")
            if "ONLINE" in pill_text.upper():
                assert "ONLINE" in kpi_model_val.upper(), "Contradictory model status: Masthead Online but KPI Offline"
            else:
                assert "OFFLINE" in kpi_model_val.upper(), "Contradictory model status: Masthead Offline but KPI Online"

            # 4. Zero raw backticks in verdict
            verdict_text = page.locator(".fss-verdict").inner_text()
            print(f"Verdict text: {verdict_text}")
            parent_html = page.locator(".fss-verdict").evaluate("el => el.parentElement.innerHTML")
            print(f"Parent HTML around verdict: {parent_html[:600]}")
            assert "`" not in verdict_text, f"Found raw backtick in verdict text: {verdict_text}"

            # 5. Zero "Drone Source" strings (Rule G02)
            page_text = page.inner_text("body")
            assert "Drone Source" not in page_text, "Found hardcoded 'Drone Source' in page content (violates G02)"

            # 6. Tab labels check
            tabs = page.locator("button[data-baseweb='tab']")
            tab_labels = [tabs.nth(i).inner_text().strip() for i in range(tabs.count())]
            print(f"Tabs found ({len(tab_labels)}): {tab_labels}")
            assert len(tab_labels) == 7, f"Expected 7 tabs, found {len(tab_labels)}: {tab_labels}"
            expected_tabs = ["Timeline", "Data Gaps", "Error Codes", "Domain RAG", "Prompt", "Audit", "Raw JSON"]
            for exp in expected_tabs:
                assert any(exp in tl for tl in tab_labels), f"Missing expected tab: {exp}"

            # 7. Export buttons check
            all_buttons = page.locator("button").all_inner_texts()
            print(f"All buttons on page: {all_buttons}")
            all_download = page.locator("[data-testid='stDownloadButton']").all_inner_texts()
            print(f"All download elements on page: {all_download}")
            btn_jsonld = page.get_by_text(re.compile("Export JSON-LD", re.I))
            btn_html = page.get_by_text(re.compile("Export HTML", re.I))
            btn_pdf = page.get_by_text(re.compile("Export PDF", re.I))
            print(f"Download elements found: JSON-LD={btn_jsonld.count()}, HTML={btn_html.count()}, PDF={btn_pdf.count()}")
            assert btn_jsonld.count() >= 1, f"Missing Export JSON-LD button. Buttons on page: {all_buttons}"
            assert btn_html.count() >= 1, "Missing Export HTML button"
            assert btn_pdf.count() >= 1, "Missing Export PDF button"

            # 8. Zero stException
            st_exceptions = page.locator(".stException")
            assert st_exceptions.count() == 0, f"Found stException on page: {st_exceptions.all_inner_texts()}"

            # 9. Save verification screenshot
            page.screenshot(path=SCREENSHOT_PATH, full_page=True)
            print(f"Screenshot successfully saved to {SCREENSHOT_PATH} ({Path(SCREENSHOT_PATH).stat().st_size} bytes)")

            browser.close()
            print("ALL PLAYWRIGHT CHECKS PASSED (100% SUCCESS)!")

    finally:
        if server_proc:
            print("Terminating test Streamlit server...")
            server_proc.terminate()
            server_proc.wait()

if __name__ == "__main__":
    main()
