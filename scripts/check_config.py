#!/usr/bin/env python
"""Check deployment configuration before starting Suixinji."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
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


def check_data_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    test_file = DATA_DIR / ".write_test"
    try:
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
    except OSError as exc:
        fail(f"data/ 不可写: {exc}")

    ok("data/ is writable")


def main() -> None:
    check_env_file()
    check_required_env()
    check_data_dir()
    print("配置检查通过。")


if __name__ == "__main__":
    main()
