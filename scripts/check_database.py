"""Perform a read-only PostgreSQL connectivity and schema check."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import inspect

from infrastructure.database import check_database_health, get_engine
from infrastructure.schema import Base


def main() -> None:
    health = check_database_health()
    inspector = inspect(get_engine())
    expected = set(Base.metadata.tables)
    existing = set(inspector.get_table_names())
    print(json.dumps({**health, "missing_tables": sorted(expected - existing), "table_count": len(existing)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
