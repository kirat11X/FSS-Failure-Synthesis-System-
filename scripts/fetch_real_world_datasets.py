#!/usr/bin/env python3
"""scripts/fetch_real_world_datasets.py
---------------------------------------
Download lightweight, real-world failure datasets across all 6 non-trivial domains
supported by the Failure Synthesis System (FSS):
  1. D1 Drones: PX4 ULog (sample.ulg)
  2. D2 Microservices: RCAEval RE2-OB (CPU, Delay, Socket error cases from HuggingFace)
  3. D3 Industrial PLC / SCADA: Modbus PCAP + HAI 22.04 industrial sensor CSV slice
  4. D4 Automotive CAN Bus: CAN-MIRGU masquerade attack log
  5. D5 Robotics: Foxglove MCAP demo log
  6. D6 Distributed Systems: Loghub 2k logs (HDFS, BGL, Zookeeper, Apache)

Outputs downloaded files to `test_data/real_world/raw/` and emits a manifest table.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parent.parent
REAL_WORLD_DIR = ROOT_DIR / "test_data" / "real_world"
RAW_DIR = REAL_WORLD_DIR / "raw"

# User-Agent for standard requests
HEADERS = {"User-Agent": "FSS-RealWorld-DataFetcher/1.0 (Mozilla/5.0 compatible)"}


def compute_sha256(file_path: Path) -> str:
    """Compute hex SHA-256 digest of a file."""
    h = hashlib.sha256()
    with file_path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def download_stream(
    url: str,
    dest_path: Path,
    max_bytes: int | None = None,
    max_lines: int | None = None,
    label: str = "",
) -> tuple[bool, int, str]:
    """Download a file or slice from a URL."""
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            bytes_written = 0
            lines_written = 0
            with dest_path.open("wb") as out:
                if max_lines is not None:
                    for line in resp:
                        out.write(line)
                        bytes_written += len(line)
                        lines_written += 1
                        if lines_written >= max_lines:
                            break
                else:
                    while True:
                        to_read = 65536
                        if max_bytes is not None:
                            remaining = max_bytes - bytes_written
                            if remaining <= 0:
                                break
                            to_read = min(to_read, remaining)
                        chunk = resp.read(to_read)
                        if not chunk:
                            break
                        out.write(chunk)
                        bytes_written += len(chunk)
        return True, bytes_written, url
    except Exception as exc:
        print(f"  [WARN] Failed to download {label} from {url}: {exc}", file=sys.stderr)
        return False, 0, ""


def fetch_rcaeval_cases() -> list[dict[str, Any]]:
    """Fetch 3 real multi-source RCAEval cases via huggingface_hub."""
    from huggingface_hub import hf_hub_download

    cases = [
        ("re2ob_checkoutservice_cpu_1", "cpu", "checkoutservice"),
        ("re2ob_checkoutservice_delay_1", "delay", "checkoutservice"),
        ("re2ob_checkoutservice_socket_1", "socket", "checkoutservice"),
    ]
    results = []
    for case_id, fault_type, root_cause in cases:
        case_dir = RAW_DIR / "rcaeval" / case_id
        case_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "case_id": case_id,
            "fault_type": fault_type,
            "expected_root_cause_service": root_cause,
            "files": {},
        }
        for fname in ["inject_time.txt", "metrics.parquet", "logs.parquet"]:
            try:
                local_path = hf_hub_download(
                    repo_id="phamquiluan/RCAEval",
                    filename=f"{case_id}/{fname}",
                    repo_type="dataset",
                )
                dest = case_dir / fname
                dest.write_bytes(Path(local_path).read_bytes())
                meta["files"][fname] = {
                    "path": str(dest.relative_to(ROOT_DIR)),
                    "size": dest.stat().st_size,
                    "sha256": compute_sha256(dest),
                }
            except Exception as e:
                print(f"  [WARN] Failed to fetch RCAEval {case_id}/{fname}: {e}")
        # Save ground truth sidecar
        gt_file = case_dir / "ground_truth.json"
        gt_file.write_text(json.dumps(meta, indent=2))
        results.append(meta)
    return results


def main() -> int:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    manifest: list[dict[str, Any]] = []

    print("================================================================================")
    print("   DOWNLOADING REAL-WORLD MULTI-DOMAIN FAILURE TELEMETRY (PHASE 1)")
    print("================================================================================")

    # 1. D1 Drones: PX4 sample.ulg
    print("\n[1/6] D1 Drones (PX4 ULog)...")
    px4_url = "https://raw.githubusercontent.com/PX4/pyulog/main/test/sample.ulg"
    px4_dest = RAW_DIR / "px4" / "sample.ulg"
    ok, size, src_url = download_stream(px4_url, px4_dest, label="PX4 sample.ulg")
    if ok:
        manifest.append({
            "domain": "D1 Drones",
            "name": "sample.ulg",
            "path": str(px4_dest.relative_to(ROOT_DIR)),
            "size_bytes": size,
            "sha256": compute_sha256(px4_dest),
            "source_url": src_url,
        })
        print(f"  -> OK: {px4_dest} ({size:,} bytes)")

    # 2. D2 Microservices: RCAEval RE2 Online Boutique
    print("\n[2/6] D2 Microservices (RCAEval RE2-OB)...")
    rca_cases = fetch_rcaeval_cases()
    for case in rca_cases:
        for fname, finfo in case["files"].items():
            manifest.append({
                "domain": "D2 Microservices",
                "name": f"{case['case_id']}_{fname}",
                "path": finfo["path"],
                "size_bytes": finfo["size"],
                "sha256": finfo["sha256"],
                "source_url": "hf://phamquiluan/RCAEval",
            })
        print(f"  -> OK: RCAEval case {case['case_id']} ({case['fault_type']} -> {case['expected_root_cause_service']})")

    # 3. D3 Industrial PLC / SCADA
    print("\n[3/6] D3 Industrial PLC / SCADA (Modbus PCAP + HAI CSV)...")
    # Modbus PCAP
    modbus_urls = [
        "https://media.githubusercontent.com/media/automayt/ICS-pcap/master/MODBUS/MODBUS-TestDataPart1/MODBUS-TestDataPart1.pcap",
        "https://github.com/automayt/ICS-pcap/raw/master/MODBUS/MODBUS-TestDataPart1/MODBUS-TestDataPart1.pcap",
    ]
    modbus_dest = RAW_DIR / "industrial" / "MODBUS-TestDataPart1.pcap"
    modbus_ok = False
    for url in modbus_urls:
        ok, size, src_url = download_stream(url, modbus_dest, label="Modbus PCAP")
        if ok and size > 500:
            manifest.append({
                "domain": "D3 Industrial PLC",
                "name": "MODBUS-TestDataPart1.pcap",
                "path": str(modbus_dest.relative_to(ROOT_DIR)),
                "size_bytes": size,
                "sha256": compute_sha256(modbus_dest),
                "source_url": src_url,
            })
            print(f"  -> OK: {modbus_dest} ({size:,} bytes)")
            modbus_ok = True
            break

    # HAI CSV slice (5,000 lines slice of real steam boiler / turbine sensors)
    hai_urls = [
        "https://media.githubusercontent.com/media/icsdataset/hai/master/hai-22.04/test1.csv",
        "https://raw.githubusercontent.com/icsdataset/hai/master/hai-22.04/test1.csv",
    ]
    hai_dest = RAW_DIR / "industrial" / "hai_test1_slice.csv"
    for url in hai_urls:
        ok, size, src_url = download_stream(url, hai_dest, max_lines=5000, label="HAI test1 slice")
        if ok and size > 1000:
            manifest.append({
                "domain": "D3 Industrial PLC",
                "name": "hai_test1_slice.csv",
                "path": str(hai_dest.relative_to(ROOT_DIR)),
                "size_bytes": size,
                "sha256": compute_sha256(hai_dest),
                "source_url": src_url,
            })
            print(f"  -> OK: {hai_dest} ({size:,} bytes, 5,000 rows)")
            break

    # 4. D4 Automotive CAN Bus
    print("\n[4/6] D4 Automotive CAN Bus (CAN-MIRGU Masquerade Attack)...")
    can_url = "https://raw.githubusercontent.com/sampathrajapaksha/CAN-MIRGU/main/Sample%20dataset/Attack/Real_attacks/Break_and_fog_light_attack.log"
    can_dest = RAW_DIR / "can_bus" / "Break_and_fog_light_attack_slice.log"
    ok, size, src_url = download_stream(can_url, can_dest, max_lines=15000, label="CAN log slice")
    if ok:
        manifest.append({
            "domain": "D4 Automotive CAN",
            "name": "Break_and_fog_light_attack_slice.log",
            "path": str(can_dest.relative_to(ROOT_DIR)),
            "size_bytes": size,
            "sha256": compute_sha256(can_dest),
            "source_url": src_url,
        })
        print(f"  -> OK: {can_dest} ({size:,} bytes, 15,000 frames)")

    # 5. Robotics MCAP
    print("\n[5/6] Robotics MCAP (Foxglove demo.mcap)...")
    mcap_urls = [
        "https://media.githubusercontent.com/media/foxglove/mcap/main/testdata/mcap/demo.mcap",
        "https://raw.githubusercontent.com/foxglove/mcap/main/testdata/mcap/demo.mcap",
    ]
    mcap_dest = RAW_DIR / "robotics" / "demo.mcap"
    for url in mcap_urls:
        ok, size, src_url = download_stream(url, mcap_dest, label="Foxglove demo.mcap")
        if ok and size > 200:
            manifest.append({
                "domain": "Robotics MCAP",
                "name": "demo.mcap",
                "path": str(mcap_dest.relative_to(ROOT_DIR)),
                "size_bytes": size,
                "sha256": compute_sha256(mcap_dest),
                "source_url": src_url,
            })
            print(f"  -> OK: {mcap_dest} ({size:,} bytes)")
            break

    # 6. Distributed Systems (Loghub 2k)
    print("\n[6/6] Distributed Systems (Loghub 2k)...")
    loghub_files = [
        ("HDFS_2k.log", "https://raw.githubusercontent.com/logpai/loghub/master/HDFS/HDFS_2k.log"),
        ("BGL_2k.log", "https://raw.githubusercontent.com/logpai/loghub/master/BGL/BGL_2k.log"),
        ("Zookeeper_2k.log", "https://raw.githubusercontent.com/logpai/loghub/master/Zookeeper/Zookeeper_2k.log"),
        ("Apache_2k.log", "https://raw.githubusercontent.com/logpai/loghub/master/Apache/Apache_2k.log"),
    ]
    for name, url in loghub_files:
        dest = RAW_DIR / "loghub" / name
        ok, size, src_url = download_stream(url, dest, label=f"Loghub {name}")
        if ok:
            manifest.append({
                "domain": "Distributed Systems",
                "name": name,
                "path": str(dest.relative_to(ROOT_DIR)),
                "size_bytes": size,
                "sha256": compute_sha256(dest),
                "source_url": src_url,
            })
            print(f"  -> OK: {dest} ({size:,} bytes)")

    # Save manifest JSON
    manifest_path = REAL_WORLD_DIR / "manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2))

    print("\n================================================================================")
    print(f"   DOWNLOAD SUMMARY — {len(manifest)} REAL-WORLD FILES ACROSS 6 DOMAINS")
    print("================================================================================")
    print(f"{'DOMAIN':<22} | {'FILE NAME':<35} | {'SIZE':<10} | {'SHA-256 (FIRST 12)':<12}")
    print("-" * 88)
    for item in manifest:
        sz_str = f"{item['size_bytes']:,} B"
        print(f"{item['domain']:<22} | {item['name']:<35} | {sz_str:<10} | {item['sha256'][:12]}")
    print(f"\nManifest saved to: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
