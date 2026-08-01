"""文件作用：本地任务历史 TTL/容量清理。

项目关系：本文件依赖 `runtime.task`、`runtime.task_registry`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from runtime.task import TASK_QUEUED, TASK_RUNNING, create_task
from runtime.task_registry import TaskRegistry


def test_task_registry_prunes_finished_tasks_but_keeps_counts():
    """函数功能：`test_task_registry_prunes_finished_tasks_but_keeps_counts` 负责验证 task registry prunes finished tasks but keeps counts 场景，服务于本文件职责：本地任务历史 TTL/容量清理。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    registry = TaskRegistry(history_limit=3, history_ttl_hours=0)

    for index in range(8):
        task = create_task("query", "s1", {}, message_id=f"m{index}")
        registry.add(task)
        registry.mark_running(task.id)
        registry.mark_success(task.id)

    stats = registry.get_stats()
    assert stats["success"] == 8
    assert stats["retained_tasks"] == 3


def test_task_registry_keeps_queued_and_running_tasks():
    """函数功能：`test_task_registry_keeps_queued_and_running_tasks` 负责验证 task registry keeps queued and running tasks 场景，服务于本文件职责：本地任务历史 TTL/容量清理。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    registry = TaskRegistry(history_limit=1, history_ttl_hours=0)
    queued = create_task("query", "s1", {}, message_id="queued")
    running = create_task("query", "s1", {}, message_id="running")
    registry.add(queued)
    registry.add(running)
    registry.mark_running(running.id)

    for index in range(3):
        task = create_task("query", "s1", {}, message_id=f"done{index}")
        registry.add(task)
        registry.mark_running(task.id)
        registry.mark_success(task.id)

    tasks = registry.get_stats()["recent_tasks"]
    statuses = {task["message_id"]: task["status"] for task in tasks}
    assert statuses["queued"] == TASK_QUEUED
    assert statuses["running"] == TASK_RUNNING
