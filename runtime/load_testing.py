"""Deterministic multi-user workload generation and HTTP submission helpers."""

from __future__ import annotations

import json
import random
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


@dataclass(frozen=True)
class LoadProfile:
    users: int
    messages_per_user: int
    concurrency: int


PROFILES = {
    "smoke": LoadProfile(users=10, messages_per_user=2, concurrency=4),
    "basic": LoadProfile(users=100, messages_per_user=10, concurrency=24),
    "medium": LoadProfile(users=1000, messages_per_user=10, concurrency=64),
    "stress": LoadProfile(users=5000, messages_per_user=5, concurrency=128),
}


@dataclass(frozen=True)
class LoadRequest:
    run_id: str
    user_profile: str
    user_id: str
    space_id: str
    chat_id: str
    message_id: str
    operation: str
    text: str
    task_type: str
    task_payload: dict[str, Any]
    tenant_id: str

    def api_payload(self) -> dict[str, Any]:
        return {
            "message_id": self.message_id,
            "space_id": self.space_id,
            "text": self.text,
            "task_type": self.task_type,
            "task_payload": self.task_payload,
            "tenant_id": self.tenant_id,
            "user_id": self.user_id,
        }


@dataclass(frozen=True)
class SubmissionResult:
    status: str
    latency_ms: int
    http_status: int | None = None
    error: str | None = None


def percentile(values: list[int], ratio: float) -> int | None:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * ratio))))
    return ordered[index]


def _user_profile(index: int) -> str:
    marker = index % 100
    if marker < 70:
        return "ordinary"
    if marker < 90:
        return "active"
    if marker < 98:
        return "burst"
    return "malicious"


def _operation(rng: random.Random, user_profile: str) -> str:
    if user_profile == "malicious" and rng.random() < 0.8:
        return "query"
    marker = rng.random()
    if marker < 0.70:
        return "ingest"
    if marker < 0.90:
        return "query"
    if marker < 0.95:
        return "summary"
    return "memory"


def _request_content(operation: str, *, user_id: str, space_id: str, chat_id: str, sequence: int) -> tuple[str, str, dict[str, Any]]:
    delivery_key = f"stage4:{space_id}:{sequence}:{operation}"
    common = {"chat_id": chat_id, "user_id": user_id, "delivery_key": delivery_key}
    if operation == "ingest":
        text = f"Stage 4 note from {user_id}, sequence {sequence}."
        return text, "ingest", {"chat_id": chat_id, "user_id": user_id, "notify_on_success": False}
    if operation == "summary":
        text = "/summary today"
        return text, "summary", {**common, "range_key": "today", "delivery_type": "load_summary"}
    if operation == "memory":
        text = "/memory stats"
        return text, "query", {**common, "question": text, "delivery_type": "load_memory"}
    text = f"/ask What did {user_id} record most recently?"
    return text, "query", {**common, "question": text, "delivery_type": "load_query"}


def generate_requests(
    *,
    users: int,
    messages_per_user: int,
    run_id: str | None = None,
    seed: int = 20260718,
) -> list[LoadRequest]:
    run_id = run_id or uuid.uuid4().hex[:12]
    tenant_id = f"load-{run_id}"
    rng = random.Random(seed)
    requests: list[LoadRequest] = []
    for user_index in range(max(1, users)):
        profile = _user_profile(user_index)
        user_id = f"{run_id}-user-{user_index:05d}"
        space_id = f"{run_id}-space-{user_index:05d}"
        chat_id = f"{run_id}-chat-{user_index:05d}"
        last_message_id = ""
        for sequence in range(max(1, messages_per_user)):
            operation = _operation(rng, profile)
            message_id = f"{run_id}-message-{user_index:05d}-{sequence:05d}"
            if profile == "malicious" and sequence % 3 == 2 and last_message_id:
                message_id = last_message_id
            text, task_type, task_payload = _request_content(
                operation,
                user_id=user_id,
                space_id=space_id,
                chat_id=chat_id,
                sequence=sequence,
            )
            requests.append(
                LoadRequest(
                    run_id=run_id,
                    user_profile=profile,
                    user_id=user_id,
                    space_id=space_id,
                    chat_id=chat_id,
                    message_id=message_id,
                    operation=operation,
                    text=text,
                    task_type=task_type,
                    task_payload=task_payload,
                    tenant_id=tenant_id,
                )
            )
            last_message_id = message_id
    return requests


def summarize_plan(requests: list[LoadRequest]) -> dict[str, Any]:
    operations: dict[str, int] = {}
    profiles: dict[str, int] = {}
    for item in requests:
        operations[item.operation] = operations.get(item.operation, 0) + 1
        profiles[item.user_profile] = profiles.get(item.user_profile, 0) + 1
    return {
        "run_id": requests[0].run_id if requests else None,
        "tenant_id": requests[0].tenant_id if requests else None,
        "submitted": len(requests),
        "unique_users": len({item.user_id for item in requests}),
        "unique_spaces": len({item.space_id for item in requests}),
        "operations": operations,
        "user_profiles": profiles,
    }


def submit_request(endpoint: str, item: LoadRequest, *, timeout_seconds: float = 10.0) -> SubmissionResult:
    started = time.perf_counter()
    request = Request(
        endpoint.rstrip("/") + "/v1/commands",
        data=json.dumps(item.api_payload()).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urlopen(request, timeout=max(0.1, timeout_seconds)) as response:
            payload = json.loads(response.read().decode("utf-8"))
            status = "duplicate" if payload.get("duplicate") else "accepted"
            return SubmissionResult(status, int((time.perf_counter() - started) * 1000), int(response.status))
    except HTTPError as exc:
        status = "rate_limited" if exc.code == 429 else "failed"
        return SubmissionResult(status, int((time.perf_counter() - started) * 1000), exc.code, f"HTTPError: {exc.reason}")
    except (TimeoutError, URLError, OSError, ValueError) as exc:
        return SubmissionResult("failed", int((time.perf_counter() - started) * 1000), error=f"{type(exc).__name__}: {exc}")


def execute_load(
    requests: list[LoadRequest],
    *,
    endpoint: str | list[str],
    concurrency: int,
    timeout_seconds: float = 10.0,
) -> dict[str, Any]:
    endpoints = [endpoint] if isinstance(endpoint, str) else list(endpoint)
    if not endpoints:
        raise ValueError("at least one endpoint is required")
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=max(1, concurrency)) as pool:
        indexed = list(enumerate(requests))
        results = list(
            pool.map(
                lambda pair: submit_request(endpoints[pair[0] % len(endpoints)], pair[1], timeout_seconds=timeout_seconds),
                indexed,
            )
        )
    counts = {"accepted": 0, "duplicate": 0, "rate_limited": 0, "failed": 0}
    for result in results:
        counts[result.status] = counts.get(result.status, 0) + 1
    latencies = [result.latency_ms for result in results]
    report = {
        **summarize_plan(requests),
        **counts,
        "started_at": started_at,
        "duration_ms": int((time.perf_counter() - started) * 1000),
        "p50_submission_latency_ms": percentile(latencies, 0.50),
        "p95_submission_latency_ms": percentile(latencies, 0.95),
        "p99_submission_latency_ms": percentile(latencies, 0.99),
        "errors": [asdict(result) for result in results if result.status == "failed"][:20],
        "endpoints": endpoints,
    }
    report["submission_conservation_ok"] = report["submitted"] == sum(counts.values())
    return report
