from concurrent.futures import ThreadPoolExecutor

from memory.repository import _connect, reserve_consolidation_run


def test_concurrent_reserve_same_key_allows_only_one_success():
    """验证“concurrent预约same键allowsonlyonesuccess”场景的预期行为与回归边界。"""
    def reserve(_idx):
        """验证“预约”场景的预期行为与回归边界。"""
        return reserve_consolidation_run("space-1", "daily", "2026-07-14")

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(8)))

    successful = [result for result in results if result is not None]

    assert len(successful) == 1
    with _connect() as conn:
        row = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM memory_consolidation_runs
            WHERE space_id = ? AND cadence = ? AND period_key = ?
            """,
            ("space-1", "daily", "2026-07-14"),
        ).fetchone()
    assert int(row["count"]) == 1
