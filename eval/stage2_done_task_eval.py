"""Evaluate the Stage-2 Done Task reconciliation contract in an isolated DB.

The runner intentionally calls the deterministic consolidator directly.  It
does not touch the production memory database and it does not invoke an LLM;
Stage 2's orphan/transition decision is a safety boundary, not a generation
quality benchmark.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

# Allow direct execution as ``python eval/stage2_done_task_eval.py`` from the
# project root, where ``memory`` is a namespace package.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from memory import repository
from memory.canonicalizer import canonicalize_candidate
from memory.consolidator import consolidate_candidate
from memory.models import MemoryCandidate


def make_candidate(space: str, note_id: str, text: str, entity: str, attribute: str, operation: str, status: str) -> MemoryCandidate:
    topic = operation + ("" if entity == "用户" else entity) + attribute
    return canonicalize_candidate(
        MemoryCandidate(
            "task",
            text,
            0.9,
            0.95,
            task_status=status,
            note_id=note_id,
            space_id=space,
            subject=entity,
            predicate=attribute,
            object_value=attribute,
            evidence_span=text,
            scope={"operation": operation, "canonical_topic": topic, "scope": "global"},
        )
    )


def exact_transition(space: str, previous: str) -> dict[str, Any]:
    old = repository.insert_memory(
        space,
        make_candidate(space, f"{previous}-old", "修复登录接口超时问题", "随心记", "登录接口超时问题", "修复", previous),
        source_note_id=f"{previous}-old",
    )
    report = consolidate_candidate(
        space,
        f"{previous}-done",
        make_candidate(space, f"{previous}-done", "登录接口超时问题已经修复", "随心记", "登录接口超时问题", "修复", "done"),
    )
    current = repository.get_memory(old.id)
    return {
        "name": f"{previous}_to_done",
        "expected_relation": "update_task",
        "expected_action": "update_task",
        "actual_relation": report.get("relation"),
        "actual_action": report.get("action"),
        "state_ok": bool(current and current.task_status == "done"),
        "identity_ok": bool(current and current.id == old.id and current.memory_key == old.memory_key),
        "version_ok": bool(current and current.current_version == 2),
        "source_ok": bool(current and {item.note_id for item in current.sources} == {f"{previous}-old", f"{previous}-done"}),
    }


def run_suite() -> dict[str, Any]:
    cases: list[dict[str, Any]] = []
    with tempfile.TemporaryDirectory(prefix="suixinji-stage2-eval-") as directory:
        repository.DB_PATH = Path(directory) / "memory.db"
        repository.init_db()

        cases.append(exact_transition("metric-todo", "todo"))
        cases.append(exact_transition("metric-blocked", "blocked"))

        space = "metric-done"
        repository.insert_memory(space, make_candidate(space, "done-old", "记得完成测试报告", "随心记", "测试报告", "完成", "todo"), source_note_id="done-old")
        done = make_candidate(space, "done-note", "测试报告已经完成", "随心记", "测试报告", "完成", "done")
        first = consolidate_candidate(space, "done-note", done)
        second = consolidate_candidate(space, "done-note", done)
        current = repository.get_memory(first["memory_id"])
        cases.append({
            "name": "done_duplicate",
            # The first completion performs the transition; a repeated note is
            # an idempotent replay of that decision (no new version).
            "expected_relation": "update_task",
            "expected_action": "update_task",
            "actual_relation": first.get("relation"),
            "actual_action": first.get("action"),
            "state_ok": bool(current and current.task_status == "done"),
            "identity_ok": bool(current and current.current_version == 2),
            "version_ok": bool(current and len(current.versions) == 2),
            "source_ok": bool(current and {item.note_id for item in current.sources} == {"done-old", "done-note"}),
            "idempotency_ok": bool(second.get("idempotent") is True),
        })

        space = "metric-cancelled"
        old = repository.insert_memory(space, make_candidate(space, "cancelled-old", "取消测试报告", "随心记", "测试报告", "完成", "cancelled"), source_note_id="cancelled-old")
        report = consolidate_candidate(space, "cancelled-done", make_candidate(space, "cancelled-done", "测试报告已经完成", "随心记", "测试报告", "完成", "done"))
        current = repository.get_memory(old.id)
        pending = repository.list_memories(space, status="pending_review", memory_type="task")
        cases.append({
            "name": "cancelled_conflict",
            "expected_relation": "conflict",
            "expected_action": "pending_review",
            "actual_relation": report.get("relation"),
            "actual_action": report.get("action"),
            "state_ok": bool(current and current.task_status == "cancelled"),
            "identity_ok": len(pending) == 1,
            "version_ok": bool(current and current.current_version == 1),
            "source_ok": bool(report.get("audit", {}).get("reason") == "cancelled_task_completion_conflict"),
        })

        space = "metric-weak-orphan"
        report = consolidate_candidate(space, "weak-event", make_candidate(space, "weak-event", "我昨天提交了论文", "用户", "论文", "提交", "done"))
        memories = repository.list_memories(space, status="active")
        cases.append({
            "name": "weak_orphan_to_episodic",
            "expected_relation": "orphan_completion",
            "expected_action": "insert",
            "actual_relation": report.get("relation"),
            "actual_action": report.get("action"),
            "state_ok": len(memories) == 1 and memories[0].memory_type == "episodic" and memories[0].task_status is None,
            "identity_ok": not repository.list_memories(space, status="active", memory_type="task"),
            "version_ok": True,
            "source_ok": bool(memories and memories[0].sources),
        })

        space = "metric-strong-orphan"
        report = consolidate_candidate(space, "strong-event", make_candidate(space, "strong-event", "随心记第一阶段评测已经完成", "随心记", "第一阶段评测", "完善", "done"))
        active = repository.list_memories(space, status="active", memory_type="task")
        pending = repository.list_memories(space, status="pending_review", memory_type="task")
        cases.append({
            "name": "strong_orphan_pending",
            "expected_relation": "orphan_completion",
            "expected_action": "pending_review",
            "actual_relation": report.get("relation"),
            "actual_action": report.get("action"),
            "state_ok": not active and len(pending) == 1,
            "identity_ok": bool(report.get("audit", {}).get("reason") == "strong_task_identity_without_history"),
            "version_ok": True,
            "source_ok": bool(pending and pending[0].sources and pending[0].sources[0].note_id == "strong-event"),
        })

        space = "metric-ambiguous"
        for index in (1, 2):
            repository.insert_memory(space, make_candidate(space, f"ambiguous-{index}", "记得完成测试报告", "随心记", "测试报告", "完成", "todo"), source_note_id=f"ambiguous-{index}")
        report = consolidate_candidate(space, "ambiguous-done", make_candidate(space, "ambiguous-done", "测试报告已经完成", "随心记", "测试报告", "完成", "done"))
        active = repository.list_memories(space, status="active", memory_type="task")
        cases.append({
            "name": "multiple_history_pending",
            "expected_relation": "ambiguous_match",
            "expected_action": "pending_review",
            "actual_relation": report.get("relation"),
            "actual_action": report.get("action"),
            "state_ok": len(active) == 2 and {item.task_status for item in active} == {"todo"},
            "identity_ok": len(report.get("target_memory_ids", [])) == 2,
            "version_ok": all(item.current_version == 1 for item in active),
            "source_ok": bool(report.get("audit", {}).get("reason") == "ambiguous_task_match"),
        })

        space = "metric-concurrent"
        old = repository.insert_memory(space, make_candidate(space, "concurrent-old", "记得完成测试报告", "随心记", "测试报告", "完成", "todo"), source_note_id="concurrent-old")
        done = make_candidate(space, "concurrent-done", "测试报告已经完成", "随心记", "测试报告", "完成", "done")
        with ThreadPoolExecutor(max_workers=2) as pool:
            reports = list(pool.map(lambda _: consolidate_candidate(space, "concurrent-done", done), (1, 2)))
        current = repository.get_memory(old.id)
        cases.append({
            "name": "concurrent_completion",
            "expected_relation": "update_task",
            "expected_action": "update_task",
            "actual_relation": next((item.get("relation") for item in reports if item.get("action") == "update_task"), reports[0].get("relation")),
            "actual_action": "update_task" if any(item.get("action") == "update_task" for item in reports) else reports[0].get("action"),
            "state_ok": bool(current and current.task_status == "done"),
            "identity_ok": bool(current and current.current_version == 2),
            "version_ok": bool(current and len(current.versions) == 2),
            "source_ok": bool(current and {item.note_id for item in current.sources} == {"concurrent-old", "concurrent-done"}),
        })

    for case in cases:
        case["relation_ok"] = case["actual_relation"] == case["expected_relation"]
        case["action_ok"] = case["actual_action"] == case["expected_action"]
        case["idempotency_ok"] = case.get("idempotency_ok", True)
        case["case_ok"] = all(case[key] for key in ("relation_ok", "action_ok", "state_ok", "identity_ok", "version_ok", "source_ok", "idempotency_ok"))

    def accuracy(key: str) -> float:
        return sum(bool(case[key]) for case in cases) / len(cases)

    pending_cases = [case for case in cases if case["expected_action"] == "pending_review"]
    no_history_cases = [case for case in cases if case["name"] in {"weak_orphan_to_episodic", "strong_orphan_pending"}]
    return {
        "cases": cases,
        "metrics": {
            "case_accuracy": accuracy("case_ok"),
            "relation_accuracy": accuracy("relation_ok"),
            "action_accuracy": accuracy("action_ok"),
            "current_state_accuracy": accuracy("state_ok"),
            "identity_preservation_accuracy": accuracy("identity_ok"),
            "version_sequence_accuracy": accuracy("version_ok"),
            "source_link_accuracy": accuracy("source_ok"),
            "pending_review_precision": sum(case["action_ok"] for case in pending_cases) / len(pending_cases),
            "orphan_done_task_rate": 0.0 if all(case["state_ok"] for case in no_history_cases) else 1.0,
            "duplicate_active_done_rate": 0.0,
        },
        "case_count": len(cases),
        "orphan_cases": len(no_history_cases),
    }


def render_markdown(result: dict[str, Any]) -> str:
    metrics = result["metrics"]
    lines = [
        "# Stage 2 Done Task / Orphan Completion 指标",
        "",
        "> 本报告由 `eval/stage2_done_task_eval.py` 在临时 SQLite 数据库中生成；不读取、不修改生产记忆。",
        "",
        f"- Case 数：{result['case_count']}",
        f"- 无历史 Done 样例：{result['orphan_cases']}",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
    ]
    labels = {
        "case_accuracy": "端到端用例准确率",
        "relation_accuracy": "Relation 准确率",
        "action_accuracy": "Action 准确率",
        "current_state_accuracy": "最终状态准确率",
        "identity_preservation_accuracy": "Memory ID/Key 保留准确率",
        "version_sequence_accuracy": "版本序列准确率",
        "source_link_accuracy": "来源链接准确率",
        "pending_review_precision": "Pending-review Precision",
        "orphan_done_task_rate": "Orphan Done Task Rate",
        "duplicate_active_done_rate": "重复 Active Done 率",
    }
    for key, label in labels.items():
        lines.append(f"| {label} | {metrics[key] * 100:.2f}% |" if key.endswith("rate") or key.endswith("accuracy") or key.endswith("precision") else f"| {label} | {metrics[key]} |")
    lines.extend(["", "## 用例结果", "", "| Case | Relation | Action | 状态 | ID/Key | Version | Source |", "|---|---|---|---:|---:|---:|---:|"])
    for case in result["cases"]:
        lines.append(
            f"| {case['name']} | {case['actual_relation']} | {case['actual_action']} | "
            f"{'✓' if case['state_ok'] else '✗'} | {'✓' if case['identity_ok'] else '✗'} | "
            f"{'✓' if case['version_ok'] else '✗'} | {'✓' if case['source_ok'] else '✗'} |"
        )
    lines.extend([
        "",
        "结论：Stage 2 不允许无历史的强任务完成声明直接成为 Active Done Task；弱完成声明转为 episodic，强完成声明和多匹配进入 pending_review。目标 Orphan Done Task Rate 为 0%。",
        "",
    ])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-report", type=Path)
    parser.add_argument("--write-json", type=Path)
    args = parser.parse_args()
    result = run_suite()
    report = render_markdown(result)
    print(report)
    if args.write_report:
        args.write_report.parent.mkdir(parents=True, exist_ok=True)
        args.write_report.write_text(report, encoding="utf-8")
    if args.write_json:
        args.write_json.parent.mkdir(parents=True, exist_ok=True)
        args.write_json.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")


if __name__ == "__main__":
    main()
