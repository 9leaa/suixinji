from __future__ import annotations

from sqlalchemy import create_engine, text

from runtime.query_metrics import capture_sql_queries


def test_capture_sql_queries_records_success_and_failure() -> None:
    """验证“capturesqlqueriesrecordssuccessandfailure”场景的预期行为与回归边界。"""
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
    """验证“capturesqlqueriestracksfailedexecution”场景的预期行为与回归边界。"""
    engine = create_engine("sqlite:///:memory:")
    with capture_sql_queries(engine) as stats:
        try:
            with engine.connect() as connection:
                connection.execute(text("SELECT missing_column FROM missing_table"))
        except Exception:
            pass

    assert stats.count == 1
    assert stats.failed == 1
