# Microservices Architecture & Distributed System Reference Manual

This technical specification documents error codes, telemetry event indicators, and failure modes across cloud-native microservices, database connection pools, RPC gateways, and service mesh proxies.

## 1. Subsystem Telemetry and Event Reference

| Subsys | Subsys Name | ECode | Error Code String | Severity | Title / Meaning | Probable Causes | Recommended Actions |
|---|---|---|---|---|---|---|---|
| 1 | DB_POOL | 0 | `HEALTHY` | NOMINAL | Database Connection Pool Healthy | Connection pool utilization within normal operating limits | Continuous monitoring; maintain connection pooling metrics |
| 1 | DB_POOL | 1 | `SATURATED` | WARNING | Database Connection Pool Saturated | High concurrent query load; slow queries holding connections; insufficient pool size | Scale read replicas; optimize query execution plans; increase max_connections |
| 1 | DB_POOL | 2 | `HIGH_WAIT_TIME` | WARNING | Database Acquisition Latency High | Connection checkout queue backing up; thread pool contention | Profile slow transactions; expand connection pool ceiling |
| 1 | DB_POOL | 3 | `POOL_EXHAUSTED` | CRITICAL | Database Connection Pool Exhausted | Connection leak in application code; long-running transactions; unindexed database queries | Audit unclosed connections; kill stalled queries; scale database pool |
| 1 | DB_POOL | 4 | `CONN_LEAK` | ERROR | Database Connection Leak Detected | Application handler failed to return connection to pool in finally block | Enable connection leak detection in HikariCP/c3p0; patch unclosed sessions |
| 1 | DB_POOL | 5 | `TIMEOUT` | ERROR | Database Connection Acquisition Timeout | Pool completely allocated; downstream database server unresponsive | Verify database reachability; check database CPU and memory metrics |
| 2 | HTTP_STATUS | 200 | `OK` | NOMINAL | HTTP Request Succeeded | Normal REST/gRPC operation completed successfully | Normal operation |
| 2 | HTTP_STATUS | 400 | `BAD_REQUEST` | WARNING | HTTP Bad Request | Invalid request payload; missing required headers; schema validation error | Inspect client payload; validate serialization contract |
| 2 | HTTP_STATUS | 401 | `UNAUTHORIZED` | WARNING | HTTP Unauthorized | Expired JWT token; missing Bearer authentication header; signature mismatch | Refresh authentication token; verify OAuth2/OIDC provider |
| 2 | HTTP_STATUS | 403 | `FORBIDDEN` | WARNING | HTTP Forbidden | Client authenticated but lacks required RBAC role or ACL permissions | Verify service role assignments; inspect IAM permission boundaries |
| 2 | HTTP_STATUS | 404 | `NOT_FOUND` | WARNING | HTTP Not Found | Requested endpoint or entity resource does not exist | Check routing configuration; verify resource ID in request path |
| 2 | HTTP_STATUS | 429 | `TOO_MANY_REQUESTS` | WARNING | HTTP Too Many Requests | Client exceeded configured rate limit or token bucket quota | Back off request rate; inspect client retry loops; increase rate quota |
| 2 | HTTP_STATUS | 500 | `INTERNAL_SERVER_ERROR` | ERROR | HTTP Internal Server Error | Unhandled exception in business logic; null pointer reference; crash | Inspect application stack trace; deploy bug fix; check dependency health |
| 2 | HTTP_STATUS | 502 | `BAD_GATEWAY` | ERROR | HTTP Bad Gateway | Upstream service dropped TCP connection; malformed HTTP header received | Verify upstream service process; check reverse proxy buffering settings |
| 2 | HTTP_STATUS | 503 | `SERVICE_UNAVAILABLE` | CRITICAL | HTTP Service Unavailable | Target service pod crashing; memory limit exceeded (OOMKilled); draining | Scale replica deployment; inspect Kubernetes pod status; restart failing pods |
| 2 | HTTP_STATUS | 504 | `GATEWAY_TIMEOUT` | CRITICAL | HTTP Gateway Timeout | Downstream dependency exceeded timeout threshold; network partition | Check downstream latency metrics; increase timeout or trip circuit breaker |
| 3 | SERVICE_MESH | 1 | `DOWNSTREAM_TIMEOUT` | ERROR | Downstream Microservice Timeout | Downstream service processing delay; network congestion; hung thread | Inspect distributed traces (Zipkin/Jaeger); activate fallback cache |
| 3 | SERVICE_MESH | 2 | `CIRCUIT_BREAKER_OPEN` | CRITICAL | Circuit Breaker Tripped | Error rate exceeded failure threshold in rolling time window | Fall back to degraded response; allow cooldown period; investigate root cause |
| 3 | SERVICE_MESH | 3 | `CONNECTION_REFUSED` | ERROR | Downstream Connection Refused | Target port closed; service process died; ingress envoy listener down | Restart target container; verify container port bindings and health checks |
| 3 | SERVICE_MESH | 4 | `DNS_RESOLUTION_FAILED` | ERROR | Service Discovery DNS Resolution Failed | CoreDNS pod overwhelmed; incorrect service name or namespace | Check CoreDNS cluster health; verify Kubernetes service FQDN |
| 3 | SERVICE_MESH | 5 | `TLS_HANDSHAKE_FAILED` | ERROR | Mutual TLS Handshake Failure | Expired mTLS certificate; mismatched cipher suites; untrusted CA | Rotate mTLS certificates via cert-manager; verify Istio/Linkerd CA trust bundle |

