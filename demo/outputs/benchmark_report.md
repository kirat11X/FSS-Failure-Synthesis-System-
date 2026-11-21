# Failure Synthesis System (FSS) — Automated Benchmark Report

**Date:** 2026-11-21 03:38:28 UTC  
**Target Architecture:** Multi-Domain Java BlockStore + Epistemic Citation Auditor  

---

## 1. End-to-End Scenarios Evaluation (SC1 – SC8)

| ID | Scenario Name | Domain | Events | Verdict | Badge | Status |
| :---: | :--- | :--- | :---: | :--- | :--- | :---: |
| **SC1** | D1 Drones Cross-Agent Causal Chain | `d1_drone_fleet` | 9 | `ACCEPTED_WITH_CORRECTIONS` | `VERIFIED_SUPPORTED` | ✅ PASS |
| **SC2** | D2 Microservices Gateway Timeout Cascade | `d2_microservices` | 3 | `ACCEPTED` | `VERIFIED_SUPPORTED` | ✅ PASS |
| **SC3** | D3 Industrial PLC Thermal Interlock Trip | `d3_industrial_plc` | 3 | `ACCEPTED` | `VERIFIED_SUPPORTED` | ✅ PASS |
| **SC4** | D4 Autonomous Vehicle CAN Big-Endian Emergency Stop | `d4_autonomous_vehicle` | 3 | `ACCEPTED` | `VERIFIED_SUPPORTED` | ✅ PASS |
| **SC5** | D5 Cross-Producer Clock Skew & Drift | `d5_warehouse_robots` | 3 | `ACCEPTED` | `VERIFIED_SUPPORTED` | ✅ PASS |
| **SC6** | Nominal Negative Control (Zero Anomalies) | `d1_drone_fleet` | 3 | `ACCEPTED` | `VERIFIED_SUPPORTED` | ✅ PASS |
| **SC7** | Unknown Event Types Negative Control | `cross_domain` | 1 | `QUARANTINED` | `UNKNOWN_EVENT_TYPE` | ✅ PASS |
| **SC8** | Truncated Retrieval & Malformed Frame Control | `cross_domain` | 2 | `GAP_FLAGGED` | `DATA_GAP_QUARANTINE` | ✅ PASS |

---

## 2. ForceBench 5-Axis Quantitative Evaluation

- **Total Benchmark Cases Evaluated:** 200
- **Syntactic Citation Rejection Rate:** `100.0%` (target: 100.0%)
- **Entity Binding Trap Detection Rate:** `100.0%` (target: 100.0%)
- **Monotonicity Violation Rate (MVR):** `0.0000` (target: 0.0000)

| ForceBench Escalation Axis | Pairs Evaluated | Overclaims Flagged | Hedged Accepted | Axis Accuracy |
| :--- | :---: | :---: | :---: | :---: |
| **Axis: Relation** | 15 | 15 | 15 | **100.0%** |
| **Axis: Modality** | 15 | 15 | 15 | **100.0%** |
| **Axis: Scope** | 15 | 15 | 15 | **100.0%** |
| **Axis: Temporal** | 15 | 15 | 15 | **100.0%** |
| **Axis: Numeric** | 15 | 15 | 15 | **100.0%** |

---

## 3. Hybrid RAG Retrieval Quality

| Metric | Score | Target | Status |
| :--- | :---: | :---: | :---: |
| **Precision@3** | `1.000` | `>= 0.850` | ✅ PASS |
| **Recall@3** | `1.000` | `>= 0.800` | ✅ PASS |
| **Precision@5** | `0.767` | `>= 0.750` | ✅ PASS |
| **Mean Reciprocal Rank (MRR)** | `1.000` | `>= 0.900` | ✅ PASS |

---

## 4. Deterministic Decoder Throughput

| Schema Pack | Domain | Endianness | Batch Size | Latency (ms) | Throughput (events/s) |
| :--- | :--- | :---: | :---: | :---: | :---: |
| `drone_v0.yaml` | `d1_drone_fleet` | `little` | 10,000 | 318.5 | **31,396.8** |
| `microservices_v0.yaml` | `d2_microservices` | `little` | 10,000 | 64.5 | **155,127.6** |
| `industrial_plc_v0.yaml` | `d3_industrial_plc` | `little` | 10,000 | 74.2 | **134,751.3** |
| `autonomous_vehicle_v0.yaml` | `d4_autonomous_vehicle` | `big` | 10,000 | 69.9 | **143,103.8** |
| `warehouse_robots_v0.yaml` | `d5_warehouse_robots` | `little` | 10,000 | 102.1 | **97,910.9** |
| `financial_hft_v0.yaml` | `financial_hft` | `little` | 10,000 | 57.7 | **173,224.9** |

---

## 5. IPC Bridge Latency Comparison

| IPC Transport | p50 (Median) | p99 | Speedup vs Subprocess |
| :--- | :---: | :---: | :---: |
| **One-Shot Subprocess JavaRetriever** | `71.21 ms` | `72.51 ms` | Baseline (1.0x) |
| **Persistent JEP-380 UdsRetriever** | `0.95 ms` | `1.46 ms` | **75.1x faster** |
