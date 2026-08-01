"""文件作用：用户反馈持久化。

项目关系：本文件依赖 `core.file_lock`；被 `bot.feishu_bot`。
"""



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
    """类功能：`FeedbackRecord` 封装与“用户反馈持久化”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    id: str
    ts: str
    space_id: str
    message_id: str | None
    text: str
    status: str = "open"


def feedback_path(space_id: str) -> Path:
    """函数功能：`feedback_path` 负责处理 feedback path，服务于本文件职责：用户反馈持久化。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `Path` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    FEEDBACK_DIR.mkdir(parents=True, exist_ok=True)
    return FEEDBACK_DIR / f"{safe_space_id(space_id)}.jsonl"


def create_feedback_record(
    *,
    space_id: str,
    text: str,
    message_id: str | None = None,
) -> FeedbackRecord:
    """函数功能：`create_feedback_record` 负责创建 feedback record，服务于本文件职责：用户反馈持久化。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        text: 输入文本内容，类型为 `str`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `FeedbackRecord` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    return FeedbackRecord(
        id=str(uuid.uuid4()),
        ts=datetime.now().astimezone().isoformat(),
        space_id=space_id,
        message_id=message_id,
        text=text.strip(),
        status="open",
    )


def append_feedback(record: FeedbackRecord) -> None:
    """函数功能：`append_feedback` 负责追加 feedback，服务于本文件职责：用户反馈持久化。
    传参：
        record: 待处理或持久化的记录对象，类型为 `FeedbackRecord`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
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
    """函数功能：`save_feedback` 负责保存 feedback，服务于本文件职责：用户反馈持久化。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        text: 输入文本内容，类型为 `str`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `FeedbackRecord` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    record = create_feedback_record(
        space_id=space_id,
        text=text,
        message_id=message_id,
    )
    append_feedback(record)
    return record


def list_feedback(space_id: str) -> list[dict]:
    """函数功能：`list_feedback` 负责列出 feedback，服务于本文件职责：用户反馈持久化。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `list[dict]`，表示按条件筛选、构造或查询得到的列表。
    """
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