---

## 2. Diagnostic Context and Recovery Workflows

### [POOL_EXHAUSTED] Database Connection Pool Exhaustion
- **Subsystem**: DB_POOL
- **Severity**: CRITICAL
- **Description**: The database connection pool has reached 100% saturation and incoming requests are blocked waiting for an available connection.
- **Probable Causes**: Unindexed queries executing full table scans; connection leak where database connections are not returned to pool; sudden traffic spike overwhelming provisioned pool size.
- **Recommended Actions**: Kill long-running database queries; increase maximum pool size; inspect connection pool leak logs; configure aggressive acquisition timeouts to fail fast.

### [SATURATED] Database Connection Pool Saturated
- **Subsystem**: DB_POOL
- **Severity**: WARNING
- **Description**: Connection pool usage has crossed the 80% watermark, indicating high load.
- **Probable Causes**: Burst in request volume; downstream database read IOPS throttling.
- **Recommended Actions**: Enable read-replica routing; review slow query log; scale application pods horizontally.

### [GATEWAY_TIMEOUT] HTTP 504 Gateway Timeout
- **Subsystem**: HTTP_STATUS
- **Severity**: CRITICAL
- **Description**: An edge gateway or intermediary proxy timed out waiting for a response from the upstream microservice.
- **Probable Causes**: Cascading failure caused by database pool exhaustion; blocking network call in upstream request handler; network partition between availability zones.
- **Recommended Actions**: Isolate slow downstream microservices; verify circuit breaker thresholds; route traffic away from failing zone; return cached fallback.

### [SERVICE_UNAVAILABLE] HTTP 503 Service Unavailable
- **Subsystem**: HTTP_STATUS
- **Severity**: CRITICAL
- **Description**: The microservice is currently unable to handle the request due to temporary overloading or maintenance.
- **Probable Causes**: All worker threads saturated; Kubernetes liveness/readiness probe failing; pod in CrashLoopBackOff.
- **Recommended Actions**: Autoscale deployment replicas; inspect container memory/CPU limits; review recent canary deployments.

### [CIRCUIT_BREAKER_OPEN] Circuit Breaker Tripped
- **Subsystem**: SERVICE_MESH
- **Severity**: CRITICAL
- **Description**: The circuit breaker has entered the OPEN state after consecutive call failures exceeded the error threshold.
- **Probable Causes**: Downstream microservice outage; persistent connection timeouts; network link failure.
- **Recommended Actions**: Route requests to static fallbacks; verify downstream service recovery before transition to HALF-OPEN.
