"""Real-LLM A/B evaluation for the legacy and business-level ReAct tool contracts."""

from __future__ import annotations

import argparse
import ast
from concurrent.futures import ThreadPoolExecutor, as_completed
import json
import math
from pathlib import Path
import statistics
import subprocess
import sys
import time
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if not (ROOT / "agent").exists():
    ROOT = Path.cwd()
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.query_agent import DEFAULT_QUERY_MIN_SCORE, REACT_SYSTEM_PROMPT
from core.settings import QUERY_TOP_K
from core.llm_client import complete_json


def legacy_prompt() -> str:
    source = subprocess.check_output(["git", "show", "HEAD:agent/query_agent.py"], cwd=ROOT, text=True)
    for node in ast.parse(source).body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "REACT_SYSTEM_PROMPT" for t in node.targets):
            return eval(
                compile(ast.Expression(node.value), "<legacy-prompt>", "eval"),
                {
                    "QUERY_TOP_K": QUERY_TOP_K,
                    "QUERY_MIN_SCORE": DEFAULT_QUERY_MIN_SCORE,
                },
            )
    raise RuntimeError("legacy prompt not found")


GROUPS = {
    "note_general": [
        "我保存过哪些关于 RAG 分块的笔记？",
        "查一下之前记录的飞书 webhook 部署资料。",
        "哪条原始记录提到了服务器账单？",
        "帮我找关于 SQLite 锁冲突的笔记正文。",
        "我之前记下的 Agent Memory 论文内容是什么？",
        "搜索笔记里和可观测性日志有关的内容。",
    ],
    "note_filter": [
        "找出 type 是学习、标签包含论文的笔记。",
        "只看生活类里带饮食标签的原始记录。",
        "筛选类型为资料且标签为文档的 Note。",
        "给我任务类、带提醒标签的笔记。",
        "查找 type=灵感、tags=产品想法 的记录。",
        "列出情绪类中标签含压力的笔记。",
    ],
    "note_recent": [
        "查看最近 3 天保存的笔记。",
        "我近 7 天写过哪些原始记录？",
        "列出过去 14 天的 Note。",
        "最近 30 天关于求职的笔记有哪些？",
        "只查最近 2 天带提醒标签的笔记。",
        "看一下近一周学习类的记录。",
    ],
    "preference": [
        "我现在喜欢喝什么饮料？",
        "查一下我对报告排版的偏好。",
        "我有没有不喜欢的早餐饮品？",
        "我平时偏好什么样的回答风格？",
        "关于咖啡，我长期有什么习惯？",
        "我明确说过要避开什么食物？",
    ],
    "semantic": [
        "我现在住在哪个城市？",
        "我目前主要负责什么工作？",
        "我的简历方向是什么？",
        "我现在重点学习哪些技术？",
        "查一下我的长期背景信息。",
        "我当前所在的公司是什么？",
    ],
    "episodic": [
        "我最近参加过什么复盘会？",
        "之前去植物园发生了什么？",
        "我上次面试的经历怎么样？",
        "最近有哪些已经发生的求职事件？",
        "回忆一下我提到的毕业答辩经历。",
        "我过去记录过哪些旅行经历？",
    ],
    "task_current": [
        "我现在有哪些待办任务？",
        "毕业论文答辩这项任务完成了吗？",
        "随心记记忆模块目前做到哪了？",
        "列出当前已经完成的任务。",
        "大模型供应商切换任务是什么状态？",
        "我还有哪些事情没做完？",
    ],
    "task_timeline": [
        "展示毕业论文任务从开始到完成的变化过程。",
        "随心记记忆模块这项任务历史上怎么变化的？",
        "查看 Agent 简历任务的完整时间线。",
        "这个部署任务最初是什么状态，后来如何演进？",
        "列出 RAG 学习任务过去的状态变化。",
        "查询大模型供应商切换任务的历史版本。",
    ],
}


