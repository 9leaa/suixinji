"""文件作用：自动总结调度。

项目关系：本文件依赖 `runtime.task`、`summary`、`summary.subscription`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from datetime import datetime, timezone, timedelta
from summary import scheduler
from summary.subscription import SummarySubscription
from runtime.task import create_task

TZ = timezone(timedelta(hours=8))


def test_is_due_before_after_and_already_sent():
    """函数功能：`test_is_due_before_after_and_already_sent` 负责验证 is due before after and already sent 场景，服务于本文件职责：自动总结调度。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    sub = SummarySubscription(space_id="space1", chat_id="chat1", time="22:00")

    assert scheduler._is_due(sub, datetime(2026, 6, 7, 21, 59, tzinfo=TZ)) is False
    assert scheduler._is_due(sub, datetime(2026, 6, 7, 22, 0, tzinfo=TZ)) is True
    assert scheduler._is_due(sub, datetime(2026, 6, 7, 23, 30, tzinfo=TZ)) is True

    sent = SummarySubscription(
        space_id="space1",
        chat_id="chat1",
        time="22:00",
        last_sent_date="2026-06-07",
    )
    assert scheduler._is_due(sent, datetime(2026, 6, 7, 23, 30, tzinfo=TZ)) is False


def test_run_scheduler_once_sends_due_subscription(monkeypatch):
    """函数功能：`test_run_scheduler_once_sends_due_subscription` 负责验证 run scheduler once sends due subscription 场景，服务于本文件职责：自动总结调度。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    sub = SummarySubscription(space_id="space1", chat_id="chat1", time="00:00")
    marked = []
    submitted = []
    fixed_now = datetime(2026, 6, 7, 23, 0, tzinfo=TZ)

    monkeypatch.setattr(scheduler, "list_enabled_summary_subscriptions", lambda: [sub])
    monkeypatch.setattr(scheduler, "mark_summary_sent", lambda space_id, day: marked.append((space_id, day)))

    class FakeExecutor:
        """类功能：`FakeExecutor` 封装与“自动总结调度”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def submit_summary(self, space_id, range_key, chat_id, message_id=None, on_success=None, delivery_key=None, delivery_type=None):
            """函数功能：`FakeExecutor.submit_summary` 在类 `FakeExecutor` 中负责处理 submit summary，服务于本文件职责：自动总结调度。
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
            submitted.append((space_id, range_key, chat_id, message_id, delivery_key, delivery_type))
            if on_success is not None:
                on_success()
            return create_task("summary", space_id, {"range_key": range_key, "chat_id": chat_id})

    count = scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor(), now=fixed_now)

    assert count == 1
    today = fixed_now.date().isoformat()
    assert submitted == [("space1", "today", "chat1", None, f"auto_summary:space1:today:{today}", "auto_summary")]
    assert marked == [("space1", today)]


def test_run_scheduler_once_skips_before_configured_time(monkeypatch):
    """函数功能：`test_run_scheduler_once_skips_before_configured_time` 负责验证 run scheduler once skips before configured time 场景，服务于本文件职责：自动总结调度。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    sub = SummarySubscription(space_id="space1", chat_id="chat1", time="22:00")
    submitted = []

    monkeypatch.setattr(scheduler, "list_enabled_summary_subscriptions", lambda: [sub])

    class FakeExecutor:
        """类功能：`FakeExecutor` 封装与“自动总结调度”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def submit_summary(self, *args, **kwargs):
            """函数功能：`FakeExecutor.submit_summary` 在类 `FakeExecutor` 中负责处理 submit summary，服务于本文件职责：自动总结调度。
            传参：
                *args: args 参数，由调用方传入。
                **kwargs: kwargs 参数，由调用方传入。
            返回结果说明：
                无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
            """
            submitted.append((args, kwargs))
            raise AssertionError("should not submit before configured time")

    count = scheduler.run_summary_scheduler_once(
        lambda chat_id, text: True,
        executor=FakeExecutor(),
        now=datetime(2026, 6, 7, 21, 59, tzinfo=TZ),
    )

    assert count == 0
    assert submitted == []


def test_run_scheduler_once_sends_at_configured_time(monkeypatch):
    """函数功能：`test_run_scheduler_once_sends_at_configured_time` 负责验证 run scheduler once sends at configured time 场景，服务于本文件职责：自动总结调度。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    sub = SummarySubscription(space_id="space1", chat_id="chat1", time="22:00")
    submitted = []

    monkeypatch.setattr(scheduler, "list_enabled_summary_subscriptions", lambda: [sub])

    class FakeExecutor:
        """类功能：`FakeExecutor` 封装与“自动总结调度”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def submit_summary(self, space_id, range_key, chat_id, message_id=None, on_success=None, delivery_key=None, delivery_type=None):
            """函数功能：`FakeExecutor.submit_summary` 在类 `FakeExecutor` 中负责处理 submit summary，服务于本文件职责：自动总结调度。
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

    fixed_now = datetime(2026, 6, 7, 22, 0, tzinfo=TZ)
    count = scheduler.run_summary_scheduler_once(
        lambda chat_id, text: True,
        executor=FakeExecutor(),
        now=fixed_now,
    )

    assert count == 1
    assert submitted == [("space1", "today", "chat1", "auto_summary:space1:today:2026-06-07", "auto_summary")]


def test_run_scheduler_once_does_not_mark_when_task_is_rejected(monkeypatch):
    """函数功能：`test_run_scheduler_once_does_not_mark_when_task_is_rejected` 负责验证 run scheduler once does not mark when task is rejected 场景，服务于本文件职责：自动总结调度。
    传参：
        monkeypatch: monkeypatch 参数，由调用方传入。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    sub = SummarySubscription(space_id="space1", chat_id="chat1", time="00:00")
    marked = []

    monkeypatch.setattr(scheduler, "list_enabled_summary_subscriptions", lambda: [sub])
    monkeypatch.setattr(scheduler, "mark_summary_sent", lambda space_id, day: marked.append((space_id, day)))

    class FakeExecutor:
        """类功能：`FakeExecutor` 封装与“自动总结调度”相关的数据结构、状态或行为。
        传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
        返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
        """
        def submit_summary(self, space_id, range_key, chat_id, message_id=None, on_success=None, delivery_key=None, delivery_type=None):
            """函数功能：`FakeExecutor.submit_summary` 在类 `FakeExecutor` 中负责处理 submit summary，服务于本文件职责：自动总结调度。
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
            task = create_task("summary", space_id, {"range_key": range_key, "chat_id": chat_id}, status="rejected")
            task.error = "task queue is full"
            return task

    count = scheduler.run_summary_scheduler_once(lambda chat_id, text: True, executor=FakeExecutor())

    assert count == 0
    assert marked == []
