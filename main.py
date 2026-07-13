"""Application entry point for starting the Telegram bot and background worker."""


from __future__ import annotations

import argparse
import time

from core.wal import append_message_once, create_pending_record
from core.worker import process_pending


DEFAULT_SPACE_ID = "p_local_demo"


def ingest_local_message(text: str, space_id: str = DEFAULT_SPACE_ID) -> None:
    message_id = f"local_{int(time.time() * 1000)}"

    record = create_pending_record(
        message_id=message_id,
        space_id=space_id,
        text=text,
        event_id=None,
        chat_id=None,
        chat_type="local",
        sender={"source": "cli"},
    )

    appended = append_message_once(record)
    if not appended:
        print("消息已存在，跳过。")
        return

    print(f"已写入 WAL：{message_id}")

    count = process_pending(space_id)
    print(f"已处理 pending 消息：{count}")


def main() -> None:
    parser = argparse.ArgumentParser(description="随心记 Agent 本地模拟入口")
    parser.add_argument("text", help="要记录的一句话")
    parser.add_argument("--space-id", default=DEFAULT_SPACE_ID)

    args = parser.parse_args()
    ingest_local_message(args.text, args.space_id)


if __name__ == "__main__":
    main()
