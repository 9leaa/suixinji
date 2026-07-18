# 随心记 Memory V2 最终收口方案

## 一、本次目标

当前 Memory V2 第二轮加固已经基本完成：

```text
✅ memory_extraction_states
✅ SQLite WAL / busy_timeout / locked retry
✅ memory_consolidation_runs
✅ Memory 检索最低分数
✅ Worker 失败恢复
✅ 文档和测试更新
```

本次只修最后三个问题：

```text
1. consolidation 失败后，同一进程当天不会自动重试
2. 一条坏笔记可能阻塞后续所有笔记
3. 手动 /memory consolidate 绕过持久化幂等
```

完成后，Memory V2 第二轮加固可以正式封板。

---

# 二、问题一：Consolidation 失败后当天不会自动重试

## 2.1 当前问题

当前 Scheduler Tick 类似：

```python
for cadence in due_cadences(current_day, state):
    report = run_memory_consolidation_once(
        cadence,
        today=current_day,
    )

    state[cadence] = current_day.isoformat()
    reports.append(report)
```

无论执行结果是：

```text
completed
failed
skipped
```

都会写入：

```python
state[cadence] = current_day.isoformat()
```

因此可能出现：

```text
daily consolidation 执行
→ 某个 space 失败
→ 数据库记录 failed
→ 内存仍认为今天已经运行
→ 本进程当天不再重试
```

虽然数据库允许 failed run 重试，但外层 Scheduler 不再调用它。

---

# 三、P1：修正 Scheduler 同日重试

## 3.1 增加报告成功判断

文件：

```text
memory/scheduler.py
```

增加：

```python
def _report_has_failures(
    report: dict[str, Any],
) -> bool:
    return any(
        item.get("status") == "failed"
        for item in report.get("results", [])
    )
```

增加：

```python
def _report_is_complete(
    report: dict[str, Any],
) -> bool:
    results = report.get("results", [])

    if not results:
        return True

    return not _report_has_failures(report)
```

说明：

```text
全部 completed → 本 cadence 今天完成
全部 skipped → 本 cadence 今天已经完成或正在执行
存在 failed → 本 cadence 今天未完成
```

---

## 3.2 修改 `run_memory_scheduler_tick`

推荐：

```python
def run_memory_scheduler_tick(
    last_run_dates: dict[str, str] | None = None,
    *,
    today: date | None = None,
) -> dict[str, Any]:
    state = (
        last_run_dates
        if last_run_dates is not None
        else {}
    )

    current_day = today or date.today()
    reports = []

    for cadence in due_cadences(
        current_day,
        state,
    ):
        report = run_memory_consolidation_once(
            cadence,
            today=current_day,
        )

        reports.append(report)

        if _report_is_complete(report):
            state[cadence] = (
                current_day.isoformat()
            )
        else:
            LOGGER.warning(
                "Memory consolidation cadence remains "
                "retryable: cadence=%s date=%s",
                cadence,
                current_day.isoformat(),
            )

    return {
        "date": current_day.isoformat(),
        "ran": [
            report["cadence"]
            for report in reports
        ],
        "reports": reports,
    }
```

---

## 3.3 期望行为

第一次 tick：

```text
space_a completed
space_b failed
→ 不记录 daily 已完成
```

下一次 tick：

```text
再次进入 daily
→ space_a 的 completed run 被数据库跳过
→ space_b 的 failed run 重新 reserve
→ 只重试 space_b
```

这样同时利用：

```text
Scheduler 同日重试
+
数据库逐 space 幂等
```

---

# 四、问题二：一条坏笔记阻塞后续笔记

## 4.1 当前问题

当前 daily consolidation：

```python
for note in notes:
    report = process_note_memory(note)
    processed.append(...)
```

如果某一条笔记抛异常：

```text
note 1 成功
note 2 失败
note 3～100 不再执行
```

整个 consolidation run 被标记 failed。

下一次重试时，如果坏笔记仍失败：

