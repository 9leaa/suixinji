"""文件作用：同 key 并发合并互斥。

项目关系：本文件依赖 `memory.repository`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from concurrent.futures import ThreadPoolExecutor

from memory.repository import _connect, reserve_consolidation_run


def test_concurrent_reserve_same_key_allows_only_one_success():
    """函数功能：`test_concurrent_reserve_same_key_allows_only_one_success` 负责验证 concurrent reserve same key allows only one success 场景，服务于本文件职责：同 key 并发合并互斥。
    传参：
        无。
    返回结果说明：
        返回计算后的结果对象；具体类型取决于实际执行分支。
    """
    def reserve(_idx):
        """函数功能：`reserve` 负责预约，服务于本文件职责：同 key 并发合并互斥。
        传参：
            _idx:  idx 参数，由调用方传入。
        返回结果说明：
            返回计算后的结果对象；具体类型取决于实际执行分支。
        """
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
