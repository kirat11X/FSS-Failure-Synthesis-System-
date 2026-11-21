# Autonomous Vehicle CAN Bus & ECU Diagnostics Reference Manual

This technical specification documents automotive CAN bus telemetry codes, Electronic Control Unit (ECU) diagnostic trouble indicators, Anti-Lock Braking System (ABS) states, and dashboard warning indicators.

## 1. Subsystem Telemetry and Event Reference

| Subsys | Subsys Name | ECode | Error Code String | Severity | Title / Meaning | Probable Causes | Recommended Actions |
|---|---|---|---|---|---|---|---|
| 1 | ECU_POWERTRAIN | 0 | `NO_FAULT` | NOMINAL | Electronic Control Unit Nominal | Controller operating within nominal voltage and communication parameters | Normal vehicle operation |
| 1 | ECU_POWERTRAIN | 1 | `SENSOR_COMM_ERROR` | ERROR | Sensor CAN Communication Error | CAN bus arbitration loss; wiring harness pinched; terminal resistor missing (120 Ohm) | Inspect CAN_H and CAN_L differential voltage; verify bus termination resistance; check harness |
| 1 | ECU_POWERTRAIN | 2 | `ACTUATOR_FAULT` | CRITICAL | Drive-by-Wire Actuator Fault | Throttle actuator motor short; steering torque feedback deviation; valve stuck | Isolate actuator power stage; inspect feedback encoder; disengage autonomous pilot mode |
| 1 | ECU_POWERTRAIN | 3 | `PLAUSIBILITY_CHECK_FAILED` | CRITICAL | Sensor Data Plausibility Failure | Dual redundant sensor discordance; wheel speed mismatch during straight driving | Compare sensor cross-check channels; calibrate wheel speed and steering angle sensors |
| 1 | ECU_POWERTRAIN | 4 | `CAN_BUS_OFF` | CRITICAL | CAN Controller Bus-Off State | Transmit Error Counter (TEC) > 255 due to continuous frame collision or short circuit | Check CAN transceiver ground reference; clear CAN controller error latch; verify bit timing |
| 1 | ECU_POWERTRAIN | 5 | `CAN_RX_OVERFLOW` | WARNING | CAN Message Receive Buffer Overflow | High bus utilization (>85%); higher-priority CAN IDs starving message mailboxes | Increase CAN baud rate or optimize CAN ID priority allocation; review message periodicity |
| 2 | BRAKING_ABS | 0 | `NORMAL` | NOMINAL | ABS System Standby / Normal | Anti-lock braking system primed and monitoring wheel speed channels | Normal braking operation |
| 2 | BRAKING_ABS | 1 | `ENGAGED` | NOTICE | ABS Pressure Modulation Active | Rapid wheel deceleration detected; low tire-road friction coefficient | Normal safety intervention; monitor vehicle yaw stability and slip ratio |
| 2 | BRAKING_ABS | 2 | `DISABLED` | ERROR | ABS System Safety Disabled | Wheel speed sensor fault; ECU communication dropped; low brake fluid pressure | Transition vehicle to safe stop; illuminate dashboard ABS warning lamp; inspect wheel sensors |
| 2 | BRAKING_ABS | 3 | `FAULT` | CRITICAL | ABS Modulator Valve Coil Fault | Hydraulic solenoid valve coil open circuit; pump motor relay stuck; internal ECU driver failure | Test solenoid coil resistance with multimeter; replace ABS hydraulic control unit (HCU) |
| 2 | BRAKING_ABS | 4 | `WHEEL_SLIP_HIGH` | WARNING | Excessive Wheel Slip Detected | Hydroplaning on wet surface; aggressive acceleration on icy road; tire tread wear | Reduce commanded drive torque; verify tire inflation pressure and tread depth |
| 3 | WHEEL_SPEED | 1 | `WHEEL_SPEED_PLAUSIBILITY` | ERROR | Axle Wheel Speed Mismatch | Speed disparity between left and right wheels exceeds differential model | Inspect tone ring for missing teeth; inspect tire rolling circumference |
| 3 | WHEEL_SPEED | 2 | `WHEEL_SPEED_SENSOR_FAIL` | CRITICAL | Wheel Speed Sensor Electrical Failure | Magnetic pickup sensor air gap out of spec; severed sensor cable harness | Measure sensor air gap (0.5 - 1.2 mm); check Hall effect sensor signal on oscilloscope |
| 3 | WHEEL_SPEED | 3 | `WHEEL_OVERSPEED` | WARNING | Wheel Speed Above Safe Limit | Vehicle descending steep grade; wheel spin during low traction condition | Apply regenerative/service braking; modulate traction control torque request |
| 4 | DASHBOARD | 1 | `DASH_WARN_BRAKE` | CRITICAL | Brake System Warning Lamp Illuminated | Low brake fluid level; parking brake switch engaged; ABS disabled | Check brake fluid reservoir level; inspect brake pad thickness and wear sensors |
| 4 | DASHBOARD | 2 | `DASH_WARN_CHECK_ENGINE` | ERROR | Engine / Powertrain MIL Lamp Illuminated | Emission threshold exceeded; misfire detected; powertrain ECU fault code stored | Connect OBD-II / UDS diagnostic scanner; read freeze frame data; resolve active DTCs |
| 4 | DASHBOARD | 3 | `DASH_WARN_STEERING` | CRITICAL | Electronic Power Steering (EPS) Lamp | Steering torque sensor offset corrupted; power steering assist motor current limit | Recalibrate steering center point; inspect 12V auxiliary power rail and assist fuse |

---

## 2. Diagnostic Context and Recovery Workflows

### [SENSOR_COMM_ERROR] Sensor CAN Communication Error
- **Subsystem**: ECU_POWERTRAIN
- **Severity**: ERROR
- **Description**: Cyclic CAN telemetry frames from sensor module missing for more than 50ms.
- **Probable Causes**: Loose connector on automotive harness; CAN transceiver ESD damage; electromagnetic interference from ignition system.
- **Recommended Actions**: Check CAN bus termination (60 Ohm across H-L); inspect harness continuity; verify shielded twisted-pair grounding.

### [ACTUATOR_FAULT] Drive-by-Wire Actuator Fault
- **Subsystem**: ECU_POWERTRAIN
- **Severity**: CRITICAL
- **Description**: Actuator position feedback does not match demanded setpoint within allowed tolerance window.
- **Probable Causes**: Mechanical binding in throttle linkage or steering rack; motor driver H-bridge thermal shutdown.
- **Recommended Actions**: Disengage autonomous pilot; safely pull vehicle to shoulder; perform actuator sweep test.

### [PLAUSIBILITY_CHECK_FAILED] Sensor Data Plausibility Failure
- **Subsystem**: ECU_POWERTRAIN
- **Severity**: CRITICAL
- **Description**: Cross-validation between independent sensor channels yielded impossible discrepancy.
- **Probable Causes**: Sensor calibration drift; damaged wheel speed tone ring; misaligned inertial measurement unit.
- **Recommended Actions**: Fall back to secondary sensor channels; execute sensor recalibration procedure; inspect physical mountings.

### [DISABLED] Anti-Lock Braking System (ABS) Disabled
- **Subsystem**: BRAKING_ABS
- **Severity**: ERROR
- **Description**: ABS safety supervisor disengaged automated pressure modulation due to upstream sensor or hardware failure.
- **Probable Causes**: Loss of reliable wheel speed input; hydraulic pump motor supply fault; ECU watchdog timeout.
- **Recommended Actions**: Inform driver via dashboard lamp; limit autonomous top speed; service wheel speed sensors and brake controller.
