"""文件作用：总结发送状态对账。

项目关系：本文件依赖 `runtime.delivery_store`、`runtime.task`、`summary`；被 暂无静态导入方或仅作为入口脚本执行。
"""


import json
from datetime import datetime, timedelta, timezone

from runtime.delivery_store import (
    auto_summary_key,
    mark_failed,
    mark_sent,
    mark_unknown,
    reserve_delivery,
)
from runtime.task import create_task
from summary import reconciliation, scheduler, subscription

FIXED_NOW = datetime(2026, 7, 14, 23, 0, tzinfo=timezone.utc)


def isolate_subscription_file(monkeypatch, tmp_path):
    """函数功能：`isolate_subscription_file` 负责处理 isolate subscription file，服务于本文件职责：总结发送状态对账。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
        tmp_path: tmp path 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(subscription, "DATA_DIR", tmp_path)
    monkeypatch.setattr(subscription, "SUBSCRIPTIONS_PATH", tmp_path / "summary_subscriptions.json")


def test_reconcile_sent_delivery_repairs_subscription_without_resend(monkeypatch, tmp_path):
    """函数功能：`test_reconcile_sent_delivery_repairs_subscription_without_resend` 负责验证 reconcile sent delivery repairs subscription without resend 场景，服务于本文件职责：总结发送状态对账。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
        tmp_path: tmp path 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    isolate_subscription_file(monkeypatch, tmp_path)
    today = datetime.now().astimezone().date().isoformat()
    key = auto_summary_key("space1", "today", today)
    subscription.enable_summary_subscription("space1", "chat1")
    reserve_delivery(key, delivery_type="auto_summary", space_id="space1")
    mark_sent(key)

    called = []

    class FakeExecutor:
        """类功能：`FakeExecutor` 封装与“总结发送状态对账”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def submit_summary(self, *args, **kwargs):
            """函数功能：`FakeExecutor.submit_summary` 在类 `FakeExecutor` 中负责处理 submit summary，服务于本文件职责：总结发送状态对账。
            传参：
                *args: args 参数，由调用方传入。
                **kwargs: kwargs 参数，由调用方传入。
            返回结果说明：
                无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
            """
            called.append((args, kwargs))
            raise AssertionError("should not submit summary when delivery is already sent")

    count = scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor())

    assert count == 0
    assert called == []
    assert subscription.get_summary_subscription("space1").last_sent_date == today


def test_sent_delivery_with_failed_subscription_update_is_repaired_next_tick(monkeypatch, tmp_path):
    """函数功能：`test_sent_delivery_with_failed_subscription_update_is_repaired_next_tick` 负责验证 sent delivery with failed subscription update is repaired next tick 场景，服务于本文件职责：总结发送状态对账。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
        tmp_path: tmp path 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    isolate_subscription_file(monkeypatch, tmp_path)
    today = datetime.now().astimezone().date().isoformat()
    key = auto_summary_key("space1", "today", today)
    subscription.enable_summary_subscription("space1", "chat1")
    reserve_delivery(key, delivery_type="auto_summary", space_id="space1")
    mark_sent(key)

    assert reconciliation.reconcile_auto_summary_delivery("space1", "today", today) is True
    assert subscription.get_summary_subscription("space1").last_sent_date == today


def test_unknown_delivery_skips_auto_summary(monkeypatch, tmp_path):
    """函数功能：`test_unknown_delivery_skips_auto_summary` 负责验证 unknown delivery skips auto summary 场景，服务于本文件职责：总结发送状态对账。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
        tmp_path: tmp path 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    isolate_subscription_file(monkeypatch, tmp_path)
    today = datetime.now().astimezone().date().isoformat()
    key = auto_summary_key("space1", "today", today)
    subscription.enable_summary_subscription("space1", "chat1")
    reserve_delivery(key, delivery_type="auto_summary", space_id="space1")
    mark_unknown(key, "timeout")

    called = []

    class FakeExecutor:
        """类功能：`FakeExecutor` 封装与“总结发送状态对账”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def submit_summary(self, *args, **kwargs):
            """函数功能：`FakeExecutor.submit_summary` 在类 `FakeExecutor` 中负责处理 submit summary，服务于本文件职责：总结发送状态对账。
            传参：
                *args: args 参数，由调用方传入。
                **kwargs: kwargs 参数，由调用方传入。
            返回结果说明：
                无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
            """
            called.append((args, kwargs))
            raise AssertionError("unknown delivery should not be resent automatically")

    assert scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor()) == 0
    assert called == []
    assert subscription.get_summary_subscription("space1").last_sent_date is None


