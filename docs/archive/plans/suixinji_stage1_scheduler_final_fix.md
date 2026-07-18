# 随心记第一阶段最终收口方案

## 1. 修正目标

当前两项核心必修已经完成：

- 自动总结 delivery 与 subscription 状态对账。
- `reserved` delivery 租约、过期恢复和最大尝试次数。

本次只处理最后一个收口问题：

> 自动总结对账异常不能导致整个 Scheduler 后台线程退出。

同时补齐真实的异常恢复测试，确保某个订阅失败不会影响其他订阅。

---

# 一、当前问题

## 1.1 对账调用缺少订阅级异常隔离

当前 Scheduler 的逻辑类似：

```python
for sub in subscriptions:
    if reconcile_auto_summary_delivery(
        sub.space_id,
        sub.range_key,
        today,
    ):
        continue

    if not _is_due(sub, now):
        continue

    try:
        submit_summary(...)
    except Exception:
        ...
```

问题是：

```python
reconcile_auto_summary_delivery(...)
```

位于 `try` 外部。

如果对账过程中发生以下异常：

- `mark_summary_sent()` 写文件失败。
- DeliveryStore 文件损坏。
- Subscription 文件读取失败。
- 日志写入异常。
- 其他未预期异常。

异常会直接中断当前 `run_summary_scheduler_once()`。

结果：

- 当前 tick 后续订阅不再执行。
- 一个用户的异常会影响其他用户。
- 异常继续向外传播。

---

## 1.2 Scheduler 主循环缺少总异常保护

当前后台线程类似：

```python
while True:
    run_summary_scheduler_once(
        send_text,
        executor=executor,
    )
    time.sleep(interval_seconds)
```

如果 `run_summary_scheduler_once()` 抛出异常：

```text
Scheduler 后台线程直接退出
```

之后即使程序仍在运行：

- 自动总结不再扫描。
- pending 状态无法通过后续 tick 自愈。
- 用户看不到明显错误，只能通过日志发现线程已经停止。

---

# 二、订阅级异常隔离

## 2.1 修改目标

每个订阅独立处理：

```text
订阅 A 对账失败
→ 记录错误
→ 跳过订阅 A
→ 继续处理订阅 B、C、D
```

任何单个 `space_id` 的异常都不能中断整个 Scheduler tick。

---

## 2.2 修改 `summary/scheduler.py`

将每个订阅的完整处理流程放入 `try`：

```python
for sub in subscriptions:
    trigger_start = time.perf_counter()
    ctx = {"space_id": sub.space_id}
    range_key = sub.range_key

    try:
        if reconcile_auto_summary_delivery(
            sub.space_id,
            range_key,
            today,
        ):
            continue

        if not _is_due(sub, now):
            continue

        log_event(
            "summary.auto.trigger",
            status="start",
            **ctx,
            extra={
                "range_key": range_key,
                "time": sub.time,
            },
        )

        if executor is None:
            from runtime.executor import get_task_executor
            executor = get_task_executor(send_text)

        def on_success(
            space_id: str = sub.space_id,
            sent_date: str = today,
            success_ctx: dict[str, str] = dict(ctx),
            success_range_key: str = range_key,
        ) -> None:
            mark_summary_sent(space_id, sent_date)
            log_event(
                "summary.auto.send",
                status="success",
                **success_ctx,
                extra={"range_key": success_range_key},
            )

        task = executor.submit_summary(
            sub.space_id,
            range_key,
            sub.chat_id,
            on_success=on_success,
            delivery_key=auto_summary_key(
                sub.space_id,
                range_key,
                today,
            ),
            delivery_type="auto_summary",
        )

        if task.status == TASK_REJECTED:
            log_event(
                "summary.auto.trigger",
                level="error",
                status="rejected",
                duration_ms=int(
                    (time.perf_counter() - trigger_start) * 1000
                ),
                error=task.error or "summary task rejected",
                **ctx,
                extra={
                    "range_key": range_key,
                    "task_id": task.id,
                },
            )
            continue

        count += 1
        log_event(
            "summary.auto.trigger",
            status="success",
            duration_ms=int(
                (time.perf_counter() - trigger_start) * 1000
            ),
            **ctx,
            extra={
                "range_key": range_key,
                "task_id": task.id,
            },
        )

    except Exception as exc:
        LOGGER.exception(
            "Failed to process summary subscription: space_id=%s",
            sub.space_id,
        )
        log_event(
            "summary.scheduler.subscription",
            level="error",
            status="failed",
            duration_ms=int(
                (time.perf_counter() - trigger_start) * 1000
            ),
            space_id=sub.space_id,
            error=f"{type(exc).__name__}: {exc}",
            extra={
                "range_key": range_key,
                "time": sub.time,
            },
        )
        continue
```

---

## 2.3 关键原则

订阅级 `try` 必须覆盖：

```text
delivery 对账
→ 到期判断
→ task executor 获取
→ summary task 提交
→ task 状态记录
```

不能只包住 `submit_summary()`。

---

## 2.4 日志要求

新增或统一使用 action：

```text
summary.scheduler.subscription
```

字段：

```json
{
  "action": "summary.scheduler.subscription",
  "status": "failed",
  "space_id": "space_xxx",
  "duration_ms": 12,
  "error": "RuntimeError: write subscription failed",
  "extra": {
    "range_key": "today",
    "time": "22:00"
  }
}
```

日志不能记录完整总结正文或敏感用户内容。

---

# 三、Scheduler 主循环总异常保护

## 3.1 修改目标

即使整个 tick 因公共步骤异常失败，Scheduler 线程也必须继续运行。

例如：

