# ArduPilot Error Code & Event Diagnostic Reference Manual

This technical manual documents ArduPilot DataFlash error codes (`ERR` messages) and event identifiers (`EV` messages) recorded in onboard flight logs. In ArduPilot log format, errors are identified by subsystem ID (`Subsys`) and error code (`ECode`).

## 1. ArduPilot DataFlash ERR Subsystems & ECodes

| Subsys | Subsys Name | ECode | Error Code String | Severity | Title / Meaning | Probable Causes | Recommended Actions |
|---|---|---|---|---|---|---|---|
| 1 | Main | 1 | `SUBSYS_1_ECODE_1` | CRITICAL | Main loop slow / stuck | CPU overload, excessive logging rate, I2C bus hang | Reduce logging rate, check I2C sensors, inspect CPU load metrics |
| 2 | Radio | 2 | `SUBSYS_2_ECODE_2` | WARNING | Radio Late Frame | RF interference, antenna shadowing, transmitter out of range | Check telemetry/RC antenna placement, check transmitter battery and RF link margins |
| 3 | Compass | 1 | `SUBSYS_3_ECODE_1` | ERROR | Compass Too Many Errors | High magnetic field gradient, EMI from power distribution, bad sensor | Calibrate compass, isolate power cables from magnetometer, run CompassMot |
| 3 | Compass | 2 | `SUBSYS_3_ECODE_2` | CRITICAL | Compass Not Healthy | Hardware failure, communication bus failure, disconnected harness | Inspect I2C wiring and connector, replace external compass unit if error persists |
| 4 | OpticalFlow | 2 | `SUBSYS_4_ECODE_2` | ERROR | Optical Flow Not Healthy | Sensor communication lost, insufficient lighting, lens obstructed | Check optical flow sensor connection, ensure minimum 100 lux illumination, clean lens |
| 5 | FailSafe | 1 | `SUBSYS_5_ECODE_1` | ERROR | Radio FailSafe Triggered | Loss of RC receiver signal for > FS_THR_VALUE seconds | Inspect RC link, verify transmitter is active, check antenna orientation |
| 5 | FailSafe | 2 | `SUBSYS_5_ECODE_2` | CRITICAL | Battery FailSafe Triggered | Battery voltage dropped below FS_BATT_VOLTAGE or capacity below FS_BATT_MAH | Land immediately, check battery health, replace degraded battery pack |
| 5 | FailSafe | 3 | `SUBSYS_5_ECODE_3` | ERROR | GCS FailSafe Triggered | Telemetry link lost with Ground Control Station > FS_GCS_ENABLE | Check GCS radio modem, USB port, and RF link margins |
| 5 | FailSafe | 4 | `SUBSYS_5_ECODE_4` | WARNING | Fence FailSafe Triggered | Geofence ceiling or radius breach | Command Return-To-Launch (RTL) or maneuver drone back inside containment fence |
| 6 | Batt | 1 | `SUBSYS_6_ECODE_1` | CRITICAL | Battery Brownout Detected | Sudden high current draw collapsing bus voltage, faulty power module | Inspect PDB and BEC regulators, verify battery C-rating matches motor draw |
| 7 | GPS | 2 | `SUBSYS_7_ECODE_2` | ERROR | GPS Not Healthy | GPS receiver lost lock, HDOP > 2.0, satellite count < 6, hardware comms lost | Check GPS antenna clearance, inspect UART wiring, wait for good satellite constellation |
| 8 | GCS | 2 | `SUBSYS_8_ECODE_2` | WARNING | GCS Late Frame | High telemetry latency, packet loss over RF serial link | Lower telemetry baud rate, increase radio transmit power, reduce GCS stream rates |
| 9 | Fence | 1 | `SUBSYS_9_ECODE_1` | WARNING | Fence Altitude Limit Exceeded | Vehicle climbed above configured FENCE_ALT_MAX | Verify barometer/GPS altitude, command descent or RTL |
| 9 | Fence | 2 | `SUBSYS_9_ECODE_2` | WARNING | Fence Circle Limit Exceeded | Vehicle drifted beyond FENCE_RADIUS from home location | Verify position hold, check wind speed, return vehicle inside perimeter |
| 9 | Fence | 3 | `SUBSYS_9_ECODE_3` | WARNING | Fence Alt and Circle Limit Exceeded | Vehicle violated both ceiling and radius boundaries | Immediate RTL or Land required |
| 9 | Fence | 4 | `SUBSYS_9_ECODE_4` | WARNING | Fence Polygon Breach | Vehicle crossed complex polygon geofence boundary | Verify position estimate and command return trajectory |
| 10 | FlightMode | 1 | `SUBSYS_10_ECODE_1` | ERROR | Flight Mode Transition Failed | Navigation unready, sensor health inadequate for requested flight mode | Check sensor readiness before arming/switching modes (e.g. GPS lock for Loiter/Auto) |
| 11 | Motor | 1 | `SUBSYS_11_ECODE_1` | CRITICAL | Motor Lost / Desync | Motor ESC desync, broken propeller, phase cable disconnected | Check ESC telemetry, inspect motor windings, replace failing motor/ESC |
| 11 | Motor | 2 | `SUBSYS_11_ECODE_2` | CRITICAL | Crash Check Triggered | Extreme tilt angle with high throttle, impact detected | Disarm immediately, inspect airframe and propulsion system |
| 12 | Crash | 1 | `SUBSYS_12_ECODE_1` | CRITICAL | Crash Detected | Inverted attitude or sudden high-g deceleration | Cut motor power, inspect structural integrity |
| 13 | Parachute | 1 | `SUBSYS_13_ECODE_1` | ERROR | Parachute Deploy Altitude Too Low | Vehicle altitude below CHUTE_ALT_MIN for safe deployment | Review emergency chute trigger thresholds and minimum altitude limits |
| 13 | Parachute | 2 | `SUBSYS_13_ECODE_2` | CRITICAL | Parachute Not Deployed | Deployment servo jam, battery dead, failsafe misconfiguration | Inspect mechanical parachute release mechanism and servo linkage |
| 14 | EKF | 1 | `SUBSYS_14_ECODE_1` | ERROR | EKF Variance Warning | Velocity or position innovation variance exceeds safety threshold | High vibration, magnetometer disturbance, or GPS multipath |
| 14 | EKF | 2 | `SUBSYS_14_ECODE_2` | CRITICAL | EKF Bad Variance / Divergence | Compass and GPS disagreement, persistent filter divergence | Land vehicle, recalibrate IMU and compass, inspect vibration dampening |
| 15 | Autotune | 1 | `SUBSYS_15_ECODE_1` | WARNING | Autotune Bad Gains | Oscillation or divergence observed during automated PID tuning | Abort autotune, revert to conservative default gains |
| 16 | Terrain | 1 | `SUBSYS_16_ECODE_1` | ERROR | Terrain Data Missing | Terrain database tile not loaded on SD card or missing from GCS | Load required SRTM terrain elevation tiles to autopilot SD card |
| 17 | Navigation | 1 | `SUBSYS_17_ECODE_1` | ERROR | Navigation Stuck | Vehicle unable to advance toward next waypoint (headwind, obstacle) | Check wind limits, inspect velocity saturation, take manual control |
| 17 | Navigation | 2 | `SUBSYS_17_ECODE_2` | ERROR | Waypoint Track Drift | Cross-track error exceeds maximum permissible navigation corridor | Tune navigation PIDs, check GPS accuracy and position lag |
| 18 | Baro | 1 | `SUBSYS_18_ECODE_1` | WARNING | Barometer Glitch | Pressure sudden spike due to propwash, sunlight or wind blast | Foam shield barometer sensor, check aerodynamic venting |
| 18 | Baro | 2 | `SUBSYS_18_ECODE_2` | CRITICAL | Barometer Not Healthy | Barometer sensor communication failed or sensor died | Inspect I2C/SPI bus, replace flight controller or sensor daughterboard |
| 19 | Compass2 | 2 | `SUBSYS_19_ECODE_2` | ERROR | Secondary Compass Not Healthy | Secondary internal/external magnetometer communication failure | Check compass bus or disable secondary compass if uninstalled |
| 20 | Rangefinder | 2 | `SUBSYS_20_ECODE_2` | ERROR | Rangefinder Not Healthy | Downward rangefinder out of range or serial link dropped | Clean sensor optic, check wiring harness, verify maximum range |
| 21 | Batt2 | 2 | `SUBSYS_21_ECODE_2` | CRITICAL | Secondary Battery FailSafe | Dual battery monitor detected low voltage or fault on pack 2 | Land drone, inspect second battery pack |
| 22 | Payload | 1 | `SUBSYS_22_ECODE_1` | WARNING | Payload Release / Gripper Fault | Gripper actuator stall or payload communication timeout | Check gripper servo mechanism and payload telemetry link |

