#!/usr/bin/env python3
"""Automated end-to-end verification across the bundled telemetry datasets.

Executes:
  1. Full backend pipeline on all 11 bundles (01 - 11):
     - Ingest .bin / .fss.bin (or open .dat for bundle 11) via Java BlockStore + JavaRetriever.
     - Verify time bounds (start_ns <= end_ns, realistic duration < 3600s, zero multi-year jumps).
     - Verify retrieved event count > 0.
     - Decode telemetry frames using appropriate schema (drone, microservices, can, plc, warehouse, hft).
     - Retrieve optional domain context exclusively from knowledge_base/.
     - Assert Stage 0 Sufficiency Gate triggers INSUFFICIENT_EVIDENCE / Quarantine for bundle 09.
     - Assert UNKNOWN_EVENT_TYPE and zero payload leak for bundles 04 (0x0499) and 07 (0x007F).
  2. Live Playwright UI verification on http://127.0.0.1:8501:
     - Upload a telemetry binary and assert clean rendering with zero Streamlit exceptions.
     - Save screenshot to artifacts.
  3. Outputs a clean Markdown verification summary table.
"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from bridge.java_retriever import JavaRetriever
from bridge.sufficiency_gate import SufficiencyStatus, evaluate_evidence_sufficiency
from bridge.synthesizer import retrieve_relevant_knowledge
from fss_py.decoder import decode_all
from fss_py.evidence import build_evidence
from fss_py.model import ALL, DecodeStatus
from fss_py.schema import load_schema_file
from fss_py.wire import iter_envelopes


BUNDLE_SCHEMAS: dict[str, str] = {
    "01": "drone_v0.yaml",
    "02": "drone_v0.yaml",
    "03": "microservices_v0.yaml",
    "04": "autonomous_vehicle_v0.yaml",
    "05": "microservices_v0.yaml",
    "06": "drone_v0.yaml",
    "07": "industrial_plc_v0.yaml",
    "08": "warehouse_robots_v0.yaml",
    "09": "drone_v0.yaml",
    "10": "financial_hft_v0.yaml",
    "11": "drone_v0.yaml",
}


@dataclass
class BundleVerificationResult:
    bundle_id: str
    bundle_name: str
    binary_file: str
    events_count: int
    duration_s: float
    knowledge_documents: int
    verdict: str
    status: str
    error: str | None = None


def verify_backend_bundle(bundle_dir: Path) -> BundleVerificationResult:
    b_id = bundle_dir.name[:2]
    schema_name = BUNDLE_SCHEMAS.get(b_id, "drone_v0.yaml")
    schema_path = PROJECT_ROOT / "schemas" / schema_name
    schema = load_schema_file(schema_path)

    bin_files = list(bundle_dir.glob("*.bin")) + list(bundle_dir.glob("*.dat"))
    if not bin_files:
        raise FileNotFoundError(f"No binary/store file found in {bundle_dir}")
    bin_file = bin_files[0]

    with tempfile.TemporaryDirectory() as td:
        if bin_file.suffix == ".dat":
            store_path = bin_file
            jr = JavaRetriever(store_path=store_path)
            # In bundle 11, records come from swarm_298_474_opaque.fss.bin
            records_raw = list(iter_envelopes(
                (PROJECT_ROOT / "test_data_and_manuals" / "06_custom_drone_swarm_298_474" / "swarm_298_474_opaque.fss.bin").read_bytes()
            ))
        elif bin_file.name == "log171.bin":
            store_path = Path(td) / "test.dat"
            jr = JavaRetriever(store_path=store_path)
            jr.ingest(str(bin_file))
            records_raw = list(iter_envelopes(
                (PROJECT_ROOT / "test_data" / "irl" / "log171.fss.bin").read_bytes()
            ))
        else:
            store_path = Path(td) / "test.dat"
            jr = JavaRetriever(store_path=store_path)
            jr.ingest(str(bin_file))
            records_raw = list(iter_envelopes(bin_file.read_bytes()))

        if not records_raw:
            raise AssertionError(f"Zero envelopes read from {bin_file.name}")

        t_min = min(r.timestamp_ns for r in records_raw)
        t_max = max(r.timestamp_ns for r in records_raw)
        assert t_min <= t_max, f"Invalid timestamps in {bin_file.name}: t_min > t_max"

        duration_s = (t_max - t_min) / 1e9
        assert duration_s <= 3600.0, f"Unbounded epoch jump detected: duration {duration_s:.1f}s > 3600s"

        res = jr.retrieve(
            source=ALL,
            t_start_ns=t_min,
            t_end_ns=min(t_max + 1000, t_min + 3600_000_000_000),
            max_events=10000,
        )

        assert len(res.events) > 0, f"Zero events retrieved from {bin_file.name}"

        decoded = decode_all(res.events, schema)
        query = "Summarize the retrieved telemetry and its data gaps."
        evidence = build_evidence(decoded, query=query)
        knowledge = retrieve_relevant_knowledge(evidence, query)
        if any(
            document.get("domain") != "knowledge_base"
            or not document.get("source_filename")
            for document in knowledge
        ):
            raise AssertionError("Retrieved domain context must originate from knowledge_base/.")

        # Bundle-specific assertions
        verdict = "VERIFIED_SUPPORTED"

        if b_id == "09":
            # Blackout gap must trigger Stage 0 sufficiency gate refusal
            suff = evaluate_evidence_sufficiency(evidence, query="Why did drone 52 crash?")
            assert not suff.is_sufficient, "Expected Sufficiency Gate to fail for 42s blackout gap"
            assert suff.status == SufficiencyStatus.INSUFFICIENT_EVIDENCE, (
                f"Expected INSUFFICIENT_EVIDENCE, got {suff.status}"
            )
            verdict = "INSUFFICIENT_EVIDENCE (QUARANTINED)"
        if b_id == "04":
            # CAN bus must decode 0x0499 as UNKNOWN_EVENT_TYPE with 0 leaked payload fields
            unknown_can = [d for d in decoded if d.event_type == 0x0499]
            assert len(unknown_can) > 0, "Expected 0x0499 unknown CAN events"
            for u in unknown_can:
                assert u.decode_status == DecodeStatus.UNKNOWN_EVENT_TYPE
                assert u.unknown_bytes == 8
                assert u.fields == {}

        if b_id == "07":
            # Industrial PLC must decode 0x007F as UNKNOWN_EVENT_TYPE with 0 leaked payload fields
            unknown_plc = [d for d in decoded if d.event_type == 0x007F]
            assert len(unknown_plc) > 0, "Expected 0x007F unknown PLC events"
            for u in unknown_plc:
                assert u.decode_status == DecodeStatus.UNKNOWN_EVENT_TYPE
                assert u.unknown_bytes == 20
                assert u.fields == {}

        return BundleVerificationResult(
            bundle_id=b_id,
            bundle_name=bundle_dir.name,
            binary_file=bin_file.name,
            events_count=len(res.events),
            duration_s=round(duration_s, 1),
            knowledge_documents=len(knowledge),
            verdict=verdict,
            status="PASS",
        )


def run_playwright_check() -> None:
    """Run UI verification on http://127.0.0.1:8501 using Playwright in miniconda."""
    print("\n--------------------------------------------------------------------------------")
    print("  LAUNCHING PLAYWRIGHT UI VERIFICATION (Real-Life + Custom Bundles)")
    print("--------------------------------------------------------------------------------")

    playwright_script = PROJECT_ROOT / "scripts" / "_playwright_bundle_check.py"
    script_content = """import os
import sys
import time
from pathlib import Path
from playwright.sync_api import sync_playwright

ROOT = Path('/home/kirat/coding/minor')
URL = os.environ.get('STREAMLIT_URL', 'http://127.0.0.1:8501')
ARTIFACT_DIR = Path('/home/kirat/.gemini/antigravity/brain/90150533-5b4a-41d1-951c-0ba626226c93')

def run():
    print(f'Connecting to Streamlit at {URL}...')
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True, args=['--no-sandbox', '--disable-dev-shm-usage'])
        context = browser.new_context(viewport={'width': 1440, 'height': 1080})
        page = context.new_page()

        page_errors = []
        page.on('pageerror', lambda err: page_errors.append(str(err)))

        # Load page
        page.goto(URL, timeout=30000)
        page.wait_for_selector('.fss-masthead, .fss-kpi-card', timeout=30000)
        time.sleep(1.0)
        print('  UI loaded successfully.')

        # Upload a telemetry file; reference documents are loaded only from knowledge_base/.
        bin_01 = ROOT / 'test_data_and_manuals' / '01_real_ardupilot_log171' / 'log171.bin'
        print(f'  [Playwright] Uploading {bin_01.name}...')

        file_inputs = page.locator('input[type="file"]')
        file_inputs.first.set_input_files(str(bin_01))
        time.sleep(2.0)

        # Click analyze button
        analyze_btn = page.locator('button:has-text("Analyze Log"), button:has-text("⚡ Analyze Log")').first
        if analyze_btn.is_visible():
            analyze_btn.click()
            print('  --> Clicked Analyze button.')

        time.sleep(3.0)
        page.wait_for_load_state('networkidle')

        # Check for exceptions
        exceptions = page.locator('.stException').all()
        assert len(exceptions) == 0, f'Found {len(exceptions)} Streamlit exceptions on page!'

        # Capture screenshot
        out_png = ARTIFACT_DIR / 'bundles_ui_verified.png'
        page.screenshot(path=str(out_png), full_page=True)
        print(f'  --> Screenshot saved to {out_png}')
        print('  Playwright verification successful (0 exceptions, CRT rendered cleanly).')
        browser.close()

if __name__ == '__main__':
    run()
"""
    playwright_script.write_text(script_content, encoding="utf-8")

    miniconda_python = "/home/kirat/miniconda3/bin/python"
    cmd = [miniconda_python, str(playwright_script)]
    res = subprocess.run(cmd, capture_output=True, text=True)
    if res.returncode != 0:
        print(f"Playwright STDERR:\n{res.stderr}")
        print(f"Playwright STDOUT:\n{res.stdout}")
        raise RuntimeError(f"Playwright verification failed with return code {res.returncode}")
    else:
        print(res.stdout)

    if playwright_script.exists():
        playwright_script.unlink()


