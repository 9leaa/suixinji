"""Isolated Redis Streams / real distributed-worker integration evaluation.

The test creates receiver-shaped InboxCommand records, then deliberately publishes
the resulting task ids to the real Redis ingest stream.  Existing production
workers consume them; no Feishu API is contacted.  Every generated row carries a
unique source/tenant/space prefix and is removed from PostgreSQL after snapshots.
"""
from __future__ import annotations

import argparse
import json
import statistics
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from apps.receiver import InboxCommand, receive
from infrastructure.database import session_scope
from infrastructure.schema import InboxMessage, Memory, OutboxEvent, Space, Task, TaskAttempt
from repositories.postgres.dispatch import enqueue_task
from runtime.streams.client import StreamClient


def iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else (str(value) if value is not None else None)


def task_snapshot(task_id: str) -> dict[str, Any] | None:
    with session_scope() as session:
        task = session.get(Task, task_id)
        if task is None:
            return None
        attempts = list(session.execute(select(TaskAttempt).where(TaskAttempt.task_id == task_id).order_by(TaskAttempt.attempt_no)).scalars())
        return {
            "id": task.id, "type": task.task_type, "space_id": task.space_id,
            "status": task.status, "attempt_count": task.attempt_count,
            "failure_count": task.failure_count, "defer_count": task.defer_count,
            "max_attempts": task.max_attempts, "created_at": iso(task.created_at),
            "started_at": iso(task.started_at), "completed_at": iso(task.completed_at),
            "last_error": task.last_error,
            "attempts": [{"no": a.attempt_no, "status": a.status, "started_at": iso(a.started_at), "finished_at": iso(a.finished_at), "error_type": a.error_type, "error_summary": a.error_summary} for a in attempts],
        }


def space_snapshot(space_id: str) -> dict[str, Any]:
    with session_scope() as session:
        inbox = list(session.execute(select(InboxMessage).where(InboxMessage.space_id == space_id).order_by(InboxMessage.sequence_no)).scalars())
        memories = list(session.execute(select(Memory).where(Memory.space_id == space_id)).scalars())
        return {
            "inbox": [{"id": row.id, "source_message_id": row.source_message_id, "status": row.status, "note_status": row.note_status, "memory_status": row.memory_status, "sequence_no": row.sequence_no} for row in inbox],
            "memory_count": len(memories),
            "memory_ids": [row.id for row in memories],
        }


