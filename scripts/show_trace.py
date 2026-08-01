"""文件作用：Trace 查看。

项目关系：本文件依赖 `memory.service`；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from memory.service import format_trace_id, format_trace_latest, format_trace_memory


def main() -> None:
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：Trace 查看。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    parser = argparse.ArgumentParser(description="Show a Memory V2 trace.")
    parser.add_argument("--trace-id")
    parser.add_argument("--memory-id")
    args = parser.parse_args()

    if args.memory_id:
        print(format_trace_memory(args.memory_id))
    elif args.trace_id:
        print(format_trace_id(args.trace_id))
    else:
        print(format_trace_latest())


if __name__ == "__main__":
    main()
