# Stage 4 Multi-Process Validation Report

## Result

Run `20260718-basic-02` used 18 independent server Python processes and the existing PostgreSQL/Redis SSH reverse ports. No Docker command or Docker Socket was used.

The correctness checks passed, but the capacity acceptance failed because the workload did not settle and Redis timed out while the backlog was high.

## Workload

- 100 virtual users and 100 isolated spaces
- 1000 submissions: 685 ingest, 206 query, 64 summary, 45 memory commands
- Receiver x2, Outbox Relay x2, Ingest Worker x4
- Query, Summary, Memory, Delivery Worker x2 each
- Scheduler x2 in leader-lock-only test mode
- Fake Feishu, LLM, and embedding; token usage and cost were zero

## Submission

- Client-confirmed accepted: 988
- Durable PostgreSQL Inbox: 994
- Duplicate requests: 6
- Client timeouts later reconciled to durable Inbox: 6
- Unaccepted requests: 0
- Submission p50/p95/p99: 5888 / 7531 / 9614 ms
- Submission duration: 221842 ms

The six 10-second client timeouts were not lost requests. PostgreSQL showed that all six had committed, so they are recorded as accepted with an initially unknown client outcome.

## Correctness

- Conservation equation passed with delta 0
- 994 unique Inbox messages across exactly 100 spaces
- Space/user isolation mismatches: 0
- Processed notes checked: 223; duplicate notes: 0
- Tasks checked: 1572; duplicate idempotency keys: 0
- Deliveries checked: 131 sent; duplicate delivery keys: 0
- Dead-letter tasks at snapshot: 0
- Worker, Relay, and Scheduler kill/restart scenarios executed
- Query and Delivery pause/resume scenarios executed

The first attempt exposed a concurrent space-upsert race on `uq_spaces_source_identity`. The upsert now ignores conflicts across both valid unique identities, and a 24-call concurrent regression test covers it.

## Capacity Snapshot

- Root tasks: 352 completed, 642 pending
- All tasks: 787 completed, 781 pending
- Retry/defer attempts: 2035
- Stream lag: 1189
- Stream pending: 49
- Unpublished Outbox: 0
- Total latency p50/p95/p99: 421933 / 851851 / 887315 ms
- Queue wait p50/p95/p99: 559936 / 870930 / 940810 ms
- Execution p50/p95/p99: 5088 / 8181 / 10030 ms
- Lock wait p50/p95/p99: 119 / 3445 / 5335 ms

## Findings

1. PostgreSQL Inbox durability, idempotency, tenant isolation, and Outbox conservation held during process failures.
2. Separate task-type streams plus database sequence checks preserve correctness but create a defer/republish storm when messages from one space arrive concurrently out of order.
3. The SSH reverse-port environment has high database round-trip latency. Submission p95 was 7.5 seconds even with fake external services.
4. Redis's 2-second socket timeout is too small for this overloaded reverse-port path; the metrics waiter observed a read timeout.
5. This configuration is not ready for the 100-user/1000-request acceptance target despite having no data loss or dead letters at the snapshot.

Recommended follow-up: partition dispatch by `space_id` before task-type execution, avoid republishing sequence-blocked work, add explicit unknown-outcome reconciliation at the HTTP boundary, and rerun this exact workload after the ordering change.
