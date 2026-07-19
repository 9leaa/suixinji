# Stage 6 Memory Hybrid Retrieval Report

Date: 2026-07-18
Branch: `optimize/stage6-memory-hybrid-retrieval`
Baseline: `main@9977ac5`

## Scope

This pass implements the first practical slice of the memory-quality hybrid retrieval design:

- Memory Key V2 for preference and open semantic facts.
- PostgreSQL hybrid adjudication retrieval through exact key, structured fields, full-text search, lexical fallback, and optional pgvector.
- User Memory Search no longer starts from a fixed 100-row scan in PostgreSQL hybrid mode.
- Candidate Retrieval no longer starts from a fixed 200-row scan in PostgreSQL hybrid mode.
- Rules-mode ingest uses `may_contain_memory()` and no longer performs full Candidate extraction before creating the Memory task.
- Rules extraction can emit multiple candidates from one note, including episodic events.
- Alembic `20260718_0007` adds key-version metadata, vector metadata, FTS index, and vector index.

## Compatibility

`SUIXINJI_MEMORY_RETRIEVAL_MODE=legacy|hybrid` controls rollback. The default is `hybrid`.

Vector retrieval is best-effort. If embeddings are unavailable, missing, or disabled through `SUIXINJI_MEMORY_HYBRID_VECTOR_ENABLED=false`, retrieval falls back to exact key, structured fields, FTS, and lexical paths.

SQLite local storage keeps a compatibility implementation for tests. The no-fixed-scan requirement is implemented for the PostgreSQL production path.

## Verification

- `conda run -n zcj_hello python -m pytest tests/test_memory_hybrid_retrieval_stage6.py tests/test_memory_extractor.py tests/test_memory_service.py tests/test_memory_extraction_state.py tests/test_memory_stage1_correctness.py -q`
  - Result: 44 passed.
- `conda run -n zcj_hello python -m pytest tests/test_stage5_dispatch_performance.py -q`
  - Result: 6 passed.
- `conda run -n zcj_hello alembic upgrade head`
  - Result: upgraded `20260718_0006 -> 20260718_0007`.
- `conda run -n zcj_hello python -m pytest tests/test_postgres_repositories.py tests/test_streams_outbox.py::test_ingest_memory_barrier_blocks_query_but_not_enrichment -q`
  - Result: 10 passed.
- `conda run -n zcj_hello python -m pytest -q`
  - Result: 268 passed, 5 third-party deprecation warnings.
- `conda run -n zcj_hello ruff check ...`
  - Result: passed.
- `conda run -n zcj_hello alembic current`
  - Result: `20260718_0007 (head)`.

## Current Metrics

The deterministic regression suite confirms the main correctness fixes:

- Preference polarity changes share one Memory Key slot.
- Open semantic facts no longer collapse into one `semantic:user:fact` slot.
- Exact key retrieval survives more than 100 similar local memories.
- Ingest no longer calls full Candidate extraction for admission.

Real LLM and real Embedding quality/cost baselines were not measured in this pass. Capacity and 10K-memory p95 targets remain open for the next evaluation pass.

## Not Completed Yet

- Full 1000-case realistic quality dataset.
- Three-mode rules/LLM/hybrid quality and cost comparison.
- Monthly consolidation semantic clustering with source/date/conflict gates.
- Automatic Memory embedding backfill and refresh lifecycle.
- 10K-memory p95 benchmark with cached and uncached embedding paths.
