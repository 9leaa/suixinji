"""文件作用：Memory 生命周期操作。

项目关系：本文件依赖 `memory.repository`；被 `eval.eval_memory`、`tests.test_memory_repository`。
"""



from __future__ import annotations

from memory.repository import correct_memory, soft_delete_memory, update_memory


def forget(memory_id: str) -> bool:
    """函数功能：`forget` 负责遗忘，服务于本文件职责：Memory 生命周期操作。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    return soft_delete_memory(memory_id) is not None


def correct(memory_id: str, content: str) -> bool:
    """函数功能：`correct` 负责处理 correct，服务于本文件职责：Memory 生命周期操作。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        content: 需要处理、保存或展示的文本内容，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    return correct_memory(memory_id, content) is not None


def expire(memory_id: str, reason: str = "expired") -> bool:
    """函数功能：`expire` 负责处理过期状态，服务于本文件职责：Memory 生命周期操作。
    传参：
        memory_id: Memory 标识，用于定位长期记忆，类型为 `str`。
        reason: reason 参数，由调用方传入，类型为 `str`，默认值为 `'expired'`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    return update_memory(memory_id, status="expired", reason=reason) is not None