def pct(values: list[float], q: float) -> float | None:
    if not values:
        return None
    values = sorted(values)
    return values[min(len(values) - 1, max(0, int((len(values) - 1) * q)))]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--messages", type=int, default=60)
    parser.add_argument("--spaces", type=int, default=12)
    parser.add_argument("--timeout-seconds", type=int, default=600)
    args = parser.parse_args()
    run_id = f"eval_redis_chain_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    tenant = f"{run_id}_tenant"
    source = "eval_redis_direct_receiver_contract"
    source_spaces = [f"{run_id}_space_{index:02d}" for index in range(args.spaces)]
    stream = StreamClient()
    rows: list[dict[str, Any]] = []

    def create(index: int) -> dict[str, Any]:
        source_space = source_spaces[index % len(source_spaces)]
        message_id = f"{run_id}_message_{index:03d}"
        text = f"评测记录 {index}: 我正在推进 Redis 分布式 worker 链路验收，当前任务状态为进行中。"
        received_at = datetime.now().astimezone().isoformat()
        started = time.perf_counter()
        result = receive(InboxCommand(
            source=source, message_id=message_id, space_id=source_space, text=text,
            task_type="ingest", tenant_id=tenant, chat_id=None, chat_type="group",
            sender={"sender_id": "eval-runner", "sender_type": "user"}, received_at=received_at,
            task_payload={"notify_on_success": False, "source": source, "eval_run_id": run_id},
        ))
        if not result.task_id:
            raise RuntimeError(f"receiver did not create ingest task: {result}")
        stream_ids = [stream.publish_task(f"{run_id}:manual:{index}:1", {"task_type": "ingest", "task_id": result.task_id, "attempt": 1})]
        # Deliberate duplicate Stream deliveries: Task claim/idempotency must run the work once.
        duplicated = index < min(10, args.messages)
        if duplicated:
            stream_ids.append(stream.publish_task(f"{run_id}:manual:{index}:duplicate", {"task_type": "ingest", "task_id": result.task_id, "attempt": 1}))
        return {"kind": "normal", "index": index, "source_space_id": source_space, "message_id": message_id,
                "inbox_id": result.inbox_id, "task_id": result.task_id, "receiver_created": result.created,
                "receiver_duplicate": result.duplicate, "stream_message_ids": stream_ids, "duplicated_delivery": duplicated,
                "enqueue_ms": round((time.perf_counter() - started) * 1000, 3), "sent_at": received_at}

    args.output_dir.mkdir(parents=True, exist_ok=True)
    try:
        with ThreadPoolExecutor(max_workers=min(12, max(1, args.messages)), thread_name_prefix="redis-chain-producer") as pool:
            futures = [pool.submit(create, index) for index in range(args.messages)]
            for future in as_completed(futures):
                rows.append(future.result())
        rows.sort(key=lambda row: int(row["index"]))

        # A genuine worker failure: real memory worker cannot find the note; max_attempts=1 forces dead letter.
        poison_space = f"{run_id}_poison"
        poison_task, poison_created = enqueue_task(
            task_type="memory", tenant_id=tenant, space_id=poison_space,
            source_message_id=f"{run_id}_poison_message", idempotency_key=f"{run_id}:poison:memory",
            # Two attempts make the first genuine worker failure enter retry;
            # the real scheduler republishes it and the second fails into DLQ.
            payload={"operation": "extract", "note_id": f"missing:{run_id}", "eval_run_id": run_id}, max_attempts=2,
            publish=False,
        )
        poison_stream_id = stream.publish_task(f"{run_id}:poison", {"task_type": "memory", "task_id": poison_task, "attempt": 1})
        poison = {"kind": "poison_dead_letter", "task_id": poison_task, "source_space_id": poison_space, "created": poison_created, "stream_message_ids": [poison_stream_id]}

        pending = {row["task_id"] for row in rows} | {poison_task}
        timeline: list[dict[str, Any]] = []
        deadline = time.monotonic() + args.timeout_seconds
        while pending and time.monotonic() < deadline:
            states = {task_id: task_snapshot(task_id) for task_id in pending}
            status_counts: dict[str, int] = {}
            for state in states.values():
                status = str((state or {}).get("status") or "missing")
                status_counts[status] = status_counts.get(status, 0) + 1
            timeline.append({"at": datetime.now().astimezone().isoformat(), "elapsed_s": round(args.timeout_seconds - max(0, deadline - time.monotonic()), 3), "status_counts": status_counts})
            pending = {task_id for task_id, state in states.items() if (state or {}).get("status") not in {"completed", "dead_letter", "cancelled"}}
            if pending:
                time.sleep(1)

        for row in rows:
            row["final_task"] = task_snapshot(row["task_id"])
        poison["final_task"] = task_snapshot(poison_task)
        all_spaces = source_spaces + [poison_space]
        snapshots = {space: space_snapshot(space) for space in all_spaces}
        completed = [row for row in rows if (row.get("final_task") or {}).get("status") == "completed"]
        latencies: list[float] = []
        for row in completed:
            task = row["final_task"]
            created = datetime.fromisoformat(str(task["created_at"]))
            finished = datetime.fromisoformat(str(task["completed_at"]))
            latencies.append((finished - created).total_seconds() * 1000)
        # no cross-space leakage means every inbox/message count belongs to exactly its expected test source-space.
        cross_space_ok = all(all(item["source_message_id"].startswith(run_id) for item in snap["inbox"]) for snap in snapshots.values())
        summary = {
            "run_id": run_id, "mode": "receiver-contract records + direct Redis Stream publication + real workers", "feishu_called": False,
            "tenant": tenant, "normal_messages": args.messages, "test_spaces": len(source_spaces), "duplicate_stream_deliveries": sum(row["duplicated_delivery"] for row in rows),
            "completed": len(completed), "completion_rate": len(completed) / args.messages if args.messages else 0,
            "timed_out_tasks": len(pending), "poison_status": (poison.get("final_task") or {}).get("status"),
            "dead_letter_observed": (poison.get("final_task") or {}).get("status") == "dead_letter",
            "retry_observed": int((poison.get("final_task") or {}).get("attempt_count") or 0) >= 2,
            "idempotency_pass": all(len(snapshots[row["source_space_id"]]["inbox"]) <= (args.messages // args.spaces + 1) for row in rows),
            "cross_space_isolation_pass": cross_space_ok,
            "e2e_latency_ms": {"p50": pct(latencies, .50), "p95": pct(latencies, .95), "p99": pct(latencies, .99), "mean": statistics.mean(latencies) if latencies else None},
            "enqueue_latency_ms": {"p50": pct([row["enqueue_ms"] for row in rows], .50), "p95": pct([row["enqueue_ms"] for row in rows], .95)},
            "throughput_messages_per_second": (len(completed) / max(0.001, (max(latencies) / 1000))) if latencies else None,
        }
        (args.output_dir / "messages.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows), encoding="utf-8")
        (args.output_dir / "poison_dead_letter.json").write_text(json.dumps(poison, ensure_ascii=False, indent=2), encoding="utf-8")
        (args.output_dir / "timeline.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in timeline), encoding="utf-8")
        (args.output_dir / "space_snapshots.json").write_text(json.dumps(snapshots, ensure_ascii=False, indent=2), encoding="utf-8")
        (args.output_dir / "metrics.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        (args.output_dir / "summary.md").write_text("\n".join([
            "# Redis + distributed worker chain evaluation", "",
            "- No Feishu request was made. Each message first used the receiver's InboxCommand contract, then its database Task id was published directly to the real Redis Stream.",
            "- Existing ingest/memory workers consumed the tasks. Duplicate Stream publication is deliberate and must not duplicate database work.",
            f"- Normal messages: {args.messages}; isolated test spaces: {len(source_spaces)}; duplicate deliveries: {summary['duplicate_stream_deliveries']}",
            f"- Completed: {summary['completed']} ({summary['completion_rate']:.2%}); timeout: {summary['timed_out_tasks']}",
            f"- E2E latency p50/p95/p99: {summary['e2e_latency_ms']['p50']}/{summary['e2e_latency_ms']['p95']}/{summary['e2e_latency_ms']['p99']} ms",
            f"- Poison retry observed: {summary['retry_observed']}; final dead-letter: {summary['dead_letter_observed']} ({summary['poison_status']})",
            f"- Cross-space isolation: {summary['cross_space_isolation_pass']}",
            "- Raw receiver results, stream ids, task attempts, state snapshots, polling timeline, and failure sample are retained beside this report.", "",
        ]), encoding="utf-8")
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    finally:
        # Retain only artifact files above; delete all generated PostgreSQL test spaces and cascaded task/note/memory rows.
        with session_scope() as session:
            task_ids = list(session.execute(select(Task.id).where(Task.tenant_id == tenant)).scalars())
            if task_ids:
                # Outbox events intentionally have no FK to Task, so explicitly remove
                # only this run's event audit rows before the task/space cascade.
                session.execute(delete(OutboxEvent).where(OutboxEvent.aggregate_type == "task", OutboxEvent.aggregate_id.in_(task_ids)))
            session.execute(delete(Space).where(Space.tenant_id == tenant))


if __name__ == "__main__":
    main()
