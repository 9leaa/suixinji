"""文件作用：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。

项目关系：本文件依赖 `agent.query_planner`、`core.sensitive`、`memory`、`memory.candidate_validator` 等 7 个模块；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import tempfile
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SAFE_ENV = {
    "STORAGE_BACKEND": "local",
    "TASK_QUEUE_BACKEND": "local",
    "COORDINATION_BACKEND": "local",
    "SUIXINJI_AGENT_HOOKS_ENABLED": "false",
    "SUIXINJI_MEMORY_EXTRACTOR_MODE": "rules",
    "SUIXINJI_MEMORY_RETRIEVAL_MODE": "hybrid",
    "SUIXINJI_MEMORY_CLAUSE_EXTRACTION_ENABLED": "true",
    "SUIXINJI_MEMORY_EXTRACTOR_SCHEMA_V3_ENABLED": "true",
    "SUIXINJI_MEMORY_CANONICAL_KEY_V3_ENABLED": "true",
    "SUIXINJI_MEMORY_RELATION_GUARD_V3_ENABLED": "true",
    "SUIXINJI_QUERY_INTENT_MODEL_ENABLED": "false",
    "SUIXINJI_QUERY_ROUTER_LLM_ON_UNCERTAIN": "false",
    "SUIXINJI_QUERY_ROUTER_LLM_ON_LOW_RECALL": "false",
}

for key, value in SAFE_ENV.items():
    os.environ[key] = value

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.query_planner import build_query_plan
from core.sensitive import contains_sensitive_data, redact_sensitive_text
from memory import repository as memory_repository
from memory import trace as memory_trace
from memory.candidate_validator import validate_candidates
from memory.extractor import extract_candidates
from memory.repository import (
    list_memories,
    purge_memory,
    search_memories,
    soft_delete_memory,
    stats,
)
from memory.service import process_note_memory


RESULTS_DIR = ROOT / "eval" / "results"
DATASET_PATH = ROOT / "eval" / "data" / "resume_memory_system_benchmark_v1.json"
DATASET_CREATED_AT = "2026-07-27T00:00:00+00:00"


def _pct(value: float) -> str:
    """函数功能：`_pct` 负责格式化百分比，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        value: 待转换、校验或计算的值，类型为 `float`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return f"{value * 100:.1f}%"


def _round(value: float) -> float:
    """函数功能：`_round` 负责按固定精度取整，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        value: 待转换、校验或计算的值，类型为 `float`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
    """
    return round(float(value), 4)


def _rate(numerator: int, denominator: int) -> float:
    """函数功能：`_rate` 负责计算比率，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        numerator: numerator 参数，由调用方传入，类型为 `int`。
        denominator: denominator 参数，由调用方传入，类型为 `int`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
    """
    return _round(numerator / denominator) if denominator else 0.0


def _p95(values: list[float]) -> float:
    """函数功能：`_p95` 负责计算第 95 百分位，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        values: values 参数，由调用方传入，类型为 `list[float]`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * 0.95
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    fraction = position - low
    return _round(ordered[low] + (ordered[high] - ordered[low]) * fraction)


def _contains_all(text: str, terms: list[str]) -> bool:
    """函数功能：`_contains_all` 负责处理 contains all，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        text: 输入文本内容，类型为 `str`。
        terms: terms 参数，由调用方传入，类型为 `list[str]`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    return all(term in text for term in terms)


def _contains_any(text: str, terms: list[str]) -> bool:
    """函数功能：`_contains_any` 负责处理 contains any，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        text: 输入文本内容，类型为 `str`。
        terms: terms 参数，由调用方传入，类型为 `list[str]`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    return any(term in text for term in terms)


def _note(space_id: str, note_id: str, text: str) -> dict[str, str]:
    """函数功能：`_note` 负责处理 note，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `dict[str, str]`，表示结构化结果、载荷或状态映射。
    """
    return {"id": note_id, "space_id": space_id, "text": text}


def _memory_payload(memory: Any, *, score: float | None = None) -> dict[str, Any]:
    """函数功能：`_memory_payload` 负责处理 memory payload，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        memory: memory 参数，由调用方传入，类型为 `Any`。
        score: score 参数，由调用方传入，类型为 `float | None`，默认值为 `None`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    payload = {
        "id": memory.id,
        "memory_type": memory.memory_type,
        "content": memory.content,
        "status": memory.status,
        "task_status": memory.task_status,
        "memory_key": memory.memory_key,
        "polarity": memory.polarity,
        "source_note_ids": [source.note_id for source in memory.sources],
        "current_version": memory.current_version,
    }
    if score is not None:
        payload["score"] = score
    return payload


