"""Feedback storage for dogfooding and evaluation improvement."""

from __future__ import annotations

import json
import uuid
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path

from core.file_lock import locked_space, safe_space_id

DATA_DIR = Path("data")
FEEDBACK_DIR = DATA_DIR / "feedback"


@dataclass
class FeedbackRecord:
    id: str
    ts: str
    space_id: str
    message_id: str | None
    text: str
    status: str = "open"


def feedback_path(space_id: str) -> Path:
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    return FEEDBACK_DIR / f"{safe_space_id(space_id)}.jsonl"


def create_feedback_record(
    *,
    space_id: str,
    text: str,
    message_id: str | None = None,
) -> FeedbackRecord:
    return FeedbackRecord(
        id=str(uuid.uuid4()),
        ts=datetime.now().astimezone().isoformat(),
        space_id=space_id,
        message_id=message_id,
        text=text.strip(),
        status="open",
    )


def append_feedback(record: FeedbackRecord) -> None:
    path = feedback_path(record.space_id)
    with locked_space(record.space_id):
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(asdict(record), ensure_ascii=False) + "\n")


def save_feedback(
    *,
    space_id: str,
    text: str,
    message_id: str | None = None,
) -> FeedbackRecord:
    record = create_feedback_record(
        space_id=space_id,
        text=text,
        message_id=message_id,
    )
    append_feedback(record)
    return record


def list_feedback(space_id: str) -> list[dict]:
    path = feedback_path(space_id)
    if not path.exists():
        return []

    records = []
    with locked_space(space_id):
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(json.loads(line))
    return records