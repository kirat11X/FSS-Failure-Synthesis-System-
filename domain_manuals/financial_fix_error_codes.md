# Financial High-Frequency Trading & FIX Protocol Reference Manual

This technical specification documents financial FIX protocol message rejects, order state transitions, market data book depth anomalies, and pre-trade risk engine violation codes.

## 1. Subsystem Telemetry and Event Reference

| Subsys | Subsys Name | ECode | Error Code String | Severity | Title / Meaning | Probable Causes | Recommended Actions |
|---|---|---|---|---|---|---|---|
| 1 | FIX_CXL_REJ | 0 | `TOO_LATE_TO_CANCEL` | WARNING | Too Late to Cancel Order | Order matched and filled on matching engine before cancel request processed | Treat cancel request as rejected; update client position with fill execution report |
| 1 | FIX_CXL_REJ | 1 | `UNKNOWN_ORDER` | ERROR | Unknown Order Identifier | OrigClOrdID not found in active order book; order expired or cancelled previously | Verify ClOrdID generation logic; inspect order store persistence cache |
| 1 | FIX_CXL_REJ | 2 | `BROKER_OPTION` | WARNING | Broker / Exchange Option Reject | Exchange regulatory restriction; matching engine in auction phase or market halt | Wait for continuous trading session; verify venue trading schedule |
| 1 | FIX_CXL_REJ | 3 | `ORDER_ALREADY_PENDING_CANCEL` | WARNING | Order Already Pending Cancel | Duplicate cancel request received before initial cancel acknowledged | Discard duplicate cancel; await 35=8 Execution Report confirming cancel |
| 1 | FIX_CXL_REJ | 4 | `UNABLE_TO_PROCESS_ORDER_MASS_CANCEL` | ERROR | Mass Cancel Request Rejected | Matching engine queue depth exceeded; invalid symbol/clearing firm scope | Retry mass cancel with granular instrument filters; inspect network pipe |
| 1 | FIX_CXL_REJ | 5 | `ORIG_ORD_MOD_TIME_MISMATCH` | ERROR | Original Modification Time Mismatch | Sequence of replace/cancel operations desynchronized with exchange book | Resynchronize order sequence numbers; update local order modification timestamp |
| 2 | FIX_ORD_STATUS | 0 | `NEW` | NOMINAL | Order Accepted on Book | Limit order placed in central limit order book (CLOB) at specified price level | Normal order lifecycle operation |
| 2 | FIX_ORD_STATUS | 1 | `PARTIALLY_FILLED` | NOTICE | Order Partially Executed | Partial quantity matched against resting counterparty orders | Await further fills or maintain resting remainder on order book |
| 2 | FIX_ORD_STATUS | 2 | `FILLED` | NOMINAL | Order Completely Executed | Full quantity executed across one or more execution fills | Complete trade settlement processing; update broker clearing position |
| 2 | FIX_ORD_STATUS | 4 | `CANCELLED` | NOTICE | Order Cancelled | Order removed from book per user request, IOC expiry, or day-end sweep | Verify cancelled leaves quantity; notify client trading desktop |
| 2 | FIX_ORD_STATUS | 8 | `REJECTED` | ERROR | Order Rejected by Venue or Risk | Pre-trade risk rule tripped; invalid tick size; price outside collar band | Inspect reject reason tag (Tag 103 / Tag 58); adjust order parameters |
| 2 | FIX_ORD_STATUS | 9 | `SUSPENDED` | WARNING | Order Suspended by Exchange | Volatility trading halt (LULD); security regulatory circuit breaker triggered | Monitor exchange dissemination channel for resumption auction |
| 3 | ORDER_BOOK | 1 | `BOOK_CROSSED` | CRITICAL | Crossed Market Depth Detected | Best bid price greater than or equal to best ask price (Bid >= Ask) | Halt automated market-making quoting; inspect multicast feed packet drops |
| 3 | ORDER_BOOK | 2 | `PRICE_COLLAR_EXCEEDED` | ERROR | Order Price Exceeds Exchange Collar | Aggressive marketable order priced beyond dynamic reference price band | Re-quote within reference band; inspect underlying index volatility |
| 3 | ORDER_BOOK | 3 | `DEPTH_DEPLETED` | WARNING | Liquidity Depth Depleted | Top-of-book depth dropped below minimum market making contractual tier | Replenish quotes on passive bid/ask levels; widen spread if volatility allows |
| 3 | ORDER_BOOK | 4 | `STALE_FEED` | ERROR | Market Data Feed Heartbeat Stale | Multicast UDP packet loss; ITCH/OUCH market data receiver socket buffer overrun | Fail over to secondary B-feed; trigger TCP historical replay to re-align book |
| 4 | RISK_GATEWAY | 1 | `CREDIT_LIMIT_EXCEEDED` | CRITICAL | Pre-Trade Buying Power Exceeded | Order notional value exceeds firm margin allocation or credit limit | Reject order internally; notify risk desk; request collateral credit increase |
| 4 | RISK_GATEWAY | 2 | `ORDER_RATE_LIMIT_EXCEEDED` | ERROR | Message Rate Throttle Exceeded | Algorithmic trading loop exceeded maximum messages-per-second limit | Throttle algorithmic quoting loops; implement exponential backoff |
| 4 | RISK_GATEWAY | 3 | `FAT_FINGER_REJECT` | CRITICAL | Max Order Clip Size Exceeded | Single order quantity exceeds maximum safety threshold (Fat-Finger check) | Intercept trade for manual compliance confirmation; verify sizing calculation |

---

## 2. Diagnostic Context and Recovery Workflows

### [TOO_LATE_TO_CANCEL] FIX Order Cancel Reject - Too Late to Cancel
- **Subsystem**: FIX_CXL_REJ
- **Severity**: WARNING
- **Description**: An order cancel request (MsgType 35=F) was rejected because the order had already been filled on the exchange matching engine.
- **Probable Causes**: Race condition between incoming market order fill and client cancel request; latency spike in cancel transmission.
- **Recommended Actions**: Reconcile order status against incoming Execution Report (35=8); update position and margin calculations with filled shares.

### [UNKNOWN_ORDER] FIX Order Cancel Reject - Unknown Order
- **Subsystem**: FIX_CXL_REJ
- **Severity**: ERROR
- **Description**: The exchange or gateway was unable to find the original order identified by Tag 41 OrigClOrdID.
- **Probable Causes**: Cancel sent before order acceptance confirmed; ClOrdID hash collision or typo; order already purged from active book.
- **Recommended Actions**: Check pending order store for unacknowledged orders; verify ClOrdID mapping and sequence integrity.

### [REJECTED] FIX Execution Report - Order Rejected
- **Subsystem**: FIX_ORD_STATUS
- **Severity**: ERROR
- **Description**: The new order single (35=D) was rejected by the exchange matching engine or pre-trade risk filter.
- **Probable Causes**: Credit limit breach; price non-compliant with tick size tables; trading halted in instrument.
- **Recommended Actions**: Inspect Text (Tag 58) and OrdRejReason (Tag 103); verify instrument trading state on exchange market data feed.

### [BOOK_CROSSED] Inverted or Crossed Market Depth Detected
- **Subsystem**: ORDER_BOOK
- **Severity**: CRITICAL
- **Description**: Market data feed indicates an anomalous inversion where the bid price exceeds or equals the ask price.
- **Probable Causes**: Packet drops on UDP multicast A-feed without B-feed synchronization; latency arbitrage during news release.
- **Recommended Actions**: Immediately pull quotes; trigger book snapshot rebuild via TCP recovery; verify feed timestamp ordering.
