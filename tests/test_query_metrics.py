"""文件作用：查询指标采集与聚合。

项目关系：本文件依赖 `runtime.query_metrics`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from __future__ import annotations

from sqlalchemy import create_engine, text

from runtime.query_metrics import capture_sql_queries


def test_capture_sql_queries_records_success_and_failure() -> None:
    """函数功能：`test_capture_sql_queries_records_success_and_failure` 负责验证 capture sql queries records success and failure 场景，服务于本文件职责：查询指标采集与聚合。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    engine = create_engine("sqlite:///:memory:")
    with capture_sql_queries(engine) as stats:
        with engine.begin() as connection:
            connection.execute(text("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)"))
            connection.execute(text("INSERT INTO items (value) VALUES ('one')"))
            assert connection.execute(text("SELECT value FROM items")).scalar_one() == "one"

    report = stats.to_dict()
    assert report["count"] == 3
    assert report["failed"] == 0
    assert report["p50_duration_ms"] is not None


def test_capture_sql_queries_tracks_failed_execution() -> None:
    """函数功能：`test_capture_sql_queries_tracks_failed_execution` 负责验证 capture sql queries tracks failed execution 场景，服务于本文件职责：查询指标采集与聚合。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    engine = create_engine("sqlite:///:memory:")
    with capture_sql_queries(engine) as stats:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT missing_column FROM missing_table"))
        except Exception:
            pass

    assert stats.count == 1
    assert stats.failed == 1
