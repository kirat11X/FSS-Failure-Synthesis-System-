# Warehouse Automated Guided Vehicle (AGV) & Robotics Reference Manual

This technical specification documents error codes, navigation re-planning events, task dispatch states, and battery management diagnostics for warehouse automated guided vehicles (AGVs) and autonomous mobile robots (AMRs).

## 1. Subsystem Telemetry and Event Reference

| Subsys | Subsys Name | ECode | Error Code String | Severity | Title / Meaning | Probable Causes | Recommended Actions |
|---|---|---|---|---|---|---|---|
| 1 | NAV_PLANNER | 0 | `ROUTE_BLOCKED` | WARNING | Assigned Route Blocked | Pallet dropped in aisle corridor; stationary robot awaiting clearance; debris | Plan dynamic detour around obstruction; notify floor supervisor if aisle unpassable |
| 1 | NAV_PLANNER | 1 | `OBSTACLE_DETECTED` | WARNING | Dynamic Obstacle in Safety Bubble | Human worker crossing robot path; forklift entering intersection; loose packaging | Decelerate robot to full stop; sound acoustic warning chime; resume once corridor clear |
| 1 | NAV_PLANNER | 2 | `LOW_BATTERY` | WARNING | Low Battery State of Charge | Robot battery charge dropped below 20%; long mission consumption | Preempt low-priority task; reroute robot immediately to designated rapid charging dock |
| 1 | NAV_PLANNER | 3 | `TRAFFIC_CONGESTION` | WARNING | Multi-Robot Intersection Gridlock | Traffic reservation lock; multiple AMRs contending for single-lane bottleneck | Fleet manager reassigns waypoint priorities; back up secondary vehicle |
| 1 | NAV_PLANNER | 4 | `LOCALIZATION_LOST` | CRITICAL | SLAM / Floor Marker Localization Lost | Damaged floor QR matrix tag; optical camera soiled; specular laser reflection | Halt robot drive; perform stationary rotational laser scan; clean floor camera lens |
| 2 | TASK_DISPATCH | 0 | `ASSIGNED` | NOMINAL | Mission Task Assigned | Work order dispatched from Warehouse Management System (WMS) to robot agent | Robot initiates path computation toward source pick station |
| 2 | TASK_DISPATCH | 1 | `IN_PROGRESS` | NOMINAL | Task In Execution | Robot navigating along planned trajectory or transferring payload | Monitor mission progress and corridor transit telemetry |
| 2 | TASK_DISPATCH | 2 | `COMPLETED` | NOMINAL | Task Successfully Completed | Payload delivered and drop confirmation sensors verified | Release robot to idle pool or assign next queued pick task |
| 2 | TASK_DISPATCH | 3 | `ABORTED` | CRITICAL | Mission Task Aborted | Robot suffered hardware fault, critical battery trip, or manual emergency stop | Dispatch maintenance technician to inspect robot; reassign work order to peer AMR |
| 2 | TASK_DISPATCH | 4 | `DELAYED` | WARNING | Mission Task Delayed Past SLA | Path obstruction detour latency; elevator wait timeout; queue delay at pick bay | Escalate mission priority in fleet manager; analyze route bottleneck telemetry |
| 3 | BMS_BATTERY | 1 | `BATTERY_CRITICAL` | CRITICAL | Battery Voltage Critical Cutoff | Cell voltage collapsed below 3.0V (pack < 21.0V); high payload grade climb | Cut drive motor power to prevent cell degradation; dispatch manual retrieval tug |
| 3 | BMS_BATTERY | 2 | `BATTERY_OVERTEMP` | ERROR | Battery Pack Over-Temperature | Sustained high-current discharge; elevated ambient warehouse temperature (>40°C) | Park robot in cool staging zone; disable fast-charging until pack temperature < 35°C |
| 3 | BMS_BATTERY | 3 | `CELL_IMBALANCE` | WARNING | Lithium Battery Cell Imbalance | Internal resistance divergence between parallel cell groups exceeds 150mV | Run overnight balance charging cycle; replace degraded battery module |
| 3 | BMS_BATTERY | 4 | `CHARGING_DOCK_FAIL` | ERROR | Charging Contact Docking Failure | Mechanical alignment error with floor charging copper pads; oxidized contact shoes | Clean charging shoe contacts with isopropyl alcohol; calibrate fine docking laser |
| 4 | SAFETY_CHASSIS | 1 | `BUMPER_IMPACT` | CRITICAL | Safety Bumper Actuated | Physical contact detected on mechanical perimeter safety bumper | Immediate emergency stop; log obstacle coordinates; require manual operator reset |
| 4 | SAFETY_CHASSIS | 2 | `WHEEL_MOTOR_STALL` | ERROR | Drive Wheel Motor Stalled | Drive wheel high-centered on floor seam; shrink-wrap tangled in drive axle | Inspect drive wheels for debris fouling; check motor drive current limit |
| 4 | SAFETY_CHASSIS | 3 | `LIFT_MECHANISM_JAM` | ERROR | Shelf / Pallet Lift Mechanism Jammed | Pallet weight exceeds rated limit; lift scissor mechanism mechanical binding | Check lift hydraulic pressure; verify load dimensions and center of gravity |

---

## 2. Diagnostic Context and Recovery Workflows

### [ROUTE_BLOCKED] Assigned Route Blocked
- **Subsystem**: NAV_PLANNER
- **Severity**: WARNING
- **Description**: The robot's primary planned global path is impassable due to an observed blockage in the warehouse grid.
- **Probable Causes**: Pallet staged outside designated storage bay; failed robot stalled in transit corridor; construction barrier.
- **Recommended Actions**: Request alternative route from fleet traffic manager; verify aisle clear width > 1.2 meters; reroute around adjacent storage rack.

### [OBSTACLE_DETECTED] Dynamic Obstacle in Safety Bubble
- **Subsystem**: NAV_PLANNER
- **Severity**: WARNING
- **Description**: Safety-rated laser scanner detected an object breaching the dynamic slowdown or stop zone.
- **Probable Causes**: Pedestrian walking into aisle; oncoming manual pallet truck; moving conveyor debris.
- **Recommended Actions**: Automatic deceleration to full stop; wait for clearance; sound audible horn after 5 seconds of inactivity.

### [LOW_BATTERY] Low Battery State of Charge
- **Subsystem**: NAV_PLANNER
- **Severity**: WARNING
- **Description**: Battery state of charge has reached low watermark, requiring immediate charging station navigation.
- **Probable Causes**: High payload duty cycle; skipped scheduled opportunity charging window; cold storage operation.
- **Recommended Actions**: Discard lower-priority pending pick missions; plot shortest route to nearest free charging terminal.

### [DELAYED] Mission Task Delayed Past SLA
- **Subsystem**: TASK_DISPATCH
- **Severity**: WARNING
- **Description**: Elapsed task execution duration has exceeded the 95th percentile expected completion threshold.
- **Probable Causes**: Multiple navigation replans due to crowded aisles; slow manual loading at pick station.
- **Recommended Actions**: Inspect replan event telemetry; re-route peer robots away from congested aisles; rebalance fleet task dispatching.

### [BATTERY_CRITICAL] Battery Voltage Critical Cutoff
- **Subsystem**: BMS_BATTERY
- **Severity**: CRITICAL
- **Description**: Battery management system triggered low-voltage protection circuit to prevent permanent cell damage.
- **Probable Causes**: Failure to reach charger before depletion; defective cell group; prolonged idle without dock connection.
- **Recommended Actions**: Transport robot to maintenance station via manual pallet jack; plug in external manual balancing charger.
