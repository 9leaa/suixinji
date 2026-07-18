# 随心记 CI 时间相关测试失败修复方案

## 一、问题结论

当前 CI 失败不是覆盖率问题。

CI 已经显示：

```text
Required test coverage of 63% reached.
Total coverage: 70.09%
```

真正失败的是以下 3 个测试：

```text
tests/test_summary_reconciliation.py::test_failed_delivery_allows_scheduler_to_submit_again

tests/test_summary_reconciliation.py::test_expired_reserved_auto_summary_can_be_submitted_again

tests/test_summary_scheduler_resilience.py::test_one_subscription_reconcile_failure_does_not_block_other_subscription
```

它们共同表现为：

```text
预期 count == 1
实际 count == 0
```

---

# 二、根因

## 2.1 Scheduler 会判断当前时间是否已经到达订阅时间

当前自动总结调度逻辑：

```python
def _is_due(sub, now):
    today = now.date().isoformat()

    if sub.last_sent_date == today:
        return False

    now_minutes = now.hour * 60 + now.minute

    return now_minutes >= _minutes(sub.time)
```

只有满足：

```text
当前时间 >= 订阅发送时间
```

Scheduler 才会提交自动总结任务。

---

## 2.2 自动总结默认发送时间是 22:00

当前默认配置：

```python
SUMMARY_DEFAULT_TIME = "22:00"
```

测试中只调用：

```python
subscription.enable_summary_subscription(
    "space1",
    "chat1",
)
```

没有修改发送时间。

因此测试创建出的订阅默认是：

```text
time = 22:00
```

---

## 2.3 CI Runner 运行时通常还没到 22:00

GitHub Actions 环境中的当前时间可能是 UTC，也可能与本地开发机时区不同。

如果测试运行时还没到 22:00：

```text
delivery 状态允许重试
→ Scheduler 继续检查 _is_due()
→ 当前时间未到 22:00
→ 不提交任务
→ count = 0
```

但测试断言：

```python
assert count == 1
```

因此失败。

---

# 三、三个失败测试的具体原因

## 3.1 failed delivery 测试

测试目标：

```text
failed delivery 应允许重新提交
```

当前实际流程：

```text
delivery = failed
→ reconciliation 返回允许重新提交
→ 订阅时间仍为 22:00
→ 当前时间未到
→ Scheduler 不提交
→ count = 0
```

问题不是 failed 状态处理错误，而是测试没有保证“发送时间已到”。

---

## 3.2 expired reserved 测试

测试目标：

```text
过期 reserved delivery 应允许重新提交
```

当前实际流程：

```text
reserved 已过期
→ reconciliation 将其视为可重试
→ 订阅时间仍为 22:00
→ 当前时间未到
→ Scheduler 不提交
→ count = 0
```

同样是测试时间条件不完整。

---

## 3.3 一个订阅失败不影响另一个订阅

测试目标：

```text
space_a 对账失败
不能阻止 space_b 正常提交
```

当前实际流程：

```text
space_a 对账失败
→ 异常被捕获
→ Scheduler 继续处理 space_b
→ space_b 默认时间为 22:00
→ 当前时间未到
→ 不提交
→ count = 0
```

这说明异常隔离本身可能已经正常工作，只是 `space_b` 没到发送时间。

---

# 四、推荐的最小修复

本次不需要修改业务逻辑。

只修改测试，明确把订阅时间设为：

```text
00:00
```

原因：

```text
一天中的任何正常运行时间都满足 当前时间 >= 00:00
```

这样测试不再依赖：

- CI 在几点运行。
- 本地开发机时区。
- GitHub Runner 时区。
- 夏令时。
- 运行测试时的真实时间。

---

# 五、修改文件一

文件：

```text
tests/test_summary_reconciliation.py
```

## 5.1 修改 failed delivery 测试

找到：

```python
subscription.enable_summary_subscription(
    "space1",
    "chat1",
)
```

后面增加：

```python
subscription.update_summary_time(
    "space1",
    "chat1",
    "00:00",
)
```

修改后的关键部分：

```python
def test_failed_delivery_allows_scheduler_to_submit_again(
    monkeypatch,
    tmp_path,
):
    isolate_subscription_file(monkeypatch, tmp_path)

    today = datetime.now().astimezone().date().isoformat()
    key = auto_summary_key("space1", "today", today)

    subscription.enable_summary_subscription(
        "space1",
        "chat1",
    )

    subscription.update_summary_time(
        "space1",
        "chat1",
        "00:00",
    )

    reserve_delivery(
        key,
        delivery_type="auto_summary",
        space_id="space1",
    )

    mark_failed(key, "send failed")

    submitted = []

    class FakeExecutor:
        def submit_summary(
            self,
            space_id,
            range_key,
            chat_id,
            message_id=None,
            on_success=None,
            delivery_key=None,
            delivery_type=None,
        ):
            submitted.append(
                (
                    space_id,
                    range_key,
                    chat_id,
                    delivery_key,
                    delivery_type,
                )
            )
            return create_task("summary", space_id, {})

    assert scheduler.run_summary_scheduler_once(
        lambda chat_id, text: True,
        executor=FakeExecutor(),
    ) == 1

    assert submitted == [
        (
            "space1",
            "today",
            "chat1",
            key,
            "auto_summary",
        )
    ]
```

