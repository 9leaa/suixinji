"""文件作用：重复飞书事件不重复回复。

项目关系：本文件依赖 `bot`、`repositories.postgres.dispatch`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from bot import feishu_bot
from repositories.postgres.dispatch import DispatchResult


def _message_event(text: str, message_id: str = "message-dup") -> SimpleNamespace:
    """函数功能：`_message_event` 负责处理 message event，服务于本文件职责：重复飞书事件不重复回复。
    传参：
        text: 输入文本内容，类型为 `str`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str`，默认值为 `'message-dup'`。
    返回结果说明：
        返回 `SimpleNamespace` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    sender_id = SimpleNamespace(open_id="open-test", user_id="user-test", union_id="union-test")
    sender = SimpleNamespace(sender_id=sender_id, sender_type="user", tenant_key="tenant-test")
    message = SimpleNamespace(
        message_type="text",
        chat_id="chat-test",
        chat_type="p2p",
        message_id=message_id,
        content=json.dumps({"text": text}, ensure_ascii=False),
        mentions=[],
    )
    return SimpleNamespace(
        header=SimpleNamespace(event_id=f"event-{message_id}"),
        event=SimpleNamespace(sender=sender, message=message),
    )


def _patch_duplicate_runtime(monkeypatch, result: DispatchResult) -> tuple[list[str], list[tuple[str, dict]]]:
    """函数功能：`_patch_duplicate_runtime` 负责处理 patch duplicate runtime，服务于本文件职责：重复飞书事件不重复回复。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
        result: 上游步骤返回的结果对象，类型为 `DispatchResult`。
    返回结果说明：
        返回 `tuple[list[str], list[tuple[str, dict]]]`，表示由多个相关值组成的结果。
    """
    sent: list[str] = []
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(feishu_bot, "TASK_QUEUE_BACKEND", "redis_streams")
    monkeypatch.setattr(feishu_bot, "safe_send_text", lambda _chat_id, text: sent.append(text) or True)
    monkeypatch.setattr(feishu_bot, "receive", lambda _command: result)
    monkeypatch.setattr(feishu_bot, "log_event", lambda action, **kwargs: events.append((action, kwargs)))
    return sent, events


@pytest.mark.parametrize(
    ("text", "task_type"),
    [
        ("今天记一笔", "ingest"),
        ("/ask 上次说的咖啡店在哪", "query"),
        ("/summary 今天", "summary"),
    ],
)
def test_duplicate_feishu_stream_events_are_silent(monkeypatch, text: str, task_type: str) -> None:
    """函数功能：`test_duplicate_feishu_stream_events_are_silent` 负责验证 duplicate feishu stream events are silent 场景，服务于本文件职责：重复飞书事件不重复回复。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
        text: 输入文本内容，类型为 `str`。
        task_type: task type 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    sent, events = _patch_duplicate_runtime(
        monkeypatch,
        DispatchResult("inbox-dup", None, False, True),
    )

    feishu_bot.handle_text_message(_message_event(text))

    assert sent == []
    duplicate_events = [item for item in events if item[0] == "feishu.message.duplicate"]
    assert duplicate_events
    assert duplicate_events[-1][1]["extra"]["task_type"] == task_type


def test_in_progress_feishu_stream_events_are_silent(monkeypatch) -> None:
    """函数功能：`test_in_progress_feishu_stream_events_are_silent` 负责验证 in progress feishu stream events are silent 场景，服务于本文件职责：重复飞书事件不重复回复。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    sent, events = _patch_duplicate_runtime(
        monkeypatch,
        DispatchResult("inbox-processing", None, False, False, True),
    )

    feishu_bot.handle_text_message(_message_event("/ask 现在有什么待办"))

    assert sent == []
    duplicate_events = [item for item in events if item[0] == "feishu.message.duplicate"]
    assert duplicate_events[-1][1]["status"] == "in_progress"


def test_first_ask_event_receives_before_visible_reply(monkeypatch) -> None:
    """函数功能：`test_first_ask_event_receives_before_visible_reply` 负责验证 first ask event receives before visible reply 场景，服务于本文件职责：重复飞书事件不重复回复。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    order: list[str] = []
    monkeypatch.setattr(feishu_bot, "TASK_QUEUE_BACKEND", "redis_streams")
    monkeypatch.setattr(feishu_bot, "log_event", lambda *_args, **_kwargs: None)

    def receive(_command):
        """函数功能：`receive` 负责接收，服务于本文件职责：重复飞书事件不重复回复。
        传参：
            _command:  command 参数，由调用方传入。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
        order.append("receive")
        return DispatchResult("inbox-first", "task-first", True, False)

    def send(_chat_id: str, text: str) -> bool:
        """函数功能：`send` 负责发送，服务于本文件职责：重复飞书事件不重复回复。
        传参：
            _chat_id:  chat id 参数，由调用方传入，类型为 `str`。
            text: 输入文本内容，类型为 `str`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        order.append(f"send:{text}")
        return True

    monkeypatch.setattr(feishu_bot, "receive", receive)
    monkeypatch.setattr(feishu_bot, "safe_send_text", send)

    feishu_bot.handle_text_message(_message_event("/ask 上次说的咖啡店在哪", "message-first"))

    assert order == ["receive", "send:我去翻一下随心记。"]


def test_local_duplicate_ingest_event_is_silent(monkeypatch) -> None:
    """函数功能：`test_local_duplicate_ingest_event_is_silent` 负责验证 local duplicate ingest event is silent 场景，服务于本文件职责：重复飞书事件不重复回复。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    sent: list[str] = []
    events: list[tuple[str, dict]] = []
    monkeypatch.setattr(feishu_bot, "TASK_QUEUE_BACKEND", "local")
    monkeypatch.setattr(feishu_bot, "safe_send_text", lambda _chat_id, text: sent.append(text) or True)
    monkeypatch.setattr(feishu_bot, "log_event", lambda action, **kwargs: events.append((action, kwargs)))
    monkeypatch.setattr(feishu_bot, "create_pending_record", lambda **kwargs: SimpleNamespace(id="record-local", **kwargs))
    monkeypatch.setattr(feishu_bot, "append_message_once", lambda _record: False)

    feishu_bot.handle_text_message(_message_event("今天记一笔", "message-local"))

    assert sent == []
    assert any(action == "feishu.message.duplicate" for action, _kwargs in events)


def test_unknown_slash_command_is_not_persisted_as_an_ingest(monkeypatch) -> None:
    """函数功能：`test_unknown_slash_command_is_not_persisted_as_an_ingest` 负责验证 unknown slash command is not persisted as an ingest 场景，服务于本文件职责：重复飞书事件不重复回复。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    sent: list[str] = []
    monkeypatch.setattr(feishu_bot, "TASK_QUEUE_BACKEND", "redis_streams")
    monkeypatch.setattr(feishu_bot, "safe_send_text", lambda _chat_id, text: sent.append(text) or True)
    monkeypatch.setattr(feishu_bot, "log_event", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(feishu_bot, "receive", lambda _command: (_ for _ in ()).throw(AssertionError("must not enqueue unknown command")))

    feishu_bot.handle_text_message(_message_event("/statu", "unknown-command"))

    assert sent and "未识别的命令" in sent[-1]