def _extraction_cases() -> list[dict[str, Any]]:
    """函数功能：`_extraction_cases` 负责处理 extraction cases，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        无。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    return [
        {
            "case_id": "extract_multi_preference_task",
            "text": "我不喜欢喝牛奶，我也不喜欢工作，现在正在投递Agent简历",
            "should_store": True,
            "min_candidates": 2,
            "expected_types": ["preference"],
            "expected_content_terms": ["牛奶", "工作"],
            "notes": "The deterministic fallback currently extracts Agent resume as semantic, not task, without LLM schema help.",
        },
        {
            "case_id": "extract_two_preferences",
            "text": "我喜欢燕麦拿铁，但是不喜欢牛奶",
            "should_store": True,
            "min_candidates": 2,
            "expected_types": ["preference"],
            "expected_content_terms": ["燕麦拿铁", "牛奶"],
        },
        {
            "case_id": "extract_task_todo",
            "text": "记得完成Memory V3验收报告",
            "should_store": True,
            "min_candidates": 1,
            "expected_types": ["task"],
            "expected_task_status": "todo",
            "expected_content_terms": ["Memory", "验收报告"],
        },
        {
            "case_id": "extract_task_todo_from_progress_wording",
            "text": "我正在完成Memory V3验收报告",
            "should_store": True,
            "min_candidates": 1,
            "expected_types": ["task"],
            "expected_task_status": "todo",
            "expected_content_terms": ["Memory", "验收报告"],
        },
        {
            "case_id": "extract_task_done",
            "text": "我已经完成Memory V3验收报告",
            "should_store": True,
            "min_candidates": 1,
            "expected_types": ["task"],
            "expected_task_status": "done",
            "expected_content_terms": ["Memory", "验收报告"],
        },
        {
            "case_id": "extract_semantic_location",
            "text": "我住在上海",
            "should_store": True,
            "min_candidates": 1,
            "expected_types": ["semantic"],
            "expected_content_terms": ["上海"],
        },
        {
            "case_id": "extract_episodic_release",
            "text": "今天发布了随心记首页",
            "should_store": True,
            "min_candidates": 1,
            "expected_types": ["episodic"],
            "expected_content_terms": ["随心记首页"],
        },
        {
            "case_id": "filter_ack",
            "text": "好的收到",
            "should_store": False,
            "min_candidates": 0,
        },
        {
            "case_id": "filter_filler",
            "text": "哈哈这个先这样吧",
            "should_store": False,
            "min_candidates": 0,
        },
        {
            "case_id": "filter_low_confidence",
            "text": "我可能喜欢某个还没想清楚的方向",
            "should_store": False,
            "min_candidates": 0,
        },
        {
            "case_id": "filter_sensitive_password",
            "text": "密码是abc12345",
            "should_store": False,
            "min_candidates": 0,
            "sensitive": True,
        },
        {
            "case_id": "filter_sensitive_identifier",
            "text": "身份证号是11010119900307543X",
            "should_store": False,
            "min_candidates": 0,
            "sensitive": True,
        },
    ]


def _service_notes() -> list[dict[str, str]]:
    """函数功能：`_service_notes` 负责处理 service notes，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        无。
    返回结果说明：
        返回 `list[dict[str, str]]`，表示按条件筛选、构造或查询得到的列表。
    """
    return [
        _note("resume-bench-main", "main-001", "我不喜欢喝牛奶"),
        _note("resume-bench-main", "main-002", "我喜欢Python异步"),
        _note("resume-bench-main", "main-003", "我正在投递Agent简历"),
        _note("resume-bench-main", "main-004", "我已经完成Memory V3验收报告"),
        _note("resume-bench-main", "main-005", "我住在上海"),
        _note("resume-bench-main", "main-006", "今天发布了随心记首页"),
        _note("resume-bench-main", "main-007", "记得整理Zeta验收截图"),
        _note("resume-bench-main", "main-008", "我不喜欢开会"),
        _note("resume-bench-main", "main-009", "我喜欢抹茶"),
    ]


def _lifecycle_scenarios() -> list[dict[str, Any]]:
    """函数功能：`_lifecycle_scenarios` 负责处理 lifecycle scenarios，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        无。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    return [
        {
            "case_id": "task_lifecycle_agent_resume_refinement",
            "space_id": "resume-bench-life-agent-resume",
            "messages": ["我需要做简历", "我正在做简历", "我已经做完Agent开发的简历了"],
            "expected_active_task_count": 1,
            "expected_final_task_status": "done",
            "expected_current_version": 3,
            "expected_source_count": 3,
            "must_include": ["Agent", "简历"],
        },
        {
            "case_id": "task_lifecycle_supplier_change",
            "space_id": "resume-bench-life-supplier",
            "messages": [
                "记得给随心记的大模型换一个供应商",
                "正在给随心记的大模型换 DeepSeek 供应商",
                "随心记的大模型供应商已经从 OpenAI 换成 DeepSeek 了",
            ],
            "expected_active_task_count": 1,
            "expected_final_task_status": "done",
            "expected_current_version": 3,
            "expected_source_count": 3,
            "must_include": ["DeepSeek"],
        },
        {
            "case_id": "task_implicit_reopen_guard",
            "space_id": "resume-bench-life-reopen",
            "messages": ["我需要完成记忆验收冲突", "我已经完成记忆验收冲突", "我正在完成记忆验收冲突"],
            "expected_active_task_count": 1,
            "expected_final_task_status": "done",
            "expected_pending_review_count": 1,
            "expected_current_version": 2,
            "expected_source_count": 2,
            "must_include": ["记忆验收冲突"],
        },
        {
            "case_id": "preference_supersede",
            "space_id": "resume-bench-life-preference",
            "messages": ["我喜欢燕麦拿铁", "我现在不喜欢燕麦拿铁"],
            "expected_active_preference_count": 1,
            "expected_superseded_preference_count": 1,
            "expected_active_polarity": "negative",
            "must_include": ["不喜欢", "燕麦拿铁"],
        },
        {
            "case_id": "semantic_location_pending_review",
            "space_id": "resume-bench-life-location",
            "messages": ["我住在北京", "我搬到上海了"],
            "expected_active_semantic_count": 1,
            "expected_pending_review_count": 1,
            "expected_pending_contains": ["上海"],
            "must_include": ["北京"],
        },
    ]