---

## 2. ArduPilot DataFlash Event Codes (EV)

ArduPilot DataFlash logs record operational state changes and system triggers in `EV` messages.

| Event ID | Event Symbol | Severity | Title / Meaning | Probable Causes | Recommended Actions |
|---|---|---|---|---|---|
| 10 | `EV_10` | INFO | Armed | Motors armed by pilot or GCS | Normal operation; proceed with flight plan |
| 11 | `EV_11` | INFO | Disarmed | Motors disarmed after landing or timeout | Normal operation |
| 15 | `EV_15` | INFO | Auto Armed | Vehicle auto-armed by autonomous mission | Verify perimeter clear before autonomous motor spin |
| 18 | `EV_18` | INFO | Land Complete Maybe | Landing touchdown candidate detected | Awaiting confirmation of zero vertical rate |
| 19 | `EV_19` | INFO | Land Complete | Vehicle confirmed on ground, motors spooling down | Disarm motors |
| 21 | `EV_21` | WARNING | Lost GPS | GPS fix degraded or lost during flight | Pilot should take manual altitude hold or loiter control |
| 22 | `EV_22` | NOTICE | Flip Cancelled | Acrobatic flip aborted due to altitude margin | Verify adequate altitude before flip mode |
| 23 | `EV_23` | INFO | Takeoff | Autonomous takeoff climb started | Monitor climb rate and target altitude |
| 24 | `EV_24` | INFO | Land Complete Confirmed | Landing complete and confirmed by weight-on-wheels | Complete post-flight checks |
| 25 | `EV_25` | WARNING | Bad Depth | Underwater vehicle depth sensor reading anomalous | Check pressure transducer, purge air bubbles |
| 27 | `EV_27` | INFO | Compass Initialized | Compass calibration and offsets verified | Normal initialization |
| 28 | `EV_28` | CRITICAL | EKF Failsafe | EKF filter variance triggered emergency failsafe | Flight controller switched to Land or Althold; inspect sensors |
| 29 | `EV_29` | ERROR | Lost GPS Failsafe | GPS lost in navigation mode triggering emergency fallback | Vehicle switches to Non-GPS flight mode or Land |
| 30 | `EV_30` | CRITICAL | Throttle Emergency Stop | E-stop switch engaged by pilot or ground station | Immediate motor stop; prepare for crash recovery |
| 31 | `EV_31` | CRITICAL | Parachute Deployed | Emergency parachute triggered by crash or failsafe | Recovery sequence; inspect damaged airframe |

