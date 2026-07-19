#!/usr/bin/env python
"""Check deployment configuration before starting Suixinji."""

from __future__ import annotations

import os
import sys
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
ENV_PATH = ROOT / ".env"
DATA_DIR = ROOT / "data"

REQUIRED = [
    "FEISHU_APP_ID",
    "FEISHU_APP_SECRET",
    "OPENAI_API_KEY",
    "OPENAI_BASE_URL",
    "OPENAI_MODEL",
    "EMBEDDING_MODEL",
    "EMBEDDING_DIMENSION",
]


def ok(message: str) -> None:
    print(f"[OK] {message}")


def fail(message: str) -> None:
    print(f"[FAIL] {message}")
    raise SystemExit(1)


def check_env_file() -> None:
    if not ENV_PATH.exists():
        fail(".env 不存在，请先从 .env.example 复制并填写")
    load_dotenv(ENV_PATH)
    ok(".env exists")


def check_required_env() -> None:
    missing = [key for key in REQUIRED if not os.getenv(key)]
    if missing:
        fail("缺少环境变量: " + ", ".join(missing))

    try:
        dimension = int(os.getenv("EMBEDDING_DIMENSION", ""))
    except ValueError:
        fail("EMBEDDING_DIMENSION 必须是整数")

    if dimension <= 0:
        fail("EMBEDDING_DIMENSION 必须大于 0")

    ok("required env vars exist")


def check_memory_config() -> None:
    mode = os.getenv("SUIXINJI_MEMORY_EXTRACTOR_MODE", "rules").strip().lower()
    if mode not in {"rules", "llm", "hybrid"}:
        fail("SUIXINJI_MEMORY_EXTRACTOR_MODE 必须是 rules、llm 或 hybrid")

    for key, default in (
        ("SUIXINJI_MEMORY_CANDIDATE_MIN_CONFIDENCE", "0.45"),
        ("SUIXINJI_MEMORY_AUTO_MUTATION_MIN_CONFIDENCE", "0.75"),
    ):
        try:
            value = float(os.getenv(key, default))
        except ValueError:
            fail(f"{key} 必须是 0 到 1 的数字")
        if not 0 <= value <= 1:
            fail(f"{key} 必须在 0 到 1 之间")

    try:
        top_k = int(os.getenv("SUIXINJI_MEMORY_ADJUDICATION_TOP_K", "8"))
    except ValueError:
        fail("SUIXINJI_MEMORY_ADJUDICATION_TOP_K 必须是正整数")
    if top_k <= 0:
        fail("SUIXINJI_MEMORY_ADJUDICATION_TOP_K 必须是正整数")
    ok("memory config is valid")


def check_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    test_file = DATA_DIR / ".write_test"
    try:
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
    except OSError as exc:
        fail(f"data/ 不可写: {exc}")

    ok("data/ is writable")


def check_storage_backend() -> None:
    backend = os.getenv("STORAGE_BACKEND", "local").strip().lower()
    if backend not in {"local", "postgres"}:
        fail("STORAGE_BACKEND 必须是 local 或 postgres")
    if backend == "postgres":
        if not os.getenv("DATABASE_URL"):
            fail("STORAGE_BACKEND=postgres 时必须配置 DATABASE_URL")
        try:
            from infrastructure.database import check_database_health

            check_database_health()
        except Exception as exc:
            fail(f"PostgreSQL 连接失败: {type(exc).__name__}: {exc}")
        ok("PostgreSQL connection is healthy")
    else:
        ok("local storage backend selected")


def check_database_budget() -> None:
    backend = os.getenv("STORAGE_BACKEND", "local").strip().lower()
    if backend != "postgres":
        ok("database connection budget skipped for local storage")
        return
    from core.settings import DATABASE_GLOBAL_BUDGET, database_pool_budget

    process_counts = {
        "receiver": 1,
        "api": 1,
        "outbox-relay": 1,
        "worker-ingest": 1,
        "worker-query": 1,
        "worker-summary": 1,
        "worker-memory": 1,
        "worker-enrichment": 1,
        "worker-delivery": 1,
        "scheduler": 1,
    }
    worker_count = sum(count for role, count in process_counts.items() if role.startswith("worker-"))
    total = sum(count * sum(database_pool_budget(role)) for role, count in process_counts.items())
    total += worker_count * sum(database_pool_budget("worker-heartbeat"))
    if total > DATABASE_GLOBAL_BUDGET:
        fail(f"数据库连接预算超限: theoretical_peak={total}, global_budget={DATABASE_GLOBAL_BUDGET}")
    ok(f"database connection budget is within limit ({total}/{DATABASE_GLOBAL_BUDGET})")


def check_api_config() -> None:
    from core.settings import API_HOST, API_PORT

    if not API_HOST:
        fail("SUIXINJI_API_HOST 不能为空")
    if any(char.isspace() for char in API_HOST) or "/" in API_HOST:
        fail("SUIXINJI_API_HOST 必须是 host name 或 IP 地址")
    if not 1 <= API_PORT <= 65535:
        fail("SUIXINJI_API_PORT 必须在 1 到 65535 之间")
    ok(f"api bind config is valid ({API_HOST}:{API_PORT})")


def check_coordination_backend() -> None:
    coordination = os.getenv("COORDINATION_BACKEND", "local").strip().lower()
    queue = os.getenv("TASK_QUEUE_BACKEND", "local").strip().lower()
    if coordination not in {"local", "redis"}:
        fail("COORDINATION_BACKEND 必须是 local 或 redis")
    if queue not in {"local", "redis_streams"}:
        fail("TASK_QUEUE_BACKEND 必须是 local 或 redis_streams")
    if coordination == "redis":
        if not os.getenv("REDIS_URL"):
            fail("COORDINATION_BACKEND=redis 时必须配置 REDIS_URL")
        try:
            from infrastructure.redis_client import check_redis_health

            check_redis_health()
        except Exception as exc:
            fail(f"Redis 连接失败: {type(exc).__name__}: {exc}")
        ok("Redis connection is healthy")
    else:
        ok("local coordination backend selected")
    if queue == "redis_streams" and (coordination != "redis" or os.getenv("STORAGE_BACKEND", "local") != "postgres"):
        fail("TASK_QUEUE_BACKEND=redis_streams 需要 PostgreSQL 和 Redis")


def main() -> None:
    check_env_file()
    check_required_env()
    check_memory_config()
    check_api_config()
    check_storage_backend()
    check_database_budget()
    check_coordination_backend()
    check_data_dir()
    print("配置检查通过。")


if __name__ == "__main__":
    main()