def _retrieval_queries() -> list[dict[str, Any]]:
    """函数功能：`_retrieval_queries` 负责处理 retrieval queries，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        无。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    return [
        {"case_id": "ret_neg_milk", "query": "不喜欢牛奶", "expected_terms": ["牛奶"], "expected_type": "preference", "expected_polarity": "negative"},
        {"case_id": "ret_python_pref", "query": "喜欢Python异步", "expected_terms": ["Python", "异步"], "expected_type": "preference", "expected_polarity": "positive"},
        {"case_id": "ret_agent_resume", "query": "投递Agent简历", "expected_terms": ["Agent", "简历"], "expected_type": "semantic"},
        {"case_id": "ret_memory_report", "query": "完成Memory V3验收报告", "expected_terms": ["Memory", "验收报告"], "expected_type": "task", "expected_task_status": "done"},
        {"case_id": "ret_location", "query": "住在上海", "expected_terms": ["上海"], "expected_type": "semantic"},
        {"case_id": "ret_release", "query": "随心记首页发布", "expected_terms": ["随心记首页"], "expected_type": "episodic"},
        {"case_id": "ret_zeta_task", "query": "整理Zeta验收截图待办", "expected_terms": ["Zeta", "截图"], "expected_type": "task", "expected_task_status": "todo"},
        {"case_id": "ret_meeting_pref", "query": "不喜欢开会", "expected_terms": ["开会"], "expected_type": "preference", "expected_polarity": "negative"},
        {"case_id": "ret_no_result", "query": "火星基地偏好", "expect_no_result": True},
        {"case_id": "ret_type_filter_task", "query": "完成Memory V3验收报告", "expected_terms": ["Memory", "验收报告"], "expected_type": "task", "memory_type": "task"},
        {"case_id": "ret_type_filter_preference", "query": "完成Memory V3验收报告", "expect_no_result": True, "memory_type": "preference"},
    ]


def _routing_cases() -> list[dict[str, Any]]:
    """函数功能：`_routing_cases` 负责处理 routing cases，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        无。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    topics = [
        "RAG混合检索",
        "SQL索引",
        "Agent简历",
        "Canonical Key",
        "Relation Guard",
        "飞书Pending",
        "燕麦拿铁",
        "Python异步",
        "订阅预算",
        "上海博物馆",
        "向量检索",
        "敏感笔记",
        "查询改写",
        "分布式系统",
        "周报整理",
        "Memory V3验收",
        "Zeta截图",
        "首页路径图",
    ]
    cases: list[dict[str, Any]] = []
    serial = 0

    def add(style: str, query: str, expected: str, strategy: str | None = None) -> None:
        """函数功能：`add` 负责处理 add，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
        传参：
            style: style 参数，由调用方传入，类型为 `str`。
            query: 检索或查询文本，类型为 `str`。
            expected: expected 参数，由调用方传入，类型为 `str`。
            strategy: strategy 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        nonlocal serial
        cases.append(
            {
                "case_id": f"route_{serial:04d}",
                "style": style,
                "query": query,
                "expected_complexity": expected,
                "expected_strategy": strategy,
            }
        )
        serial += 1

    for index, topic in enumerate(topics):
        add("simple_fact", f"{topic}的当前记录是什么", "simple")
        add("simple_task_status", f"{topic}这项任务状态", "simple")
        add("simple_preference", f"我对{topic}的偏好", "simple")
        add("simple_filler", f"请帮我查一下{topic}", "simple")
        add("simple_single_scope", f"只查询{topic}，不要比较其它主题，也不要分析原因", "simple")
        add(
            "simple_long_single_topic",
            f"请从长期记忆中只查找{topic}这一项主题的唯一当前结论并返回对应的一条记录，不需要拆分问题",
            "simple",
        )

        other = topics[(index + 1) % len(topics)]
        third = topics[(index + 2) % len(topics)]
        add("complex_compare", f"比较{topic}和{other}的当前结论，并说明适用场景", "complex", "decomposition")
        add("complex_causal", f"为什么{topic}的状态发生变化，结合之前记录解释原因", "complex", "step_back")
        add("complex_multi_evidence", f"结合{topic}、{other}以及{third}的记录，归纳当前结论", "complex", "decomposition")
        add("complex_multi_part", f"{topic}现在什么状态，并且{other}是否完成，另外说明{third}的偏好", "complex", "decomposition")
        add("complex_trend", f"分析{topic}前后变化和趋势，并指出证据", "complex", "step_back")
        add("complex_relation", f"分别找出{topic}与{other}的记录，分析它们之间的关联、差异和对当前决策的影响", "complex", "decomposition")

    return cases


def _dataset_manifest() -> dict[str, Any]:
    """函数功能：`_dataset_manifest` 负责处理 dataset manifest，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        无。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    routing_cases = _routing_cases()
    return {
        "dataset": "resume_memory_system_benchmark_v1",
        "created_at": DATASET_CREATED_AT,
        "scope": "offline synthetic benchmark; local SQLite temp DB; deterministic rules/planner; no LLM or Feishu calls",
        "case_counts": {
            "extraction": len(_extraction_cases()),
            "service_notes": len(_service_notes()),
            "lifecycle_scenarios": len(_lifecycle_scenarios()),
            "retrieval_queries": len(_retrieval_queries()),
            "routing_queries": len(routing_cases),
            "routing_simple": sum(1 for case in routing_cases if case["expected_complexity"] == "simple"),
            "routing_complex": sum(1 for case in routing_cases if case["expected_complexity"] == "complex"),
        },
        "extraction_cases": _extraction_cases(),
        "service_notes": _service_notes(),
        "lifecycle_scenarios": _lifecycle_scenarios(),
        "retrieval_queries": _retrieval_queries(),
        "routing_cases": routing_cases,
    }


