# FSS Custom Telemetry Protocol & Error Codes Reference Manual

This technical specification documents the native Flight Software Subsystem (FSS) binary telemetry frame event types, subsystem error codes, status indicators, and recovery procedures.

## 1. FSS Telemetry Event Types & Status Codes

| Event Type (Hex) | Event Type (Dec) | Event Symbol | Subsystem | Status / Reason Code | Code Name | Severity | Title / Meaning | Probable Causes | Recommended Actions |
|---|---|---|---|---|---|---|---|---|---|
| `0x10` | 16 | `LIDAR_STATUS` | Perception | 0 | `OK` | NOMINAL | LiDAR Nominal Operation | Normal laser scan capture | Continuous monitoring |
| `0x10` | 16 | `LIDAR_STATUS` | Perception | 17 | `LIDAR_TIMEOUT` | ERROR | LiDAR Scan Timeout | 3+ consecutive missed sweeps, serial timeout, spinning motor stall | Reset LiDAR serial driver, check supply voltage, verify mirror motor rotation |
| `0x10` | 16 | `LIDAR_STATUS` | Perception | 18 | `SENSOR_DISCONNECTED` | CRITICAL | LiDAR Sensor Disconnected | Physical cable unseated, power rail dropout, CAN bus transceiver failure | Check physical wiring harness, measure 5V/12V rail, check termination resistor |
| `0x10` | 16 | `LIDAR_STATUS` | Perception | 19 | `INVALID_SCAN` | WARNING | LiDAR Invalid Scan Integrity | Optical glare saturation, high dust/fog scatter, checksum/CRC failure | Calibrate ambient light filter, clean front optical lens, inspect CRC logs |
| `0x20` | 32 | `AVOIDANCE` | Guidance | 1 | `COLLISION_RISK` | WARNING | Proximity Collision Avoidance | Inter-drone separation below safety bubble, intruder detected | Executed automated evasive climb/turn; verify adjacent drone slot tracking |
| `0x20` | 32 | `AVOIDANCE` | Guidance | 2 | `GEOFENCE` | WARNING | Geofence Boundary Avoidance | Drone trajectory within buffer margin of forbidden perimeter | Command heading correction, re-plan trajectory inside safe flight polygon |
| `0x20` | 32 | `AVOIDANCE` | Guidance | 3 | `OPERATOR_COMMAND` | NOTICE | Manual Operator Override Evasion | Ground pilot manually commanded avoidance override | Review pilot logs, verify manual steering reason |
| `0x21` | 33 | `FORMATION_STATE` | Swarm | 0 | `IN_FORMATION` | NOMINAL | In Assigned Formation Slot | Drone tracking nominal swarm position | Normal formation operations |
| `0x21` | 33 | `FORMATION_STATE` | Swarm | 1 | `LEFT_FORMATION` | WARNING | Slot Abandonment / Left Formation | Drone deviated from assigned slot following avoidance or sensor fault | Assess root cause, monitor safe standoff distance, verify adjacent slot positions |
| `0x21` | 33 | `FORMATION_STATE` | Swarm | 2 | `REJOINING` | NOTICE | Rejoining Formation Slot | Drone executing planned rendezvous maneuver back to swarm corridor | Track convergence rate, ensure collision avoidance stays active during rejoin |
| `0x30` | 48 | `DIAG_STATUS` | Diagnostics | 0 | `NOMINAL` | NOMINAL | System Diagnostic Nominal | All subsystems reporting healthy telemetry | Normal operation |
| `0x30` | 48 | `DIAG_STATUS` | Diagnostics | 1 | `DEGRADED` | WARNING | Subsystem Degraded | Thermal elevation (>65°C), high CPU utilization, packet drop | Throttle non-critical logging, inspect heatsinks, monitor battery temperature |
| `0x30` | 48 | `DIAG_STATUS` | Diagnostics | 2 | `FAULT` | CRITICAL | Subsystem Critical Fault | Internal driver crash, hardware interlock trip, actuator failure | Initiate emergency landing or failsafe egress immediately |
| `0x31` | 49 | `HEARTBEAT` | Comms | 0 | `HEARTBEAT_OK` | NOMINAL | Node Heartbeat Healthy | Periodic keep-alive received within 500ms window | Normal telemetry stream |
| `0x31` | 49 | `HEARTBEAT` | Comms | 1 | `HEARTBEAT_TIMEOUT` | WARNING | Node Heartbeat Timeout | Missed 3 consecutive heartbeat cycles (>1500ms) | Check mesh radio signal, verify processor load on remote node |
| `0x31` | 49 | `HEARTBEAT` | Comms | 2 | `BUS_RESET` | ERROR | Communication Bus Reset | Hardware watchdog reset triggered on telemetry bus controller | Inspect SPI/UART bus lines, replace faulty transceiver |
| `0x40` | 64 | `MOTOR_ESC` | Propulsion | 0 | `ESC_NOMINAL` | NOMINAL | Motor ESC Healthy | Commutation speed, voltage, and current nominal | Normal flight |
| `0x40` | 64 | `MOTOR_ESC` | Propulsion | 1 | `ESC_OVERCURRENT` | ERROR | Motor ESC Over-Current | Current draw exceeding motor rated continuous amperage | Check propeller fouling, inspect motor bearings, verify payload weight |
| `0x40` | 64 | `MOTOR_ESC` | Propulsion | 2 | `ESC_OVERTEMP` | ERROR | Motor ESC Over-Temperature | ESC FET temperature exceeded safe limit (>85°C) | Reduce throttle, improve airflow over ESC arms, inspect thermal pads |
| `0x40` | 64 | `MOTOR_ESC` | Propulsion | 3 | `ESC_PHASE_LOSS` | CRITICAL | Motor Phase Lead Loss | Broken bullet connector or severed motor winding phase | Land immediately, replace damaged motor or ESC |
| `0x40` | 64 | `MOTOR_ESC` | Propulsion | 4 | `ESC_DESYNC` | CRITICAL | Motor Commutation Desynchronization | Back-EMF detection lost during rapid throttle transients | Flash ESC firmware, adjust motor timing advance, verify PWM/DShot frequency |
| `0x41` | 65 | `BARO_DRIFT` | Sensors | 0 | `BARO_NOMINAL` | NOMINAL | Barometer Reading Nominal | Altitude estimate matches complementary filter | Normal operation |
| `0x41` | 65 | `BARO_DRIFT` | Sensors | 1 | `BARO_EXCESSIVE_VARIANCE` | WARNING | Excessive Barometer Variance | Rapid pressure fluctuations exceeding noise threshold | Inspect barometer enclosure for direct wind impingement, add open-cell foam |
| `0x41` | 65 | `BARO_DRIFT` | Sensors | 2 | `BARO_ALTITUDE_JUMP` | ERROR | Sudden Barometer Altitude Jump | Discontinuous vertical jump > 5 meters in < 100ms | Check for static discharge, recalibrate sea level pressure reference |
| `0x42` | 66 | `COMPASS_SPIKE` | Navigation | 0 | `COMPASS_NOMINAL` | NOMINAL | Magnetometer Nominal | Earth magnetic field vector stable | Normal operation |
| `0x42` | 66 | `COMPASS_SPIKE` | Navigation | 1 | `COMPASS_EMI_INTERFERENCE` | WARNING | Electromagnetic Spike on Compass | High current pulse induced magnetic deflection > 30 degrees | Relocate magnetometer away from battery harness, calibrate CompassMot |
| `0x42` | 66 | `COMPASS_SPIKE` | Navigation | 2 | `COMPASS_CALIBRATION_LOST` | CRITICAL | Magnetometer Calibration Invalidation | Stored calibration offsets corrupted or inconsistent with Earth field | Land drone, perform complete 3D ellipsoid compass calibration |
| `0x45` | 69 | `ERROR_RECORD` | Safety | 0 | `ERR_NOTICE` | NOTICE | Flight Software Notice Logged | Non-critical event requiring operator audit | Logged for post-flight telemetry review |
| `0x45` | 69 | `ERROR_RECORD` | Safety | 1 | `ERR_RECOVERABLE_FAULT` | ERROR | Recoverable Subsystem Fault | Subsystem failure recovered through redundant channel | Schedule post-mission bench inspection |
| `0x45` | 69 | `ERROR_RECORD` | Safety | 2 | `ERR_CRITICAL_SAFETY_STOP` | CRITICAL | Emergency Safety Stop Engaged | Unrecoverable flight safety condition triggered emergency abort | Abort flight, deploy parachute or land immediately |

---

## 2. Ingestion & Protocol Decoding Notes

1. **Exact Alphanumeric Code Preservations**:
   - Event types may be reported as hexadecimal (`0x10`, `0x20`, `0x30`, `0x40`, `0x41`, `0x42`, `0x45`) or decimal (`16`, `32`, `48`, `64`, `65`, `66`, `69`).
   - Status/Reason codes (`17`, `18`, `19`, `1`, `2`, `3`, `4`) must be correlated with their parent event type.
   - Symbolic aliases (e.g. `LIDAR_TIMEOUT`, `SENSOR_DISCONNECTED`, `COLLISION_RISK`, `LEFT_FORMATION`, `ESC_DESYNC`) must be cross-indexed with numeric values.

2. **Opaque Data Quarantine**:
   - Any payload bytes following documented schema fields in `0x30` or `0x31` that cannot be decoded must be reported as opaque data gaps, never guessed or fabricated.