def test_failed_delivery_allows_scheduler_to_submit_again(monkeypatch, tmp_path):
    """函数功能：`test_failed_delivery_allows_scheduler_to_submit_again` 负责验证 failed delivery allows scheduler to submit again 场景，服务于本文件职责：总结发送状态对账。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
        tmp_path: tmp path 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    isolate_subscription_file(monkeypatch, tmp_path)
    today = FIXED_NOW.date().isoformat()
    key = auto_summary_key("space1", "today", today)
    subscription.enable_summary_subscription("space1", "chat1")
    subscription.update_summary_time("space1", "chat1", "00:00")
    reserve_delivery(key, delivery_type="auto_summary", space_id="space1")
    mark_failed(key, "send failed")
    submitted = []

    class FakeExecutor:
        """类功能：`FakeExecutor` 封装与“总结发送状态对账”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def submit_summary(self, space_id, range_key, chat_id, message_id=None, on_success=None, delivery_key=None, delivery_type=None):
            """函数功能：`FakeExecutor.submit_summary` 在类 `FakeExecutor` 中负责处理 submit summary，服务于本文件职责：总结发送状态对账。
            传参：
                space_id: 业务空间标识，用于隔离不同会话或租户下的数据。
                range_key: range key 参数，由调用方传入。
                chat_id: chat id 参数，由调用方传入。
                message_id: 外部或本地消息标识，用于入口幂等和追踪，默认值为 `None`。
                on_success: on success 参数，由调用方传入，默认值为 `None`。
                delivery_key: delivery key 参数，由调用方传入，默认值为 `None`。
                delivery_type: delivery type 参数，由调用方传入，默认值为 `None`。
            返回结果说明：
                返回计算后的结果对象；具体类型取决于实际执行分支。
            """
            submitted.append((space_id, range_key, chat_id, delivery_key, delivery_type))
            return create_task("summary", space_id, {})

    assert scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor(), now=FIXED_NOW) == 1
    assert submitted == [("space1", "today", "chat1", key, "auto_summary")]


def test_expired_reserved_auto_summary_can_be_submitted_again(monkeypatch, tmp_path):
    """函数功能：`test_expired_reserved_auto_summary_can_be_submitted_again` 负责验证 expired reserved auto summary can be submitted again 场景，服务于本文件职责：总结发送状态对账。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
        tmp_path: tmp path 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    isolate_subscription_file(monkeypatch, tmp_path)
    today = FIXED_NOW.date().isoformat()
    key = auto_summary_key("space1", "today", today)
    subscription.enable_summary_subscription("space1", "chat1")
    subscription.update_summary_time("space1", "chat1", "00:00")
    reserve_delivery(key, delivery_type="auto_summary", space_id="space1")
    _patch_delivery(
        tmp_path,
        key,
        lease_expires_at=(datetime.now().astimezone() - timedelta(minutes=1)).isoformat(),
    )
    submitted = []

    class FakeExecutor:
        """类功能：`FakeExecutor` 封装与“总结发送状态对账”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def submit_summary(self, space_id, range_key, chat_id, message_id=None, on_success=None, delivery_key=None, delivery_type=None):
            """函数功能：`FakeExecutor.submit_summary` 在类 `FakeExecutor` 中负责处理 submit summary，服务于本文件职责：总结发送状态对账。
            传参：
                space_id: 业务空间标识，用于隔离不同会话或租户下的数据。
                range_key: range key 参数，由调用方传入。
                chat_id: chat id 参数，由调用方传入。
                message_id: 外部或本地消息标识，用于入口幂等和追踪，默认值为 `None`。
                on_success: on success 参数，由调用方传入，默认值为 `None`。
                delivery_key: delivery key 参数，由调用方传入，默认值为 `None`。
                delivery_type: delivery type 参数，由调用方传入，默认值为 `None`。
            返回结果说明：
                返回计算后的结果对象；具体类型取决于实际执行分支。
            """
            submitted.append((space_id, range_key, chat_id, delivery_key, delivery_type))
            return create_task("summary", space_id, {})

    assert scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor(), now=FIXED_NOW) == 1
    assert submitted == [("space1", "today", "chat1", key, "auto_summary")]


def _patch_delivery(tmp_path, key, **updates):
    """函数功能：`_patch_delivery` 负责处理 patch delivery，服务于本文件职责：总结发送状态对账。
    传参：
        tmp_path: tmp path 参数，由调用方传入。
        key: key 参数，由调用方传入。
        **updates: updates 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    path = tmp_path / "deliveries" / "index.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    raw[key].update(updates)
    path.write_text(json.dumps(raw, ensure_ascii=False, indent=2), encoding="utf-8")
