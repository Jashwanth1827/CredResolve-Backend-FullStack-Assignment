# SmartDialer

A complete progressive and predictive auto-dialer prototype built as a backend engineering demonstration.

## Overview

SmartDialer implements the core components of an outbound call-center dialing system:

- **Campaign Manager** — orchestrates dialing campaigns
- **Predictive Pacing Engine** — explainable rule-based algorithm that determines call volume
- **Safety Controller** — independent hard-limit enforcer (cannot be bypassed by pacing logic)
- **Call Allocator** — atomic agent and borrower reservation using conditional SQL UPDATEs
- **Mock Telecom Providers** — two implementations (reliable Provider A, unreliable Provider B)
- **Event Processor** — idempotent provider event handling with duplicate/out-of-order protection
- **Recovery Service** — reclaims stale leases from crashed workers

## Stack

- Python 3.12+
- FastAPI
- SQLite (WAL mode) + SQLAlchemy 2.0
- Pydantic v2
- pytest + pytest-asyncio

## Architecture & Design Decisions

SmartDialer separates throughput optimization from safety invariant enforcement:

```
Campaign Manager
    ↓
Predictive Pacing Engine  (Calculates desired call volume)
    ↓
Safety Controller         (INDEPENDENT HARD CEILING: never bypassed)
    ↓
Call Allocator            (Atomic conditional SQL reservation)
    ↓
Mock Telecom Providers    (Provider A: reliable / Provider B: lag, out-of-order)
    ↓
Event Processor           (Two-tier idempotency: unique table + state machine)
```

### Key Architectural Invariants
1. **Safety Controller Priority**: The predictive engine *never* dials a provider directly. Every call request must pass through the independent Safety Controller, which guarantees $\text{active\_calls} + \text{approved} \le \text{available\_agents}$.
2. **Atomic Agent Reservation**: Prevents race conditions among workers using conditional SQL:
   `UPDATE agents SET status='RESERVED' WHERE id=? AND status='AVAILABLE'`
   Reservation succeeds if and only if `rowcount == 1`.
3. **Two-Tier Idempotency**:
   - **Tier 1**: A durable `provider_events` table with unique constraint `(event_id, provider_name)` drops duplicate webhooks before domain mutations.
   - **Tier 2**: Forward-only state machine validation rejects regressive or out-of-order events on terminal states (`COMPLETED`, `FAILED`, `CANCELLED`).
4. **Predictive Pacing with Zero Abandoned Calls**: Pacing computes $\lceil \text{free\_capacity} / \text{answer\_rate}\rceil$ with bounded increments and progressive fallback, while the Safety Controller maintains strict agent-bound allocation.
5. **Worker Crash Recovery**: Lease expiration timestamps (`lease_expires_at`) allow `RecoveryService` to reclaim abandoned agents and requeue borrowers.

For in-depth architectural diagrams, state machines, and scaling blueprints:
- See [ARCHITECTURE.md](ARCHITECTURE.md) for full system diagrams, state transition graphs, and recovery flows.
- See [ARCHITECTURE_DECISIONS.md](ARCHITECTURE_DECISIONS.md) for SQLite vs PostgreSQL analysis, concurrency trade-offs, and 10,000-agent scaling architecture.

## Installation

```bash
git clone https://github.com/Jashwanth1827/CredResolve-Backend-FullStack-Assignment.git
cd CredResolve-Backend-FullStack-Assignment
python -m venv venv

# Windows
venv\Scripts\activate
# Linux/Mac
source venv/bin/activate

pip install -r requirements.txt
```

## Running the Application

```bash
uvicorn app.main:app --reload --port 8000
```

Open the interactive API docs at: http://localhost:8000/docs

## Running Tests

```bash
pytest -v
```