```text
它会再次阻塞后续笔记
```

---

# 五、P2：增加笔记级异常隔离

文件：

```text
memory/consolidator.py
```

## 5.1 推荐返回结构

当前返回：

```python
{
    "processed": [...],
    "processed_count": ...,
    "skipped_count": ...,
}
```

改为：

```python
{
    "space_id": space_id,
    "processed": processed,
    "failed": failed,
    "processed_count": len(processed),
    "failed_count": len(failed),
    "skipped_count": skipped,
    "status": (
        "partial"
        if failed
        else "completed"
    ),
}
```

---

## 5.2 修改循环

推荐：

```python
def process_unextracted_notes(
    space_id: str,
    *,
    limit: int = 100,
) -> dict[str, Any]:
    from memory.service import process_note_memory

    processed = []
    failed = []
    skipped = 0

    notes = load_index(space_id)[
        : max(1, min(int(limit), 500))
    ]

    for note in notes:
        note_id = str(note.get("id") or "")

        if not note_id:
            skipped += 1
            continue

        state = get_extraction_state(note_id)

        if (
            state is not None
            and state.status in {"completed", "empty"}
        ):
            skipped += 1
            continue

        if (
            state is not None
            and state.status == "processing"
        ):
            if not _is_processing_stale(
                state.updated_at
            ):
                skipped += 1
                continue

            mark_extraction_failed(
                note_id,
                space_id,
                error="stale processing lease expired",
            )

        try:
            report = process_note_memory(note)

            processed.append(
                {
                    "note_id": note_id,
                    "trace_id": report.get("trace_id"),
                    "candidates": report.get("candidates"),
                    "extraction_status": report.get(
                        "extraction_status"
                    ),
                }
            )

        except Exception as exc:
            LOGGER.exception(
                "Memory extraction failed during "
                "daily consolidation: "
                "space_id=%s note_id=%s",
                space_id,
                note_id,
            )

            failed.append(
                {
                    "note_id": note_id,
                    "error": (
                        f"{type(exc).__name__}: {exc}"
                    ),
                }
            )

            continue

    return {
        "space_id": space_id,
        "processed": processed,
        "failed": failed,
        "processed_count": len(processed),
        "failed_count": len(failed),
        "skipped_count": skipped,
        "status": (
            "partial"
            if failed
            else "completed"
        ),
    }
```

---

## 5.3 错误日志要求

记录：

```text
space_id
note_id
error type
```

不要记录完整原文。

建议 action：

```text
memory.daily.note.failed
```

---

## 5.4 consolidation run 应如何处理 partial

如果 daily 返回：

```text
status = partial
```

推荐规则：

```text
只要 failed_count > 0
→ 本次 consolidation run 标 failed
→ 下一个 Scheduler tick 可重试
```

原因：

```text
partial 代表仍有未完成工作
```

修改：

```python
result = run_memory_consolidation(
    safe_id,
    cadence,
)

if result.get("status") == "partial":
    error = (
        f"{result.get('failed_count', 0)} "
        "notes failed"
    )

    mark_consolidation_failed(
        run.id,
        error,
    )

    results.append(
        {
            **result,
            "cadence": cadence,
            "period_key": period_key,
            "run_id": run.id,
            "status": "failed",
            "error": error,
        }
    )

    continue
```

这样下一次只会重新处理：

```text
failed / partial / stale processing
```

已经 completed 和 empty 的笔记会跳过。

---

# 六、问题三：手动 consolidation 绕过幂等

## 6.1 当前问题

后台任务使用：

```python
run_memory_consolidation_once()
```

会先写：

```text
memory_consolidation_runs
```

但手动命令使用：

```python
run_memory_consolidation()
```

会直接运行。

用户连续输入：

```text
/memory consolidate monthly
/memory consolidate monthly
```

可能重复创建稳定 semantic memory。

---

# 七、P3：手动 consolidation 默认也走幂等