---

## 3. Detailed Subsystem Diagnoses

### [ERR-1-1] SUBSYS 1 CODE 1 (Main Slow Loop)
- **Subsystem**: Main Scheduler
- **Severity**: CRITICAL
- **Description**: Main scheduler loop exceeded deadline by > 50%. Flight controller cannot maintain control loop timing.
- **Probable Causes**: High logging rates (e.g. LOG_BITMASK set to all), heavy I2C bus latency from failing sensors, high telemetry stream rate.
- **Recommended Actions**: Reduce logging bitmask, check I2C error counters, update firmware.

### [ERR-5-2] SUBSYS 5 CODE 2 (Battery FailSafe)
- **Subsystem**: FailSafe
- **Severity**: CRITICAL
- **Description**: Power monitoring system triggered battery failsafe due to low voltage or mAh capacity exhaustion.
- **Probable Causes**: Under-sized battery, cell degradation, inaccurate current sensor calibration.
- **Recommended Actions**: Land immediately; inspect individual cell voltages; recalibrate BATT_AMP_PERVLT.

### [ERR-14-1] SUBSYS 14 CODE 1 (EKF Variance Warning)
- **Subsystem**: EKF (Extended Kalman Filter)
- **Severity**: ERROR
- **Description**: Innovation test ratio exceeded limit for velocity or position states.
- **Probable Causes**: Magnetic interference from motors, high vibration, poor GPS geometry.
- **Recommended Actions**: Check VIBE message in logs; inspect compass interference; move GPS away from transmitter.

### [ERR-14-2] SUBSYS 14 CODE 2 (EKF Bad Variance)
- **Subsystem**: EKF
- **Severity**: CRITICAL
- **Description**: EKF has declared its state estimate invalid and triggered emergency failsafe.
- **Probable Causes**: Severe sensor disagreement, compass calibration error, GPS glitch.
- **Recommended Actions**: Land immediately; perform 3D compass calibration; verify IMU vibration dampers.
