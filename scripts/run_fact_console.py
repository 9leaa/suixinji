#!/usr/bin/env python3
"""Start/restart the isolated fact-memory console and its distributed workers."""
import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time

from dotenv import load_dotenv
from sqlalchemy.engine import make_url

ROOT = Path(__file__).resolve().parents[1]
parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("--restart", action="store_true")
parser.add_argument("--runtime-dir", type=Path, default=ROOT / "data" / "fact-console-runtime")
args = parser.parse_args()
load_dotenv(ROOT / ".env", override=True)
if make_url(os.environ["DATABASE_URL"]).database != "suixinji_fact_memory_test_20261020":
    raise SystemExit("此启动器仅允许本次独立测试数据库。")
runtime = args.runtime_dir.resolve()
runtime.mkdir(parents=True, exist_ok=True)
state = runtime / "processes.json"
old = json.loads(state.read_text()) if state.exists() else {}
alive = {}
for name, pid in old.items():
    result = subprocess.run(["lsof", "-a", "-p", str(pid), "-d", "cwd", "-Fn"],
                            capture_output=True, text=True)
    if result.returncode == 0:
        if "\nn" + str(ROOT) + "\n" not in result.stdout:
            raise SystemExit(f"记录的 {name} PID 已被其他进程使用；未停止它。")
        alive[name] = pid
if alive and not args.restart:
    raise SystemExit("测试服务已在运行；更新代码后使用 --restart。")
for pid in alive.values():
    os.kill(pid, signal.SIGTERM)
deadline = time.monotonic() + 15
while alive and time.monotonic() < deadline:
    for name, pid in list(alive.items()):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            alive.pop(name)
    if alive:
        time.sleep(.2)
if alive:
    raise SystemExit("旧测试服务仍在退出，请稍后重试；未强制终止。")
commands = {
    "console": [sys.executable, "scripts/run_console.py"],
    "worker": [sys.executable, "-m", "apps.worker", "adaptive", "--worker-id", "fact-memory-test"],
    "scheduler": [sys.executable, "-m", "apps.scheduler"],
    "relay": [sys.executable, "-u", "-m", "apps.outbox_relay"],
}
processes = {}
for name, command in commands.items():
    with (runtime / (name + ".log")).open("a") as log:
        process = subprocess.Popen(command, cwd=ROOT, env=os.environ.copy(),
                                   stdout=log, stderr=log, start_new_session=True)
    processes[name] = process.pid
state.write_text(json.dumps(processes))
print("已启动隔离测试服务：http://127.0.0.1:18080/console")
print("进程和日志目录：" + str(runtime))
