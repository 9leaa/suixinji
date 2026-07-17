"""CLI entry point for one Redis Stream worker role."""

from __future__ import annotations

import argparse
import logging

from apps.handlers import HANDLERS
from runtime.delivery_store import recover_stale_reserved_deliveries
from runtime.streams.worker import StreamWorker


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("task_type", choices=sorted(HANDLERS))
    parser.add_argument("--worker-id")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    if args.task_type == "delivery":
        recover_stale_reserved_deliveries()
    StreamWorker(args.task_type, HANDLERS[args.task_type], worker_id=args.worker_id).run_forever()


if __name__ == "__main__":
    main()
