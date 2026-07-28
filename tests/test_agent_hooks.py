from __future__ import annotations

import pytest

from agent.hooks.base import AgentHook
from agent.hooks.context import AgentRunContext
from agent.hooks.manager import HookManager


class RecordingHook(AgentHook):
    def __init__(self, name: str, events: list[str]) -> None:
        """初始化`RecordingHook` 实例并建立后续调用所需的状态。"""
        self.name = name
        self.events = events

    def before_agent(self, context):
        """验证“Agent 执行前的 Hook 前置处理”场景的预期行为与回归边界。"""
        self.events.append(f"before_agent:{self.name}")

    def after_agent(self, context, result):
        """验证“Agent 执行后的 Hook 后置处理”场景的预期行为与回归边界。"""
        self.events.append(f"after_agent:{self.name}")

    def before_llm(self, context, request):
        """验证“LLM 调用前的 Hook 前置处理”场景的预期行为与回归边界。"""
        self.events.append(f"before_llm:{self.name}")

    def after_llm(self, context, request, result):
        """验证“LLM 调用后的 Hook 后置处理”场景的预期行为与回归边界。"""
        self.events.append(f"after_llm:{self.name}")

    def before_tool(self, context, tool_name, args):
        """验证“工具调用前的 Hook 前置处理”场景的预期行为与回归边界。"""
        self.events.append(f"before_tool:{self.name}")

    def after_tool(self, context, tool_name, args, result):
        """验证“工具调用后的 Hook 后置处理”场景的预期行为与回归边界。"""
        self.events.append(f"after_tool:{self.name}")

    def on_error(self, context, error, scope):
        """验证“异常发生时的 Hook 错误处理”场景的预期行为与回归边界。"""
        self.events.append(f"error:{scope}:{self.name}")


def _context():
    """验证“上下文”场景的预期行为与回归边界。"""
    return AgentRunContext.create(space_id="hook-space", run_type="query")


def test_hook_order_is_stack_shaped():
    """验证“hookorder是否为stackshaped”场景的预期行为与回归边界。"""
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
    """验证“hook错误cleanuprunsreverseorder”场景的预期行为与回归边界。"""
    events = []
    manager = HookManager([RecordingHook("a", events), RecordingHook("b", events)])
    with pytest.raises(RuntimeError, match="boom"):
        manager.run_agent(_context(), lambda: (_ for _ in ()).throw(RuntimeError("boom")))
    assert events[-2:] == ["error:agent:b", "error:agent:a"]