def build_cases() -> list[dict[str, Any]]:
    cases = []
    for group, questions in GROUPS.items():
        for question in questions:
            expected: dict[str, Any] = {"query_required": group in {"note_general", "preference", "semantic", "episodic", "task_timeline"}}
            if group == "note_filter":
                expected["filter_required"] = True
            if group == "note_recent":
                expected["days_required"] = True
            if group in {"preference", "semantic", "episodic"}:
                expected["memory_type"] = group
            if group == "task_current":
                expected["view"] = "current"
            if group == "task_timeline":
                expected["view"] = "timeline"
            if group.startswith("note_"):
                new_action = "search_notes"
                legacy = ["filter_notes" if group == "note_filter" else "list_recent" if group == "note_recent" else "semantic_search"]
            elif group in {"preference", "semantic", "episodic"}:
                new_action, legacy = "search_memories", ["memory_search"]
            else:
                new_action, legacy = "search_tasks", ["task_status_search", "memory_search"]
            cases.append({"group": group, "question": question, "new_action": new_action, "legacy_actions": legacy, "expected": expected})
    for question, new_action, legacy_action, note_id in [
        ("请读取 note_7f2a 的详细正文。", "get_note", "get_note", "note_7f2a"),
        ("打开笔记 n_2048 看完整内容。", "get_note", "get_note", "n_2048"),
        ("读取 note_rag_01 的详情。", "get_note", "get_note", "note_rag_01"),
        ("查看 note_7f2a 关联的其他笔记。", "get_related_notes", "follow_links", "note_7f2a"),
        ("n_2048 指向和被指向的 Note 有哪些？", "get_related_notes", "follow_links", "n_2048"),
        ("列出 note_rag_01 的关联笔记。", "get_related_notes", "follow_links", "note_rag_01"),
    ]:
        cases.append(
            {
                "group": "explicit_id",
                "question": question,
                "new_action": new_action,
                "legacy_actions": [legacy_action],
                "expected": {"note_id": note_id},
            }
        )
    for index, case in enumerate(cases, 1):
        case["case_id"] = f"tool_{index:03d}"
    return cases


SCHEMAS = {
    "search_notes": {"query", "type", "note_type", "tags", "days", "match_all_tags", "limit", "min_score"},
    "get_note": {"note_id"},
    "get_related_notes": {"note_id", "limit"},
    "search_memories": {"query", "memory_types", "time_scope", "limit", "min_score"},
    "search_tasks": {"query", "view", "status", "limit", "min_score"},
    "filter_notes": {"type", "note_type", "tags", "match_all_tags", "limit"},
    "semantic_search": {"query", "top_k", "limit", "min_score"},
    "list_recent": {"days", "limit"},
    "follow_links": {"note_id", "limit"},
    "memory_search": {"query", "memory_type", "limit", "min_score"},
    "task_status_search": {"query", "limit", "min_score"},
}


def type_values(args: dict[str, Any]) -> set[str]:
    value = args.get("memory_types", args.get("memory_type"))
    if isinstance(value, str):
        return {p.strip().casefold() for p in value.replace("，", ",").split(",") if p.strip()}
    if isinstance(value, list):
        return {str(p).strip().casefold() for p in value if str(p).strip()}
    return set()


def score(case: dict[str, Any], variant: str, output: dict[str, Any] | None, error: str | None) -> dict[str, Any]:
    output = output or {}
    action = str(output.get("action") or "").strip()
    args = output.get("args") if isinstance(output.get("args"), dict) else {}
    action_ok = action in ([case["new_action"]] if variant == "new" else case["legacy_actions"])
    schema_ok = bool(action) and action in SCHEMAS and set(args).issubset(SCHEMAS[action])
    expected = case["expected"]
    semantic_ok = True
    if expected.get("query_required"):
        semantic_ok &= bool(str(args.get("query") or "").strip())
    if expected.get("filter_required"):
        semantic_ok &= bool(args.get("type") or args.get("note_type") or args.get("tags"))
    if expected.get("days_required"):
        semantic_ok &= isinstance(args.get("days"), (int, float)) and args["days"] > 0
    if expected.get("memory_type"):
        values = type_values(args)
        semantic_ok &= expected["memory_type"] in values if variant == "new" else (not values or expected["memory_type"] in values)
    if expected.get("view") and variant == "new":
        semantic_ok &= str(args.get("view") or "current").casefold() == expected["view"]
    if expected.get("note_id"):
        semantic_ok &= str(args.get("note_id") or "") == expected["note_id"]
    request_ok = error is None and isinstance(output, dict)
    return {
        "request_ok": request_ok,
        "action_ok": action_ok,
        "schema_ok": schema_ok,
        "semantic_args_ok": semantic_ok,
        "contract_ok": request_ok and action_ok and schema_ok and semantic_ok,
        "action": action,
        "args": args,
    }


def call(prompt: str, case: dict[str, Any], variant: str, retries: int) -> dict[str, Any]:
    payload = {"question": case["question"], "step": 1, "observations": []}
    attempts = []
    started = time.perf_counter()
    output = None
    error = None
    for attempt in range(1, retries + 2):
        one = time.perf_counter()
        try:
            output = complete_json(
                prompt, json.dumps(payload, ensure_ascii=False), model_role="fast", llm_task="query_routing", timeout_seconds=30
            )
            attempts.append({"attempt": attempt, "ok": True, "latency_s": round(time.perf_counter() - one, 4)})
            error = None
            break
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            attempts.append({"attempt": attempt, "ok": False, "latency_s": round(time.perf_counter() - one, 4), "error": error})
    return {
        "case_id": case["case_id"],
        "group": case["group"],
        "question": case["question"],
        "variant": variant,
        "expected_action": case["new_action"] if variant == "new" else case["legacy_actions"],
        "output": output,
        "error": error,
        "attempts": attempts,
        "latency_s": round(time.perf_counter() - started, 4),
        **score(case, variant, output, error),
    }


