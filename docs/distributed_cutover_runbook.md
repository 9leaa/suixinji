# Distributed Cutover Runbook

## Safety Rules

- Keep the Feishu receiver single-active until PostgreSQL migration verification passes.
- Do not start the `local-infra` Compose profile when `.env` points to external PostgreSQL or Redis.
- Run large load tests with `SUIXINJI_FAKE_EXTERNALS=true`; use real LLM and embedding only for a small sampled run.
- Stage 4 uses independent Python processes and the existing `DATABASE_URL`/`REDIS_URL`; it never invokes Docker or controls Docker Socket.

## Pre-Cutover

1. Run `make backup` and keep the generated local backup read-only.
2. Run `make migrate-dry-run`, `make migrate`, and `make verify-migration`.
3. Compare local and PostgreSQL counts, then sample note text, memory versions, summaries, subscriptions, and deliveries.
4. Run `python scripts/check_distributed_cutover.py`. Resolve every blocker.
5. Start a write-freeze window or run one final incremental migration.

## Configuration

```text
STORAGE_BACKEND=postgres
COORDINATION_BACKEND=redis
TASK_QUEUE_BACKEND=redis_streams
SUIXINJI_FAKE_EXTERNALS=false
```

`TASK_QUEUE_BACKEND` is the repository's concrete name for the queue switch described as `QUEUE_BACKEND` in the design document.

## Rollout

1. Start Outbox Relay and one worker for each task type.
2. Confirm consumer groups, PostgreSQL task state, and Redis Streams lag.
3. Start one Scheduler and verify leader lock acquisition.
4. Start the API test receiver and submit a small smoke workload.
5. Start the Feishu receiver last.
6. Scale workers gradually while watching p95 latency, failure rate, pending tasks, stream lag, retries, and dead letters.

## Rollback

1. Stop the Feishu receiver to prevent new distributed writes.
2. Stop Scheduler, workers, and Outbox Relay after recording pending task and Outbox counts.
3. Keep PostgreSQL and Redis intact for diagnosis; do not flush Redis or delete pending Streams entries.
4. Restore the previous application revision and switch the old runtime to read-only unless an explicitly tested reverse migration exists.
5. Export PostgreSQL notes and memories to Markdown/local backup before any destructive recovery action.

## Stage 4 Validation

```bash
make stage4-start
make stage4-validate-basic
make stage4-status
make stage4-stop
```

The Chaos command is a dry run by default. `--execute` restarts or pauses only Stage 4 Python processes. It never restarts PostgreSQL, Redis, or any container. After metrics are recorded, stop the process matrix and clean only the generated `load-*` tenant and `stage4-*` Redis namespace.
