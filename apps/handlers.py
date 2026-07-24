"""Business handlers used by independent Redis Stream workers."""

from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any

from agent.query_agent import answer_question
from bot.feishu_bot import send_text
from core.settings import FAKE_EXTERNALS
from core.worker import enrich_note, process_record
from infrastructure.redis_keys import KEYS
from infrastructure.redis_lock import coordinated_lock
from memory.service import process_note_memory
from memory.extractor import may_contain_memory
from repositories.postgres.dispatch import enqueue_task, load_inbox_record
from repositories.postgres.notes import find_note_content
from runtime.delivery_store import get_delivery, mark_failed, mark_sent, mark_unknown, reserve_delivery
from runtime.streams.worker import RetryLater, TaskOutcome
from storage.note_storage import find_note
from summary.daily_summary import generate_summary
from summary.subscription import mark_summary_sent


def _payload(task: dict[str, Any]) -> dict[str, Any]:
    return dict(task.get("payload_json") or {})


def _tenant_id(task: dict[str, Any]) -> str:
    return str(task.get("tenant_id") or "default")


def _enqueue_delivery(task: dict[str, Any], *, text: str, payload: dict[str, Any]) -> None:
    delivery_key = str(payload["delivery_key"])
    followup = {
        "chat_id": str(payload["chat_id"]),
        "text": text,
        "delivery_key": delivery_key,
        "delivery_type": str(payload.get("delivery_type") or "message"),
        "message_id": task.get("source_message_id"),
        "sent_date": payload.get("sent_date"),
    }
    enqueue_task(
        task_type="delivery",
        tenant_id=_tenant_id(task),
        space_id=str(task["space_id"]),
        source_message_id=task.get("source_message_id"),
        idempotency_key=f"delivery:{delivery_key}",
        payload=followup,
    )


def handle_ingest(task: dict[str, Any]) -> TaskOutcome:
    payload = _payload(task)
    inbox_id = str(payload.get("inbox_id") or "")
    record = load_inbox_record(inbox_id)
    if record is None:
        raise ValueError(f"inbox record not found: {inbox_id}")

    critical_task_id: str | None = None
    with coordinated_lock(KEYS.lock_space(_tenant_id(task), str(task["space_id"])), critical=True):
        note = process_record(record, defer_memory=True, defer_wal_completion=True)
        if note is not None:
            note_data = asdict(note) if is_dataclass(note) else dict(note)
            note_id = str(note_data["id"])
            classification = {
                "title": note_data.get("title"),
                "tags": list(note_data.get("tags") or []),
                "type": note_data.get("type"),
                "summary": note_data.get("summary"),
            }
            if not may_contain_memory(str(note_data.get("text") or ""), classification=classification):
                process_note_memory(note_data, classification=classification)
            else:
                critical_task_id, _ = enqueue_task(
                    task_type="memory",
                    tenant_id=_tenant_id(task),
                    space_id=str(task["space_id"]),
                    source_message_id=task.get("source_message_id"),
                    idempotency_key=f"memory:extract:{note_id}",
                    payload={
                        "operation": "extract",
                        "note_id": note_id,
                        "barrier_inbox_id": inbox_id,
                        "parent_task_id": str(task["id"]),
                        "sequence_no": int(payload.get("sequence_no") or 0),
                    },
                    initial_status="queued",
                    publish=True,
                )
            enqueue_task(
                task_type="enrichment",
                tenant_id=_tenant_id(task),
                space_id=str(task["space_id"]),
                source_message_id=task.get("source_message_id"),
                idempotency_key=f"enrichment:{note_id}",
                payload={"operation": "enrich", "note_id": note_id},
            )

    if payload.get("notify_on_success") and payload.get("chat_id"):
        delivery_key = f"ingest:{task['space_id']}:{task.get('source_message_id')}:archived"
        _enqueue_delivery(
            task,
            text="\u5df2\u6574\u7406\u5230\u968f\u5fc3\u8bb0\u3002",
            payload={"chat_id": payload["chat_id"], "delivery_key": delivery_key, "delivery_type": "ingest_archived"},
        )

    if critical_task_id:
        return TaskOutcome(note_ready_inbox_id=inbox_id)
    return TaskOutcome(ingest_complete_inbox_id=inbox_id)


def handle_query(task: dict[str, Any]) -> TaskOutcome:
    payload = _payload(task)
    inbox_id = str(payload.get("inbox_id") or "")
    if FAKE_EXTERNALS:
        answer = f"[stage4 fake answer] {str(payload['question'])[:120]}"
    else:
        answer = answer_question(
            str(task["space_id"]),
            str(payload["question"]),
            tenant_id=_tenant_id(task),
            user_id=str(payload.get("user_id") or "") or None,
            message_id=task.get("source_message_id"),
            task_id=str(task["id"]),
        )
    _enqueue_delivery(task, text=answer, payload=payload)
    return TaskOutcome(release_inbox_id=inbox_id)


