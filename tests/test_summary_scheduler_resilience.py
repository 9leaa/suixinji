"""文件作用：scheduler 异常隔离和恢复。

项目关系：本文件依赖 `runtime.delivery_store`、`runtime.task`、`summary`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from datetime import datetime, timezone

import pytest

from runtime.delivery_store import auto_summary_key, mark_sent, reserve_delivery
from runtime.task import create_task
from summary import reconciliation, scheduler, subscription

FIXED_NOW = datetime(2026, 7, 14, 23, 0, tzinfo=timezone.utc)


def isolate_subscription_file(monkeypatch, tmp_path):
    """函数功能：`isolate_subscription_file` 负责处理 isolate subscription file，服务于本文件职责：scheduler 异常隔离和恢复。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
        tmp_path: tmp path 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(subscription, "DATA_DIR", tmp_path)
    monkeypatch.setattr(subscription, "SUBSCRIPTIONS_PATH", tmp_path / "summary_subscriptions.json")


def test_reconcile_failure_skips_subscription_without_submitting(monkeypatch, tmp_path):
    """函数功能：`test_reconcile_failure_skips_subscription_without_submitting` 负责验证 reconcile failure skips subscription without submitting 场景，服务于本文件职责：scheduler 异常隔离和恢复。
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
    monkeypatch.setattr(reconciliation, "mark_summary_sent", lambda space_id, day: (_ for _ in ()).throw(RuntimeError("write failed")))
    submitted = []

    class FakeExecutor:
        """类功能：`FakeExecutor` 封装与“scheduler 异常隔离和恢复”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def submit_summary(self, *args, **kwargs):
            """函数功能：`FakeExecutor.submit_summary` 在类 `FakeExecutor` 中负责处理 submit summary，服务于本文件职责：scheduler 异常隔离和恢复。
            传参：
                *args: args 参数，由调用方传入。
                **kwargs: kwargs 参数，由调用方传入。
            返回结果说明：
                无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
            """
            submitted.append((args, kwargs))
            raise AssertionError("should not submit when sent delivery reconciliation fails")

    assert scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor()) == 0
    assert submitted == []
    assert subscription.get_summary_subscription("space1").last_sent_date is None


def test_next_tick_repairs_subscription_after_previous_reconcile_failure(monkeypatch, tmp_path):
    """函数功能：`test_next_tick_repairs_subscription_after_previous_reconcile_failure` 负责验证 next tick repairs subscription after previous reconcile failure 场景，服务于本文件职责：scheduler 异常隔离和恢复。
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
    calls = {"count": 0}

    def flaky_mark(space_id, day):
        """函数功能：`flaky_mark` 负责标记 flaky，服务于本文件职责：scheduler 异常隔离和恢复。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据。
            day: day 参数，由调用方传入。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("write failed")
        subscription.mark_summary_sent(space_id, day)

    monkeypatch.setattr(reconciliation, "mark_summary_sent", flaky_mark)

    class FakeExecutor:
        """类功能：`FakeExecutor` 封装与“scheduler 异常隔离和恢复”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def submit_summary(self, *args, **kwargs):
            """函数功能：`FakeExecutor.submit_summary` 在类 `FakeExecutor` 中负责处理 submit summary，服务于本文件职责：scheduler 异常隔离和恢复。
            传参：
                *args: args 参数，由调用方传入。
                **kwargs: kwargs 参数，由调用方传入。
            返回结果说明：
                无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
            """
            raise AssertionError("sent delivery reconciliation should not submit summary")

    assert scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor()) == 0
    assert subscription.get_summary_subscription("space1").last_sent_date is None
    assert scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor()) == 0
    assert subscription.get_summary_subscription("space1").last_sent_date == today
    assert calls["count"] == 2


def test_one_subscription_reconcile_failure_does_not_block_other_subscription(monkeypatch, tmp_path):
    """函数功能：`test_one_subscription_reconcile_failure_does_not_block_other_subscription` 负责验证 one subscription reconcile failure does not block other subscription 场景，服务于本文件职责：scheduler 异常隔离和恢复。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
        tmp_path: tmp path 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    isolate_subscription_file(monkeypatch, tmp_path)
    today = FIXED_NOW.date().isoformat()
    key = auto_summary_key("space_a", "today", today)
    subscription.enable_summary_subscription("space_a", "chat_a")
    subscription.enable_summary_subscription("space_b", "chat_b")
    subscription.update_summary_time("space_a", "chat_a", "00:00")
    subscription.update_summary_time("space_b", "chat_b", "00:00")
    reserve_delivery(key, delivery_type="auto_summary", space_id="space_a")
    mark_sent(key)

    def fail_for_space_a(space_id, day):
        """函数功能：`fail_for_space_a` 负责处理 fail for space a，服务于本文件职责：scheduler 异常隔离和恢复。
        传参：
            space_id: 业务空间标识，用于隔离不同会话或租户下的数据。
            day: day 参数，由调用方传入。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        if space_id == "space_a":
            raise RuntimeError("write failed")
        subscription.mark_summary_sent(space_id, day)

    monkeypatch.setattr(reconciliation, "mark_summary_sent", fail_for_space_a)
    submitted = []

    class FakeExecutor:
        """类功能：`FakeExecutor` 封装与“scheduler 异常隔离和恢复”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def submit_summary(self, space_id, range_key, chat_id, message_id=None, on_success=None, delivery_key=None, delivery_type=None):
            """函数功能：`FakeExecutor.submit_summary` 在类 `FakeExecutor` 中负责处理 submit summary，服务于本文件职责：scheduler 异常隔离和恢复。
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
    assert submitted == [("space_b", "today", "chat_b", auto_summary_key("space_b", "today", today), "auto_summary")]


def test_run_scheduler_tick_safely_catches_tick_failure(monkeypatch):
    """函数功能：`test_run_scheduler_tick_safely_catches_tick_failure` 负责验证 run scheduler tick safely catches tick failure 场景，服务于本文件职责：scheduler 异常隔离和恢复。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    calls = {"count": 0}

    def fake_run_once(send_text, executor=None):
        """函数功能：`fake_run_once` 负责运行 once，服务于本文件职责：scheduler 异常隔离和恢复。
        传参：
            send_text: send text 参数，由调用方传入。
            executor: executor 参数，由调用方传入，默认值为 `None`。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        calls["count"] += 1
        if calls["count"] == 1:
            raise RuntimeError("tick failed")

    monkeypatch.setattr(scheduler, "run_summary_scheduler_once", fake_run_once)

    scheduler.run_scheduler_tick_safely(lambda chat_id, text: True)
    scheduler.run_scheduler_tick_safely(lambda chat_id, text: True)

    assert calls["count"] == 2


def test_run_scheduler_tick_safely_survives_failure_logging_error(monkeypatch):
    """函数功能：`test_run_scheduler_tick_safely_survives_failure_logging_error` 负责验证 run scheduler tick safely survives failure logging error 场景，服务于本文件职责：scheduler 异常隔离和恢复。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    monkeypatch.setattr(scheduler, "run_summary_scheduler_once", lambda send_text, executor=None: (_ for _ in ()).throw(RuntimeError("tick failed")))
    monkeypatch.setattr(scheduler, "log_event", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("log failed")))

    try:
        scheduler.run_scheduler_tick_safely(lambda chat_id, text: True)
    except Exception as exc:
        pytest.fail(f"safe tick should not raise, got {exc!r}")
