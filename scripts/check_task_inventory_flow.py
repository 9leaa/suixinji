"""Replay inventory/completion on copies of six existing real-model task outputs."""

import argparse
from copy import deepcopy
from dataclasses import replace
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    root = Path(__file__).resolve().parents[1]
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
    from apps.console_commands import route_message

    repo.DB_PATH = output / "memory.db"
    paths = [
        root / "eval/results/task-simple-four-20261021" / key / "steps.jsonl"
        for key in ("progress_blockers_04", "progress_blockers_05", "progress_blockers_06", "progress_blockers_08")
    ]
    paths.append(root / "eval/results/task-boundary-three-20261021/identity_instances_04/steps.jsonl")
    imported = []
    for path in paths:
        row = json.loads(path.read_text().splitlines()[-1])
        for original in row["cards"]:
            card = {k: deepcopy(original[k]) for k in ("subject", "task", "instance", "task_status", "description", "sources")}
            raw = card["sources"][-1]["original_text"]
            data = {
                "subject": card["subject"],
                "task": card["task"],
                "instance": card["instance"],
                "task_status": card["task_status"],
                "description": card["description"],
                "evidence_span": raw,
            }
            candidate = task_cards.candidate_from_row(card["sources"][-1]["note_id"], raw, data)
            candidate = replace(
                candidate,
                content=task_cards.search_text(card),
                scope={"owner_id": "demo-user", "access_scope": "owner", "task_card": card},
                memory_key=task_cards.card_key(card["subject"], card["task"], card["instance"], "demo-user"),
            )
            decision = MemoryDecision(
                candidate_id=candidate.candidate_id,
                decision_id="demo-import:" + original["id"],
                relation="new",
                recommended_action="insert",
                target_memory_ids=[],
                confidence=1,
                reason="copy existing evaluation output",
                evidence=["file:" + str(path)],
            )
            result = repo.apply_memory_decision("inventory-demo", card["sources"][-1]["note_id"], candidate, decision)
            imported.append({"memory_id": result["memory_id"], "original_memory_id": original["id"], "source_file": str(path)})

    def ask(text, mid):
        route = route_message(text)
        assert route["task_type"] == "query"
        return answer_question("inventory-demo", text, user_id="demo-user", message_id=mid)

    first = ask("当前还有哪些未完成任务？列举出来。", "demo-list")
    snapshot = workflow_state.list_items("inventory-demo", "task_list_snapshot")[0]
    assert len(snapshot["items"]) == 6
    answer = ask("任务1，任务5请标记完成", "demo-complete")
    wanted = {snapshot["items"][i]["id"] for i in (0, 4)}
    records = list(repo.list_memories("inventory-demo", memory_type="task", limit=100))
    assert {r.id for r in records if r.task_status == "done"} == wanted
    assert len(records) == 6
    after = ask("当前还有哪些未完成任务？列举出来。", "demo-list-after")
    assert "共 4 项" in after
    assert ask("任务1，任务5请标记完成", "demo-complete") == answer
    result = {
        "storage": "isolated SQLite",
        "fixture": "six cards copied from previous real-model evaluation outputs, unchanged descriptions/statuses",
        "model_calls": 0,
        "imported": imported,
        "snapshot": snapshot,
        "list_answer": first,
        "completion_answer": answer,
        "after_answer": after,
        "cards_after": [task_cards.evidence(r) for r in records],
        "checks": {
            "listed_all_six": True,
            "only_original_1_5_done": True,
            "remaining_four": True,
            "retry_after_new_list_does_not_rebind": True,
        },
    }
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n")
    for name, text in [("全部未完成任务.txt", first), ("完成任务1和5.txt", answer), ("剩余未完成任务.txt", after)]:
        (output / name).write_text(text + "\n")
    print(json.dumps({"checks": result["checks"], "completion_answer": answer, "output": str(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
