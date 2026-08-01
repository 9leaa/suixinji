"""文件作用：Hook 生命周期、顺序、幂等/缓存/观测接线。

项目关系：本文件依赖 `agent.hooks.base`、`agent.hooks.context`、`agent.hooks.manager`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from __future__ import annotations

import pytest

from agent.hooks.base import AgentHook
from agent.hooks.context import AgentRunContext
from agent.hooks.manager import HookManager


class RecordingHook(AgentHook):
    """类功能：`RecordingHook` 封装与“Hook 生命周期、顺序、幂等/缓存/观测接线”相关的数据结构、状态或行为。
    继承关系：继承 `AgentHook`，复用其接口或生命周期约定。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(self, name: str, events: list[str]) -> None:
        """函数功能：`RecordingHook.__init__` 在类 `RecordingHook` 中负责初始化实例状态，服务于本文件职责：Hook 生命周期、顺序、幂等/缓存/观测接线。
        传参：
            name: name 参数，由调用方传入，类型为 `str`。
            events: events 参数，由调用方传入，类型为 `list[str]`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.name = name
        self.events = events

    def before_agent(self, context):
        """函数功能：`RecordingHook.before_agent` 在类 `RecordingHook` 中负责处理 before agent，服务于本文件职责：Hook 生命周期、顺序、幂等/缓存/观测接线。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.events.append(f"before_agent:{self.name}")

    def after_agent(self, context, result):
        """函数功能：`RecordingHook.after_agent` 在类 `RecordingHook` 中负责处理 after agent，服务于本文件职责：Hook 生命周期、顺序、幂等/缓存/观测接线。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息。
            result: 上游步骤返回的结果对象。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.events.append(f"after_agent:{self.name}")

    def before_llm(self, context, request):
        """函数功能：`RecordingHook.before_llm` 在类 `RecordingHook` 中负责处理 before llm，服务于本文件职责：Hook 生命周期、顺序、幂等/缓存/观测接线。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息。
            request: 请求对象或请求载荷。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.events.append(f"before_llm:{self.name}")

    def after_llm(self, context, request, result):
        """函数功能：`RecordingHook.after_llm` 在类 `RecordingHook` 中负责处理 after llm，服务于本文件职责：Hook 生命周期、顺序、幂等/缓存/观测接线。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息。
            request: 请求对象或请求载荷。
            result: 上游步骤返回的结果对象。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.events.append(f"after_llm:{self.name}")

    def before_tool(self, context, tool_name, args):
        """函数功能：`RecordingHook.before_tool` 在类 `RecordingHook` 中负责处理 before tool，服务于本文件职责：Hook 生命周期、顺序、幂等/缓存/观测接线。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息。
            tool_name: tool name 参数，由调用方传入。
            args: args 参数，由调用方传入。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.events.append(f"before_tool:{self.name}")

    def after_tool(self, context, tool_name, args, result):
        """函数功能：`RecordingHook.after_tool` 在类 `RecordingHook` 中负责处理 after tool，服务于本文件职责：Hook 生命周期、顺序、幂等/缓存/观测接线。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息。
            tool_name: tool name 参数，由调用方传入。
            args: args 参数，由调用方传入。
            result: 上游步骤返回的结果对象。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.events.append(f"after_tool:{self.name}")

    def on_error(self, context, error, scope):
        """函数功能：`RecordingHook.on_error` 在类 `RecordingHook` 中负责处理 on error，服务于本文件职责：Hook 生命周期、顺序、幂等/缓存/观测接线。
        传参：
            context: 当前 Agent 或运行时上下文，携带租户、空间、请求和统计信息。
            error: 当前捕获的异常对象。
            scope: scope 参数，由调用方传入。
        返回结果说明：
            无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.events.append(f"error:{scope}:{self.name}")


def _context():
    """函数功能：`_context` 负责处理 context，服务于本文件职责：Hook 生命周期、顺序、幂等/缓存/观测接线。
    传参：
        无。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    return AgentRunContext.create(space_id="hook-space", run_type="query")


def test_hook_order_is_stack_shaped():
    """函数功能：`test_hook_order_is_stack_shaped` 负责验证 hook order is stack shaped 场景，服务于本文件职责：Hook 生命周期、顺序、幂等/缓存/观测接线。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    events = []
    manager = HookManager([RecordingHook("a", events), RecordingHook("b", events)])
    context = _context()
    result = manager.run_agent(
        context,
        lambda: manager.run_llm(
            context,
            {"name": "test"},
            lambda: manager.run_tool(context, "get_note", {}, lambda: "ok"),
        ),
    )
    assert result == "ok"
    assert events == [
        "before_agent:a", "before_agent:b",
        "before_llm:a", "before_llm:b",
        "before_tool:a", "before_tool:b",
        "after_tool:b", "after_tool:a",
        "after_llm:b", "after_llm:a",
        "after_agent:b", "after_agent:a",
    ]


def test_hook_error_cleanup_runs_in_reverse_order():
    """函数功能：`test_hook_error_cleanup_runs_in_reverse_order` 负责验证 hook error cleanup runs in reverse order 场景，服务于本文件职责：Hook 生命周期、顺序、幂等/缓存/观测接线。
    传参：
        无。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    events = []
    manager = HookManager([RecordingHook("a", events), RecordingHook("b", events)])
    with pytest.raises(RuntimeError, match="boom"):
        manager.run_agent(_context(), lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert events[-2:] == ["error:agent:b", "error:agent:a"]