def _set_eval_store(root: Path) -> None:
    """函数功能：`_set_eval_store` 负责设置 eval store，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        root: 项目或数据根目录路径，类型为 `Path`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    memory_repository.DB_PATH = root / "memory.db"
    memory_trace.TRACE_PATH = root / "traces.jsonl"


def _score_extraction(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """函数功能：`_score_extraction` 负责评分 extraction，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        cases: cases 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    rows: list[dict[str, Any]] = []
    for case in cases:
        started = time.perf_counter_ns()
        candidates = extract_candidates(str(case["case_id"]), str(case["text"]))
        valid_candidates, rejections = validate_candidates(candidates, note_text=str(case["text"]))
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        joined = "\n".join(candidate.content for candidate in valid_candidates)
        types = {candidate.memory_type for candidate in valid_candidates}
        expected_types = set(case.get("expected_types") or [])
        should_store = bool(case.get("should_store"))
        should_ok = bool(valid_candidates) == should_store
        count_ok = len(valid_candidates) >= int(case.get("min_candidates", 1 if should_store else 0))
        type_ok = expected_types.issubset(types)
        content_ok = _contains_all(joined, [str(term) for term in case.get("expected_content_terms") or []])
        expected_status = case.get("expected_task_status")
        status_ok = expected_status is None or any(candidate.task_status == expected_status for candidate in valid_candidates)
        passed = should_ok and count_ok and type_ok and content_ok and status_ok
        rows.append(
            {
                "case_id": case["case_id"],
                "passed": passed,
                "should_store": should_store,
                "valid_candidate_count": len(valid_candidates),
                "raw_candidate_count": len(candidates),
                "rejection_reasons": [rejection.reason for rejection in rejections],
                "types": sorted(types),
                "task_statuses": sorted({candidate.task_status for candidate in valid_candidates if candidate.task_status}),
                "contents": [candidate.content for candidate in valid_candidates],
                "latency_ms": _round(elapsed_ms),
                "notes": case.get("notes"),
            }
        )
    positive = [row for row, case in zip(rows, cases) if case.get("should_store")]
    negative = [row for row, case in zip(rows, cases) if not case.get("should_store")]
    sensitive = [row for row, case in zip(rows, cases) if case.get("sensitive")]
    positive_hits = sum(row["passed"] for row in positive)
    false_memories = sum(1 for row in negative if row["valid_candidate_count"] > 0)
    metrics = {
        "cases": len(rows),
        "positive_cases": len(positive),
        "negative_cases": len(negative),
        "sensitive_cases": len(sensitive),
        "admission_accuracy": _rate(sum(row["passed"] for row in rows), len(rows)),
        "positive_coverage": _rate(positive_hits, len(positive)),
        "false_memory_rate": _rate(false_memories, len(negative)),
        "sensitive_block_rate": _rate(sum(row["valid_candidate_count"] == 0 for row in sensitive), len(sensitive)),
        "multi_clause_split_recall": _rate(
            sum(1 for row in rows if row["case_id"] in {"extract_multi_preference_task", "extract_two_preferences"} and row["valid_candidate_count"] >= 2),
            2,
        ),
        "avg_candidates_per_positive_note": _round(
            statistics.mean(row["valid_candidate_count"] for row in positive) if positive else 0.0
        ),
        "latency_ms": {
            "p50": _round(statistics.median(row["latency_ms"] for row in rows)) if rows else 0.0,
            "p95": _p95([row["latency_ms"] for row in rows]),
            "max": max((row["latency_ms"] for row in rows), default=0.0),
        },
    }
    return {"metrics": metrics, "rows": rows, "failures": [row for row in rows if not row["passed"]]}


def _run_lifecycle_scenario(case: dict[str, Any]) -> dict[str, Any]:
    """函数功能：`_run_lifecycle_scenario` 负责运行 lifecycle scenario，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    space_id = str(case["space_id"])
    reports = []
    for index, message in enumerate(case["messages"], start=1):
        report = process_note_memory(_note(space_id, f"{case['case_id']}-{index:02d}", str(message)))
        reports.append(
            {
                "message": message,
                "extraction_status": report.get("extraction_status"),
                "candidates": report.get("candidates"),
                "results": [
                    {
                        "action": result.get("action"),
                        "relation": result.get("relation"),
                        "memory_id": result.get("memory_id"),
                        "target_memory_id": result.get("target_memory_id"),
                        "task_status": result.get("task_status"),
                    }
                    for result in report.get("results", [])
                ],
            }
        )

    active = list_memories(space_id, status="active", limit=50)
    pending = list_memories(space_id, status="pending_review", limit=50)
    superseded = list_memories(space_id, status="superseded", limit=50)
    active_tasks = [memory for memory in active if memory.memory_type == "task"]
    active_preferences = [memory for memory in active if memory.memory_type == "preference"]
    active_semantic = [memory for memory in active if memory.memory_type == "semantic"]
    pending_tasks = [memory for memory in pending if memory.memory_type == "task"]

    checks: dict[str, bool] = {}
    if "expected_active_task_count" in case:
        checks["active_task_count"] = len(active_tasks) == int(case["expected_active_task_count"])
    if "expected_final_task_status" in case:
        checks["final_task_status"] = bool(active_tasks) and active_tasks[0].task_status == case["expected_final_task_status"]
    if "expected_current_version" in case:
        target = active_tasks[0] if active_tasks else (active[0] if active else None)
        checks["current_version"] = bool(target) and target.current_version == int(case["expected_current_version"])
    if "expected_source_count" in case:
        target = active_tasks[0] if active_tasks else (active[0] if active else None)
        checks["source_count"] = bool(target) and len(target.sources) == int(case["expected_source_count"])
    if "expected_pending_review_count" in case:
        checks["pending_review_count"] = len(pending) == int(case["expected_pending_review_count"])
    if "expected_active_preference_count" in case:
        checks["active_preference_count"] = len(active_preferences) == int(case["expected_active_preference_count"])
    if "expected_superseded_preference_count" in case:
        checks["superseded_preference_count"] = (
            len([memory for memory in superseded if memory.memory_type == "preference"])
            == int(case["expected_superseded_preference_count"])
        )
    if "expected_active_polarity" in case:
        checks["active_polarity"] = bool(active_preferences) and active_preferences[0].polarity == case["expected_active_polarity"]
    if "expected_active_semantic_count" in case:
        checks["active_semantic_count"] = len(active_semantic) == int(case["expected_active_semantic_count"])
    if "expected_pending_contains" in case:
        checks["pending_contains"] = _contains_any(
            "\n".join(memory.content for memory in pending),
            [str(term) for term in case["expected_pending_contains"]],
        )
    if "must_include" in case:
        checks["active_content_terms"] = _contains_all(
            "\n".join(memory.content for memory in active),
            [str(term) for term in case["must_include"]],
        )

    return {
        "case_id": case["case_id"],
        "passed": all(checks.values()) if checks else False,
        "checks": checks,
        "reports": reports,
        "active": [_memory_payload(memory) for memory in active],
        "pending_review": [_memory_payload(memory) for memory in pending],
        "superseded": [_memory_payload(memory) for memory in superseded],
        "pending_task_count": len(pending_tasks),
    }


def _score_lifecycle(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """函数功能：`_score_lifecycle` 负责评分 lifecycle，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        cases: cases 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    rows = [_run_lifecycle_scenario(case) for case in cases]
    task_rows = [row for row in rows if row["case_id"].startswith("task_")]
    task_success = [row for row in task_rows if row["passed"]]
    source_rows = [row for row in rows if "source_count" in row["checks"]]
    source_success = [row for row in source_rows if row["checks"].get("source_count")]
    reopen_row = next((row for row in rows if row["case_id"] == "task_implicit_reopen_guard"), None)
    preference_row = next((row for row in rows if row["case_id"] == "preference_supersede"), None)
    semantic_row = next((row for row in rows if row["case_id"] == "semantic_location_pending_review"), None)
    metrics = {
        "cases": len(rows),
        "scenario_pass_rate": _rate(sum(row["passed"] for row in rows), len(rows)),
        "task_lifecycle_pass_rate": _rate(len(task_success), len(task_rows)),
        "source_preservation_rate": _rate(len(source_success), len(source_rows)),
        "implicit_reopen_guard_rate": 1.0 if reopen_row and reopen_row["checks"].get("pending_review_count") else 0.0,
        "preference_supersede_accuracy": 1.0 if preference_row and preference_row["passed"] else 0.0,
        "semantic_change_pending_review_rate": 1.0 if semantic_row and semantic_row["passed"] else 0.0,
        "stale_active_task_rate": _rate(
            sum(
                1
                for row in rows
                if row["case_id"].startswith("task_")
                and any(memory["task_status"] == "todo" for memory in row["active"])
                and any(memory["task_status"] == "done" for memory in row["active"])
            ),
            len(task_rows),
        ),
        "duplicate_active_task_rate": _rate(
            sum(
                1
                for row in task_rows
                if len([memory for memory in row["active"] if memory["memory_type"] == "task"]) > 1
            ),
            len(task_rows),
        ),
    }
    return {"metrics": metrics, "rows": rows, "failures": [row for row in rows if not row["passed"]]}