文件：

```text
memory/service.py
```

## 7.1 修改导入

当前：

```python
from memory.scheduler import (
    run_memory_consolidation,
)
```

改为：

```python
from datetime import date

from memory.scheduler import (
    run_memory_consolidation_once,
)
```

---

## 7.2 修改格式化命令

当前：

```python
def format_memory_consolidate(
    space_id: str,
    cadence: str,
) -> str:
    result = run_memory_consolidation(
        space_id,
        cadence,
    )
```

改为：

```python
def format_memory_consolidate(
    space_id: str,
    cadence: str,
) -> str:
    cadence = cadence.strip().lower()

    if cadence not in {
        "daily",
        "weekly",
        "monthly",
    }:
        return (
            "用法：/memory consolidate "
            "daily｜weekly｜monthly"
        )

    report = run_memory_consolidation_once(
        cadence,
        space_ids=[space_id],
        today=date.today(),
    )

    result = (
        report.get("results") or [{}]
    )[0]

    status = result.get("status")

    if status == "skipped":
        return (
            "本周期已经执行过，"
            "未重复运行 consolidation。"
        )

    if status == "failed":
        return (
            "记忆 consolidation 执行失败："
            f"{result.get('error', 'unknown error')}"
        )

    return (
        "记忆 consolidation 完成："
        f"{result}"
    )
```

---

## 7.3 是否增加 force

本轮建议先不增加 `force`。

原因：

```text
force 很容易重新引入重复数据
```

后续如果确实需要，可以设计：

```text
/memory consolidate monthly --force
```

但必须：

```text
只有明确管理员命令才允许
+
写入 force reason
+
保留审计日志
```

---

# 八、并发 Reserve 仍需补测试

当前 consolidation reserve 使用：

```text
SELECT
→ 判断
→ INSERT ... ON CONFLICT UPDATE
```

顺序测试已经覆盖，但并发时还应确认：

```text
两个线程同时 reserve 相同 key
→ 只能有一个拿到 run
```

新增：

```text
tests/test_memory_consolidation_concurrency.py
```

测试：

```python
from concurrent.futures import ThreadPoolExecutor

def reserve():
    return reserve_consolidation_run(
        "space-1",
        "daily",
        "2026-07-14",
    )

with ThreadPoolExecutor(max_workers=8) as pool:
    results = list(
        pool.map(
            lambda _: reserve(),
            range(8),
        )
    )

successful = [
    result
    for result in results
    if result is not None
]

assert len(successful) == 1
```

如果此测试失败，说明 `reserve_consolidation_run()` 不是原子的。

---

# 九、建议将 Reserve 改成原子事务

为了避免并发竞态，推荐：

```python
def _operation() -> str | None:
    with _connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")

        row = conn.execute(
            """
            SELECT *
            FROM memory_consolidation_runs
            WHERE space_id = ?
              AND cadence = ?
              AND period_key = ?
            """,
            (
                space_id,
                cadence,
                period_key,
            ),
        ).fetchone()

        ...
```

`BEGIN IMMEDIATE` 会提前取得写锁，避免两个线程都在读取阶段认为“可以 reserve”。

锁冲突继续由：

```text
_run_write()
```

有限重试。

---

# 十、测试方案

## 10.1 Scheduler 同日重试

增加：

```text
tests/test_memory_scheduler_retry.py
```

场景：

```text
第一次 tick：
space_a failed
→ last_run_dates 不更新

第二次 tick：
space_a 成功
→ last_run_dates 更新
```

---

## 10.2 部分 space 失败

场景：

```text
space_a completed
space_b failed
```

第二次 tick：

```text
space_a skipped
space_b completed
```

断言：

```text
space_a 不重复执行
space_b 被重试
```

---

## 10.3 单条坏笔记隔离

构造三条笔记：

```text
note_a 成功
note_b 抛异常
note_c 成功
```

断言：

```text
processed_count = 2
failed_count = 1
note_c 仍被执行
status = partial
```

