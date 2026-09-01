#!/usr/bin/env python3
"""Run a Python script while exposing SIGUSR1 stack dumps for eval diagnosis."""

from __future__ import annotations

import faulthandler
import os
import runpy
import signal
import sys


def main() -> int:
    if len(sys.argv) < 2:
        raise SystemExit("usage: run_with_faulthandler.py SCRIPT [ARGS ...]")
    script = sys.argv[1]
    log_path = os.environ.get("SUIXINJI_EVAL_STACK_LOG", "/tmp/suixinji_eval_stack.log")
    stack_file = open(log_path, "a", buffering=1)
    faulthandler.register(signal.SIGUSR1, file=stack_file, all_threads=True)
    sys.argv = [script, *sys.argv[2:]]
    runpy.run_path(script, run_name="__main__")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
