from pathlib import Path

from eval import eval_memory


def test_memory_eval_dry_run(tmp_path):
    report = eval_memory.run(dry_run=True, output_dir=tmp_path)

    assert report["mode"] == "dry_run"
    assert report["cases"]["extraction"] >= 1
    assert (tmp_path / "memory_results.json").exists()


def test_memory_eval_full_run(tmp_path):
    report = eval_memory.run(dry_run=False, output_dir=tmp_path)

    assert report["mode"] == "memory"
    assert "summary" in report
    assert Path(tmp_path / "memory_extraction.json").exists()

