#!/usr/bin/env python
"""Ensure Stage 4 containers receive container-reachable database URLs."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def selected_url(stage4_name: str, fallback_name: str) -> str:
    return (os.getenv(stage4_name) or os.getenv(fallback_name) or "").strip()


def main() -> None:
    failures = []
    for stage4_name, fallback_name in (
        ("STAGE4_DATABASE_URL", "DATABASE_URL"),
        ("STAGE4_REDIS_URL", "REDIS_URL"),
    ):
        value = selected_url(stage4_name, fallback_name)
        host = urlparse(value).hostname if value else None
        if not value:
            failures.append(f"{stage4_name} is missing")
        elif host in {"127.0.0.1", "localhost", "::1"}:
            failures.append(f"{stage4_name} must use a container-reachable host, not {host}")
    if failures:
        print("Stage 4 network preflight failed:")
        for failure in failures:
            print(f"- {failure}")
        print("Set STAGE4_DATABASE_URL and STAGE4_REDIS_URL in .env, using the Mac LAN address or host.docker.internal.")
        raise SystemExit(1)
    print("Stage 4 database and Redis URLs are container-reachable.")


if __name__ == "__main__":
    main()
