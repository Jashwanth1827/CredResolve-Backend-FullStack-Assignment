# SmartDialer — System Architecture

This document describes the architectural design, state machines, safety boundaries, and failure recovery flows of the SmartDialer system.

---

## 1. System Architecture Diagram

```
+--------------------------------------------------------------------------+
|                            Campaign Manager                              |
|   (Coordinates campaign lifecycle, picks eligible borrowers, loops)      |
+--------------------------------------------------------------------------+
                                     │
                 ┌───────────────────┴───────────────────┐
                 │                                       │ (Progressive Mode)
                 ▼ (Predictive Mode)                     │
+----------------------------------+                     │
|     Predictive Pacing Engine     |                     │
|  - Rolling answer rate & stats   |                     │
|  - Desired outstanding calls     |                     │
|  - Bounded increment pacing      |                     │
+----------------------------------+                     │
                 │                                       │
                 ▼                                       ▼
+--------------------------------------------------------------------------+
|                             Safety Controller                            |
|  - HARD SAFETY CEILING ENFORCEMENT                                       |
|  - Validates actual real-time agent availability from Database           |
|  - Invariant: active_agent_bound_calls + approved <= available_agents   |
|  - Duplicate borrower call check (prevents double dial)                  |
|  - Provider health check (outage fallback / call throttling)             |
|  - NEVER BYPASSED BY PREDICTIVE ENGINE                                   |
+--------------------------------------------------------------------------+
                                     │ (Approved Safe Count N)
                                     ▼
+--------------------------------------------------------------------------+
|                              Call Allocator                              |
|  - Atomic agent reservation: UPDATE agents SET status='RESERVED'         |
|                             WHERE id=? AND status='AVAILABLE'            |
|  - Atomic borrower reservation: UPDATE borrowers SET status='RESERVED'   |
|                                 WHERE id=? AND status='PENDING'          |
|  - Call record creation: status='RESERVED', lease_expires_at=now()+60s   |
|  - Advance to INITIATED & DIALING upon provider acknowledgement         |
|  - Safe release / rollback on any failure                                |
+--------------------------------------------------------------------------+
                                     │
                                     ▼
+--------------------------------------------------------------------------+
|                     Mock Telecom Provider Interface                      |
|                  (Common TelecomProvider Protocol)                       |
|           ┌────────────────────────┴────────────────────────┐            |
|           ▼                                                 ▼            |
|   +-----------------------+                         +------------------+ |
|   |      Provider A       |                         |    Provider B    | |
|   |  - Fast, reliable     |                         |  - Latency / lag | |
|   |  - Ordered events     |                         |  - Duplicates    | |
|   |  - Low error rate     |                         |  - Out-of-order  | |
|   +-----------------------+                         +------------------+ |
+--------------------------------------------------------------------------+
                                     │
                             (Provider Events)
                                     ▼
+--------------------------------------------------------------------------+
|                              Event Processor                             |
|  - Idempotency Table: provider_events (event_id, provider_name) UNIQUE   |
|  - Discards duplicates before touching entity tables                     |
|  - State transition validation: rejects out-of-order & regressive events|
|  - Advances call & agent states safely                                   |
+--------------------------------------------------------------------------+
```

---

## 2. Agent State Machine

```
              ┌────────────┐
              │  OFFLINE   │◄─────────────────────────┐
              └─────┬──────┘                          │
                    │ login / activate                │
                    ▼                                 │
              ┌────────────┐                          │
     ┌───────►│ AVAILABLE  │◄────────────┐            │
     │        └─────┬──────┘             │            │
     │              │ reserve            │            │
     │              ▼                    │            │
     │        ┌────────────┐             │            │
     │        │  RESERVED  │             │            │
     │        └─────┬──────┘             │            │
     │              │ provider initiates │            │
     │              ▼                    │ failure/   │
     │        ┌────────────┐             │ cancel     │
     │        │  DIALING   │─────────────┤            │
     │        └─────┬──────┘             │            │
     │              │ customer answers   │            │
     │              ▼                    │            │
     │        ┌────────────┐             │            │
     │        │ CONNECTED  │             │            │
     │        └─────┬──────┘             │            │
     │              │ hangup             │            │
     │              ▼                    │            │
     │        ┌────────────┐             │            │
     │        │  WRAP_UP   │─────────────┴────────────┘
     │        └────────────┘
     │
     │ (Pause toggle)
     ▼
┌────────────┐
│   PAUSED   │
└────────────┘
```

### Transition Invariants
- An agent can only be transitioned from `AVAILABLE` to `RESERVED` through an atomic conditional SQL update:
  `UPDATE agents SET status='RESERVED' WHERE id=:id AND status='AVAILABLE'`.
- Reservation fails if rowcount == 0.
- If a call fails during setup, dialing, or ringing, the agent returns to `AVAILABLE` immediately.
- After a successful call, the agent moves through `WRAP_UP` before returning to `AVAILABLE` or `OFFLINE`.

---

## 3. Call State Machine

