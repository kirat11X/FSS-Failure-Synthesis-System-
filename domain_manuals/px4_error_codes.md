# PX4 Autopilot Error Code & Fault Diagnostic Reference Manual

This technical manual documents PX4 Autopilot system fault flags, estimator status errors, sensor health failures, failsafe triggers, battery alerts, and actuator saturation flags.

## 1. PX4 Diagnostic & Fault Code Catalog

| Error Code | Subsystem | Severity | Title / Meaning | Description | Probable Causes | Recommended Actions |
|---|---|---|---|---|---|---|
| `EST_FAULT_VEL_HORIZ` | Estimator | ERROR | Horizontal Velocity Innovation Fault | EKF2 rejected horizontal velocity measurement due to high innovation | GPS spoofing/glitch, optical flow noise, excessive vehicle acceleration | Check GPS signal quality, inspect velocity gate parameters `EKF2_VEL_NOISE`, inspect optical flow lens |
| `EST_FAULT_VEL_VERT` | Estimator | ERROR | Vertical Velocity Innovation Fault | EKF2 rejected vertical velocity measurement | Barometer pressure spike, propwash in fuselage, GPS altitude jump | Shield barometer with open-cell foam, verify static pressure port, tune `EKF2_BARO_GATE` |
| `EST_FAULT_POS_HORIZ` | Estimator | ERROR | Horizontal Position Innovation Fault | EKF2 rejected horizontal position updates | High GPS multipath, satellite constellation geometry shift, magnetometer drift | Inspect HDOP/VDOP metrics, verify clear sky view, perform magnetometer calibration |
| `EST_FAULT_POS_VERT` | Estimator | ERROR | Vertical Position Innovation Fault | EKF2 vertical position estimate diverged from reference | Sudden change in barometric pressure, ground effect disturbance | Land vehicle, calibrate barometer ground reference, inspect enclosure airflow |
| `EST_FAULT_ACCEL_BIAS` | Estimator | ERROR | Accelerometer Bias Fault | EKF2 detected excessive accelerometer bias exceeding calibration threshold | Mechanical shock, temperature drift, uncalibrated sensor | Perform 6-axis accelerometer calibration, isolate flight controller from frame vibrations |
| `EST_FAULT_MAG_HEADING` | Estimator | CRITICAL | Magnetometer Heading Divergence | EKF2 detected yaw disagreement between magnetometer and gyro integration | Electromagnetic interference from high-current DC cables, proximity to iron structure | Perform mag calibration via QGroundControl, enable mag compensation `CAL_MAG_COMP_TYP`, install external compass |
| `EST_FAULT_INNOVATION_REJECT` | Estimator | CRITICAL | General EKF2 Innovation Rejection | Multiple sensor innovations exceeded gating limits simultaneously | Severe mechanical vibration, simultaneous sensor dropouts | Land vehicle, inspect IMU vibration isolation damping grommets |
| `SENSOR_GYRO_FAULT` | Sensors | CRITICAL | Gyroscope Health Failure | Gyroscope axis failure, excessive noise, or SPI communication loss | Silicon MEMS sensor damage, loose ribbon cable, high vibration clipping | Replace flight controller or IMU module, inspect vibration damping |
| `SENSOR_ACCEL_FAULT` | Sensors | CRITICAL | Accelerometer Health Failure | Accelerometer clipping detected on multiple axes | Hard landing impact, motor vibration peak matching sensor resonant frequency | Balance propellers, check motor bearings, inspect soft mounting |
| `SENSOR_MAG_FAULT` | Sensors | ERROR | Magnetometer Health Failure | Magnetometer reading frozen or out of valid field range [0.2 - 0.7 Gauss] | Local magnetic saturation, disconnected I2C bus harness | Check wiring connector, demagnetize airframe parts, replace magnetometer |
| `SENSOR_BARO_FAULT` | Sensors | ERROR | Barometer Bus Failure | I2C/SPI bus read failure or flatlining pressure readings | Dirt/moisture on sensor diaphragm, static electrical discharge | Clean sensor chamber, verify bus pull-up resistors |
| `SENSOR_GPS_FAULT` | Sensors | ERROR | GPS Hardware Fault | UART communication lost with GNSS module or antenna disconnected | Cut wire harness, loose connector, GPS internal firmware crash | Check 5V power supply rail, reseat Molex/JST-GH connector |
| `SENSOR_DIFF_PRESS_FAULT` | Sensors | WARNING | Differential Pressure (Airspeed) Fault | Pitot tube blocked or airspeed reading disagrees with ground speed | Insect/water in pitot dynamic port, disconnected silicone tubing | Clear pitot tube with low pressure syringe, inspect tubing for kinks |
| `FAILSAFE_RTL` | Failsafe | WARNING | Return-To-Launch Triggered | Autopilot entered automated Return-To-Launch trajectory | RC loss, data link timeout, geofence boundary hit | Monitor RTL progression, verify clear airspace between current position and home |
| `FAILSAFE_LAND` | Failsafe | ERROR | Failsafe Autonomous Land | Autopilot forced automated descent and landing | Critical battery exhaustion, non-recoverable navigation loss | Monitor landing spot, abort if obstacle is detected and manual link exists |
| `FAILSAFE_TERMINATION` | Failsafe | CRITICAL | Flight Termination Activated | Immediate motor shutdown and parachute/terminator fire | Extreme loss of attitude control, manual kill switch thrown | Retrieve aircraft, conduct full structural and electrical diagnostic |
| `FAILSAFE_HOLD` | Failsafe | NOTICE | Failsafe Position Hold / Loiter | Vehicle paused trajectory and entered stationary hover/orbit | Minor telemetry link drop, mission command queue pause | Restore GCS connection, verify link status |
| `FAILSAFE_DESCENT` | Failsafe | ERROR | Controlled Emergency Descent | Vehicle descending at fixed safe descent rate | Loss of position estimate while maintaining attitude stabilization | Ensure open ground below aircraft, maintain level wings |
| `BATT_WARNING_LOW` | Battery | WARNING | Battery Low Warning | Pack capacity fell below `BAT_LOW_THR` (typically 15-20%) | High flight duration, headwind increasing motor power draw | Return to home immediately, prepare for landing |
| `BATT_WARNING_CRITICAL` | Battery | ERROR | Battery Critical Warning | Pack capacity fell below `BAT_CRIT_THR` (typically 7-10%) | Exhausted capacity, battery sag under burst current | Immediate landing required to avoid uncontrolled descent |
| `BATT_WARNING_EMERGENCY` | Battery | CRITICAL | Battery Emergency Cutoff Warning | Voltage approaching cell collapse limit (< 3.2V per cell) | Severe cell exhaustion, high internal resistance | Land immediately at current coordinate |
| `BATT_CELL_IMBALANCE` | Battery | ERROR | Battery Cell Voltage Imbalance | Delta between highest and lowest cell voltage > 150 mV | Defective or aging LiPo cell, degraded internal resistance | Retire battery pack, inspect with balancing charger |
| `BATT_OVERTEMP` | Battery | ERROR | Battery Over-Temperature Detected | Battery pack temperature exceeded safe threshold (> 60°C) | Continuous high-C discharge, insufficient cooling airflow | Reduce throttle, land, allow pack to cool down before handling |
| `GEOFENCE_MAX_ALT_EXCEEDED` | Geofence | WARNING | Geofence Maximum Altitude Exceeded | Vehicle climbed past `GF_MAX_VER_DIST` | Commanded climb beyond airspace authorization, barometer drift | Throttle down, autopilot executes fence action |
| `GEOFENCE_MAX_DIST_EXCEEDED` | Geofence | WARNING | Geofence Maximum Distance Exceeded | Vehicle exceeded maximum radius `GF_MAX_HOR_DIST` from home | Flyaway risk, strong wind drift exceeding vehicle cruise speed | Trigger RTL or immediate loiter |
| `GEOFENCE_POLYGON_BREACH` | Geofence | ERROR | Geofence Polygon Boundary Breached | Vehicle crossed inclusion/exclusion geofence polygon vertex | Route planning error, control latency | Failsafe executes boundary recovery or RTL |
| `ACTUATOR_SATURATION` | Actuator | WARNING | Control Output Saturation | Motor mixer or servo output reached 100% saturation on one or more axes | Strong gust, payload center-of-gravity imbalance, motor degradation | Rebalance payload, land if high winds persist |
| `ACTUATOR_MOTOR_FAIL` | Actuator | CRITICAL | Motor Desync / Mechanical Failure | Autopilot detected uncommanded roll/pitch rate with saturated opposite motor | Burned stator winding, thrown propeller, ESC commutation failure | Emergency land multirotor; multi-engine planes fly single-engine recovery |
| `ACTUATOR_ESC_OFFLINE` | Actuator | ERROR | ESC Telemetry Link Lost | DShot / CAN bus telemetry from Electronic Speed Controller ceased | CAN transceiver bus fault, power drop to ESC | Land aircraft, inspect ESC CAN bus wiring and termination resistors |
| `ACTUATOR_SERVO_STALL` | Actuator | ERROR | Control Surface Servo Stall | Servo draw current excessive or position feedback stalled | Jammed flight control surface hinge, damaged gear train | Inspect control horns, hinges, and pushrods |

---

## 2. PX4 Detailed Fault Descriptions

### [ERR-PX4-EST-01] EST_FAULT_VEL_HORIZ
- **Subsystem**: Estimator (EKF2)
- **Severity**: ERROR
- **Description**: The Extended Kalman Filter rejected horizontal velocity measurements because the innovation test ratio exceeded `EKF2_VEL_GATE`.
- **Probable Causes**: GPS multi-path reflection near tall obstacles; sudden wind shear; optical flow sensor noise.
- **Recommended Actions**: Check satellite constellation health in uORB `vehicle_gps_position`; inspect optical flow surface texture; widen innovation gate if noise is expected.

### [ERR-PX4-EST-02] EST_FAULT_MAG_HEADING
- **Subsystem**: Estimator (EKF2)
- **Severity**: CRITICAL
- **Description**: Persistent disagreement between magnetic field vectors and gyroscopic rate integration causes yaw state divergence.
- **Probable Causes**: High DC currents passing close to the flight controller magnetometer; ferromagnetic screws or payloads; external power lines.
- **Recommended Actions**: Move power distribution boards away from magnetometer; perform full 3D compass calibration; calibrate current-based magnetic compensation.
