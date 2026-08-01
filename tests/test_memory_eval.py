"""文件作用：Memory eval 读取、计分和报告。

项目关系：本文件依赖 `eval`；被 暂无静态导入方或仅作为入口脚本执行。
"""


from pathlib import Path

from eval import eval_memory


def test_memory_eval_dry_run(tmp_path):
    """函数功能：`test_memory_eval_dry_run` 负责验证 memory eval dry run 场景，服务于本文件职责：Memory eval 读取、计分和报告。
    传参：
        tmp_path: tmp path 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    report = eval_memory.run(dry_run=True, output_dir=tmp_path)

    assert report["mode"] == "dry_run"
    assert report["cases"]["extraction"] >= 1
    assert (tmp_path / "memory_results.json").exists()


def test_memory_eval_full_run(tmp_path):
    """函数功能：`test_memory_eval_full_run` 负责验证 memory eval full run 场景，服务于本文件职责：Memory eval 读取、计分和报告。
    传参：
        tmp_path: tmp path 参数，由调用方传入。
    返回结果说明：
        无显式返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    report = eval_memory.run(dry_run=False, output_dir=tmp_path)

    assert report["mode"] == "memory"
    assert "summary" in report
    assert report["summary"]["consolidation_duplicate_rate"] == 0.0
    assert report["summary"]["low_relevance_filter_rate"] >= 0.9
    assert Path(tmp_path / "memory_extraction.json").exists()
    assert Path(tmp_path / "memory_hardening.json").exists()