def _seed_retrieval_space() -> dict[str, Any]:
    """函数功能：`_seed_retrieval_space` 负责写入测试种子数据 retrieval space，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        无。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    space_id = "resume-bench-main"
    reports = []
    for note in _service_notes():
        report = process_note_memory(note)
        reports.append(
            {
                "note_id": note["id"],
                "text": note["text"],
                "extraction_status": report.get("extraction_status"),
                "candidates": report.get("candidates"),
                "actions": [result.get("action") for result in report.get("results", [])],
            }
        )
    memories = list_memories(space_id, status="active", limit=100)
    return {"space_id": space_id, "reports": reports, "memories": memories}


def _rank_of_hit(results: list[tuple[Any, float]], case: dict[str, Any]) -> int | None:
    """函数功能：`_rank_of_hit` 负责排序 of hit，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        results: results 参数，由调用方传入，类型为 `list[tuple[Any, float]]`。
        case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `int | None`；未命中或无需处理时可返回 `None`。
    """
    terms = [str(term) for term in case.get("expected_terms") or []]
    expected_type = case.get("expected_type")
    expected_status = case.get("expected_task_status")
    expected_polarity = case.get("expected_polarity")
    for index, (memory, _score) in enumerate(results, start=1):
        if terms and not _contains_all(memory.content, terms):
            continue
        if expected_type and memory.memory_type != expected_type:
            continue
        if expected_status and memory.task_status != expected_status:
            continue
        if expected_polarity and memory.polarity != expected_polarity:
            continue
        return index
    return None


def _score_retrieval() -> dict[str, Any]:
    """函数功能：`_score_retrieval` 负责评分 retrieval，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        无。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    seeded = _seed_retrieval_space()
    space_id = seeded["space_id"]
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    for case in _retrieval_queries():
        started = time.perf_counter_ns()
        results = search_memories(
            space_id,
            str(case["query"]),
            memory_type=case.get("memory_type"),
            limit=5,
            mark_access=False,
        )
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        latencies.append(elapsed_ms)
        if case.get("expect_no_result"):
            rank = None
            passed = len(results) == 0
        else:
            rank = _rank_of_hit(results, case)
            passed = rank is not None and rank <= 5
        rows.append(
            {
                "case_id": case["case_id"],
                "query": case["query"],
                "memory_type_filter": case.get("memory_type"),
                "passed": passed,
                "rank": rank,
                "top_results": [_memory_payload(memory, score=score) for memory, score in results],
                "latency_ms": _round(elapsed_ms),
            }
        )

    positive = [row for row, case in zip(rows, _retrieval_queries()) if not case.get("expect_no_result")]
    negative = [row for row, case in zip(rows, _retrieval_queries()) if case.get("expect_no_result")]
    ranks = [row["rank"] for row in positive if row["rank"] is not None]
    mrr = statistics.mean(1 / rank for rank in ranks) if ranks else 0.0
    metrics = {
        "cases": len(rows),
        "seed_notes": len(seeded["reports"]),
        "active_memories": len(seeded["memories"]),
        "recall_at_1": _rate(sum(row["rank"] == 1 for row in positive), len(positive)),
        "recall_at_3": _rate(sum(row["rank"] is not None and row["rank"] <= 3 for row in positive), len(positive)),
        "recall_at_5": _rate(sum(row["rank"] is not None and row["rank"] <= 5 for row in positive), len(positive)),
        "mrr": _round(mrr),
        "no_result_rejection_rate": _rate(sum(row["passed"] for row in negative), len(negative)),
        "memory_type_filter_accuracy": _rate(
            sum(row["passed"] for row in rows if row["case_id"].startswith("ret_type_filter")),
            len([row for row in rows if row["case_id"].startswith("ret_type_filter")]),
        ),
        "latency_ms": {
            "p50": _round(statistics.median(latencies)) if latencies else 0.0,
            "p95": _p95(latencies),
            "max": _round(max(latencies, default=0.0)),
        },
    }
    return {
        "metrics": metrics,
        "seed_reports": seeded["reports"],
        "seed_memories": [_memory_payload(memory) for memory in seeded["memories"]],
        "rows": rows,
        "failures": [row for row in rows if not row["passed"]],
    }