Expected output: all 53 tests pass. See [Test Results](#test-results) below.

## Running a Simulation

### Via CLI (Standalone Runner)

```bash
python run_simulation.py
```
This runs the full deterministic suite (Scenarios A, B, C, and D) with seed reproducibility and outputs:
- Calls initiated, answered, connected, completed, and failed
- Continuous time-averaged agent capacity utilization (%)
- Sample pacing decisions (desired outstanding calls, rolling answer rates)
- Sample safety decisions (capacity limits, outage rejections, throttles)

### Via API (server must be running)

```bash
# Scenario A: 20% answer rate, 120s talk time
curl -X POST http://localhost:8000/simulation/run \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Scenario A",
    "answer_rate": 0.20,
    "avg_talk_seconds": 120,
    "num_agents": 5,
    "num_borrowers": 20,
    "provider_name": "provider_a",
    "mode": "progressive",
    "random_seed": 42
  }'

# Scenario D: dynamic outage, agent capacity changes, answer rate shift
curl -X POST http://localhost:8000/simulation/run \
  -H "Content-Type: application/json" \
  -d '{
    "name": "Scenario D",
    "answer_rate": 0.35,
    "avg_talk_seconds": 120,
    "num_agents": 5,
    "num_borrowers": 25,
    "provider_name": "provider_b",
    "simulate_provider_outage": true,
    "outage_after_calls": 8,
    "mode": "predictive",
    "random_seed": 99
  }'
```

## API Examples

### Create an agent

```bash
curl -X POST http://localhost:8000/agents \
  -H "Content-Type: application/json" \
  -d '{"name": "Alice", "campaign_id": "<campaign_id>"}'
```

### Set agent available

```bash
curl -X POST http://localhost:8000/agents/<agent_id>/availability \
  -H "Content-Type: application/json" \
  -d '{"status": "AVAILABLE"}'
```

### Create a campaign

```bash
curl -X POST http://localhost:8000/campaigns \
  -H "Content-Type: application/json" \
  -d '{"name": "Q4 Collections", "provider_name": "provider_a", "seed_answer_rate": 0.3}'
```

### Activate a campaign

```bash
curl -X POST http://localhost:8000/campaigns/<campaign_id>/activate
```

### Add borrowers

```bash
curl -X POST http://localhost:8000/campaigns/<campaign_id>/borrowers \
  -H "Content-Type: application/json" \
  -d '{"name": "John Smith", "phone_number": "+15551234567"}'
```

### Progressive dial (1 call per available agent)

```bash
curl -X POST http://localhost:8000/campaigns/<campaign_id>/dial
```

### Predictive dial (pacing engine + safety controller)

```bash
curl -X POST http://localhost:8000/campaigns/<campaign_id>/predictive-dial
```

### Submit a provider event

```bash
curl -X POST http://localhost:8000/provider/events \
  -H "Content-Type: application/json" \
  -d '{
    "event_id": "evt-001",
    "provider_name": "provider_a",
    "provider_call_id": "pA-abc123",
    "event_type": "ANSWERED",
    "metadata": {}
  }'
```

### Health check

```bash
curl http://localhost:8000/health
```

## Test Results

Run `pytest -v` to see full output (**53 passed across 8 test suites** in ~3s). Tests cover:

1. **Atomic Agent Reservation**: Conditional `UPDATE ... WHERE status='AVAILABLE'` guarantees exactly one winner (`rowcount == 1`).
2. **Two-Worker Concurrency**: True concurrent database sessions racing for the same agent.
3. **Progressive Dialing Capacity**: Guarantees $N$ available agents never initiate $> N$ calls.
4. **Predictive Pacing Calculation**: Desired outstanding formula, rolling answer rates, and bounded increments.
5. **Safety Controller Capacity Limits**: Approves, reduces, or rejects calls based strictly on real-time database agent capacity; provider health checks.
6. **Idempotency & Deduplication**: Unique `(event_id, provider_name)` constraint drops duplicate webhooks before database mutation.
7. **Forward-Only State Transitions**: Terminal states (`COMPLETED`, `FAILED`, `CANCELLED`) cannot regress; out-of-order `INITIATED`/`RINGING` rejected.
8. **Call & Agent Lifecycle Transitions**: `ANSWERED` -> `CONNECTED` -> `COMPLETED`/`WRAP_UP` and `ANSWERED` -> `FAILED`.
9. **Provider Parameterization & Outages**: Verifies providers honor configurable answer rates, talk times, and outage triggers.
10. **Worker Crash & Stale Lease Recovery**: Reclaims abandoned leases on worker failure, releasing agents and requeuing borrowers.
11. **Agent Disappearance During Setup**: Gracefully handles agent deletion or offline toggle before provider dispatch.
12. **Agent Utilization**: Mathematical tests for continuous time-averaged duty-cycle and talk-time utilization.

## Known Limitations

- **Single-writer SQLite**: SQLite's WAL mode supports concurrent reads but only one writer at a time. The `busy_timeout` pragma handles lock contention, but under very high write load, throughput is limited.
- **No distributed coordination**: The atomic reservation guarantee only holds within a single SQLite database file. Distributing across multiple database servers requires PostgreSQL with `SELECT ... FOR UPDATE SKIP LOCKED`.
- **In-memory provider event bus**: The `dispatch_event` function uses an in-process list of handlers. In production, this must be a durable message queue (Kafka, SQS, etc.).
- **No authentication**: API endpoints have no authentication or authorization.
- **Simulation is sequential**: The simulation endpoint runs synchronously in the request handler. For long simulations, this should be a background task.
- **No real telephony integration**: Providers are mocks. Real integration would use Twilio, Plivo, or similar.