def handle_summary(task: dict[str, Any]) -> TaskOutcome:
    payload = _payload(task)
    inbox_id = str(payload.get("inbox_id") or "")
    if FAKE_EXTERNALS:
        summary_text = f"[stage4 fake summary] range={str(payload['range_key'])}"
    else:
        result = generate_summary(
            str(task["space_id"]),
            str(payload["range_key"]),
            tenant_id=_tenant_id(task),
            user_id=str(payload.get("user_id") or "") or None,
            message_id=task.get("source_message_id"),
            task_id=str(task["id"]),
        )
        summary_text = result.markdown
    _enqueue_delivery(task, text=summary_text, payload=payload)
    return TaskOutcome(release_inbox_id=inbox_id)


def handle_memory(task: dict[str, Any]) -> TaskOutcome | None:
    payload = _payload(task)
    operation = str(payload.get("operation") or "extract")
    note_id = str(payload.get("note_id") or "")
    if operation == "consolidate":
        from memory.scheduler import run_memory_consolidation_once

        run_memory_consolidation_once(str(payload["cadence"]), space_ids=[str(task["space_id"])])
        return None
    if operation == "memory_embedding":
        return handle_memory_embedding(task)
    note = find_note_content(str(task["space_id"]), note_id)
    if note is None:
        raise ValueError(f"note not found: {note_id}")
    if operation == "enrich":
        handle_enrichment(task)
        return None
    process_note_memory(
        note,
        classification={
            "title": note.get("title"),
            "tags": list(note.get("tags") or []),
            "type": note.get("type"),
            "summary": note.get("summary"),
        },
    )
    barrier_inbox_id = str(payload.get("barrier_inbox_id") or "")
    return TaskOutcome(memory_ready_inbox_id=barrier_inbox_id or None)


def handle_memory_embedding(task: dict[str, Any]) -> TaskOutcome | None:
    from repositories.postgres.memory import claim_memory_vector, complete_memory_vector, fail_memory_vector

    payload = _payload(task)
    memory_id = str(payload.get("memory_id") or "")
    expected_hash = str(payload.get("content_hash") or "") or None
    claim = claim_memory_vector(memory_id, expected_hash=expected_hash)
    if claim is None:
        return None
    try:
        if FAKE_EXTERNALS:
            embedding = [0.0] * int(claim["dimension"])
        else:
            from core.llm_client import embed_text

            embedding = embed_text(str(claim["text"]))
        if len(embedding) != int(claim["dimension"]):
            raise ValueError(
                f"memory embedding dimension mismatch: expected {claim['dimension']}, got {len(embedding)}"
            )
        complete_memory_vector(
            memory_id,
            content_hash=str(claim["content_hash"]),
            embedding=embedding,
            model=str(claim["model"]),
            dimension=int(claim["dimension"]),
            embedding_version=str(claim["embedding_version"]),
        )
    except Exception as exc:
        fail_memory_vector(memory_id, content_hash=str(claim["content_hash"]), error=f"{type(exc).__name__}: {exc}")
        raise
    return None


def handle_enrichment(task: dict[str, Any]) -> None:
    if FAKE_EXTERNALS:
        return
    payload = _payload(task)
    note_id = str(payload.get("note_id") or "")
    note = find_note(str(task["space_id"]), note_id)
    if note is None:
        raise ValueError(f"note not found: {note_id}")
    with coordinated_lock(KEYS.lock_space(_tenant_id(task), str(task["space_id"])), critical=True):
        enrich_note(str(task["space_id"]), note_id)


def handle_delivery(task: dict[str, Any]) -> None:
    payload = _payload(task)
    key = str(payload["delivery_key"])
    reservation = reserve_delivery(
        key,
        delivery_type=str(payload.get("delivery_type") or "message"),
        tenant_id=_tenant_id(task),
        space_id=str(task["space_id"]),
        message_id=payload.get("message_id"),
    )
    if reservation is None:
        existing = get_delivery(key)
        if existing is not None and existing.status in {"sent", "unknown"}:
            return
        raise RetryLater("delivery is already reserved", delay_seconds=1.0)
    if FAKE_EXTERNALS:
        mark_sent(key)
        if payload.get("delivery_type") == "auto_summary" and payload.get("sent_date"):
            mark_summary_sent(str(task["space_id"]), str(payload["sent_date"]))
        return
    try:
        send_text(str(payload["chat_id"]), str(payload["text"]))
    except TimeoutError as exc:
        mark_unknown(key, type(exc).__name__)
        return
    except Exception as exc:
        mark_failed(key, f"{type(exc).__name__}: {exc}")
        raise
    mark_sent(key)
    if payload.get("delivery_type") == "auto_summary" and payload.get("sent_date"):
        mark_summary_sent(str(task["space_id"]), str(payload["sent_date"]))


HANDLERS = {
    "ingest": handle_ingest,
    "query": handle_query,
    "summary": handle_summary,
    "memory": handle_memory,
    "memory_embedding": handle_memory,
    "enrichment": handle_enrichment,
    "delivery": handle_delivery,
}
