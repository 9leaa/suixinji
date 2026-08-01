"""文件作用：policy 分发与公共合并接口。

项目关系：本文件依赖 `memory.models`；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

from memory.models import normalize_content


def merge_content(memory_type: str, old_content: str, new_content: str) -> str:
    """函数功能：`merge_content` 负责合并 content，服务于本文件职责：policy 分发与公共合并接口。
    传参：
        memory_type: memory type 参数，由调用方传入，类型为 `str`。
        old_content: old content 参数，由调用方传入，类型为 `str`。
        new_content: new content 参数，由调用方传入，类型为 `str`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    old_norm = normalize_content(old_content)
    new_norm = normalize_content(new_content)
    if not old_norm:
        return new_content
    if old_norm in new_norm:
        return new_content
    if new_norm in old_norm:
        return old_content
    separator = "；"
    return f"{old_content.rstrip('。；; ')}{separator}{new_content.lstrip('用户').rstrip('。；; ')}"
