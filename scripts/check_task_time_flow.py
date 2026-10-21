"""Two original T5 messages plus ten temporary messages, with live extraction."""

import argparse
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
            "SUIXINJI_PREFERENCE_CARDS_ENABLED": "false",
            "SUIXINJI_MEMORY_EXTRACTOR_MODE": "llm",
            "SUIXINJI_FAKE_EXTERNALS": "false",
            "SUIXINJI_AGENT_HOOKS_ENABLED": "false",
            "SUIXINJI_MEMORY_V3_SHADOW_MODE": "false",
        }
    )
    os.chdir(output)
    from core import llm_client
    from memory import extractor, repository as repo, task_cards, trace, workflow_state
    from memory.service import process_note_memory
    from agent.query_agent import answer_question

    rows = []
    for key in ("temporal_01", "temporal_02"):
        original = json.loads((root / "eval/results/task-cards-100-live-20261020" / key / "steps.jsonl").read_text().splitlines()[0])
        rows.append(
            {
                "case": key,
                "text": original["text"],
                "ts": original["message_time"],
                "periods": original["expected"]["instance_date_ranges"],
                "count": 2,
            }
        )
    rows += [
        {
            "case": "lifecycle",
            "text": "本周要交运营周报，周五前交。",
            "ts": "2026-10-20T09:00:00+08:00",
            "periods": [["2026-10-19", "2026-10-25"]],
            "due": "2026-10-23",
        },
        {
            "case": "lifecycle",
            "text": "这份运营周报推迟到下周一交。",
            "ts": "2026-10-22T09:00:00+08:00",
            "periods": [["2026-10-19", "2026-10-25"]],
            "due": "2026-10-26",
            "same": True,
        },
        {
            "case": "lifecycle",
            "text": "上周那份运营周报交了。",
            "ts": "2026-10-26T09:00:00+08:00",
            "periods": [["2026-10-19", "2026-10-25"]],
            "due": "2026-10-26",
            "status": "done",
            "same": True,
        },
        {
            "case": "lifecycle",
            "text": "这周也要交一份运营周报，周五前交，这是新一周的任务。",
            "ts": "2026-10-26T10:00:00+08:00",
            "periods": [["2026-10-19", "2026-10-25"], ["2026-10-26", "2026-11-01"]],
            "count": 2,
        },
        {
            "case": "sunday",
            "text": "下周要做一次设备清洁检查。",
            "ts": "2026-10-25T23:59:00+08:00",
            "periods": [["2026-10-26", "2026-11-01"]],
        },
        {
            "case": "monday",
            "text": "我要补交上周的设备检查报告。",
            "ts": "2026-10-26T00:01:00+08:00",
            "periods": [["2026-10-19", "2026-10-25"]],
        },
        {"case": "year", "text": "本周要交财务周报。", "ts": "2026-01-01T09:00:00+08:00", "periods": [["2025-12-29", "2026-01-04"]]},
        {
            "case": "leap",
            "text": "下个月的设备巡检是一个任务，月底前完成。",
            "ts": "2028-01-31T09:00:00+08:00",
            "periods": [["2028-02-01", "2028-02-29"]],
            "due": "2028-02-29",
        },
        {
            "case": "vague",
            "text": "我要整理旧照片，过阵子做，具体日期没定。",
            "ts": "2026-10-20T09:00:00+08:00",
            "periods": [[None, None]],
            "due": None,
        },
        {
            "case": "clock",
            "text": "我要提交采购申请，截止明天下午3点。",
            "ts": "2026-10-20T09:00:00+08:00",
            "periods": [[None, None]],
            "due": "2026-10-21T15:00:00+08:00",
        },
    ]
    (output / "dataset.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2))
    previous, identities, results, queries = {}, {}, [], []
    calls = []

    def wrap(stage):
        def call(**kw):
            value = llm_client.complete_json(**kw)
            calls.append({"stage": stage, "input": json.loads(kw["user_prompt"]), "output": value})
            return value

        return call

    extractor.complete_json = wrap("extraction")
    task_cards.complete_json = wrap("association")
    for i, row in enumerate(rows):
        key = row["case"]
        folder = output / key
        folder.mkdir(exist_ok=True)
        repo.DB_PATH = folder / "memory.db"
        trace.TRACE_PATH = folder / "trace.jsonl"
        note = {
            "id": f"time-{i}",
            "space_id": key,
            "text": row["text"],
            "ts": row["ts"],
            "previous_messages": list(reversed(previous.get(key, [])[-3:])),
        }
        start = len(calls)
        report = process_note_memory(note)
        cards = [task_cards.evidence(r) for r in task_cards.records(key, "owner")]
        ranges = sorted([[c.get("period_start"), c.get("period_end")] for c in cards], key=str)
        checks = {
            "count": len(cards) == row.get("count", 1),
            "periods": ranges == sorted(row["periods"], key=str),
            "no_pending": not any(p.get("note", {}).get("id") == note["id"] for p in workflow_state.list_items(key, "pending")),
        }
        if "due" in row:
            checks["due"] = len(cards) == 1 and cards[0].get("due_at") == row["due"]
        if "status" in row:
            checks["status"] = len(cards) == 1 and cards[0]["task_status"] == row["status"]
        if row.get("same"):
            checks["same_card"] = {c["id"] for c in cards} == identities[key]
        identities[key] = {c["id"] for c in cards}
        previous.setdefault(key, []).append(
            {"note_id": note["id"], "text": note["text"], "ts": note["ts"], "role": "user", "sequence_no": i + 1}
        )
        result = {"input": row, "checks": checks, "passed": all(checks.values()), "cards": cards, "report": report, "calls": calls[start:]}
        results.append(result)
        (folder / f"step-{i}.json").write_text(json.dumps(result, ensure_ascii=False, indent=2))
        print(json.dumps({"case": key, "text": row["text"], "checks": checks}, ensure_ascii=False), flush=True)
        if key == "lifecycle" and row.get("count") == 2:
            for text, expected in [
                ("列出2026-10-19到2026-10-25的任务", "共 1 条"),
                ("列出2026-10-26到2026-11-01到期的未完成任务", "共 1 条"),
            ]:
                answer = answer_question(key, text)
                queries.append({"text": text, "answer": answer, "passed": expected in answer})
    summary = {
        "model": "deepseek-flash",
        "total_messages": len(results),
        "passed_messages": sum(r["passed"] for r in results),
        "results": results,
        "queries": queries,
        "storage": "isolated SQLite",
    }
    (output / "result.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(
        json.dumps({"passed_messages": summary["passed_messages"], "total_messages": len(results), "queries": queries}, ensure_ascii=False)
    )
    raise SystemExit(0 if all(r["passed"] for r in results + queries) else 1)


if __name__ == "__main__":
    main()
