# SmartDialer — Architecture Decisions & Trade-offs

This document details the critical architectural decisions, engineering trade-offs, concurrency models, and scaling pathways for SmartDialer.

---

## 1. Why SQLite Was Selected for the Prototype

### Context & Requirements
The goal of this assignment is a self-contained, easily runnable, zero-external-infrastructure backend prototype prioritizing correctness, explainability, atomic reservations, and lifecycle safety.

### Decision
SQLite with Write-Ahead Logging (`PRAGMA journal_mode=WAL`) and `PRAGMA busy_timeout=30000` was chosen.

### Advantages
1. **Zero External Dependencies**: Runs out of the box on any machine with Python 3.12 without requiring PostgreSQL, Docker, Redis, or cloud credentials.
2. **ACID Compliance**: Full support for transactions, rollbacks, unique constraints, and conditional DML statements.
3. **Single-Writer Concurrency Guarantee**: In SQLite, all writes pass through a single write lock. This makes the atomic conditional `UPDATE agents SET status='RESERVED' WHERE id=? AND status='AVAILABLE'` strictly serialized by the database engine itself, eliminating phantom updates across local threads.
4. **Fast In-Memory Testing**: In-memory SQLite with `StaticPool` allows running the full test suite in under a second with complete test isolation.

### Explicit Caveats
- SQLite is **not** a distributed database. Its concurrency guarantees hold within a single file or process.
- Under high concurrent write contention, SQLite threads queue on `busy_timeout`. For horizontally distributed workers, an external RDBMS is mandatory (see Section 6).

---

## 2. How Atomic Reservation Works

### The Race Condition Problem
In call centers, multiple workers or pacing cycles concurrently search for free agents. A naive read-then-write pattern:
```python
# BROKEN / NAIVE PATTERN (TOCTOU race):
agent = db.query(Agent).filter_by(status='AVAILABLE').first()
if agent:
    agent.status = 'RESERVED'
    db.commit()
```
Two workers executing concurrently will read the same available agent, both see `status == 'AVAILABLE'`, and both attempt to assign calls to that agent (over-allocation).

### The Atomic Reservation Solution
SmartDialer uses a single conditional SQL update:
```sql
UPDATE agents
SET status = 'RESERVED', updated_at = :now
WHERE id = :agent_id
  AND status = 'AVAILABLE';
```
- The conditional `WHERE status = 'AVAILABLE'` is evaluated **at the instant of write lock execution**.
- The database returns `rowcount`.
- **In-variant**: The reservation succeeds **if and only if `rowcount == 1`**.
- If two workers race for the same agent:
  - Worker A's update executes first, flips status to `RESERVED`, and receives `rowcount = 1`.
  - Worker B's update executes second. Because status is now `RESERVED`, the `WHERE` clause matches 0 rows, and Worker B receives `rowcount = 0`.
  - Worker B recognizes the contention, aborts the attempt, and queries for another available agent.

The borrower reservation uses the identical atomic pattern:
```sql
UPDATE borrowers
SET status = 'RESERVED', updated_at = :now
WHERE id = :borrower_id
  AND status = 'PENDING';
```
If either reservation fails, the transaction is rolled back and any locked resource is released.

---

## 3. How Idempotency Works

### Telecom Event Hazards
Telecom providers operate asynchronously and unpredictably:
1. **Replays & Duplicates**: Network retries cause the same event (e.g. `ANSWERED`) to be delivered multiple times.
2. **Out-of-Order Delivery**: UDP or multi-region routing can deliver `RINGING` after `ANSWERED`, or `INITIATED` after `RINGING`.
3. **Post-Terminal Replays**: A late `RINGING` or `FAILED` event can arrive after a call has already been marked `COMPLETED`.

### Two-Tier Idempotency Defense

