#!/usr/bin/env python3
"""Small localhost-only RESP server for a frozen embedding-cache export."""

from __future__ import annotations

import argparse
import gzip
import json
import socketserver
import threading
from pathlib import Path
from typing import BinaryIO


def _read_line(stream: BinaryIO) -> bytes:
    line = stream.readline()
    if not line:
        raise EOFError
    if not line.endswith(b"\r\n"):
        raise ValueError("invalid RESP line")
    return line[:-2]


def _read_value(stream: BinaryIO):
    prefix = stream.read(1)
    if not prefix:
        raise EOFError
    if prefix == b"*":
        count = int(_read_line(stream))
        return [_read_value(stream) for _ in range(count)]
    if prefix == b"$":
        size = int(_read_line(stream))
        if size < 0:
            return None
        value = stream.read(size)
        if stream.read(2) != b"\r\n":
            raise ValueError("invalid RESP bulk terminator")
        return value
    if prefix in {b"+", b":", b"-"}:
        return _read_line(stream)
    raise ValueError(f"unsupported RESP prefix: {prefix!r}")


def _simple(value: str) -> bytes:
    return f"+{value}\r\n".encode()


def _integer(value: int) -> bytes:
    return f":{value}\r\n".encode()


def _bulk(value: str | bytes | None) -> bytes:
    if value is None:
        return b"$-1\r\n"
    raw = value if isinstance(value, bytes) else value.encode()
    return f"${len(raw)}\r\n".encode() + raw + b"\r\n"


class CacheState:
    def __init__(self, entries: dict[str, str]) -> None:
        self.entries = dict(entries)
        self.lock = threading.Lock()
        self.unknown_commands: set[str] = set()


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        state: CacheState = self.server.state  # type: ignore[attr-defined]
        while True:
            try:
                payload = _read_value(self.rfile)
            except EOFError:
                return
            except Exception as exc:
                self.wfile.write(f"-ERR {exc}\r\n".encode())
                self.wfile.flush()
                return
            if not isinstance(payload, list) or not payload:
                self.wfile.write(b"-ERR command must be an array\r\n")
                self.wfile.flush()
                continue
            parts = [
                item.decode("utf-8", errors="replace")
                if isinstance(item, bytes)
                else str(item or "")
                for item in payload
            ]
            command = parts[0].upper()
            args = parts[1:]
            close = False
            with state.lock:
                if command == "PING":
                    response = _bulk(args[0]) if args else _simple("PONG")
                elif command == "ECHO":
                    response = _bulk(args[0] if args else "")
                elif command == "GET":
                    response = _bulk(state.entries.get(args[0]) if args else None)
                elif command == "MGET":
                    values = [state.entries.get(key) for key in args]
                    response = (
                        f"*{len(values)}\r\n".encode()
                        + b"".join(_bulk(value) for value in values)
                    )
                elif command == "SET" and len(args) >= 2:
                    state.entries[args[0]] = args[1]
                    response = _simple("OK")
                elif command in {"SETEX", "PSETEX"} and len(args) >= 3:
                    state.entries[args[0]] = args[2]
                    response = _simple("OK")
                elif command in {"DEL", "UNLINK"}:
                    removed = sum(state.entries.pop(key, None) is not None for key in args)
                    response = _integer(removed)
                elif command == "EXISTS":
                    response = _integer(sum(key in state.entries for key in args))
                elif command in {"EXPIRE", "PEXPIRE"}:
                    response = _integer(int(bool(args and args[0] in state.entries)))
                elif command in {"TTL", "PTTL"}:
                    response = _integer(-1 if args and args[0] in state.entries else -2)
                elif command in {"INCR", "INCRBY"} and args:
                    amount = 1 if command == "INCR" else int(args[1])
                    value = int(state.entries.get(args[0], "0")) + amount
                    state.entries[args[0]] = str(value)
                    response = _integer(value)
                elif command in {"AUTH", "SELECT", "CLIENT"}:
                    response = _simple("OK")
                elif command == "QUIT":
                    response = _simple("OK")
                    close = True
                else:
                    state.unknown_commands.add(command)
                    response = b"-ERR unsupported command in frozen eval cache\r\n"
            self.wfile.write(response)
            self.wfile.flush()
            if close:
                return


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=46379)
    args = parser.parse_args()
    path = Path(args.data)
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as stream:
        payload = json.load(stream)
    entries = payload.get("entries") or {}
    if len(entries) != int(payload.get("entry_count") or 0):
        raise ValueError("embedding cache entry count mismatch")
    state = CacheState({str(key): str(value) for key, value in entries.items()})
    with Server((args.host, args.port), Handler) as server:
        server.state = state  # type: ignore[attr-defined]
        print(
            json.dumps(
                {"status": "ready", "entries": len(entries), "port": args.port}
            ),
            flush=True,
        )
        server.serve_forever()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
