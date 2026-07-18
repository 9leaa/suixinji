# Stage 4 Multi-Process Validation Report

## Result

Run `20260718-causal-05` used independent server Python processes and the existing PostgreSQL/Redis SSH reverse ports. No Docker command, Docker Socket, or `DOCKER_HOST` was used.

Causal ordering correctness passed. The run fully settled after worker startup resilience was fixed and the same durable tenant was resumed. The measured latency is not a clean capacity baseline because the run includes the diagnostic worker outage and full process-matrix restart.

## Implementation Under Test

- Only one root task per `space_id` is published; later root tasks remain `blocked` in PostgreSQL.
- Ingest completion activates a critical Memory extraction task.
- Critical Memory completion advances `memory_watermark` and releases exactly one next root task in one transaction.
- Terminal root or critical Memory failure records a memory gap and releases the next task.
- Enrichment runs on an independent Redis Stream and does not gate the watermark.
- Failure and defer counters are independent, so ordering waits do not consume the dead-letter budget.
- Worker startup and Redis metric collection retry transient Redis reverse-port timeouts.

## Workload

- 100 virtual users and 100 isolated spaces
- 1000 submissions: 685 ingest, 206 query, 64 summary, 45 memory commands
- Receiver x2, Outbox Relay x2, Ingest Worker x4
- Query x2, Summary x2, Critical Memory x8, Enrichment x2, Delivery x2
- Scheduler x2 in leader-lock-only test mode
- Fake Feishu, LLM, and embedding; token usage and cost were zero

## Submission

- Client-confirmed accepted: 977
- Durable PostgreSQL Inbox: 993
- Duplicate requests: 6
- Client timeouts later reconciled to durable Inbox: 16
- Unaccepted requests: 1
- Submission p50/p95/p99: 6022 / 8360 / 10010 ms
- Submission duration: 238057 ms

## Correctness

- Conservation equation passed with delta 0
- 993 root tasks completed; pending root tasks: 0
- 2669 total tasks completed; pending tasks: 0
- Failure count: 0
- Defer count: 0
- Dead-letter tasks: 0
- Memory gap spaces: 0
- Maximum final memory-watermark lag: 0
- Redis Stream lag: 0
- Redis Stream pending: 0
- Unpublished Outbox: 0
- Deliveries sent: 310
- Worker, Relay, and Scheduler restart scenarios executed
- Query and Delivery pause/resume scenarios executed
- The test tenant and its Redis namespace were deleted after metrics collection

## Preference Evolution Smoke Test

A dedicated three-message distributed smoke test sent, in order:

```text
I like drinking milk
I dislike drinking milk
What do I like drinking?
```

All Inbox messages and eight resulting tasks settled with zero failure/defer/gap. The final `memory_watermark` reached sequence 3. PostgreSQL Memory state contained:

- `User dislikes drinking milk`: `active`
- `User likes drinking milk`: `superseded`

This verifies that the query root cannot execute before both critical Memory evolutions are durable.

## Capacity Metrics

- Total latency p50/p95/p99: 474212 / 1021709 / 1112633 ms
- Queue wait p50/p95/p99: 471281 / 1018464 / 1108971 ms
- Execution p50/p95/p99: 3026 / 4265 / 5897 ms
- Lock wait p50/p95/p99: 72 / 625 / 1081 ms
- Failure rate: 0.001

These queue and total latency values include the startup timeout diagnosis and the full process-matrix restart. They prove durable recovery and eventual drain, but should not be treated as an uninterrupted capacity acceptance result.

## Baseline Comparison

The previous `20260718-basic-02` snapshot had 642 pending root tasks, 2035 retry/defer attempts, Redis lag 1189, and never settled. The causal run ended with zero pending work, zero defer, zero retry, and zero Redis lag.

The ordering storm is removed. Remaining capacity cost is now explicit critical Memory work plus PostgreSQL round trips over the SSH reverse port, rather than repeated execution of out-of-order tasks.

## Remaining Risk

A clean no-intervention capacity run is still needed before declaring a strict p95 capacity target. Submission p95 remains dominated by PostgreSQL round trips, and causal per-space execution intentionally trades same-space latency for correct read-after-memory behavior.