#### Tier 1: Durable Event Deduplication Table
SmartDialer logs every incoming provider event in a dedicated `provider_events` table:
```sql
CREATE TABLE provider_events (
    id VARCHAR(36) PRIMARY KEY,
    event_id VARCHAR(200) NOT NULL,
    provider_name VARCHAR(50) NOT NULL,
    provider_call_id VARCHAR(200) NOT NULL,
    event_type VARCHAR(50) NOT NULL,
    processed_at DATETIME NOT NULL,
    CONSTRAINT uq_provider_event UNIQUE (event_id, provider_name)
);
```
Before updating any domain entity:
- SmartDialer attempts to insert the event record.
- If a duplicate event arrives with an existing `(event_id, provider_name)`, the unique constraint triggers an `IntegrityError`.
- The transaction rolls back, and the event is immediately discarded without touching the `calls` or `agents` tables.

#### Tier 2: Forward-Only State Machine Validation
If an event is new but arrived out of order:
- The system checks `CALL_VALID_TRANSITIONS`:
  ```python
  CALL_VALID_TRANSITIONS = {
      QUEUED:    {RESERVED, CANCELLED},
      RESERVED:  {INITIATED, CANCELLED, FAILED},
      INITIATED: {RINGING, FAILED},
      RINGING:   {ANSWERED, FAILED, CANCELLED},
      ANSWERED:  {CONNECTED, COMPLETED, FAILED},
      CONNECTED: {COMPLETED, FAILED},
      COMPLETED: set(),  # Terminal
      FAILED:    set(),  # Terminal
      CANCELLED: set(),  # Terminal
  }
  ```
- If the call is already in a terminal state (`COMPLETED`, `FAILED`, `CANCELLED`), the event is ignored.
- If the requested state transition is not in `valid_next`, the transition is rejected as a backward regression.

---

## 4. Why Predictive Pacing Is Separated from Safety

### Architectural Principle: Separation of Optimization and Invariant Enforcement
In high-throughput systems, optimization algorithms are inherently heuristic and subject to tuning, drift, and edge cases. In contrast, safety limits are hard invariants that must never be compromised.

```
+------------------------------------+
|      Predictive Pacing Engine      |
|  - Goal: MAXIMIZE AGENT THROUGHPUT |
|  - Rule-based heuristics           |
|  - Answer rate rolling estimates   |
|  - Asks: "How many calls SHOULD    |
|    we initiate in an ideal world?" |
+------------------------------------+
                  │ (Requested Count)
                  ▼
+------------------------------------+
|          Safety Controller         |
|  - Goal: HARD CAPACITY CEILING     |
|  - Zero abandoned calls invariant  |
|  - Provider health restrictions    |
|  - Prevents double borrower dials  |
|  - Asks: "How many calls CAN we    |
|    SAFELY initiate right now?"     |
+------------------------------------+
```

### Why This Matters
1. **Pacing Engine Bugs Cannot Cause Safety Violations**: If a calculation error, division by zero, or corrupted answer-rate statistic causes the pacing engine to request 500 calls when only 2 agents are free, the Safety Controller unconditionally clamps the request to 2 calls.
2. **Independent Testability**: The Safety Controller is tested against adversarial and degenerate inputs in isolation (`test_dialing.py`).
### Predictive Mode: Pacing Calculation with Conservative Agent-Bound Allocation
In this prototype implementation:
- **Pacing Engine**: Computes explainable rule-based predictive volume ($\text{desired} = \lceil\text{free\_capacity} / \text{answer\_rate}\rceil$, subtracting existing outstanding calls, with increment caps and progressive fallback).
- **Allocation & Safety**: Enforces hard agent-bound allocation ($\text{approved} \le \text{available\_agents} - \text{active\_calls}$). Calls are allocated an agent atomically at launch.
- **Design Trade-off**: This prevents abandoned calls entirely by never initiating unbound ringing calls that could answer without a free agent. In a scaled deployment with telephony media gateways (e.g. FreeSWITCH/Asterisk), unallocated ringing calls would be dialed with an automated audio queue holding answered calls for the next available agent within a 2-second SLA.

---

