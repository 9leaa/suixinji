"""Isolated real Ingest -> Memory -> Ask V2 evaluation.

This runner intentionally enters through the production receiver contract and
publishes its Task ids to the real Redis Stream.  It does not call Feishu and it
does not insert Notes/Memories directly.  Each run uses a unique tenant/space
prefix and removes generated PostgreSQL rows after retaining JSON artifacts.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import delete, select

from agent.ask_workflow import answer_question_v2
from apps.receiver import InboxCommand, receive
from infrastructure.database import session_scope
from infrastructure.schema import InboxMessage, Memory, OutboxEvent, Space, Task
from runtime.streams.client import GROUPS, StreamClient


CASES = [
    {
        "case_id": "task_state",
        "messages": ["我要完成环境部署文档。", "环境部署文档已经完成。"],
        "question": "环境部署文档现在是什么状态？",
        "must_include_any": [["完成"]],
        "expected_memory_type": "task",
        "expected_task_status": "done",
    },
    {
        "case_id": "semantic_recency",
        "messages": ["我现在住在北京。", "我已经搬到上海。"],
        "question": "我现在住在哪里？",
        "must_include_any": [["上海"]],
        "expected_memory_type": "semantic",
    },
    {
        "case_id": "preference_override",
        "messages": ["我喜欢燕麦拿铁。", "我现在不喜欢燕麦拿铁，喝了容易胃不舒服。"],
        "question": "我现在喜欢燕麦拿铁吗？",
        "must_include_any": [["不喜欢"]],
        "expected_memory_type": "preference",
    },
    {
        "case_id": "episodic_recall",
        "messages": ["上周末我去爬香山，走了八公里。"],
        "question": "上周末我做了什么？",
        "must_include_any": [["香山"]],
        "expected_memory_type": "episodic",
    },
]
CORE_PIPELINE_TASK_TYPES = {"ingest", "memory", "memory_embedding", "enrichment"}


def iso(value: Any) -> str | None:
    return value.isoformat() if value is not None and hasattr(value, "isoformat") else (str(value) if value is not None else None)


def snapshot(space_id: str) -> dict[str, Any]:
    with session_scope() as session:
        tasks = list(session.execute(select(Task).where(Task.space_id == space_id).order_by(Task.created_at)).scalars())
        inbox = list(session.execute(select(InboxMessage).where(InboxMessage.space_id == space_id).order_by(InboxMessage.sequence_no)).scalars())
        memories = list(session.execute(select(Memory).where(Memory.space_id == space_id).order_by(Memory.created_at)).scalars())
        return {
            "tasks": [{"id": row.id, "type": row.task_type, "status": row.status, "attempts": row.attempt_count, "error": row.last_error} for row in tasks],
            "inbox": [{"id": row.id, "status": row.status, "note_status": row.note_status, "memory_status": row.memory_status, "sequence": row.sequence_no} for row in inbox],
            "memories": [{"id": row.id, "type": row.memory_type, "status": row.status, "content": row.content, "task_status": row.task_status} for row in memories],
        }


def all_terminal(space_id: str) -> bool:
    with session_scope() as session:
        statuses = list(session.execute(
            select(Task.status).where(Task.space_id == space_id, Task.task_type.in_(CORE_PIPELINE_TASK_TYPES))
        ).scalars())
    return bool(statuses) and all(status in {"completed", "dead_letter", "cancelled"} for status in statuses)


def wait_for_pipeline(space_id: str, timeout_seconds: int) -> tuple[bool, float]:
    started = time.monotonic()
    # memory tasks can be created immediately after an ingest task completes.
    quiet_rounds = 0
    while time.monotonic() - started < timeout_seconds:
        if all_terminal(space_id):
            quiet_rounds += 1
            if quiet_rounds >= 3:
                return True, round(time.monotonic() - started, 3)
        else:
            quiet_rounds = 0
        time.sleep(1)
    return False, round(time.monotonic() - started, 3)


def wait_for_stream_acks(tenant: str, stream: StreamClient, timeout_seconds: int = 45) -> bool:
    """Do not delete a test tenant while one of its Stream messages is pending.

    PostgreSQL task completion and Redis ACK are separate operations.  Deleting
    a completed task in that narrow interval turns a later reclaim into a
    poison message for a single worker, so cleanup must wait for the ACK.
    """
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        with session_scope() as session:
            task_ids = {str(value) for value in session.execute(select(Task.id).where(Task.tenant_id == tenant)).scalars()}
            task_types = {str(value) for value in session.execute(select(Task.task_type).where(Task.tenant_id == tenant)).scalars()}
        if not task_ids:
            return True
        pending_ids: set[str] = set()
        for task_type in task_types:
            group = GROUPS.get(task_type)
            if not group:
                continue
            redis_stream = stream.keys.stream(task_type)
            try:
                pending = stream.client.xpending_range(redis_stream, group, "-", "+", 1000)
            except Exception:
                continue
            for item in pending:
                message_id = str(item.get("message_id") or item.get(b"message_id") or "")
                if not message_id:
                    continue
                rows = stream.client.xrange(redis_stream, message_id, message_id)
                if not rows:
                    continue
                fields = rows[0][1]
                task_id = str(fields.get("task_id") or fields.get(b"task_id") or "")
                if task_id in task_ids:
                    pending_ids.add(task_id)
        if not pending_ids:
            return True
        time.sleep(0.5)
    return False


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout-seconds", type=int, default=240)
    args = parser.parse_args()
    run_id = f"eval_ask_lifecycle_{int(time.time())}_{uuid.uuid4().hex[:8]}"
    tenant = f"{run_id}_tenant"
    stream = StreamClient()
    args.output_dir.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    cleanup_allowed = False
    try:
        for case in CASES:
            space_id = f"{run_id}_{case['case_id']}"
            sent: list[dict[str, Any]] = []
            for index, text in enumerate(case["messages"]):
                message_id = f"{case['case_id']}_{index}_{uuid.uuid4().hex[:8]}"
                received = receive(InboxCommand(
                    source="eval_ask_lifecycle", message_id=message_id, space_id=space_id,
                    text=text, task_type="ingest", tenant_id=tenant, chat_id=None,
                    chat_type="group", sender={"sender_id": "eval-runner", "sender_type": "user"},
                    received_at=datetime.now().astimezone().isoformat(),
                    task_payload={"notify_on_success": False, "eval_run_id": run_id},
                ))
                if not received.task_id:
                    raise RuntimeError(f"receiver did not create task for {case['case_id']}: {received}")
                stream_id = stream.publish_task(
                    f"{run_id}:{case['case_id']}:{index}",
                    {"task_type": "ingest", "task_id": received.task_id, "attempt": 1},
                )
                sent.append({"message": text, "message_id": message_id, "task_id": received.task_id, "stream_id": stream_id})
            pipeline_ready, pipeline_wait_s = wait_for_pipeline(space_id, args.timeout_seconds)
            before_ask = snapshot(space_id)
            ask_started = time.monotonic()
            ask_error = None
            try:
                outcome = answer_question_v2(space_id, case["question"])
                answer = outcome.answer
                answer_source = outcome.answer_source
                selected = outcome.selected_records
            except Exception as exc:  # retained as an artifact, do not hide integration failures
                answer, answer_source, selected = "", "error", []
                ask_error = f"{type(exc).__name__}: {exc}"
            answer_latency_s = round(time.monotonic() - ask_started, 3)
            after_ask = snapshot(space_id)
            answer_groups = list(case.get("must_include_any") or [[token] for token in case.get("must_include", [])])
            answer_pass = all(any(token in answer for token in alternatives) for alternatives in answer_groups)
            type_pass = any(row["type"] == case["expected_memory_type"] for row in after_ask["memories"])
            core_tasks = [row for row in after_ask["tasks"] if row["type"] in CORE_PIPELINE_TASK_TYPES]
            worker_pass = pipeline_ready and bool(core_tasks) and all(row["status"] == "completed" for row in core_tasks)
            expected_task_status = case.get("expected_task_status")
            active_tasks = [row for row in after_ask["memories"] if row["type"] == "task" and row.get("status") == "active"]
            task_state_pass = (
                len(active_tasks) == 1 and active_tasks[0].get("task_status") == expected_task_status
                if expected_task_status else None
            )
            results.append({
                "case_id": case["case_id"], "space_id": space_id, "messages": sent,
                "question": case["question"], "must_include_any": answer_groups,
                "answer": answer, "answer_source": answer_source, "selected_evidence": selected,
                "pipeline_ready": pipeline_ready, "pipeline_wait_s": pipeline_wait_s,
                "answer_latency_s": answer_latency_s, "answer_pass": answer_pass, "task_state_pass": task_state_pass,
                "memory_type_pass": type_pass, "worker_pass": worker_pass,
                "ask_error": ask_error, "before_ask": before_ask, "after_ask": after_ask,
            })
            print(json.dumps({"case": case["case_id"], "pipeline_ready": pipeline_ready, "answer_pass": answer_pass, "type_pass": type_pass, "answer_source": answer_source}, ensure_ascii=False), flush=True)
        metrics = {
            "run_id": run_id,
            "mode": "receiver -> real Redis Stream -> real ingest/memory workers -> Ask V2 direct",
            "feishu_called": False,
            "case_count": len(results),
            "worker_pipeline_pass_rate": sum(row["worker_pass"] for row in results) / len(results),
            "memory_type_pass_rate": sum(row["memory_type_pass"] for row in results) / len(results),
            "answer_substring_pass_rate": sum(row["answer_pass"] for row in results) / len(results),
            "task_state_pass_rate": sum(row["task_state_pass"] is True for row in results) / max(1, sum(row["task_state_pass"] is not None for row in results)),
            "end_to_end_pass_rate": sum(row["worker_pass"] and row["memory_type_pass"] and row["answer_pass"] and row["task_state_pass"] is not False for row in results) / len(results),
            "pipeline_wait_s": [row["pipeline_wait_s"] for row in results],
            "answer_latency_s": [row["answer_latency_s"] for row in results],
        }
        metrics["stream_ack_settled_before_cleanup"] = wait_for_stream_acks(tenant, stream)
        cleanup_allowed = bool(metrics["stream_ack_settled_before_cleanup"])
        (args.output_dir / "results.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in results), encoding="utf-8")
        (args.output_dir / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
        (args.output_dir / "summary.md").write_text("\n".join([
            "# Isolated Ingest -> Memory -> Ask V2 evaluation", "",
            "- No Feishu API was called. Test inputs enter via the production receiver contract, then are published to the real Redis Stream and consumed by the existing ingest/memory workers.",
            "- Ask V2 is invoked directly only for evaluation; the user-visible feature switch is not changed.",
            f"- Cases: {metrics['case_count']}; worker pipeline: {metrics['worker_pipeline_pass_rate']:.2%}; expected memory type: {metrics['memory_type_pass_rate']:.2%}; answer evidence: {metrics['answer_substring_pass_rate']:.2%}; task-state: {metrics['task_state_pass_rate']:.2%}; end-to-end: {metrics['end_to_end_pass_rate']:.2%}.",
            "- `results.jsonl` retains receiver ids, stream ids, worker/inbox/memory snapshots, selected evidence and errors. Semantic-profile projection is retained as a diagnostic background task, but does not gate the Ingest -> Memory -> Ask chain. The generated PostgreSQL tenant is deleted after artifact writing.", "",
        ]), encoding="utf-8")
        print(json.dumps(metrics, ensure_ascii=False, indent=2))
    finally:
        if cleanup_allowed:
            with session_scope() as session:
                task_ids = list(session.execute(select(Task.id).where(Task.tenant_id == tenant)).scalars())
                if task_ids:
                    session.execute(delete(OutboxEvent).where(OutboxEvent.aggregate_type == "task", OutboxEvent.aggregate_id.in_(task_ids)))
                session.execute(delete(Space).where(Space.tenant_id == tenant))


if __name__ == "__main__":
    main()