---

## 5.2 修改 expired reserved 测试

在：

```python
subscription.enable_summary_subscription(
    "space1",
    "chat1",
)
```

后增加：

```python
subscription.update_summary_time(
    "space1",
    "chat1",
    "00:00",
)
```

修改后的关键部分：

```python
def test_expired_reserved_auto_summary_can_be_submitted_again(
    monkeypatch,
    tmp_path,
):
    isolate_subscription_file(monkeypatch, tmp_path)

    today = datetime.now().astimezone().date().isoformat()
    key = auto_summary_key("space1", "today", today)

    subscription.enable_summary_subscription(
        "space1",
        "chat1",
    )

    subscription.update_summary_time(
        "space1",
        "chat1",
        "00:00",
    )

    reserve_delivery(
        key,
        delivery_type="auto_summary",
        space_id="space1",
    )

    _patch_delivery(
        tmp_path,
        key,
        lease_expires_at=(
            datetime.now().astimezone()
            - timedelta(minutes=1)
        ).isoformat(),
    )

    submitted = []

    class FakeExecutor:
        def submit_summary(
            self,
            space_id,
            range_key,
            chat_id,
            message_id=None,
            on_success=None,
            delivery_key=None,
            delivery_type=None,
        ):
            submitted.append(
                (
                    space_id,
                    range_key,
                    chat_id,
                    delivery_key,
                    delivery_type,
                )
            )
            return create_task("summary", space_id, {})

    assert scheduler.run_summary_scheduler_once(
        lambda chat_id, text: True,
        executor=FakeExecutor(),
    ) == 1

    assert submitted == [
        (
            "space1",
            "today",
            "chat1",
            key,
            "auto_summary",
        )
    ]
```

---

# 六、修改文件二

文件：

```text
tests/test_summary_scheduler_resilience.py
```

修改测试：

```text
test_one_subscription_reconcile_failure_does_not_block_other_subscription
```

当前创建了两个订阅：

```python
subscription.enable_summary_subscription(
    "space_a",
    "chat_a",
)

subscription.enable_summary_subscription(
    "space_b",
    "chat_b",
)
```

在后面增加：

```python
subscription.update_summary_time(
    "space_a",
    "chat_a",
    "00:00",
)

subscription.update_summary_time(
    "space_b",
    "chat_b",
    "00:00",
)
```

修改后的关键部分：

```python
def test_one_subscription_reconcile_failure_does_not_block_other_subscription(
    monkeypatch,
    tmp_path,
):
    isolate_subscription_file(monkeypatch, tmp_path)

    today = datetime.now().astimezone().date().isoformat()
    key = auto_summary_key("space_a", "today", today)

    subscription.enable_summary_subscription(
        "space_a",
        "chat_a",
    )

    subscription.enable_summary_subscription(
        "space_b",
        "chat_b",
    )

    subscription.update_summary_time(
        "space_a",
        "chat_a",
        "00:00",
    )

    subscription.update_summary_time(
        "space_b",
        "chat_b",
        "00:00",
    )

    reserve_delivery(
        key,
        delivery_type="auto_summary",
        space_id="space_a",
    )

    mark_sent(key)

    def fail_for_space_a(space_id, day):
        if space_id == "space_a":
            raise RuntimeError("write failed")

        subscription.mark_summary_sent(
            space_id,
            day,
        )

    monkeypatch.setattr(
        reconciliation,
        "mark_summary_sent",
        fail_for_space_a,
    )

    submitted = []

    class FakeExecutor:
        def submit_summary(
            self,
            space_id,
            range_key,
            chat_id,
            message_id=None,
            on_success=None,
            delivery_key=None,
            delivery_type=None,
        ):
            submitted.append(
                (
                    space_id,
                    range_key,
                    chat_id,
                    delivery_key,
                    delivery_type,
                )
            )

            return create_task(
                "summary",
                space_id,
                {},
            )

    assert scheduler.run_summary_scheduler_once(
        lambda chat_id, text: True,
        executor=FakeExecutor(),
    ) == 1

    assert submitted == [
        (
            "space_b",
            "today",
            "chat_b",
            auto_summary_key(
                "space_b",
                "today",
                today,
            ),
            "auto_summary",
        )
    ]
```

---

# 七、更规范的长期方案

最小修复使用 `00:00` 已经可以解决当前 CI。

但长期更推荐让 Scheduler 支持注入固定时间。