- 订阅文件整体读取失败。
- JSON 文件暂时损坏。
- 公共日志写入异常。
- 未被订阅级处理捕获的程序错误。

---

## 3.2 修改 `start_summary_scheduler()`

推荐实现：

```python
def loop() -> None:
    LOGGER.info("P4 summary scheduler started")

    while True:
        tick_started = time.perf_counter()

        try:
            run_summary_scheduler_once(
                send_text,
                executor=executor,
            )
        except Exception as exc:
            LOGGER.exception("Summary scheduler tick failed")
            try:
                log_event(
                    "summary.scheduler.tick",
                    level="error",
                    status="failed",
                    duration_ms=int(
                        (time.perf_counter() - tick_started) * 1000
                    ),
                    error=f"{type(exc).__name__}: {exc}",
                )
            except Exception:
                LOGGER.exception(
                    "Failed to write scheduler failure log"
                )
        finally:
            time.sleep(interval_seconds)
```

---

## 3.3 为什么日志还需要二次保护

如果异常本身由日志系统导致：

```text
log_event() 抛异常
```

此时再次调用 `log_event()` 可能重复失败。

所以：

```python
try:
    log_event(...)
except Exception:
    LOGGER.exception(...)
```

确保 Scheduler 不会因错误报告失败而退出。

---

## 3.4 循环行为要求

每次 tick 无论成功失败，都必须进入：

```python
time.sleep(interval_seconds)
```

避免异常后立即高速循环，产生：

- CPU 空转。
- 日志刷屏。
- 文件持续读写。
- 外部 API 高频调用。

---

# 四、修正测试

## 4.1 当前测试缺口

现有测试只验证了正常情况下：

```text
delivery = sent
→ reconcile
→ last_sent_date 被补写
```

但没有真实模拟：

```text
第一次 mark_summary_sent() 失败
→ Scheduler 不退出
→ 下一轮恢复正常
→ 自动完成对账
```

---

## 4.2 新增测试文件

建议新增：

```text
tests/test_summary_scheduler_resilience.py
```

也可以扩展：

```text
tests/test_summary_reconciliation.py
```

---

# 五、测试场景一：对账失败不提交总结

## 5.1 场景

已有状态：

```text
delivery = sent
last_sent_date = None
```

模拟：

```python
mark_summary_sent()
```

第一次抛出异常。

---

## 5.2 断言

第一次 tick：

```text
不调用 generate_summary
不提交 summary task
不发送消息
记录 subscription error
run_summary_scheduler_once 正常返回
```

---

# 六、测试场景二：下一轮自动恢复

## 6.1 场景

第一次 tick：

```text
delivery = sent
mark_summary_sent() 失败
```

第二次 tick：

```text
mark_summary_sent() 恢复正常
```

---

## 6.2 断言

第二次 tick：

```text
成功补写 last_sent_date
不调用 LLM
不生成总结
不发送消息
不提交 summary task
```

---

# 七、测试场景三：一个订阅失败不影响其他订阅

## 7.1 场景

存在两个订阅：

```text
space_a
space_b
```

其中：

```text
space_a 对账抛异常
space_b 已到发送时间且状态正常
```

---

## 7.2 断言

```text
space_a 记录失败
space_b 正常提交 summary task
Scheduler tick 返回 count = 1
```

---

# 八、测试场景四：主循环异常后仍继续下一轮

## 8.1 建议重构

为了方便测试，将单轮安全执行抽出：

```python
def run_scheduler_tick_safely(
    send_text,
    executor,
) -> None:
    try:
        run_summary_scheduler_once(
            send_text,
            executor=executor,
        )
    except Exception:
        LOGGER.exception("Summary scheduler tick failed")
```

循环调用：

```python
while True:
    run_scheduler_tick_safely(send_text, executor)
    time.sleep(interval_seconds)
```

---

## 8.2 测试

模拟：

```text
第一次 tick 抛异常
第二次 tick 成功
```

断言：

```text
run_scheduler_tick_safely 第一次不向外抛异常
第二次仍然被调用
```

---

# 九、建议修改文件

```text
summary/scheduler.py
tests/test_summary_reconciliation.py
tests/test_summary_scheduler_resilience.py

README.md
DESIGN.md
```

README 和 DESIGN 只需要补充一句：

```text
Scheduler 对每个订阅进行异常隔离，并在 tick 层提供总异常保护；单个订阅或单次 tick 失败不会导致后台线程退出。
```

---

# 十、实施顺序

```text
S1：将 reconcile 和提交逻辑全部放入订阅级 try
S2：增加 summary.scheduler.subscription 错误日志
S3：为 Scheduler tick 增加总异常保护
S4：抽取 run_scheduler_tick_safely，便于测试
S5：补充首次失败、下一轮恢复测试
S6：补充一个订阅失败不影响其他订阅测试
S7：同步 README 和 DESIGN
S8：运行 Ruff、pytest 和 dry-run
```

---

# 十一、最终验收标准

```text
[ ] 对账异常不会逃出单个订阅处理流程
[ ] 一个订阅失败不会阻塞其他订阅
[ ] run_summary_scheduler_once 不因单个订阅异常整体失败
[ ] Scheduler 后台线程不会因一次 tick 异常永久退出
[ ] delivery=sent 时不会重新生成总结
[ ] delivery=sent 时不会重复发送消息
[ ] 第一次订阅状态更新失败后，下一轮可以自动修复
[ ] 对账恢复过程不调用 LLM
[ ] 对账恢复过程不提交 summary task
[ ] 异常日志不包含完整用户内容
[ ] 新增测试全部通过
[ ] Ruff、pytest、四条 dry-run 全部通过
```

完成以上内容后，第一阶段代码可以正式封板并进入 Memory V2。
