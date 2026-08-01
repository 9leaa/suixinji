"""文件作用：稳定事实/语义策略。

项目关系：本文件依赖 无直接本地模块依赖；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations


CHANGE_MARKERS = ("现在", "改为", "搬到", "转为", "不再", "短期", "只学", "重点")


def explicitly_replaces(new_content: str, *, predicate: str | None = None) -> bool:
    """函数功能：`explicitly_replaces` 负责处理 explicitly replaces，服务于本文件职责：稳定事实/语义策略。
    传参：
        new_content: new content 参数，由调用方传入，类型为 `str`。
        predicate: predicate 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    if predicate == "location" and any(marker in new_content for marker in ("搬到", "现在住在", "已经住在", "改住")):
        return True
    return any(marker in new_content for marker in CHANGE_MARKERS)