```
   ┌─────────┐
   │ QUEUED  │───────────────────┐
   └────┬────┘                   │
        │ reserve                │
        ▼                        │
   ┌──────────┐                  │
   │ RESERVED │────────────┐     │
   └────┬─────┘            │     │
        │ initiate         │     │
        ▼                  │     │
   ┌───────────┐           │     │
   │ INITIATED │─────┐     │     │
   └────┬──────┘     │     │     │
        │ ringing    │     │     │
        ▼            │     │     │
   ┌─────────┐       │     │     │
   │ RINGING │─┐     │     │     │
   └────┬────┘ │     │     │     │
        │ ans  │     │     │     │
        ▼      │     │     │     │
   ┌──────────┐│     │     │     │
   │ ANSWERED ││     │     │     │
   └────┬─────┘│     │     │     │
        │ conn │     │     │     │
        ▼      │     │     │     │
   ┌───────────┐     │     │     │
   │ CONNECTED │     │     │     │
   └────┬──────┘     │     │     │
        │ comp       │ fail│     │ cancel
        ▼            ▼     ▼     ▼
 ┌───────────┐      ┌─────────┐  ┌───────────┐
 │ COMPLETED │      │ FAILED  │  │ CANCELLED │
 └───────────┘      └─────────┘  └───────────┘
       ▲                  ▲            ▲
       └──────────────────┴────────────┘
                Terminal States
             (Cannot be regressed)
```

### Idempotency and Transition Protection
- **Terminal States**: `COMPLETED`, `FAILED`, and `CANCELLED`. Once a call reaches a terminal state, incoming events (e.g. late `RINGING`, replayed `INITIATED`, duplicate `FAILED`) are silently dropped.
- **Out-of-Order Rejection**: Transitions only move forward along the valid transition graph `CALL_VALID_TRANSITIONS`. For example, an `INITIATED` event arriving after a `RINGING` event is discarded as a regression.
- **Provider Event Deduplication**: Every event is checked against the `provider_events` table with a `(event_id, provider_name)` unique constraint. Replayed events are discarded on constraint violation before altering state.

---

## 4. Failure Recovery Flow

```
Worker Initiates Call
  │
  ├─ Allocates Agent & Call with lease_expires_at = now() + 60s
  │
  ▼
[Worker Process Crashes / Network Partitions]
  │
  ▼
Recovery Service Run (Periodic or Manual)
  │
  ├─ Query: SELECT * FROM calls
  │         WHERE status IN ('RESERVED', 'INITIATED')
  │         AND lease_expires_at < now()
  │
  ├─ For each stale call:
  │    1. UPDATE calls SET status='FAILED', failure_reason='Recovered: stale lease'
  │    2. UPDATE agents SET status='AVAILABLE', current_call_id=NULL WHERE id=agent_id
  │    3. UPDATE borrowers SET status='PENDING', retry_count=retry_count+1 WHERE id=borrower_id
  │
  └─ Commit transaction
```

### Agent Disappearance During Call Setup
If an agent is deleted or taken offline concurrently after reservation but prior to provider handoff:
1. `CampaignManager` verifies the agent entity exists and is in `RESERVED` status.
2. If the agent disappeared, `CallAllocator.fail_call_and_release` is invoked.
3. Call is marked `FAILED` with reason `"Agent disappeared during setup"`.
4. The borrower is returned to `PENDING` with retry increment so they are not lost.

---

## 5. Safety Controller Responsibilities

The Safety Controller is the primary invariant guardian of SmartDialer.

| Responsibility | Mechanism |
|---|---|
| **Zero Over-Allocation** | Enforces `agent_bound_active_calls + approved <= available_agents`. The predictive engine can request 100 calls, but if only 3 agents are free, the controller approves at most 3. |
| **Independent Verification** | Reads the database directly rather than trusting memory state or pacing engine assumptions. |
| **Duplicate Borrower Protection** | Verifies `Call.borrower_id` does not already have an active call (`RESERVED`, `INITIATED`, `RINGING`, `ANSWERED`, `CONNECTED`). |
| **Provider Health Protection** | Queries provider health status. If a provider reports an outage or degraded state, approves 0 calls and instructs pacing to fall back to progressive dialing. |
| **Explainable Decisions** | Every decision returns an explicit textual rationale (e.g. `Safety REDUCE: pacing requested 10 -> approved 2...`) recorded for auditability. |
### Note on Predictive Mode Allocation Architecture
In this prototype, predictive dialing mode uses full **predictive pacing calculations** (rolling answer rate estimation, desired outstanding calls formula, bounded increment dampening, and provider health evaluation) combined with **conservative agent-bound allocation**. All calls passing through the Safety Controller are allocated to available agents at initiation (`active_agent_bound_calls + approved <= available_agents`). This deliberately prevents abandoned calls (where a customer answers but no agent is free). Scaling to unallocated ringing calls would require an audio bridging / queuing server and abandon-rate enforcement within statutory limits.

---

## 6. Agent Utilization & Metrics Measurement

### Flaw with Instantaneous Snapshots
A common pitfall in auto-dialer simulations is computing agent utilization from a single snapshot at the end of the simulation:
$$\text{utilization}_{\text{snapshot}} = \frac{\text{busy\_agents}(t_{\text{end}})}{\text{total\_agents}}$$
Because campaigns finish when calls drain, an end-of-run snapshot frequently reports 0% or low utilization, ignoring the heavy duty cycle during the active dialing phase.

### SmartDialer's Time-Averaged Continuous Duty Cycle
SmartDialer employs an `AgentUtilizationTracker` that records agent busy occupancy at each dialing cycle:
$$\text{Duty Cycle Utilization} = \frac{\sum_{k=1}^M \text{busy\_agents}(t_k)}{M \times N_{\text{total\_agents}}} \times 100\%$$
Where:
- Busy states include `RESERVED`, `DIALING`, `CONNECTED`, and `WRAP_UP`.
- $M$ is the number of active observation cycles.
- $N_{\text{total\_agents}}$ is total agent capacity.

For continuous duration metrics, the system also supports talk-time utilization:
$$\text{Talk Time Utilization} = \frac{\sum \text{call.talk\_seconds}}{T_{\text{elapsed}} \times N_{\text{total\_agents}}} \times 100\%$$
This provides mathematically sound, reproducible utilization figures across simulation scenarios.