def percentile(values: list[float], p: float) -> float:
    return sorted(values)[max(0, min(len(values) - 1, math.ceil(p * len(values)) - 1))] if values else 0.0


def aggregate(rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(rows)
    result: dict[str, Any] = {"cases": total}
    for key in ("request_ok", "action_ok", "schema_ok", "semantic_args_ok", "contract_ok"):
        count = sum(bool(r[key]) for r in rows)
        result[key] = {"count": count, "rate": round(count / total, 4)}
    latencies = [r["latency_s"] for r in rows if r["request_ok"]]
    result["latency_s"] = {
        "mean": round(statistics.mean(latencies), 4),
        "p50": round(percentile(latencies, 0.5), 4),
        "p95": round(percentile(latencies, 0.95), 4),
        "max": round(max(latencies), 4),
    }
    result["retry_cases"] = sum(len(r["attempts"]) > 1 for r in rows)
    result["by_group"] = {
        g: {
            "cases": len(b := [r for r in rows if r["group"] == g]),
            "action_accuracy": round(sum(r["action_ok"] for r in b) / len(b), 4),
            "contract_accuracy": round(sum(r["contract_ok"] for r in b) / len(b), 4),
        }
        for g in sorted({r["group"] for r in rows})
    }
    return result


def markdown(report: dict[str, Any]) -> str:
    lines = [
        "# ReAct 工具契约优化真实 LLM 对照报告",
        "",
        f"- 冻结数据集：{report['case_count']} 条；只测第一步工具选择和参数，不执行检索、不调用 embedding。",
        "- 首轮网络失败允许补发 1 次；主指标使用最终结果，补发 Case 单独记录。",
        "",
        "## 总体指标",
        "",
        "| 版本 | 请求成功率 | 工具选择准确率 | Schema 有效率 | 语义参数准确率 | 完整契约准确率 | P50 | P95 | 补发 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for variant, label in (("legacy", "旧版低层工具"), ("new", "新版业务工具")):
        s = report["summary"][variant]
        lines.append(
            f"| {label} | {s['request_ok']['rate']:.2%} | {s['action_ok']['rate']:.2%} | {s['schema_ok']['rate']:.2%} | {s['semantic_args_ok']['rate']:.2%} | {s['contract_ok']['rate']:.2%} | {s['latency_s']['p50']:.3f}s | {s['latency_s']['p95']:.3f}s | {s['retry_cases']} |"
        )
    lines += ["", "## 分组完整契约准确率", "", "| 场景 | 旧版 | 新版 | 差值 |", "|---|---:|---:|---:|"]
    for group, value in report["summary"]["new"]["by_group"].items():
        old = report["summary"]["legacy"]["by_group"][group]["contract_accuracy"]
        new = value["contract_accuracy"]
        lines.append(f"| {group} | {old:.2%} | {new:.2%} | {new - old:+.2%} |")
    failures = [r for r in report["results"] if not r["contract_ok"]]
    lines += ["", "## 失败样本", ""]
    lines += [
        f"- `{r['variant']}/{r['case_id']}` {r['question']} -> `{r['action'] or 'NONE'}` {json.dumps(r['args'], ensure_ascii=False)}"
        for r in failures
    ] or ["无。"]
    lines += ["", "## 结论边界", "", "本报告只证明 LLM 能否理解工具合同，不证明底层召回质量和最终回答正确率；后两项必须另测。"]
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--workers", type=int, default=3)
    parser.add_argument("--retries", type=int, default=1)
    args = parser.parse_args()
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    cases = build_cases()
    prompts = {"legacy": legacy_prompt(), "new": REACT_SYSTEM_PROMPT}
    tasks = [(variant, case) for variant in ("legacy", "new") for case in cases]
    results = []
    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
        futures = [pool.submit(call, prompts[v], c, v, max(0, args.retries)) for v, c in tasks]
        for done, future in enumerate(as_completed(futures), 1):
            row = future.result()
            results.append(row)
            print(f"[{done}/{len(tasks)}] {row['variant']} {row['case_id']} contract={row['contract_ok']}", flush=True)
    results.sort(key=lambda r: (r["case_id"], r["variant"]))
    report = {
        "case_count": len(cases),
        "call_count": len(results),
        "summary": {v: aggregate([r for r in results if r["variant"] == v]) for v in ("legacy", "new")},
        "results": results,
    }
    (out / "query_tool_contract_results.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (out / "QUERY_TOOL_CONTRACT_EVALUATION.md").write_text(markdown(report), encoding="utf-8")
    print(json.dumps(report["summary"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
