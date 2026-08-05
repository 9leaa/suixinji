from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any


def read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def pct(value: Any) -> str:
    return "—" if value is None else f"{float(value):.2%}"


def number(value: Any, digits: int = 2) -> str:
    return "—" if value is None else f"{float(value):.{digits}f}"


def layer1(lines: list[str], root: Path) -> None:
    lines += [
        "## 实验 A：第一阶段离线抽取正确性（独立）",
        "",
        "- 数据：5 个 Layer 1 数据集，共 730 个 case。",
        "- Rules：仅规则抽取。Hybrid：规则 hints + 真实 DeepSeek LLM 权威抽取；LLM 失败不降级为 Rules，也不计为 LLM 成功。",
        "- 注意：这里的候选/字段指标不能与第二阶段状态演进指标合并。",
        "",
    ]
    headers = "| Dataset | Mode | Cases | Should-store F1 | Candidate F1 | Type Macro-F1 | Key-field Accuracy | All-fields Exact | LLM 成功/调用 | P50/P95 |"
    lines += [headers, "|---|---|---:|---:|---:|---:|---:|---:|---:|---:|"]
    for mode in ("rules", "hybrid"):
        metrics = read(root / f"layer1_{mode}" / "metrics.json")
        for dataset, result in metrics["datasets"].items():
            llm = result.get("llm")
            success = "—" if llm is None else f"{llm['success']}/{llm['calls']} ({pct(llm['success_rate'])})"
            runtime = result["runtime"]
            lines.append(
                "| {dataset} | {mode} | {cases} | {store} | {candidate} | {types} | {fields} | {exact} | {llm} | {p50}/{p95} ms |".format(
                    dataset=dataset,
                    mode=mode,
                    cases=result["cases"],
                    store=pct(result["should_store"]["should_store_f1"]),
                    candidate=pct(result["candidate"]["candidate_f1"]),
                    types=pct(result["candidate"]["memory_type_macro_f1"]),
                    fields=pct(result["fields"]["key_field_accuracy"]),
                    exact=pct(result["fields"]["all_fields_exact"]),
                    llm=success,
                    p50=number(runtime["latency_ms_p50"], 0),
                    p95=number(runtime["latency_ms_p95"], 0),
                )
            )
    lines += ["", "原始输出：`layer1_rules/cases.jsonl`、`layer1_hybrid/cases.jsonl`；失败样本分别在同目录的 `failures.jsonl`。", ""]


def layer2(lines: list[str], root: Path) -> None:
    metrics = read(root / "layer2_postgres" / "all" / "metrics.json")
    lines += [
        "## 实验 B：第二阶段 PostgreSQL 状态演进正确性（独立）",
        "",
        "- 输入：数据集提供的已验证 `MemoryCandidate`；不调用第一阶段抽取器，也不调用 Hybrid/LLM。",
        "- 后端：真实 PostgreSQL。普通 case 做 case-level 并发（3 个并发，受客户端连接池 3+2 的上限约束）；同一 case 严格按 `processing_order` 顺序执行；每个 case 完成快照后删除临时 Space。",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
    ]
    entries = [
        ("Cases / decisions", f"{metrics['cases']} / {metrics['decision_count']}"),
        ("Case Exact Match", pct(metrics["case_exact_match"])),
        ("Relation Macro-F1", pct(metrics["relation_macro_f1"])),
        ("Action Accuracy", pct(metrics["action_accuracy"])),
        ("Task transition accuracy", pct(metrics["task_transition_accuracy"])),
        ("Version sequence accuracy", pct(metrics["version_sequence_accuracy"])),
        ("Version creation accuracy", pct(metrics["version_creation_accuracy"])),
        ("Idempotence accuracy", pct(metrics["idempotence_accuracy"])),
        ("Source exact-set accuracy", pct(metrics["source_exact_set_accuracy"])),
        ("Duplicate active rate", pct(metrics["duplicate_active_rate"])),
        ("Stale active rate", pct(metrics["stale_active_rate"])),
        ("Orphan done task rate", pct(metrics["orphan_done_task_rate"])),
    ]
    lines += [f"| {key} | {value} |" for key, value in entries]
    lines += [
        "",
        "原始预测和失败样本：`layer2_postgres/<dataset>/predictions.jsonl`、`case_exact_failures.jsonl`、`runtime_errors.jsonl`。",
        "",
    ]


