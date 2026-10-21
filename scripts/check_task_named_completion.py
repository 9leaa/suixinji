"""Isolated live-model checks for task-name and conversational completion."""

import argparse
from dataclasses import replace
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    output = args.output.resolve()
    output.mkdir(parents=True, exist_ok=False)
    os.environ.update(
        {
            "STORAGE_BACKEND": "local",
            "COORDINATION_BACKEND": "local",
            "TASK_QUEUE_BACKEND": "local",
            "SUIXINJI_TASK_CARDS_ENABLED": "true",
            "SUIXINJI_AGENT_HOOKS_ENABLED": "false",
            "SUIXINJI_PREFERENCE_CARDS_ENABLED": "false",
        }
    )
    os.chdir(output)
    from memory import repository as repo, task_cards, workflow_state
    from memory.models import MemoryDecision
    from agent.query_agent import answer_question

    repo.DB_PATH = output / "memory.db"

    def seed(key, task, instance=None):
        value = task_cards.TaskInput(task=task, instance=instance, task_status="todo", description=task, evidence_span=task)
        note = {"id": key, "text": task, "ts": "2026-10-20T09:00:00+08:00"}
        card = task_cards.merge(value, None, task_cards.TaskPlan(action="new"), task_cards.source_for(note, value))
        candidate = task_cards.candidate_from_row(key, task, value.model_dump())
        candidate = replace(
            candidate,
            content=task_cards.search_text(card),
            scope={"owner_id": "owner", "access_scope": "owner", "task_card": card},
            memory_key=task_cards.card_key("用户", task, instance, "owner"),
        )
        decision = MemoryDecision(
            candidate_id=candidate.candidate_id,
            decision_id=key,
            relation="new",
            recommended_action="insert",
            confidence=1,
            target_memory_ids=[],
            reason="isolated fixture",
            evidence=["note:" + key],
        )
        return repo.apply_memory_decision("named-demo", key, candidate, decision)["memory_id"]

    ids = {
        "print": seed("print", "打印采购合同"),
        "send": seed("send", "向客户发送演示文稿"),
        "review": seed("review", "审核客户演示文稿"),
        "week1": seed("week1", "清洁检查", "本周"),
        "week2": seed("week2", "清洁检查", "下周"),
        "submit": seed("submit", "提交项目报告"),
    }
    results = []

    def ask(text):
        answer = answer_question("named-demo", text, message_id=f"message-{len(results)}")
        states = {key: repo.get_memory(mid).task_status for key, mid in ids.items()}
        results.append({"text": text, "answer": answer, "states": states})
        (output / "steps.json").write_text(json.dumps(results, ensure_ascii=False, indent=2))
        print(json.dumps(results[-1], ensure_ascii=False), flush=True)
        return answer

    checks = {}
    ask("合同打印好了")
    checks["name_without_list"] = repo.get_memory(ids["print"]).task_status == "done"
    ask("列出所有未完成任务")
    ask("刚才那个发送任务完成了")
    checks["context_send_not_review"] = (
        repo.get_memory(ids["send"]).task_status == "done" and repo.get_memory(ids["review"]).task_status == "todo"
    )
    ambiguous = ask("清洁检查完成了")
    checks["ambiguous_rounds_no_write"] = "多个可能" in ambiguous and all(
        repo.get_memory(ids[k]).task_status == "todo" for k in ("week1", "week2")
    )
    if checks["ambiguous_rounds_no_write"]:
        ask("任务1请标记完成")
        checks["clarification_one_round"] = sum(repo.get_memory(ids[k]).task_status == "done" for k in ("week1", "week2")) == 1
    ask("本周的清洁检查完成了")
    checks["named_round_clarification"] = repo.get_memory(ids["week1"]).task_status == "done"
    ask("项目报告初稿写完了")
    checks["partial_progress_not_done"] = repo.get_memory(ids["submit"]).task_status == "todo"
    unknown = ask("自行车保养完成了")
    checks["unknown_no_new_card"] = "没有找到" in unknown and len(list(task_cards.records("named-demo", "owner"))) == 6
    result = {
        "model": "deepseek-flash",
        "storage": "isolated SQLite",
        "checks": checks,
        "steps": results,
        "resolutions": workflow_state.list_items("named-demo", "task_reference_resolution"),
    }
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
    print(json.dumps({"checks": checks, "output": str(output)}, ensure_ascii=False))
    if not all(checks.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    main()
