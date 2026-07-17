"""Compare local source counts with PostgreSQL after migration."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.migrate_local_to_postgres import _collect, _database_counts, _local_counts


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    args = parser.parse_args()
    payload, failures = _collect(args.data_dir)
    local = _local_counts(payload)
    postgres = _database_counts()
    checks = {
        name: {"local": count, "postgres": postgres.get(name, 0), "ok": postgres.get(name, 0) >= count}
        for name, count in local.items()
        if name in postgres
    }
    report = {"status": "ok" if all(item["ok"] for item in checks.values()) and not failures else "mismatch", "checks": checks, "read_failures": failures}
    print(json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True))
    if report["status"] != "ok":
        raise SystemExit(1)


if __name__ == "__main__":
    main()
