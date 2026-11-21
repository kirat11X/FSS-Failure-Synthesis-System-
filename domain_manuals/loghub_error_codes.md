# Distributed Systems & Loghub Architecture Reference Manual

This technical specification documents error codes, telemetry event indicators, and failure modes across distributed storage clusters (HDFS), supercomputer kernel nodes (BGL), consensus coordination clusters (Zookeeper), and web servers (Apache).

## 1. Subsystem Telemetry and Event Reference

| Subsys | Subsys Name | ECode | Error Code String | Severity | Title / Meaning | Probable Causes | Recommended Actions |
|---|---|---|---|---|---|---|---|
| 1 | HDFS | 1001 | `BLOCK_MISSING` | CRITICAL | HDFS DataNode Block Replica Missing | DataNode disk failure; packet responder timeout; corrupt block checksum | Re-replicate block from secondary replica; inspect DataNode disk health |
| 1 | HDFS | 1002 | `PACKET_RESPONDER_FAIL` | ERROR | DataNode Packet Responder Terminated | Network packet loss between client and DataNode; socket timeout; thread crash | Restart DataNode daemon; verify inter-node network MTU |
| 1 | HDFS | 1003 | `NAMENODE_SYNC_TIMEOUT` | WARNING | NameNode Metadata Synchronization Delay | High edit log append rate; slow journal node disks; RPC queue saturation | Scale JournalNode quorum; profile NameNode heap memory |
| 2 | ZOOKEEPER | 1010 | `LEADER_ELECTION_TIMEOUT` | CRITICAL | Zookeeper FastLeaderElection Notification Timeout | Quorum network partition; GC pause on quorum peer; thread starvation | Check peer network latency; tune JVM garbage collection flags; check quorum size |
| 2 | ZOOKEEPER | 1011 | `SEND_WORKER_LEAVING` | WARNING | Quorum Connection Manager Worker Thread Left | Peer connection closed by remote peer; socket reset; heartbeat expired | Re-establish quorum connection; verify firewall and keepalive settings |
| 2 | ZOOKEEPER | 1012 | `SESSION_EXPIRED` | ERROR | Client Ephemeral Session Expired | Client heartbeat timeout; network disconnection; high client-side CPU load | Reconnect client; re-register ephemeral znode watchers |
| 3 | BGL_KERNEL | 1020 | `CACHE_PARITY_ERROR` | WARNING | Instruction Cache Parity Error Corrected | Soft memory error due to cosmic rays; transient voltage fluctuation | Monitor single-bit parity rate; schedule hardware diagnostic if recurring |
| 3 | BGL_KERNEL | 1021 | `DDR_MACHINE_CHECK` | CRITICAL | Double-Data-Rate Memory Controller Machine Check | Uncorrectable multi-bit ECC error; physical RAM DIMM degradation | Drain node from scheduler queue; replace failing memory module |
| 3 | BGL_KERNEL | 1022 | `NETWORK_LINK_DROP` | ERROR | Torus Interconnect Network Link Dropped | Optical transceiver failure; loose fiber cable; router ASIC reset | Re-route MPI traffic through redundant torus link; inspect transceiver |
| 4 | APACHE_HTTPD | 1030 | `WORKER_ENV_ERROR` | ERROR | Apache mod_jk Child Worker Environment Error | Downstream Tomcat / Java backend worker hung; IPC shared memory corruption | Restart worker process; verify mod_jk worker configuration and thread limits |
| 4 | APACHE_HTTPD | 1031 | `SCOREBOARD_FULL` | WARNING | Apache Scoreboard Slot Saturation | Traffic surge exceeding MaxRequestWorkers; slow clients holding HTTP slots | Increase MaxRequestWorkers; enable mod_reqtimeout; configure keepalive timeouts |
| 5 | HTTP_STATUS | 504 | `GATEWAY_TIMEOUT` | CRITICAL | Distributed Gateway Timeout | Downstream node failed to respond within RPC deadline | Inspect downstream node load; trip circuit breaker |
| 5 | HTTP_STATUS | 503 | `SERVICE_UNAVAILABLE` | CRITICAL | Distributed Service Unavailable | Quorum lost; majority of peers offline | Restore quorum quorum nodes; restart failing instances |

---

## 2. Diagnostic Context and Recovery Workflows

### [BLOCK_MISSING] HDFS DataNode Block Missing
- **Subsystem**: HDFS
- **Severity**: CRITICAL
- **Description**: A requested HDFS block cannot be found on the designated DataNode, risking data loss if replication factor is 1.
- **Probable Causes**: Physical disk corruption; DataNode daemon process crashed; network partition preventing NameNode heartbeat.
- **Recommended Actions**: Trigger `hdfs fsck` to identify under-replicated blocks; initiate manual block re-replication; replace faulty disk drive.

### [LEADER_ELECTION_TIMEOUT] Zookeeper FastLeaderElection Notification Timeout
- **Subsystem**: ZOOKEEPER
- **Severity**: CRITICAL
- **Description**: FastLeaderElection notification timed out before quorum convergence, triggering repeat voting round.
- **Probable Causes**: Network latency between quorum peers exceeded tickTime; severe JVM stop-the-world garbage collection pause on peer.
- **Recommended Actions**: Check inter-peer ping latency; inspect GC logs for pauses > 1000ms; verify electionPort reachability on all ensemble servers.

### [CACHE_PARITY_ERROR] BGL Instruction Cache Parity Error Corrected
- **Subsystem**: BGL_KERNEL
- **Severity**: WARNING
- **Description**: L1/L2 instruction cache hardware detected a single-bit parity error and successfully corrected it via cache line invalidation and reload.
- **Probable Causes**: Ambient radiation/cosmic ray particle flip; voltage drop on processor core; aging CPU cache SRAM cells.
- **Recommended Actions**: Log occurrence in hardware maintenance database; if frequency exceeds 5 events per hour on the same node, take node offline for core diagnostics.

### [WORKER_ENV_ERROR] Apache mod_jk Worker Environment Error
- **Subsystem**: APACHE_HTTPD
- **Severity**: ERROR
- **Description**: Apache child worker environment entered an invalid state (error state 6), dropping incoming HTTP requests.
- **Probable Causes**: Backend application server closed AJP connection unexpectedly; shared memory segment corrupted; file descriptor exhaustion.
- **Recommended Actions**: Verify backend application health; check Apache error_log for segmentation faults; restart httpd daemon and verify ulimit -n.
