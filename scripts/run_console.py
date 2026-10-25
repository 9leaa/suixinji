#!/usr/bin/env python3
"""Start the local console; use the ordinary distributed workers for processing."""

from pathlib import Path
import os
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
RUNTIME = Path(os.getenv("SUIXINJI_RUNTIME_DIR", str(ROOT)))
os.chdir(RUNTIME)
from dotenv import load_dotenv

load_dotenv(os.getenv("SUIXINJI_ENV_FILE", str(RUNTIME / ".env")))
os.environ["SUIXINJI_CONSOLE_ENABLED"] = "1"
os.environ.setdefault("SUIXINJI_PROCESS_ROLE", "api")
token_file = RUNTIME / "data/console.token"
if os.getenv("SUIXINJI_CONSOLE_AUTH_MODE", "accounts") == "token" and not os.getenv("SUIXINJI_CONSOLE_TOKEN"):
    token_file.parent.mkdir(exist_ok=True)
    if not token_file.exists():
        fd = os.open(token_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(secrets.token_urlsafe(32))
    os.environ["SUIXINJI_CONSOLE_TOKEN"] = token_file.read_text().strip()
from repositories.postgres.lifecycle import initialize

initialize()
print("Console: http://127.0.0.1:18080/console", flush=True)
print("Authentication: " + os.getenv("SUIXINJI_CONSOLE_AUTH_MODE", "accounts"), flush=True)
import uvicorn

uvicorn.run("apps.api:app", host="127.0.0.1", port=int(os.getenv("SUIXINJI_CONSOLE_PORT", "18080")))
