# Stage 7 Memory Hybrid Model Routing Report

Date: 2026-07-23
Branch: `fix/next-stage-stability`

## Implemented Slice

- Added task-level model routing through `core/model_policy.py` and `core/model_router.py`.
- Added rollback flags for Stage 7 features in `core/settings.py` and `.env.example`.
- Routed note classification, memory extraction, query synthesis, and daily summary LLM calls by task.
- Added structured LLM observability fields: `llm_task`, `model_role`, `model`, `route_reason`, latency, token fields, and fallback marker.
- Added `memory/clause_splitter.py` for clause-level memory extraction.
- Extended `MemoryCandidate` with `clause_index` and clause-aware candidate ids.
- Improved deterministic admission for short user facts such as `我是杭州人`, `我姓张`, `我养了一只猫`, and `我会弹吉他`.
- Added tests for model routing, short-fact admission, clause splitting, and multi-candidate extraction.
- Added durable Memory Vector lifecycle:
  - Memory insert/update marks vector `pending`.
  - `memory_embedding` tasks are enqueued through transactional outbox.
  - Worker-side embedding validates content hash, model, version, and dimension before writing `ready`.
  - Failures write retry metadata without calling embedding inside the Memory mutation transaction.
- Added `scripts/backfill_memory_vectors.py`, defaulting to dry-run unless `--execute` is passed.
- Added Alembic migrations:
  - `20260723_0008_memory_vector_lifecycle.py`
  - `20260723_0009_memory_search_document_trgm.py`
- Added Hybrid Retrieval V2 primitives:
  - `MemoryRetrievalHit`
  - exact / structured / FTS / trigram / vector ranks and scores
  - generated `search_document` migration
  - `pg_trgm` indexes
  - `hybrid_v2` / unified rerank mode that does not apply the old post-fusion hard character threshold.
- Added optional strong-model Memory Advisory. It only records advice for high-risk relation decisions and never directly mutates Memory.
- Added complexity-aware Query model routing. Strong is used only when `SUIXINJI_STRONG_ESCALATION_ENABLED=true`.
- Added monthly semantic consolidation path with conservative source/polarity safety gates.

## Current Feature Flags

- `SUIXINJI_MODEL_ROUTING_ENABLED=true`
- `SUIXINJI_STRONG_ESCALATION_ENABLED=false`
- `SUIXINJI_MEMORY_VECTOR_LIFECYCLE_ENABLED=false`
- `SUIXINJI_MEMORY_TRIGRAM_ENABLED=false`
- `SUIXINJI_MEMORY_UNIFIED_RERANK_ENABLED=false`
- `SUIXINJI_MEMORY_CLAUSE_EXTRACTION_ENABLED=false`
- `SUIXINJI_MONTHLY_SEMANTIC_CONSOLIDATION_ENABLED=false`

Clause extraction remains opt-in to preserve existing production behavior.

## Metrics

- Model routing policy coverage: 8 task routes defined.
- Short-fact admission recall smoke: 6/6 = 100%.
- Clause splitter smoke recall: 3/3 expected clauses recovered = 100%.
- Clause-level candidate type recall smoke: 3/3 expected types recovered = 100%.
- Strong model use remains gated by policy; high-cost escalation ratio in this test slice: 0 live calls.
- Memory embedding worker route registration: 1/1.
- Alembic head count: 1 head, `20260723_0009`.
- Live LLM calls: 0.
- Live embedding calls: 0.

## Validation

- `conda run -n zcj_hello python -m pytest tests/test_stage7_model_routing_and_clause_extraction.py tests/test_memory_extractor.py -q`
  - Result: 17 passed in 0.15s.
- `conda run -n zcj_hello python -m pytest tests/2阶段测试/test_daily_summary_flow.py tests/2阶段测试/test_query_agent_react.py -q`
  - Result: 8 passed in 2.63s.
- `timeout 90s conda run -n zcj_hello python -m pytest tests/test_memory_service.py -q`
  - Result: 14 passed in 56.03s.
- `conda run -n zcj_hello python -m pytest tests/test_stage7_model_routing_and_clause_extraction.py tests/test_memory_extractor.py tests/test_memory_repository.py tests/test_memory_service.py tests/2阶段测试/test_daily_summary_flow.py tests/2阶段测试/test_query_agent_react.py tests/test_stage5_dispatch_performance.py::test_adaptive_worker_polls_and_handles_multiple_stream_groups tests/test_streams_outbox.py::test_multi_stream_read_recovers_only_missing_groups -q`
  - Result: 49 passed in 57.95s.
- `conda run -n zcj_hello python -m compileall -q core memory repositories apps runtime infrastructure scripts tests/test_stage7_model_routing_and_clause_extraction.py`
  - Result: passed.
- `git diff --check`
  - Result: passed.
- `conda run -n zcj_hello alembic heads`
  - Result: `20260723_0009 (head)`.

## Notes

No live LLM or embedding calls were made by these tests, so this pass validates routing correctness, lifecycle boundaries, extraction behavior, and stream registration rather than provider latency or token cost. Active Vector Coverage and Freshness p95 require running migrations and workers against the PostgreSQL/Redis environment, then executing the backfill command.

## Deployment

1. Run migrations through Alembic.
2. Keep `SUIXINJI_MEMORY_VECTOR_LIFECYCLE_ENABLED=false` for first deploy if you want a schema-only rollout.
3. Enable `SUIXINJI_MEMORY_VECTOR_LIFECYCLE_ENABLED=true`.
4. Start a worker for `memory_embedding`, or use the adaptive worker.
5. Run:
   - `python scripts/backfill_memory_vectors.py --status active --dry-run`
   - `python scripts/backfill_memory_vectors.py --status active --execute`
6. Enable retrieval upgrades separately:
   - `SUIXINJI_MEMORY_TRIGRAM_ENABLED=true`
   - `SUIXINJI_MEMORY_UNIFIED_RERANK_ENABLED=true`
   - `SUIXINJI_MEMORY_RETRIEVAL_MODE=hybrid_v2`

## Rollback

- Set `SUIXINJI_MEMORY_VECTOR_LIFECYCLE_ENABLED=false` to stop new Memory Vector tasks.
- Set `SUIXINJI_MEMORY_RETRIEVAL_MODE=hybrid` or `legacy` to leave Hybrid V2.
- Set `SUIXINJI_MEMORY_TRIGRAM_ENABLED=false` and `SUIXINJI_MEMORY_UNIFIED_RERANK_ENABLED=false`.
- Set `SUIXINJI_STRONG_ESCALATION_ENABLED=false` to prevent gpt-5.5 use.