def _score_deletion_and_privacy() -> dict[str, Any]:
    """函数功能：`_score_deletion_and_privacy` 负责评分 deletion and privacy，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        无。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    space_id = "resume-bench-deletion"
    process_note_memory(_note(space_id, "delete-001", "我喜欢抹茶"))
    target = next(memory for memory in list_memories(space_id, limit=20) if "抹茶" in memory.content)
    soft_delete_memory(target.id)
    soft_deleted_results = search_memories(space_id, "抹茶", limit=5, mark_access=False)

    process_note_memory(_note(space_id, "delete-002", "我喜欢热美式"))
    purge_target = next(memory for memory in list_memories(space_id, limit=20) if "热美式" in memory.content)
    purge_ok = purge_memory(purge_target.id)
    purged_results = search_memories(space_id, "热美式", limit=5, mark_access=False)

    sensitive_space = "resume-bench-sensitive"
    sensitive_cases = [
        "密码是abc12345",
        "Bearer abcdefghijklmnopqrstuvwxyz123456",
        "postgres://user:pass123@localhost/db",
        "https://x.test/cb?api_key=secret123456&mode=test",
        "身份证号是11010119900307543X",
        "sk-test_abcdefghijklmnopqrstuvwxyz",
    ]
    sensitive_rows = []
    for index, text in enumerate(sensitive_cases, start=1):
        report = process_note_memory(_note(sensitive_space, f"sensitive-{index:03d}", text))
        redacted = redact_sensitive_text(text)
        sensitive_rows.append(
            {
                "case_id": f"sensitive-{index:03d}",
                "blocked": report.get("extraction_status") == "empty" and report.get("candidates") == 0,
                "detected_sensitive": contains_sensitive_data(text),
                "redacted": redacted,
                "redaction_changed_text": redacted != text,
                "raw_secret_leaked_in_redaction": text in redacted,
                "report": {
                    "extraction_status": report.get("extraction_status"),
                    "candidates": report.get("candidates"),
                },
            }
        )
    sensitive_memories = list_memories(sensitive_space, status=None, limit=100)
    rows = [
        {
            "case_id": "soft_delete_non_retrieval",
            "passed": len(soft_deleted_results) == 0,
            "results": [_memory_payload(memory, score=score) for memory, score in soft_deleted_results],
        },
        {
            "case_id": "purge_non_retrieval",
            "passed": purge_ok and len(purged_results) == 0,
            "results": [_memory_payload(memory, score=score) for memory, score in purged_results],
        },
        {
            "case_id": "sensitive_service_block",
            "passed": all(row["blocked"] for row in sensitive_rows) and len(sensitive_memories) == 0,
            "sensitive_rows": sensitive_rows,
            "memory_count": len(sensitive_memories),
        },
        {
            "case_id": "redaction_safety",
            "passed": all(row["redaction_changed_text"] and not row["raw_secret_leaked_in_redaction"] for row in sensitive_rows),
            "sensitive_rows": sensitive_rows,
        },
    ]
    metrics = {
        "delete_leakage_rate": _rate(sum(len(row.get("results", [])) > 0 for row in rows[:2]), 2),
        "deleted_memory_non_retrieval_rate": _rate(sum(row["passed"] for row in rows[:2]), 2),
        "sensitive_detection_rate": _rate(sum(row["detected_sensitive"] for row in sensitive_rows), len(sensitive_rows)),
        "sensitive_storage_block_rate": _rate(sum(row["blocked"] for row in sensitive_rows), len(sensitive_rows)),
        "redaction_success_rate": _rate(
            sum(row["redaction_changed_text"] and not row["raw_secret_leaked_in_redaction"] for row in sensitive_rows),
            len(sensitive_rows),
        ),
    }
    return {"metrics": metrics, "rows": rows, "failures": [row for row in rows if not row["passed"]]}


def _score_idempotency() -> dict[str, Any]:
    """函数功能：`_score_idempotency` 负责评分 idempotency，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        无。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    space_id = "resume-bench-idempotency"
    note = _note(space_id, "idem-001", "记得完成Memory V3验收报告")
    first = process_note_memory(note)
    before = list_memories(space_id, status="active", limit=20)
    second = process_note_memory(note)
    after = list_memories(space_id, status="active", limit=20)
    rows = [
        {
            "case_id": "same_note_reprocess_is_idempotent",
            "passed": len(before) == len(after) == 1 and bool(second.get("idempotent")),
            "first": {"candidates": first.get("candidates"), "extraction_status": first.get("extraction_status")},
            "second": {
                "candidates": second.get("candidates"),
                "extraction_status": second.get("extraction_status"),
                "idempotent": second.get("idempotent"),
            },
            "before": [_memory_payload(memory) for memory in before],
            "after": [_memory_payload(memory) for memory in after],
        }
    ]
    metrics = {
        "cases": len(rows),
        "idempotent_reprocess_rate": _rate(sum(row["passed"] for row in rows), len(rows)),
        "duplicate_created_on_reprocess": 0 if rows[0]["passed"] else max(0, len(after) - len(before)),
    }
    return {"metrics": metrics, "rows": rows, "failures": [row for row in rows if not row["passed"]]}


