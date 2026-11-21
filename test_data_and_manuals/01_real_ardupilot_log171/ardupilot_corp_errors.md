# ArduPilot Corporate Flight Software Error & Status Reference Manual

This technical specification documents flight computer status indicators, extended Kalman filter (EKF) health transitions, failsafe event triggers, and sensor diagnostics for ArduPilot multirotor and fixed-wing autopilots.

## 1. Subsystem Error & Failsafe Code Reference Table

| Error Code | Subsystem | Severity | Title / Meaning | Causes | Actions |
|---|---|---|---|---|---|
| `SUBSYS_16_ECODE_2` | Estimator_EKF | CRITICAL | EKF Navigation Failsafe Triggered | Compass magnetic interference, velocity innovation gate exceeded, accelerometer vibration clipping | Switch to ALT_HOLD mode, check compass calibration and separation from power wiring, inspect vibration isolation mounts |
| `SUBSYS_16_ECODE_0` | Estimator_EKF | NOMINAL | EKF Failsafe Cleared / Navigation Restored | Filter innovations returned to normal envelope, satellite lock stabilized | Resume autonomous waypoint mission |
| `SUBSYS_16_ECODE_1` | Estimator_EKF | WARNING | EKF Variance High Warning | Moderate sensor disagreement, elevated magnetic noise | Monitor vehicle position, prepare for manual takeover |
| `SUBSYS_8_ECODE_0` | Telemetry_GCS | NOMINAL | GCS Heartbeat Failsafe Cleared | Telemetry radio link communication restored | Continue flight operation |
| `SUBSYS_8_ECODE_1` | Telemetry_GCS | WARNING | GCS Heartbeat Lost | RF telemetry link timeout, out-of-range | Check ground station antenna orientation |
| `SUBSYS_10_ECODE_0` | Flight_Mode | NOMINAL | Flight Mode Nominal / Armed | System armed and ready for flight | Normal operation |
| `SUBSYS_15_ECODE_0` | Autotune | NOMINAL | Autotune Nominal / Armed | Autotune sequence ready | Normal operation |
| `SUBSYS_25_ECODE_0` | Depth_Sensor | NOMINAL | Depth / Pressure Sensor Cleared | Pressure readings consistent | Normal operation |
| `SUBSYS_28_ECODE_0` | EKF_Monitor | CRITICAL | EKF Primary Failsafe Active | Redundant EKF core disagreement | Land vehicle immediately |
| `ECODE_0` | Safety_System | NOMINAL | Error Resolved / Failsafe Cleared | All subsystem error flags cleared | Normal flight |
| `EV_10` | Flight_Events | NOMINAL | Vehicle Armed | Motors energized and ready | Ensure safe perimeter |
| `EV_15` | Flight_Events | NOMINAL | Auto Armed | Autonomous mission start | Clear flight area |
| `EV_25` | Flight_Events | WARNING | Bad Depth / Pressure | Pressure sensor fluctuation | Check sensor port |
| `EV_28` | Flight_Events | CRITICAL | EKF Failsafe Event | Extended Kalman filter failsafe tripped | Land aircraft |

---

## 2. Detailed Diagnostic Descriptions

### [SUBSYS_16_ECODE_2] EKF Navigation Failsafe
- **Subsystem**: Estimator_EKF
- **Severity**: CRITICAL
- **Description**: Autopilot triggered an autonomous failsafe due to high EKF velocity or position variance exceeding safety thresholds.
- **Probable Causes**: High DC current magnetic interference with magnetometer; GPS multipath reflections; severe propeller vibration.
- **Recommended Actions**: Inspect compass calibration; verify distance between battery leads and flight controller; review vibration logs.

### [SUBSYS_16_ECODE_0] EKF Failsafe Cleared
- **Subsystem**: Estimator_EKF
- **Severity**: NOMINAL
- **Description**: Extended Kalman filter restored valid state estimate after temporary disturbance.
- **Probable Causes**: Temporary GPS glitch resolved; magnetic vector stabilized away from EMI source.
- **Recommended Actions**: Verify telemetry coordinates match actual vehicle position before resuming high-speed transit.
