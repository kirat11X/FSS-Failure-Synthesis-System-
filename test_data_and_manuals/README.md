# FSS Test Data & Vendor Error Manuals Directory (`test_data_and_manuals/`)

This directory provides **11 self-contained test bundles** pairing telemetry binaries (`.bin`, `.fss.bin`, `.dat`) with genuine company error-code manuals (`.md`, `.txt`, `.json`, `.csv`). Every bundle is designed to be uploaded directly in the Failure Synthesis System (FSS) UI with **zero configuration**, producing **> 0 matched error rules** and fully grounded citations.

---

## Master Quick-Reference Table

| # | Bundle Directory | Telemetry Binary | Format | Paired Vendor Manual | Format | Domain | Key Fault Scenario | Expected Matched Rules |
|---|---|---|---|---|---|---|---|---|
| **01** | `01_real_ardupilot_log171` | `log171.bin` | ArduPilot DataFlash | `ardupilot_corp_errors.md` | Markdown | D1 Drones | EKF / Compass variance failsafe trigger (`SUBSYS_16_ECODE_2`) | >= 7 rules |
| **02** | `02_real_px4_baro_dropout` | `px4_real_flight.fss.bin` | Contract v0 Little-Endian | `px4_autopilot_errors.txt` | Plain Text | D1 Drones | Sensor diagnostic degradation (`DEGRADED`, `FAULT`) & baro drift | >= 2 rules |
| **03** | `03_real_rcaeval_microservices` | `rcaeval_boutique_cascade.fss.bin` | Contract v0 Little-Endian | `boutique_cloud_errors.json` | JSON | D2 Microservices | Online Boutique DB pool exhaustion & HTTP 504 gateway timeout cascade | >= 2 rules |
| **04** | `04_real_can_vehicle_bigendian` | `vehicle_can_bigendian.fss.bin` | Contract v0 Big-Endian | `oem_ecu_can_errors.csv` | CSV | D4 Automotive | CAN bus sensor communication fault & ABS failure with opaque CAN ID `0x0499` | >= 2 rules |
| **05** | `05_real_loghub_cluster` | `loghub_distributed_ops.fss.bin` | Contract v0 Little-Endian | `cluster_ops_errors.md` | Markdown | D6 Distributed | HDFS DataNode packet responder termination (`1002`) & ZooKeeper quorum drop | >= 2 rules |
| **06** | `06_custom_drone_swarm_298_474` | `swarm_298_474_opaque.fss.bin` | Contract v0 Little-Endian | `aerodrone_inc_manual.md` | Markdown | D1 Swarm | Cross-agent cascade: Source 298 LiDAR fault -> Source 474 avoidance -> slot exit | >= 4 rules |
| **07** | `07_custom_industrial_plc_line` | `plc_spindle_interlock.fss.bin` | Contract v0 Little-Endian | `siemens_line_plc_errors.txt` | Plain Text | D3 Industrial | Spindle thermal runaway (88.5°C) -> INTERLOCK trip -> Conveyor starved | >= 2 rules |
| **08** | `08_custom_warehouse_agv_skew` | `agv_clock_skew_chain.fss.bin` | Contract v0 Little-Endian | `logibot_agv_errors.json` | JSON | D5 Warehouse | AGV-12 battery collapse halting aisle; AGV-7 replans with skewed clock | >= 2 rules |
| **09** | `09_custom_blackout_data_gap` | `telemetry_42s_blackout.fss.bin` | Contract v0 Little-Endian | `aerodrone_inc_manual.md` | Markdown | D1 Drones | 42-second unrecorded blackout gap across failure trigger (Sufficiency Gate refusal) | Quarantine / Insufficient |
| **10** | `10_custom_hft_fix_storm` | `hft_fix_reject_storm.fss.bin` | Contract v0 Little-Endian | `apex_fix_gateway_errors.csv` | CSV | D6 Financial | FIX gateway order-reject and cancel-reject storm during volatility spike | >= 2 rules |
| **11** | `11_custom_persisted_blockstore_dat` | `recovered_blockstore.dat` | 64KB CRC-32C BlockStore | `aerodrone_inc_manual.md` | Markdown | Core Storage | Direct loading of pre-persisted Java BlockStore file over IPC | >= 4 rules |

---

## How to Test Any Bundle in the UI

1. Open the **FSS Telemetry Diagnostics Terminal** in your browser (`http://127.0.0.1:8501`).
2. Depending on the bundle:
   - For Bundles **01–10**: Under **Storage & Data Source**, choose `Upload Custom Binary (.bin / .fss.bin)` and upload the bundle's binary file.
   - For Bundle **11**: Choose `Existing Java BlockStore (.dat)` and enter the path `test_data_and_manuals/11_custom_persisted_blockstore_dat/recovered_blockstore.dat`.
3. In the **Domain / Error Manual** uploader, upload the bundle's paired manual file (`.md`, `.txt`, `.json`, or `.csv`).
4. Read `TEST_GUIDE.txt` inside that bundle's directory for the recommended query and expected verdict.
5. Click **Execute Synthesis** and inspect the grounded `[ev-...]` and `[err-...]` citation badges.
