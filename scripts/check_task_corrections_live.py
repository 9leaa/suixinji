"""Replay T6 and ten new correction/transfer scenarios in isolated databases."""

import argparse
import json
import os
from pathlib import Path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--cases", nargs="*")
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
    from scripts.eval_task_cards_live import run_case
    from memory import repository as repo

    cases = []
    for key in ("subjects_09", "nonfacts_corrections_07", "nonfacts_corrections_09"):
        old = [json.loads(x) for x in (root / "eval/results/task-cards-100-live-20261020" / key / "steps.jsonl").read_text().splitlines()]
        cases.append(
            {
                "id": key,
                "name": key,
                "query": {"subjects_09": "团队车票", "nonfacts_corrections_07": "宣传视频", "nonfacts_corrections_09": "302"}[key],
                "steps": [
                    {
                        "text": r["text"],
                        "ts": r["message_time"],
                        "expect": {k: v for k, v in r["expected"].items() if k not in {"closures", "kinds"}},
                    }
                    for r in old
                ],
            }
        )

    def step(text, status="todo", subject=None, count=1, same=None, pending=False):
        expect = {"cards": count, "statuses": [status] * count, "pending": pending}
        if subject:
            expect["subjects"] = [subject] * count
        if same is not None:
            expect["same_as"] = same
        return {"text": text, "expect": expect}

    temporary = [
        (
            "转交姐姐",
            "采购耗材",
            [step("我要采购办公耗材。"), step("办公耗材采购这同一项任务转交姐姐负责，任务继续。", subject="姐姐", same=0)],
        ),
        (
            "纠正误报完成",
            "上传合同",
            [step("扫描合同已经上传完成。", "done"), step("纠正上一条，上传完成是口误，扫描合同其实还没上传，原任务继续。", same=0)],
        ),
        (
            "纠正房间保留时间",
            "402",
            [
                step("我要预约401会议室，截止周五下午3点完成预约。"),
                step("纠正房间号，是预约402会议室，不是401，仍是原任务，截止时间不变。", same=0),
            ],
        ),
        (
            "纠正执行人",
            "领取样品",
            [
                step("小王要领取实验样品。", subject="小王"),
                step("纠正执行人，领取实验样品的其实是小李，不是小王，还是同一项任务。", subject="小李", same=0),
            ],
        ),
        (
            "转交后转回本人",
            "打印资料",
            [
                step("我要打印会议资料。"),
                step("这项打印会议资料任务改由哥哥负责，任务继续。", subject="哥哥", same=0),
                step("打印会议资料这同一项任务改由我负责，哥哥不再负责，任务继续。", subject="用户", same=0),
            ],
        ),
        (
            "条件句不转交",
            "采购车票",
            [
                step("我要采购团队车票。"),
                step("如果把采购团队车票的任务转交哥哥负责就好了，目前还没决定，仍由我负责。", subject="用户", same=0),
            ],
        ),
        (
            "否定转交",
            "购买礼品",
            [step("我要购买客户礼品。"), step("不要把购买客户礼品的任务转交哥哥，这个任务仍由我负责。", subject="用户", same=0)],
        ),
        ("询问不改状态", "审核报价", [step("我要审核供应商报价。"), step("供应商报价审核完成了吗？", same=0)]),
        (
            "独立新任务不覆盖",
            "会议室",
            [step("我要预约501会议室。"), step("另建一个独立任务：预约502会议室，原501预约任务继续。", count=2)],
        ),
        (
            "轮次不明不猜测",
            "设备检查",
            [
                step("我要做本周设备检查，还要做下周设备检查，这是两次任务。", count=2),
                step("设备检查这项任务转交哥哥，但我还没确定转交的是本周还是下周的那次。", count=2, same=0, pending=True),
            ],
        ),
    ]
    for i, (name, query, steps) in enumerate(temporary, 1):
        cases.append({"id": f"temporary_{i:02}", "name": name, "query": query, "steps": steps})
    if args.cases:
        cases = [c for c in cases if c["id"] in args.cases]
    (output / "dataset.json").write_text(json.dumps({"cases": cases}, ensure_ascii=False, indent=2))
    results = []
    for case in cases:
        result = run_case(case, str(output))
        rows = [json.loads(x) for x in (output / case["id"] / "steps.jsonl").read_text().splitlines()]
        final = rows[-1]
        cards = final["cards"]
        checks = {"step_checks": all(r["passed"] for r in rows)}
        records = [repo.get_memory(c["id"]) for c in cards]
        checks["database_identity_matches_card"] = all(
            r.subject == c["subject"] and r.object_value == c["task"] for r, c in zip(records, cards)
        )
        checks["owner_unchanged"] = all(r.scope.get("owner_id") == "owner" for r in records)
        key = case["id"]
        if key in {"nonfacts_corrections_09", "temporary_03"}:
            room = "302" if key == "nonfacts_corrections_09" else "402"
            oldroom = "301" if room == "302" else "401"
            checks["correct_room_identity"] = len(cards) == 1 and room in cards[0]["task"] and oldroom not in cards[0]["task"]
            checks["time_preserved"] = len(cards) == 1 and "周五" in cards[0]["description"] and "下午" in cards[0]["description"]
            if key == "temporary_03":
                checks["due_preserved"] = len(cards) == 1 and cards[0].get("due_at") == "2026-10-23T15:00:00+08:00"
        if key == "temporary_09":
            checks["both_room_identities"] = (
                len(cards) == 2 and any("501" in c["task"] for c in cards) and any("502" in c["task"] for c in cards)
            )
        if key == "temporary_05":
            checks["self_identity_restored"] = len(records) == 1 and records[0].scope.get("subject_id") == "user:owner"
        if key in {"temporary_06", "temporary_07", "temporary_08", "temporary_10"}:
            checks["no_identity_or_status_change"] = all(c["subject"] == "用户" and c["task_status"] == "todo" for c in cards)
        result.update(checks=checks, target_passed=all(checks.values()), rows=rows)
        results.append(result)
        print(
            json.dumps({"case": key, "name": case["name"], "checks": checks, "failures": result["failed_steps"]}, ensure_ascii=False),
            flush=True,
        )
    summary = {
        "model": "deepseek-flash",
        "storage": "isolated SQLite",
        "original_passed": sum(r["target_passed"] for r in results if not r["id"].startswith("temporary_")),
        "temporary_passed": sum(r["target_passed"] for r in results if r["id"].startswith("temporary_")),
        "original_count": sum(not r["id"].startswith("temporary_") for r in results),
        "temporary_count": sum(r["id"].startswith("temporary_") for r in results),
        "total_messages": sum(r["steps"] for r in results),
        "results": results,
    }
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2))
    print(json.dumps({k: v for k, v in summary.items() if k != "results"}, ensure_ascii=False))
    return all(r["target_passed"] for r in results)


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