def concurrency(lines: list[str], root: Path) -> None:
    base = read(Path("eval/results/layer2_postgres_concurrency/layer2_postgres_concurrency_metrics.json"))
    extended = read(root / "layer2_postgres_concurrency" / "extended" / "metrics.json")
    lines += [
        "## 实验 C：PostgreSQL 并发 / 幂等专项（独立）",
        "",
        "| 专项 | 场景 | 不变量通过率 | 错误 | 重复 active | 重复 version | 重复 source | 跨空间污染 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
        f"| 基线 | 20 个 `concurrent_same` / `concurrent_conflict` case × 3 repeat = {base['cases']} | {pct(base['invariant_pass_rate'])} | {base['errors']} | {base['duplicate_active_cases']} | {base['duplicate_version_cases']} | {base['duplicate_source_cases']} | {base['foreign_space_cases']} |",
        f"| 扩展 | {extended['scenario_counts']}，共 {extended['total_scenarios']} 个结果 | {pct(extended['invariant_pass_rate'])} | {extended['errors']} | {extended['duplicate_active_cases']} | {extended['duplicate_version_cases']} | {extended['duplicate_source_cases']} | {extended['cross_space_contamination_cases']} |",
        "",
        "扩展覆盖完整重复投递、同 key 新来源、同 key 更新、冲突并发更新和 10 组跨空间成对隔离。逐 run 决策与状态快照：`layer2_postgres_concurrency/extended/results.jsonl`。",
        "",
    ]


def redis(lines: list[str], root: Path) -> None:
    metrics = read(root / "redis_worker_chain" / "metrics.json")
    lines += [
        "## 实验 D：Redis Stream + 真实分布式 worker 链路（独立）",
        "",
        "- 不经过飞书：先用 `InboxCommand` 建立与 receiver 相同的数据库任务契约，再把真实 task id 直接发布到 Redis Stream，由当前 ingest/memory worker 消费。",
        "- 所有测试使用独立 tenant / test spaces；无 Feishu 请求。",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
    ]
    entries = [
        ("Normal messages", metrics["normal_messages"]),
        ("Completed / completion rate", f"{metrics['completed']} / {pct(metrics['completion_rate'])}"),
        ("Duplicate Stream deliveries", metrics["duplicate_stream_deliveries"]),
        ("Idempotency pass", metrics["idempotency_pass"]),
        ("Cross-space isolation", metrics["cross_space_isolation_pass"]),
        ("Retry observed", metrics["retry_observed"]),
        ("Dead-letter observed", metrics["dead_letter_observed"]),
        (
            "E2E p50 / p95 / p99",
            f"{number(metrics['e2e_latency_ms']['p50'], 0)} / {number(metrics['e2e_latency_ms']['p95'], 0)} / {number(metrics['e2e_latency_ms']['p99'], 0)} ms",
        ),
        ("Throughput", f"{number(metrics['throughput_messages_per_second'], 3)} msg/s"),
    ]
    lines += [f"| {key} | {value} |" for key, value in entries]
    lines += [
        "",
        "失败样本是一个不存在 note 的 memory task：真实 worker 第一次失败后由 scheduler 重投，第二次进入 `dead_letter`。完整 task attempt、Stream id、轮询时间线和最终快照：`redis_worker_chain/poison_dead_letter.json`、`messages.jsonl`、`timeline.jsonl`、`space_snapshots.json`。",
        "",
    ]


def main() -> None:
    root = Path(sys.argv[1])
    lines = [
        "# 随心记记忆评测总报告",
        "",
        f"- 结果目录：`{root}`",
        "- 运行原则：四个实验分别报告，不将第一阶段抽取、第二阶段状态演进、PostgreSQL 并发和 Redis 端到端链路混成单一指标。",
        "- 测试业务数据均使用专用 eval tenant/space；Layer 2 临时 Space 已清理。",
        "",
    ]
    layer1(lines, root)
    layer2(lines, root)
    concurrency(lines, root)
    redis(lines, root)
    lines += [
        "## 结论边界",
        "",
        "- 第一阶段反映抽取模型能力；Hybrid 的 LLM 成功率、逐条原始候选和失败样本必须与规则基线一起解读。",
        "- 第二阶段反映在结构化候选已给定时的 relation/action/state 演进正确性，因而不代表 LLM 的抽取质量。",
        "- Redis 实验反映部署链路的吞吐、端到端延迟、幂等、重试和死信；其消息文本不是 gold 标注集，不能用来计算抽取 F1。",
        "",
    ]
    (root / "MEMORY_EVALUATION_EXPERIMENT_REPORT.md").write_text("\n".join(lines), encoding="utf-8")
    print(root / "MEMORY_EVALUATION_EXPERIMENT_REPORT.md")


if __name__ == "__main__":
    main()
