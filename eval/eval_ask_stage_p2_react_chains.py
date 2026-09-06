"""P2 Ask evaluation: real-LLM multi-step ReAct tool-chain correctness."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
from pathlib import Path
import statistics
import sys
import time
from typing import Any

ROOT = Path.cwd()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.query_agent import REACT_SYSTEM_PROMPT
from core.llm_client import complete_json

TOOLS = {"search_notes", "get_note", "get_related_notes", "search_memories", "search_tasks"}


def build_cases() -> list[dict[str, Any]]:
    groups = {
        "note_then_related": [
            ("找出我关于 RAG 分块的笔记，再查看和它关联的笔记。", "RAG 分块"),
            ("先找到飞书 webhook 部署记录，再列出相关 Note。", "飞书 webhook"),
            ("查到 SQLite 锁冲突那条笔记，并看看它关联了什么。", "SQLite 锁冲突"),
            ("定位 Agent Memory 论文笔记，再查它的关联记录。", "Agent Memory"),
            ("找到可观测性日志的原始 Note，再查看关联 Note。", "可观测性日志"),
            ("先找服务器账单笔记，然后列出与它相关的笔记。", "服务器账单"),
        ],
        "note_then_detail": [
            ("找到 RAG 评测那条笔记，然后读取完整正文。", "RAG 评测"),
            ("先搜索毕业答辩记录，再打开候选 Note 的详情。", "毕业答辩"),
            ("找到植物园记录，然后读取这条 Note 的完整内容。", "植物园"),
            ("先定位 DeepSeek 网络问题笔记，再读取正文。", "DeepSeek 网络"),
            ("找到 Agent 简历笔记，再打开详情。", "Agent 简历"),
            ("先查 PostgreSQL 并发测试记录，再读取全文。", "PostgreSQL 并发"),
        ],
        "task_and_preference": [
            ("我当前有哪些任务，并且我偏好什么报告风格？", "报告风格"),
            ("毕业论文任务现在怎样，我又喜欢什么饮料？", "饮料"),
            ("随心记改造做到哪了，同时查一下我的回答风格偏好。", "回答风格"),
            ("供应商切换任务是什么状态，我对早餐饮品有什么偏好？", "早餐饮品"),
            ("Agent 简历任务完成了吗，我平时避开什么食物？", "食物"),
            ("截图整理还有没有完成，并告诉我咖啡相关偏好。", "咖啡"),
        ],
        "task_current_and_timeline": [
            ("毕业论文任务现在是什么状态，过去又经历了哪些变化？", "毕业论文"),
            ("随心记记忆模块当前做到哪，完整演进过程是什么？", "随心记记忆模块"),
            ("Agent 简历任务目前状态和历史时间线分别是什么？", "Agent 简历"),
            ("部署任务现在完成了吗，从开始到现在怎么变化的？", "部署任务"),
            ("RAG 学习任务当前状态及过去版本是什么？", "RAG 学习"),
            ("供应商切换当前进度和历史过程分别怎样？", "供应商切换"),
        ],
        "note_and_memory": [
            ("找出我保存的北京旅行笔记，并告诉我长期记录里现在住哪里。", "北京旅行"),
            ("查找咖啡原始 Note，同时总结我的咖啡偏好。", "咖啡"),
            ("找毕业答辩的原始笔记，并回忆这次经历。", "毕业答辩"),
            ("查 Agent 简历原始记录，也告诉我长期背景中的简历方向。", "Agent 简历"),
            ("找植物园 Note，并概括我记录过的植物园经历。", "植物园"),
            ("搜索 RAG 学习笔记，同时说明我长期重点学习什么。", "RAG 学习"),
        ],
    }
    cases = []
    index = 0
    for group, items in groups.items():
        for question, topic in items:
            index += 1
            if group == "note_then_related":
                required = ["search_notes", "get_related_notes"]
                order_strict = True
            elif group == "note_then_detail":
                required = ["search_notes", "get_note"]
                order_strict = True
            elif group == "task_and_preference":
                required = ["search_tasks:current", "search_memories:preference"]
                order_strict = False
            elif group == "task_current_and_timeline":
                required = ["search_tasks:current", "search_tasks:timeline"]
                order_strict = False
            else:
                memory_type = {
                    "北京旅行": "semantic",
                    "咖啡": "preference",
                    "毕业答辩": "episodic",
                    "Agent 简历": "semantic",
                    "植物园": "episodic",
                    "RAG 学习": "semantic",
                }[topic]
                required = ["search_notes", f"search_memories:{memory_type}"]
                order_strict = False
            cases.append(
                {
                    "case_id": f"p2_{index:03d}",
                    "group": group,
                    "question": question,
                    "topic": topic,
                    "required": required,
                    "order_strict": order_strict,
                }
            )
    return cases


def action_token(action: str, args: dict[str, Any]) -> str:
    if action == "search_tasks":
        return f"search_tasks:{str(args.get('view') or 'current').casefold()}"
    if action == "search_memories":
        values = args.get("memory_types") or []
        if isinstance(values, str):
            values = [values]
        normalized = {str(value).casefold() for value in values}
        if len(normalized) == 1:
            return f"search_memories:{next(iter(normalized))}"
    return action


def tool_result(case: dict[str, Any], action: str, args: dict[str, Any]) -> Any:
    topic = case["topic"]
    if action == "search_notes":
        return [
            {
                "id": f"note_{case['case_id']}",
                "title": f"{topic}记录",
                "summary": f"这是与{topic}直接相关的原始记录。",
                "score": 0.94,
            }
        ]
    if action == "get_note":
        return {
            "id": str(args.get("note_id") or f"note_{case['case_id']}"),
            "title": f"{topic}记录",
            "text": f"{topic}的完整正文与必要细节。",
        }
    if action == "get_related_notes":
        return [
            {
                "id": f"related_{case['case_id']}",
                "title": f"{topic}关联记录",
                "summary": f"与{topic}存在直接关联。",
            }
        ]
    if action == "search_tasks":
        view = str(args.get("view") or "current")
        if view == "timeline":
            return [
                {"id": f"task_{case['case_id']}_v1", "content": f"{topic}最初为 todo", "task_status": "todo"},
                {"id": f"task_{case['case_id']}_v2", "content": f"{topic}后来为 done", "task_status": "done"},
            ]
        return [{"id": f"task_{case['case_id']}", "content": f"{topic}当前已完成", "task_status": "done"}]
    if action == "search_memories":
        values = args.get("memory_types") or []
        if isinstance(values, str):
            values = [values]
        memory_type = str(values[0] if values else "semantic")
        return [
            {
                "id": f"memory_{case['case_id']}",
                "memory_type": memory_type,
                "content": f"与{topic}相关的长期{memory_type}证据。",
                "updated_at": "2026-08-31T10:00:00+08:00",
            }
        ]
    return {"error": f"unknown tool: {action}"}


def valid_args(
    action: str,
    args: dict[str, Any],
    observations: list[dict[str, Any]],
    question: str,
) -> bool:
    if action in {"search_notes", "search_memories", "search_tasks"}:
        if action == "search_tasks" and not str(args.get("query") or "").strip():
            return str(args.get("view") or "current") == "current" and any(
                marker in question for marker in ("有哪些任务", "当前任务", "任务清单")
            )
        return bool(str(args.get("query") or "").strip())
    if action in {"get_note", "get_related_notes"}:
        note_id = str(args.get("note_id") or "")
        observed_ids = {
            str(item.get("id") or "")
            for observation in observations
            for item in (observation.get("result") if isinstance(observation.get("result"), list) else [observation.get("result")])
            if isinstance(item, dict)
        }
        return bool(note_id) and note_id in observed_ids
    return False


def ordered_coverage(required: list[str], actual: list[str]) -> bool:
    position = 0
    for token in actual:
        if position < len(required) and token == required[position]:
            position += 1
    return position == len(required)


def evaluate_case(case: dict[str, Any], retries: int, max_steps: int) -> dict[str, Any]:
    observations: list[dict[str, Any]] = []
    calls: list[dict[str, Any]] = []
    attempts: list[dict[str, Any]] = []
    final_answer = ""
    error = None
    started = time.perf_counter()
    premature_final = False
    for step in range(1, max_steps + 1):
        payload = {"question": case["question"], "step": step, "observations": observations}
        decision = None
        for attempt in range(1, retries + 2):
            one = time.perf_counter()
            try:
                decision = complete_json(
                    REACT_SYSTEM_PROMPT,
                    json.dumps(payload, ensure_ascii=False, indent=2),
                    model_role="strong",
                    llm_task="query_complex_reasoning",
                    timeout_seconds=45,
                )
                attempts.append({"step": step, "attempt": attempt, "ok": True, "latency_s": round(time.perf_counter() - one, 4)})
                break
            except Exception as exc:
                error = f"{type(exc).__name__}: {exc}"
                attempts.append(
                    {
                        "step": step,
                        "attempt": attempt,
                        "ok": False,
                        "latency_s": round(time.perf_counter() - one, 4),
                        "error": error,
                    }
                )
        if not isinstance(decision, dict):
            break
        answer = str(decision.get("final_answer") or "").strip()
        if answer:
            final_answer = answer
            actual = [row["token"] for row in calls]
            premature_final = not all(token in actual for token in case["required"])
            error = None
            break
        action = str(decision.get("action") or "")
        args = decision.get("args") if isinstance(decision.get("args"), dict) else {}
        token = action_token(action, args)
        arg_ok = valid_args(action, args, observations, case["question"])
        result = tool_result(case, action, args)
        calls.append({"step": step, "action": action, "token": token, "args": args, "args_ok": arg_ok})
        observations.append({"thought": decision.get("thought"), "tool": action, "args": args, "result": result})

    actual = [row["token"] for row in calls]
    required = list(case["required"])
    coverage = sum(token in actual for token in required) / len(required)
    chain_complete = coverage == 1.0
    order_ok = ordered_coverage(required, actual) if case["order_strict"] else chain_complete
    required_counts = {token: required.count(token) for token in set(required)}
    extra_calls = sum(max(0, actual.count(token) - required_counts.get(token, 0)) for token in set(actual))
    return {
        **case,
        "calls": calls,
        "observations": observations,
        "actual_tokens": actual,
        "final_answer": final_answer,
        "error": error,
        "request_ok": error is None,
        "required_tool_coverage": round(coverage, 6),
        "chain_complete": chain_complete,
        "order_ok": order_ok,
        "args_ok": bool(calls) and all(row["args_ok"] for row in calls),
        "terminated_with_final": bool(final_answer),
        "premature_final": premature_final,
        "invalid_action": any(row["action"] not in TOOLS for row in calls),
        "extra_calls": extra_calls,
        "tool_call_count": len(calls),
        "attempts": attempts,
        "latency_s": round(time.perf_counter() - started, 4),
    }


def percentile(values: list[float], p: float) -> float:
    return sorted(values)[max(0, min(len(values) - 1, math.ceil(p * len(values)) - 1))] if values else 0.0


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)

    def rate(key: str) -> float:
        return round(sum(bool(row[key]) for row in rows) / total, 6)

    latencies = [float(row["latency_s"]) for row in rows if row["request_ok"]]
    calls = [int(row["tool_call_count"]) for row in rows]
    return {
        "cases": total,
        "request_success_rate": rate("request_ok"),
        "required_tool_coverage": round(statistics.fmean(float(row["required_tool_coverage"]) for row in rows), 6),
        "chain_complete_rate": rate("chain_complete"),
        "order_accuracy": rate("order_ok"),
        "argument_accuracy": rate("args_ok"),
        "final_termination_rate": rate("terminated_with_final"),
        "premature_final_rate": rate("premature_final"),
        "invalid_action_rate": rate("invalid_action"),
        "redundant_call_rate": round(sum(int(row["extra_calls"]) for row in rows) / max(1, sum(calls)), 6),
        "mean_tool_calls": round(statistics.fmean(calls), 4),
        "p95_tool_calls": percentile([float(value) for value in calls], 0.95),
        "latency_s": {
            "mean": round(statistics.fmean(latencies), 4) if latencies else 0.0,
            "p50": round(percentile(latencies, 0.50), 4),
            "p95": round(percentile(latencies, 0.95), 4),
        },
        "by_group": {
            group: {
                "cases": len(bucket := [row for row in rows if row["group"] == group]),
                "chain_complete_rate": round(sum(row["chain_complete"] for row in bucket) / len(bucket), 6),
                "premature_final_rate": round(sum(row["premature_final"] for row in bucket) / len(bucket), 6),
            }
            for group in sorted({row["group"] for row in rows})
        },
    }


def pct(value: float) -> str:
    return f"{value:.2%}"


def report_text(metrics: dict[str, Any], failures: list[dict[str, Any]]) -> str:
    latency = metrics["latency_s"]
    lines = [
        "# Ask P2 多步 ReAct 工具链报告",
        "",
        "- 数据：30 条双目标问题；真实 LLM，固定工具 observation，不访问数据库、不测召回和答案语义。",
        "- 目标：只判断模型是否把两步证据调用完整，并正确停止。",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| 请求成功率 | {pct(metrics['request_success_rate'])} |",
        f"| 必需工具覆盖率 | {pct(metrics['required_tool_coverage'])} |",
        f"| 完整工具链成功率 | {pct(metrics['chain_complete_rate'])} |",
        f"| 严格顺序准确率 | {pct(metrics['order_accuracy'])} |",
        f"| 参数准确率 | {pct(metrics['argument_accuracy'])} |",
        f"| 正常 Final 终止率 | {pct(metrics['final_termination_rate'])} |",
        f"| 提前回答率 | {pct(metrics['premature_final_rate'])} |",
        f"| 非法工具率 | {pct(metrics['invalid_action_rate'])} |",
        f"| 冗余调用率 | {pct(metrics['redundant_call_rate'])} |",
        f"| 工具调用 Mean/P95 | {metrics['mean_tool_calls']:.2f}/{metrics['p95_tool_calls']:.0f} |",
        f"| 总耗时 Mean/P50/P95 | {latency['mean']:.3f}/{latency['p50']:.3f}/{latency['p95']:.3f}s |",
        "",
        "## 分场景",
        "",
        "| 场景 | Cases | 完整工具链 | 提前回答 |",
        "|---|---:|---:|---:|",
    ]
    for group, row in metrics["by_group"].items():
        lines.append(
            f"| {group} | {row['cases']} | {pct(row['chain_complete_rate'])} | {pct(row['premature_final_rate'])} |"
        )
    lines += [
        "",
        "## 结论边界",
        "",
        f"- 严格失败 {len(failures)} 条，见 `failures.jsonl`。",
        "- 固定 observation 隔离了检索质量；本阶段不证明证据真实、引用正确或最终答案正确。",
    ]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--retries", type=int, default=1)
    parser.add_argument("--max-steps", type=int, default=4)
    args = parser.parse_args()
    output = Path(args.output_dir)
    output.mkdir(parents=True, exist_ok=True)
    cases = build_cases()
    rows = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = [executor.submit(evaluate_case, case, max(0, args.retries), max(2, args.max_steps)) for case in cases]
        for index, future in enumerate(as_completed(futures), 1):
            row = future.result()
            rows.append(row)
            print(f"progress={index}/{len(futures)} chain={row['chain_complete']}", flush=True)
    rows.sort(key=lambda row: row["case_id"])
    metrics = aggregate(rows)
    failures = [
        row
        for row in rows
        if not row["chain_complete"] or not row["order_ok"] or not row["args_ok"] or not row["terminated_with_final"]
    ]
    (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    with (output / "cases.jsonl").open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    with (output / "failures.jsonl").open("w", encoding="utf-8") as handle:
        for row in failures:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
    (output / "ASK_P2_REACT_CHAIN_EVALUATION.md").write_text(report_text(metrics, failures), encoding="utf-8")
    print(json.dumps({"metrics": metrics, "failure_count": len(failures)}, ensure_ascii=False, indent=2), flush=True)


if __name__ == "__main__":
    main()