## 7.1 修改 Scheduler 接口

当前：

```python
def run_summary_scheduler_once(
    send_text,
    executor=None,
) -> int:
    now = datetime.now().astimezone()
```

建议修改为：

```python
def run_summary_scheduler_once(
    send_text,
    executor=None,
    *,
    now: datetime | None = None,
) -> int:
    now = now or datetime.now().astimezone()
```

注意：

```text
参数名可以使用 now，也可以使用 current_time。
必须是 keyword-only，避免影响原有调用。
```

---

## 7.2 测试传入固定时间

例如：

```python
from datetime import datetime, timezone

fixed_now = datetime(
    2026,
    7,
    14,
    23,
    0,
    tzinfo=timezone.utc,
)

count = scheduler.run_summary_scheduler_once(
    lambda chat_id, text: True,
    executor=FakeExecutor(),
    now=fixed_now,
)
```

这样可以明确表达测试条件：

```text
当前固定时间为 23:00
订阅时间为 22:00
所以任务应当被提交
```

---

## 7.3 增加时间边界测试

建议补充：

```python
def test_summary_not_due_before_configured_time():
    ...
```

场景：

```text
当前时间：21:59
订阅时间：22:00
预期：不提交
```

以及：

```python
def test_summary_is_due_at_configured_time():
    ...
```

场景：

```text
当前时间：22:00
订阅时间：22:00
预期：提交
```

再增加：

```python
def test_summary_is_due_after_configured_time():
    ...
```

场景：

```text
当前时间：23:00
订阅时间：22:00
预期：提交
```

---

# 八、推荐实施顺序

```text
T1：先在 3 个失败测试中把订阅时间设成 00:00
T2：本地只运行这 3 个测试
T3：运行完整 pytest 和 coverage
T4：运行所有 dry-run 评测
T5：提交代码并重新触发 CI
T6：后续再考虑为 Scheduler 增加 now 参数
```

---

# 九、本地验证命令

## 9.1 只运行失败测试

```bash
python -m pytest   tests/test_summary_reconciliation.py::test_failed_delivery_allows_scheduler_to_submit_again   tests/test_summary_reconciliation.py::test_expired_reserved_auto_summary_can_be_submitted_again   tests/test_summary_scheduler_resilience.py::test_one_subscription_reconcile_failure_does_not_block_other_subscription   -q
```

预期：

```text
3 passed
```

---

## 9.2 完整测试

```bash
python -m pytest tests   --cov=.   --cov-report=term-missing   --cov-fail-under=63
```

预期：

```text
111 passed
Total coverage >= 63%
```

实际测试数量以后续仓库为准。

---

## 9.3 Ruff

```bash
python -m ruff check .
```

预期：

```text
All checks passed!
```

---

## 9.4 Dry-run

```bash
python eval/eval_classification.py --dry-run
python eval/eval_retrieval.py --dry-run
python eval/eval_summary.py --dry-run
python eval/eval_query_react.py --dry-run
python eval/eval_memory.py --dry-run
```

---

# 十、不要采用的错误修法

## 10.1 不要修改产品默认发送时间

不要把：

```python
SUMMARY_DEFAULT_TIME = "22:00"
```

改为：

```python
SUMMARY_DEFAULT_TIME = "00:00"
```

这会影响真实产品行为。

---

## 10.2 不要删除到期判断

不要删除：

```python
if not _is_due(sub, now):
    continue
```

否则自动总结可能在用户配置时间之前发送。

---

## 10.3 不要直接把断言改成 0

不要把：

```python
assert count == 1
```

修改为：

```python
assert count == 0
```

这会让测试不再验证：

```text
failed / expired delivery 可以重新提交
```

---

## 10.4 不要降低覆盖率门槛

当前覆盖率已经达到：

```text
70.09%
```

超过要求：

```text
63%
```

所以本次完全不需要修改：

```yaml
--cov-fail-under=63
```

---

# 十一、最终验收标准

```text
[ ] 三个失败测试都明确设置了到期时间
[ ] 测试不再依赖真实时钟
[ ] test_failed_delivery_allows_scheduler_to_submit_again 通过
[ ] test_expired_reserved_auto_summary_can_be_submitted_again 通过
[ ] test_one_subscription_reconcile_failure_does_not_block_other_subscription 通过
[ ] 完整 pytest 通过
[ ] 覆盖率仍高于 63%
[ ] Python 3.10 CI 通过
[ ] Python 3.11 CI 通过
[ ] Ruff 通过
[ ] 五条 dry-run 评测通过
```

---

# 十二、一句话总结

本次 CI 失败的本质是：

> 测试想验证“可以重新提交”，但没有保证“当前时间已经到发送时间”。

最小修复：

```text
在 3 个测试中把订阅时间设置为 00:00。
```

不需要修改业务逻辑，也不需要降低覆盖率门槛。