def _score_query_routing(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """函数功能：`_score_query_routing` 负责评分 query routing，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        cases: cases 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    rows: list[dict[str, Any]] = []
    latencies: list[float] = []
    errors: list[dict[str, Any]] = []
    for case in cases:
        started = time.perf_counter_ns()
        try:
            plan = build_query_plan(str(case["query"]))
        except Exception as exc:
            errors.append({"case_id": case["case_id"], "error": f"{type(exc).__name__}: {exc}"})
            continue
        elapsed_ms = (time.perf_counter_ns() - started) / 1_000_000
        latencies.append(elapsed_ms)
        expansion = bool(plan.retrieval_queries or plan.use_query_rewrite or plan.use_decomposition or plan.use_step_back)
        strategy = case.get("expected_strategy")
        if strategy == "decomposition":
            strategy_hit = bool(plan.use_decomposition)
        elif strategy == "step_back":
            strategy_hit = bool(plan.use_step_back)
        elif strategy == "rewrite":
            strategy_hit = bool(plan.use_query_rewrite)
        elif strategy == "any":
            strategy_hit = expansion
        else:
            strategy_hit = None
        rows.append(
            {
                **case,
                "actual_complexity": plan.complexity,
                "complexity_correct": plan.complexity == case["expected_complexity"],
                "expansion": expansion,
                "strategy_hit": strategy_hit,
                "variant_count": len(plan.retrieval_queries),
                "routing_state": plan.routing_state,
                "routing_confidence": plan.routing_confidence,
                "routing_reasons": list(plan.routing_reasons),
                "plan": asdict(plan),
                "latency_ms": _round(elapsed_ms),
            }
        )

    simple = [row for row in rows if row["expected_complexity"] == "simple"]
    complex_rows = [row for row in rows if row["expected_complexity"] == "complex"]
    tp = sum(row["actual_complexity"] == "complex" for row in complex_rows)
    fp = sum(row["actual_complexity"] == "complex" for row in simple)
    fn = sum(row["actual_complexity"] == "simple" for row in complex_rows)
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    strategy_stats: dict[str, dict[str, Any]] = {}
    for strategy in ("decomposition", "step_back", "rewrite", "any"):
        subset = [row for row in complex_rows if row.get("expected_strategy") == strategy]
        hits = sum(bool(row["strategy_hit"]) for row in subset)
        strategy_stats[strategy] = {"cases": len(subset), "hits": hits, "recall": _rate(hits, len(subset))}
    by_style = {}
    for style in sorted({row["style"] for row in rows}):
        subset = [row for row in rows if row["style"] == style]
        by_style[style] = {
            "cases": len(subset),
            "accuracy": _rate(sum(row["complexity_correct"] for row in subset), len(subset)),
            "expansion_rate": _rate(sum(row["expansion"] for row in subset), len(subset)),
        }

    metrics = {
        "cases": len(cases),
        "executed_cases": len(rows),
        "errors": len(errors),
        "dataset_complex_query_rate": _rate(len(complex_rows), len(rows)),
        "predicted_complex_query_rate": _rate(sum(row["actual_complexity"] == "complex" for row in rows), len(rows)),
        "classification_accuracy": _rate(sum(row["complexity_correct"] for row in rows), len(rows)),
        "complex_precision": _round(precision),
        "complex_recall": _round(recall),
        "complex_f1": _round(f1),
        "simple_false_complex_rate": _rate(fp, len(simple)),
        "complex_false_simple_rate": _rate(fn, len(complex_rows)),
        "complex_expansion_coverage": _rate(sum(row["expansion"] for row in complex_rows), len(complex_rows)),
        "simple_unnecessary_expansion_rate": _rate(sum(row["expansion"] for row in simple), len(simple)),
        "avg_variant_count": _round(statistics.mean(row["variant_count"] for row in rows)) if rows else 0.0,
        "max_variant_count": max((row["variant_count"] for row in rows), default=0),
        "strategy": strategy_stats,
        "activation_rates_on_complex": {
            "query_rewrite": _rate(sum(row["plan"]["use_query_rewrite"] for row in complex_rows), len(complex_rows)),
            "decomposition": _rate(sum(row["plan"]["use_decomposition"] for row in complex_rows), len(complex_rows)),
            "step_back": _rate(sum(row["plan"]["use_step_back"] for row in complex_rows), len(complex_rows)),
        },
        "latency_ms": {
            "p50": _round(statistics.median(latencies)) if latencies else 0.0,
            "p95": _p95(latencies),
            "max": _round(max(latencies, default=0.0)),
        },
        "llm_calls": 0,
    }
    failures = [
        row
        for row in rows
        if not row["complexity_correct"]
        or (row["expected_complexity"] == "complex" and not row["expansion"])
        or (row["expected_complexity"] == "simple" and row["expansion"])
    ]
    return {"metrics": metrics, "by_style": by_style, "rows": rows, "errors": errors, "failures": failures[:80]}


def _overall(reports: dict[str, Any]) -> dict[str, Any]:
    """函数功能：`_overall` 负责处理 overall，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        reports: reports 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    return {
        "offline": True,
        "llm_calls": 0,
        "storage_backend": SAFE_ENV["STORAGE_BACKEND"],
        "extractor_mode": SAFE_ENV["SUIXINJI_MEMORY_EXTRACTOR_MODE"],
        "total_cases": (
            reports["extraction"]["metrics"]["cases"]
            + reports["lifecycle"]["metrics"]["cases"]
            + reports["retrieval"]["metrics"]["cases"]
            + len(reports["privacy"]["rows"])
            + reports["idempotency"]["metrics"]["cases"]
            + reports["query_routing"]["metrics"]["cases"]
        ),
        "extraction_admission_accuracy": reports["extraction"]["metrics"]["admission_accuracy"],
        "false_memory_rate": reports["extraction"]["metrics"]["false_memory_rate"],
        "sensitive_storage_block_rate": reports["privacy"]["metrics"]["sensitive_storage_block_rate"],
        "task_lifecycle_pass_rate": reports["lifecycle"]["metrics"]["task_lifecycle_pass_rate"],
        "retrieval_recall_at_1": reports["retrieval"]["metrics"]["recall_at_1"],
        "retrieval_recall_at_5": reports["retrieval"]["metrics"]["recall_at_5"],
        "complex_query_rate_gold": reports["query_routing"]["metrics"]["dataset_complex_query_rate"],
        "complex_query_rate_predicted": reports["query_routing"]["metrics"]["predicted_complex_query_rate"],
        "complex_query_f1": reports["query_routing"]["metrics"]["complex_f1"],
        "planner_latency_p95_ms": reports["query_routing"]["metrics"]["latency_ms"]["p95"],
        "deleted_memory_non_retrieval_rate": reports["privacy"]["metrics"]["deleted_memory_non_retrieval_rate"],
        "idempotent_reprocess_rate": reports["idempotency"]["metrics"]["idempotent_reprocess_rate"],
    }


def _write_dataset(manifest: dict[str, Any]) -> None:
    """函数功能：`_write_dataset` 负责写入 dataset，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        manifest: manifest 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    DATASET_PATH.parent.mkdir(parents=True, exist_ok=True)
    DATASET_PATH.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")


def _write_markdown(output: dict[str, Any], markdown_path: Path) -> None:
    """函数功能：`_write_markdown` 负责写入 markdown，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        output: output 参数，由调用方传入，类型为 `dict[str, Any]`。
        markdown_path: markdown path 参数，由调用方传入，类型为 `Path`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    overall = output["overall"]
    extraction = output["reports"]["extraction"]["metrics"]
    lifecycle = output["reports"]["lifecycle"]["metrics"]
    retrieval = output["reports"]["retrieval"]["metrics"]
    privacy = output["reports"]["privacy"]["metrics"]
    routing = output["reports"]["query_routing"]["metrics"]
    idem = output["reports"]["idempotency"]["metrics"]
    lines = [
        "# Resume Memory System Benchmark",
        "",
        f"- Dataset: `{DATASET_PATH.relative_to(ROOT)}`",
        "- Scope: offline synthetic benchmark, temp SQLite DB, deterministic rules/planner, no Feishu writes, no LLM calls.",
        f"- Executed at: {output['created_at']}",
        f"- Total measured cases: {overall['total_cases']}",
        "",
        "## Resume-Ready Metrics",
        "",
        "| Metric | Value |",
        "|---|---:|",
        f"| Memory extraction admission accuracy | {_pct(extraction['admission_accuracy'])} |",
        f"| False memory rate on negative/noise cases | {_pct(extraction['false_memory_rate'])} |",
        f"| Sensitive storage block rate | {_pct(privacy['sensitive_storage_block_rate'])} |",
        f"| Multi-clause split recall | {_pct(extraction['multi_clause_split_recall'])} |",
        f"| Task lifecycle scenario pass rate | {_pct(lifecycle['task_lifecycle_pass_rate'])} |",
        f"| Source preservation rate | {_pct(lifecycle['source_preservation_rate'])} |",
        f"| Duplicate active task rate | {_pct(lifecycle['duplicate_active_task_rate'])} |",
        f"| Stale active task rate | {_pct(lifecycle['stale_active_task_rate'])} |",
        f"| Preference supersede accuracy | {_pct(lifecycle['preference_supersede_accuracy'])} |",
        f"| Semantic change pending-review rate | {_pct(lifecycle['semantic_change_pending_review_rate'])} |",
        f"| Retrieval Recall@1 / @3 / @5 | {_pct(retrieval['recall_at_1'])} / {_pct(retrieval['recall_at_3'])} / {_pct(retrieval['recall_at_5'])} |",
        f"| Retrieval MRR | {retrieval['mrr']:.3f} |",
        f"| No-result rejection rate | {_pct(retrieval['no_result_rejection_rate'])} |",
        f"| Deleted memory non-retrieval rate | {_pct(privacy['deleted_memory_non_retrieval_rate'])} |",
        f"| Idempotent same-note reprocess rate | {_pct(idem['idempotent_reprocess_rate'])} |",
        f"| Query-routing gold complex query rate | {_pct(routing['dataset_complex_query_rate'])} |",
        f"| Query-routing predicted complex query rate | {_pct(routing['predicted_complex_query_rate'])} |",
        f"| Query complexity accuracy | {_pct(routing['classification_accuracy'])} |",
        f"| Complex precision / recall / F1 | {routing['complex_precision']:.3f} / {routing['complex_recall']:.3f} / {routing['complex_f1']:.3f} |",
        f"| Complex expansion coverage | {_pct(routing['complex_expansion_coverage'])} |",
        f"| Simple false-complex rate | {_pct(routing['simple_false_complex_rate'])} |",
        f"| Planner latency p50 / p95 / max | {routing['latency_ms']['p50']:.3f} / {routing['latency_ms']['p95']:.3f} / {routing['latency_ms']['max']:.3f} ms |",
        "",
        "## Notes",
        "",
        "- These numbers are suitable as development benchmark metrics, not production traffic metrics.",
        "- The benchmark intentionally keeps model calls at zero; live LLM extraction quality and vendor latency need a separate online eval.",
        "- Full JSON includes per-case rows, plans, memory payloads, and failure examples.",
    ]
    markdown_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run(*, dry_run: bool = False, output_dir: Path = RESULTS_DIR) -> dict[str, Any]:
    """函数功能：`run` 负责运行，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        dry_run: dry run 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
        output_dir: output dir 参数，由调用方传入，类型为 `Path`，默认值为 `RESULTS_DIR`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    manifest = _dataset_manifest()
    _write_dataset(manifest)
    if dry_run:
        return {"mode": "dry_run", "dataset": str(DATASET_PATH), "case_counts": manifest["case_counts"]}

    with tempfile.TemporaryDirectory(prefix="suixinji-memory-bench-") as tmp:
        _set_eval_store(Path(tmp))
        reports = {
            "extraction": _score_extraction(_extraction_cases()),
            "lifecycle": _score_lifecycle(_lifecycle_scenarios()),
            "retrieval": _score_retrieval(),
            "privacy": _score_deletion_and_privacy(),
            "idempotency": _score_idempotency(),
            "query_routing": _score_query_routing(_routing_cases()),
            "store_stats": {
                "main": stats("resume-bench-main"),
                "sensitive": stats("resume-bench-sensitive"),
                "delete": stats("resume-bench-deletion"),
            },
        }

    output = {
        "mode": "resume_memory_system_benchmark",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "dataset": str(DATASET_PATH),
        "safe_env": SAFE_ENV,
        "overall": _overall(reports),
        "reports": reports,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    json_path = output_dir / f"resume_memory_system_benchmark_{stamp}.json"
    markdown_path = output_dir / f"resume_memory_system_benchmark_{stamp}.md"
    json_path.write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    _write_markdown(output, markdown_path)
    output["result_json"] = str(json_path)
    output["result_markdown"] = str(markdown_path)
    return output


def main() -> None:
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：评测脚本，负责 resume memory system benchmark 场景的数据构造、执行或指标汇总。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    parser = argparse.ArgumentParser(description="Run an offline Memory system benchmark for resume-facing metrics.")
    parser.add_argument("--dry-run", action="store_true", help="Only generate/describe the synthetic dataset.")
    parser.add_argument("--output-dir", default=str(RESULTS_DIR))
    args = parser.parse_args()
    report = run(dry_run=args.dry_run, output_dir=Path(args.output_dir))
    if args.dry_run:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return
    printable = {
        "mode": report["mode"],
        "dataset": report["dataset"],
        "result_json": report["result_json"],
        "result_markdown": report["result_markdown"],
        "overall": report["overall"],
    }
    print(json.dumps(printable, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
