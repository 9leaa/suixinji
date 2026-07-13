"""Background worker for classifying notes and writing them to storage."""

from __future__ import annotations

import logging
from typing import Any

from core.classifier import classify_text
from core.observability import observe
from core.wal import load_pending_records, mark_processed
from storage.note_storage import NoteMetadata, load_index, note_exists, save_note
from core.llm_client import embed_text
from storage.vector_store import (
    VectorItem,
    add_vector_item,
    search_related_note_ids,
    vector_item_exists,
)

LOGGER = logging.getLogger(__name__)


def _find_note_by_message_id(space_id: str, message_id: str) -> dict[str, Any] | None:
    for note in load_index(space_id):
        if note.get("message_id") == message_id:
            return note
    return None


def backfill_vector_if_missing(space_id: str, message_id: str) -> bool:
    """补写已存在笔记缺失的向量记录。

    如果 worker 在 save_note 成功后、add_vector_item 成功前崩溃，
    重跑时 index.json 已经有笔记，但 vectors/index.json 可能缺记录。
    这个函数用 index.json 中的笔记内容补写向量，恢复 semantic_search 能力。
    """
    note = _find_note_by_message_id(space_id, message_id)
    if note is None:
        return False

    note_id = str(note.get("id") or "")
    if not note_id:
        return False

    if vector_item_exists(space_id, note_id, message_id):
        return False

    text = str(note.get("text") or "").strip()
    if not text:
        return False

    with observe(
        "worker.write_vector",
        space_id=space_id,
        message_id=message_id,
        record_id=note_id,
        extra={"note_id": note_id, "reason": "backfill"},
    ):
        embedding = embed_text(text)
        return add_vector_item(
            space_id,
            VectorItem(
                note_id=note_id,
                message_id=message_id,
                text=text,
                embedding=embedding,
                metadata={
                    "title": note.get("title"),
                    "tags": note.get("tags", []),
                    "type": note.get("type"),
                    "summary": note.get("summary"),
                    "ts": note.get("ts"),
                },
            ),
        )


def process_record(record: dict[str, Any]) -> None:
    """处理单条 pending WAL 记录。

    功能说明:
        调用分类器生成结构化信息，将结果保存为 markdown 笔记和 index.json 索引，
        最后把对应 WAL 记录标记为 processed。如果该 message_id 已经存在于
        index.json，则跳过重复保存并直接把 WAL 标记为 processed。

    传参说明:
        record: 从 WAL 中读取出的单条消息记录字典。

    返回类型说明:
        None: 处理完成后通过文件系统产生副作用，不返回业务结果。
    """
    space_id = record["space_id"]
    record_id = record["id"]
    message_id = record["message_id"]
    ctx = {"space_id": space_id, "message_id": message_id, "record_id": record_id}

    with observe("worker.process_record", **ctx):
        if note_exists(space_id, message_id):
            added_vector = backfill_vector_if_missing(space_id, message_id)
            LOGGER.info(
                "Skip WAL record because note already exists: space_id=%s message_id=%s record_id=%s backfilled_vector=%s",
                space_id,
                message_id,
                record_id,
                added_vector,
            )
            with observe("worker.mark_processed", extra={"reason": "note_exists", "backfilled_vector": added_vector}, **ctx):
                mark_processed(space_id, record_id)
            return

        with observe("worker.classify", **ctx):
            classification = classify_text(record["text"])

        embedding = embed_text(record["text"])

        related = search_related_note_ids(
            space_id,
            embedding,
            top_k=3,
            exclude_note_id=record_id,
            min_score=0.5,
        )

        note = NoteMetadata(
            id=record_id,
            message_id=message_id,
            space_id=space_id,
            ts=record["ts"],
            title=classification.title,
            tags=classification.tags,
            type=classification.type,
            summary=classification.summary,
            text=record["text"],
            related=related,
        )

        with observe("worker.write_note", extra={"note_id": record_id}, **ctx):
            save_note(note)

        with observe("worker.write_vector", extra={"note_id": record_id}, **ctx):
            add_vector_item(
                space_id,
                VectorItem(
                    note_id=record_id,
                    message_id=message_id,
                    text=record["text"],
                    embedding=embedding,
                    metadata={
                        "title": classification.title,
                        "tags": classification.tags,
                        "type": classification.type,
                        "summary": classification.summary,
                        "ts": record["ts"],
                    },
                ),
            )

        with observe("worker.mark_processed", **ctx):
            mark_processed(space_id, record_id)


def process_pending(space_id: str) -> int:
    """处理指定 space_id 下所有 pending WAL 记录。

    功能说明:
        读取当前 space_id 下所有 pending 记录，并逐条调用 process_record 完成分类、
        保存和状态更新。恢复过程中会按 message_id 跳过重复 pending 记录，
        且单条记录失败不会中断整个 space_id 的恢复。

    传参说明:
        space_id: 会话/用户隔离 ID。

    返回类型说明:
        int: 本次成功处理或成功跳过的 pending 记录数量。
    """
    records = load_pending_records(space_id)
    count = 0
    seen_message_ids: set[str] = set()

    for record in records:
        record_id = record.get("id")
        message_id = record.get("message_id")

        if message_id in seen_message_ids:
            LOGGER.warning(
                "Skip duplicate pending WAL record in same recovery batch: space_id=%s message_id=%s record_id=%s",
                space_id,
                message_id,
                record_id,
            )
            if record_id:
                with observe(
                    "worker.mark_processed",
                    space_id=space_id,
                    message_id=message_id,
                    record_id=record_id,
                    extra={"reason": "duplicate_pending"},
                ):
                    mark_processed(space_id, record_id)
                count += 1
            continue

        if message_id:
            seen_message_ids.add(message_id)

        try:
            process_record(record)
            count += 1
        except Exception:
            LOGGER.exception(
                "Failed to process pending WAL record: space_id=%s message_id=%s record_id=%s",
                space_id,
                message_id,
                record_id,
            )

    return count