## 5. What Breaks When Scaling to 10,000 Agents

At 10,000 concurrent agents and typical dial ratios (2–4 calls per agent):
- Active concurrent calls: 20,000 to 40,000 calls.
- Event throughput: 5,000 to 15,000 provider webhooks per second.
- State checks: Tens of thousands of queries per second.

Here is what breaks in the prototype design:

1. **SQLite Database Locking**:
   - SQLite allows only one writer at a time.
   - 10,000 agents attempting atomic reservations and receiving thousands of provider events per second will result in perpetual `database is locked` (`busy_timeout`) errors.
2. **In-Memory Event Handlers**:
   - The current `dispatch_event` bus is in-process memory. If a server restarts, all pending events are lost.
3. **Table Scans for Agent / Borrower Selection**:
   - `SELECT id FROM agents WHERE campaign_id=? AND status='AVAILABLE' LIMIT 1` will experience lock contention and index thrashing.
4. **Connection Exhaustion**:
   - A single monolithic process cannot handle 15,000 webhook requests/sec alongside pacing loops.
5. **Abandoned Call Risk with Over-dialing**:
   - True predictive dialing with high over-dial ratios at 10,000 agents requires Erlang-C queueing math and audio bridging infrastructure to handle abandoned calls (where customer answers but all agents are busy) within legal compliance limits (e.g. FTC < 3% abandon rate).

---

## 6. How PostgreSQL and Multiple Workers Could Be Introduced

To transition SmartDialer to a production-grade distributed system supporting 10,000+ agents:

### 1. Database: PostgreSQL with Row-Level Locking
Replace SQLite with PostgreSQL:
```sql
-- Atomic agent checkout without lock contention:
WITH next_agent AS (
    SELECT id
    FROM agents
    WHERE campaign_id = :campaign_id
      AND status = 'AVAILABLE'
    ORDER BY last_call_ended_at ASC
    FOR UPDATE SKIP LOCKED
    LIMIT 1
)
UPDATE agents
SET status = 'RESERVED',
    updated_at = NOW(),
    lease_expires_at = NOW() + INTERVAL '60 seconds'
FROM next_agent
WHERE agents.id = next_agent.id
RETURNING agents.id;
```
- `FOR UPDATE SKIP LOCKED` allows hundreds of parallel workers to grab distinct agents simultaneously without blocking each other or causing deadlocks.
- The same query pattern applies to borrowers (`FOR UPDATE SKIP LOCKED`).

### 2. Message Queue & Event Streaming: Kafka / RabbitMQ
- Provider webhooks land on stateless API gateways and are immediately acknowledged (`HTTP 202`) and pushed to a Kafka topic partitioned by `call_id`.
- Dedicated consumer workers process events sequentially per call, guaranteeing that event order per call is preserved even under high load.

### 3. Distributed Leases & Recovery: Redis / PostgreSQL Leases
- Call and agent leases are backed by Redis with TTL or PostgreSQL timestamp leases.
- A background worker running every 10 seconds executes:
  ```sql
  UPDATE calls SET status='FAILED', failure_reason='Lease expired'
  WHERE status IN ('RESERVED', 'INITIATED') AND lease_expires_at < NOW();
  ```

### 4. Distributed Architecture Diagram

```
                 [Telecom Providers (Twilio, Plivo, Telnyx)]
                                    │
                                    ▼ (Webhook Events)
                    [Stateless FastAPI Webhook Ingestion]
                                    │
                                    ▼
                         [Kafka Topic: call-events]
                         (Partitioned by call_id)
                                    │
                                    ▼
               [Event Consumer Workers (Idempotent Processor)]
                                    │
           ┌────────────────────────┴────────────────────────┐
           ▼                                                 ▼
[PostgreSQL Primary + Replicas]                  [Redis Cache / Cluster]
  - FOR UPDATE SKIP LOCKED                         - Rolling campaign stats
  - Partitioned calls table                        - Real-time agent presence
  - Idempotent provider_events table
```
