# AeroDrone Inc. Flight Protocol & Telemetry Reference Manual

This technical specification documents native Flight Software Subsystem (FSS) binary telemetry frame event types, status indicators, avoidance maneuvers, and formation controls for AeroDrone commercial swarms.

## 1. Telemetry Event Types & Status Codes

| Event Type (Hex) | Event Type (Dec) | Event Symbol | Subsystem | Status / Reason Code | Code Name | Severity | Title / Meaning | Causes | Actions |
|---|---|---|---|---|---|---|---|---|---|
| `0x10` | 16 | `LIDAR_STATUS` | Perception | 0 | `OK` | NOMINAL | LiDAR Nominal Operation | Normal laser scan capture | Continuous monitoring |
| `0x10` | 16 | `LIDAR_STATUS` | Perception | 17 | `LIDAR_TIMEOUT` | ERROR | LiDAR Scan Timeout | 3+ consecutive missed sweeps, serial timeout, spinning motor stall | Reset LiDAR serial driver, check supply voltage, verify mirror motor rotation |
| `0x10` | 16 | `LIDAR_STATUS` | Perception | 18 | `SENSOR_DISCONNECTED` | CRITICAL | LiDAR Sensor Disconnected | Physical cable unseated, power rail dropout, CAN bus transceiver failure | Check physical wiring harness, measure 5V/12V rail |
| `0x20` | 32 | `AVOIDANCE` | Guidance | 1 | `COLLISION_RISK` | WARNING | Proximity Collision Avoidance | Inter-drone separation below safety bubble, intruder detected | Executed automated evasive climb/turn; verify adjacent drone slot tracking |
| `0x20` | 32 | `AVOIDANCE` | Guidance | 2 | `GEOFENCE` | WARNING | Geofence Boundary Avoidance | Drone trajectory within buffer margin of forbidden perimeter | Command heading correction |
| `0x21` | 33 | `FORMATION_STATE` | Swarm | 0 | `IN_FORMATION` | NOMINAL | In Assigned Formation Slot | Drone tracking nominal swarm position | Normal formation operations |
| `0x21` | 33 | `FORMATION_STATE` | Swarm | 1 | `LEFT_FORMATION` | WARNING | Slot Abandonment / Left Formation | Drone deviated from assigned slot following avoidance or sensor fault | Assess root cause, monitor safe standoff distance |
| `0x30` | 48 | `DIAG_STATUS` | Diagnostics | 0 | `NOMINAL` | NOMINAL | System Diagnostic Nominal | All subsystems reporting healthy telemetry | Normal operation |
| `0x30` | 48 | `DIAG_STATUS` | Diagnostics | 1 | `DEGRADED` | WARNING | Subsystem Degraded | Thermal elevation (>65°C), high CPU utilization, packet drop | Throttle non-critical logging, inspect heatsinks |
| `0x30` | 48 | `DIAG_STATUS` | Diagnostics | 2 | `FAULT` | CRITICAL | Subsystem Critical Fault | Internal driver crash, hardware interlock trip | Initiate emergency landing |

---

## 2. Ingestion & Opaque Quarantine Rules

1. **Opaque Data Quarantine**:
   - Any payload bytes following documented schema fields in `0x30` that cannot be decoded must be reported as opaque data gaps, never guessed or fabricated.