---

## 10.4 下一轮只重试坏笔记

第一次：

```text
note_a completed
note_b failed
note_c completed
```

第二次：

```text
note_a skip
note_b success
note_c skip
```

断言：

```text
第二次只调用 note_b
```

---

## 10.5 手动命令幂等

连续执行两次：

```text
/memory consolidate monthly
```

断言：

```text
第一次 completed
第二次 skipped
consolidation 真实函数只执行一次
```

---

## 10.6 并发 reserve

八个线程同时 reserve：

```text
space_id + cadence + period_key
```

断言：

```text
只有一个返回 ConsolidationRun
其余全部返回 None
数据库只有一条记录
```

---

# 十一、建议修改文件

```text
memory/scheduler.py
memory/consolidator.py
memory/repository.py
memory/service.py

tests/test_memory_scheduler.py
tests/test_memory_scheduler_retry.py
tests/test_memory_consolidation_runs.py
tests/test_memory_consolidation_concurrency.py
tests/test_memory_consolidator_resilience.py
tests/test_memory_service.py

README.md
DESIGN.md
tests/README.md
```

---

# 十二、实施顺序

```text
F1：修改 process_unextracted_notes，增加 note 级异常隔离
F2：partial consolidation 转成 failed run
F3：修改 Scheduler，只在 cadence 完成后更新 last_run_dates
F4：修改手动命令，统一走 run_memory_consolidation_once
F5：将 reserve 改为 BEGIN IMMEDIATE 原子事务
F6：补同日重试测试
F7：补单条坏笔记隔离测试
F8：补手动命令幂等测试
F9：补并发 reserve 测试
F10：同步文档并运行完整 CI
```

---

# 十三、验收命令

## Ruff

```bash
python -m ruff check .
```

## 完整测试

```bash
python -m pytest tests \
  --cov=. \
  --cov-report=term-missing \
  --cov-fail-under=63
```

## 五条 Dry-run

```bash
python eval/eval_classification.py --dry-run
python eval/eval_retrieval.py --dry-run
python eval/eval_summary.py --dry-run
python eval/eval_query_react.py --dry-run
python eval/eval_memory.py --dry-run
```

---

# 十四、最终验收标准

## Scheduler

```text
[ ] consolidation 失败后，同一天下一 tick 会重试
[ ] 只要存在 failed space，就不记录 cadence 当天完成
[ ] 已完成 space 不会重复执行
[ ] 失败 space 可以单独恢复
```

## Daily extraction

```text
[ ] 单条坏笔记不会阻塞后续笔记
[ ] 返回 processed_count
[ ] 返回 failed_count
[ ] 返回 skipped_count
[ ] 有失败时 status=partial
[ ] 下一轮只重试失败笔记
```

## 手动命令

```text
[ ] 手动 consolidation 默认遵守数据库幂等
[ ] 同周期第二次执行返回 skipped
[ ] monthly 不会重复创建稳定 semantic memory
```

## 并发

```text
[ ] 同 key 并发 reserve 只有一个成功
[ ] reserve 使用原子事务
[ ] 锁冲突由有限重试处理
```

## CI

```text
[ ] Python 3.10 通过
[ ] Python 3.11 通过
[ ] Ruff 通过
[ ] 全部 pytest 通过
[ ] Coverage 高于 63%
[ ] 五条 dry-run 通过
```

---

# 十五、本轮完成后的状态

完成后：

```text
第一阶段工程底座：封板
Memory V2 功能实现：封板
Memory V2 第二轮工程加固：封板
```

之后可以开始：

```text
LLM Memory Extractor
Memory Embedding
更复杂的关系分类
冲突消解策略
真实集成评测
```

---

# 十六、一句话总结

本次修改的核心是：

> 让一次失败不会阻塞整批任务，让 Scheduler 当天能够自动恢复，并保证后台和手动 consolidation 都遵守同一套幂等机制。
