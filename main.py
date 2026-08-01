"""文件作用：本地命令行记录入口。

项目关系：本文件依赖 `core.wal`、`core.worker`；被 暂无静态导入方或仅作为入口脚本执行。
"""




from __future__ import annotations

import argparse
import time

from core.wal import append_message_once, create_pending_record
from core.worker import process_pending


DEFAULT_SPACE_ID = "p_local_demo"


def ingest_local_message(text: str, space_id: str = DEFAULT_SPACE_ID) -> None:
    """函数功能：`ingest_local_message` 负责处理 ingest local message，服务于本文件职责：本地命令行记录入口。
    传参：
        text: 输入文本内容，类型为 `str`。
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`，默认值为 `DEFAULT_SPACE_ID`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：本地命令行记录入口。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    parser = argparse.ArgumentParser(description="随心记 Agent 本地模拟入口")
    parser.add_argument("text", help="要记录的一句话")
    parser.add_argument("--space-id", default=DEFAULT_SPACE_ID)

    args = parser.parse_args()
    ingest_local_message(args.text, args.space_id)


if __name__ == "__main__":
    main()
