"""文件作用：数据库健康检查。

项目关系：本文件依赖 `infrastructure.database`、`infrastructure.schema`；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import inspect

from infrastructure.database import check_database_health, get_engine
from infrastructure.schema import Base


def main() -> None:
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：数据库健康检查。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    health = check_database_health()
    inspector = inspect(get_engine())
    expected = set(Base.metadata.tables)
    existing = set(inspector.get_table_names())
    print(json.dumps({**health, "missing_tables": sorted(expected - existing), "table_count": len(existing)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
