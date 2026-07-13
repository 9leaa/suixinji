"""Show Memory V2 trace records."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.service import format_trace_id, format_trace_latest, format_trace_memory


def main() -> None:
    parser = argparse.ArgumentParser(description="Show a Memory V2 trace.")
    parser.add_argument("--trace-id")
    parser.add_argument("--memory-id")
    args = parser.parse_args()

    if args.memory_id:
        print(format_trace_memory(args.memory_id))
    elif args.trace_id:
        print(format_trace_id(args.trace_id))
    else:
        print(format_trace_latest())


if __name__ == "__main__":
    main()