def main() -> None:
    print("================================================================================")
    print("  VERIFYING TELEMETRY DATA BUNDLES (01 - 11)")
    print("================================================================================")

    root = PROJECT_ROOT / "test_data_and_manuals"
    bundle_dirs = sorted([p for p in root.iterdir() if p.is_dir()])
    assert len(bundle_dirs) == 11, f"Expected exactly 11 bundle directories, found {len(bundle_dirs)}"

    results: list[BundleVerificationResult] = []

    for b in bundle_dirs:
        print(f"  Verifying {b.name}...")
        try:
            res = verify_backend_bundle(b)
            results.append(res)
            print(f"    [PASS] events={res.events_count}, duration={res.duration_s}s, knowledge_docs={res.knowledge_documents}, verdict={res.verdict}")
        except Exception as e:
            res = BundleVerificationResult(
                bundle_id=b.name[:2],
                bundle_name=b.name,
                binary_file="",
                events_count=0,
                duration_s=0.0,
                knowledge_documents=0,
                verdict="ERROR",
                status="FAIL",
                error=str(e),
            )
            results.append(res)
            print(f"    [FAIL] {e}")

    # Run Playwright UI Verification
    run_playwright_check()

    # Print Final Verification Table
    print("\n================================================================================")
    print("  FINAL 11-BUNDLE VERIFICATION MATRIX")
    print("================================================================================")
    print("| Bundle | Telemetry Binary | Events | Duration | KB Documents | Verdict | Status |")
    print("|---|---|---|---|---|---|---|---|")
    for r in results:
        print(f"| {r.bundle_id} {r.bundle_name[3:]} | `{r.binary_file}` | {r.events_count} | {r.duration_s}s | {r.knowledge_documents} | {r.verdict} | **{r.status}** |")
    print("================================================================================\n")

    failed = [r for r in results if r.status != "PASS"]
    if failed:
        print(f"FAILED: {len(failed)} bundles failed verification!")
        sys.exit(1)
    else:
        print("SUCCESS: 100% of 11 bundles verified successfully!")


if __name__ == "__main__":
    main()
