# Stage 5 Dispatch Round-Trip and Adaptive Worker Report

Date: 2026-07-18

Branch: `optimize/stage5-dispatch-roundtrips`

Baseline: `9b0d60e`

## Decision

The next useful optimization was dispatch overhead, not another increase in dedicated Worker counts. The implementation reduces PostgreSQL round trips, avoids empty Memory tasks in deterministic rules mode, and lets a shared Worker fleet pull from all task streams with foreground weighting.

Functional acceptance passes. The 120-second capacity target for the full 100-user, 1000-request profile still does not pass.

## Changes

- Task claim, leaf completion, Inbox completion, Watermark advancement, blocked Task activation, and Outbox creation use PostgreSQL CTE paths.
- Task completion now verifies that the Task and Inbox share the same tenant and internal space.
- Rules-mode notes with no Memory candidate finish the Memory barrier inline and do not create a Memory Task.
- Query, Summary, and Ingest handlers rely on dispatch-time Watermark gating instead of repeating Watermark reads.
- Memory extraction state uses an atomic upsert; Memory Note loading uses one lightweight SQL query while retaining title, tags, type, and summary for model extraction.
- Redis consumer groups are cached, pipelined across streams, and rebuilt after a Redis stream/group reset.
- Twelve adaptive Workers prioritize `ingest`, `query`, `summary`, and `memory`, while periodically servicing `delivery` and `enrichment`.
- Idle adaptive Workers back off from 20 ms to 250 ms to reduce empty Redis polling.
- Lease renewal remains at one third of the lease period so one transient database failure does not immediately risk ownership expiry.

## Measured Results

### Ordinary Ingest Microbenchmark

| Metric | Before | After | Change |
|---|---:|---:|---:|
| Tasks | 3 | 2 | -33.3% |
| SQL statements | 105 | 40 | -61.9% |

The removed Task is the empty rules-mode Memory Task. Preference, task-state, correction, and other real Memory candidates still use the asynchronous Memory Worker and causal barrier.

### 100 Independent Spaces

| Metric | Earlier Stage 5 topology | Final topology | Change |
|---|---:|---:|---:|
| Queue wait p95 | 59,570 ms | 49,848 ms | -16.3% |
| End-to-end p95 | 70,131 ms | 60,823 ms | -13.3% |
| Completed | 100/100 | 100/100 | no loss |
| PostgreSQL connections | 35 | 34 | within 40 |

### 100 Users, 1000 Submissions

The corpus contains six intentional duplicate message IDs, so the expected unique Inbox count is 994.

- First submission: 866 client-confirmed accepts, 134 timeout/503 responses, 298.9 seconds.
- Idempotent replay: 34 new accepts, 966 duplicate confirmations, no failures, 28.4 seconds.
- Durable final state: 994 Inbox rows and 1,988 Tasks completed.
- Final pending, dead-letter, retry, defer, Memory gap, Stream lag, and Stream pending counts: zero.
- Reconciled result: 900 client-confirmed accepts, 94 first-pass timeouts already durable, six intrinsic duplicates, zero unaccepted unique requests.

Compared with the Stage 2 profile:

| Metric | Stage 2 | Stage 5 | Change |
|---|---:|---:|---:|
| Unique completed | 988/994 | 994/994 | +0.6 percentage points |
| Total Tasks | 2,656 | 1,988 | -25.2% |
| Queue wait p95 | 813,221 ms | 515,915 ms | -36.6% |
| End-to-end p95 | 815,361 ms | 524,545 ms | -35.7% |
| Worker execution p95 | 4,949 ms | 13,619 ms | +175.2% |
| Outbox publish p95 | 2,802 ms | 3,308 ms | +18.1% |

The queue improvement is real, but slow handler execution and Receiver database contention are now dominant. End-to-end p95 is 524.5 seconds, or 4.37 times the 120-second target.

## Verification

- Ruff: passed.
- Full regression: 263 passed, five third-party deprecation warnings.
- Final targeted PostgreSQL/Redis and classification-context regression: 15 passed.
- One intermediate full run hit a transient default-pool checkout timeout in the 12-thread Space creation test; the isolated test and final full rerun passed.
- Alembic current/head: `20260718_0006`.
- Stage 5 test processes after validation: zero.
- Capacity test tenant and its isolated Redis namespace were deleted. No container, Docker socket, or volume operation was used.

## Model and Cost Note

Capacity tests used `SUIXINJI_FAKE_EXTERNALS=true`, so LLM requests, tokens, and measured API cost are zero. Model routing and quality/cost claims cannot be inferred from this load test. The existing fast/balanced/deep role split remains the correct place to map GPT-5.4 mini, GPT-5.4, and GPT-5.5.

## Next Recommendation

The next stage should isolate Receiver admission connections from Worker pressure and bound slow handler concurrency separately from stream polling. It should target first-response latency and Worker execution p95, retain the 40-connection PostgreSQL budget, and rerun the same 100-user corpus before considering more Worker processes.
