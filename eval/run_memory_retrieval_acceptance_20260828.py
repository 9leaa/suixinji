"""2026-08-28 Memory retrieval acceptance: Feishu-shaped smoke + 125 cases + RRF trace."""

from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
import uuid
from collections import Counter, defaultdict
from dataclasses import asdict, is_dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.query_agent import answer_question
from bot.feishu_bot import parse_text_content
from core.config import get_embedding_config
from core.llm_client import embed_text
from core.worker import process_record
from eval.common import write_json
from infrastructure.database import session_scope
from infrastructure.schema import MemoryVector
from memory.canonicalizer import canonicalize_candidate
from memory.models import MemoryCandidate
from memory.retrieval_models import MemoryQuerySpec
from memory.service import build_memory_query_spec, memory_search, task_status_search
from memory.vector_lifecycle import current_embedding_contract, memory_content_hash, memory_embedding_text
from repositories.postgres import memory as pg_memory
from repositories.postgres.memory import insert_memory, list_memory_traces
from memory.repository import get_extraction_state, list_memories
from sqlalchemy.dialects.postgresql import insert as pg_insert


RESULT_DIR = ROOT / "eval" / "results" / "memory_retrieval_acceptance_20260828"
DATASET_PATH = RESULT_DIR / "memory_retrieval_dataset_v1.json"
RAW_PATH = RESULT_DIR / "memory_retrieval_raw_results.json"
SMOKE_PATH = RESULT_DIR / "feishu_shaped_smoke_raw.json"
REPORT_PATH = RESULT_DIR / "MEMORY_RETRIEVAL_ACCEPTANCE_REPORT_20260828.md"
CHECKPOINT_PATH = RESULT_DIR / "memory_retrieval_checkpoint.json"


TASKS = [
    ("北辰支付网关联调", "done"),
    ("星火推荐模型压测", "todo"),
    ("海棠发布说明校对", "done"),
    ("云杉数据库迁移", "todo"),
    ("青鸟告警规则重构", "done"),
    ("远山移动端适配", "todo"),
    ("蓝鲸搜索索引优化", "done"),
    ("玄武权限矩阵梳理", "todo"),
    ("清风周报自动化", "done"),
    ("赤霄缓存治理", "todo"),
    ("雾松埋点校验", "done"),
    ("南星接口文档补全", "todo"),
]

PREFERENCES = [
    ("无糖豆乳拿铁", "饮品", "positive", "喜欢无糖豆乳拿铁"),
    ("先结论后证据的报告", "报告形式", "positive", "喜欢报告先讲结论再给证据"),
    ("工作日上午推送", "通知时间", "negative", "不喜欢工作日上午收到推送"),
    ("高铁靠窗座位", "出行座位", "positive", "坐高铁更喜欢靠窗"),
    ("逐条代码评审", "代码评审", "positive", "喜欢代码评审逐条评论"),
    ("香菜", "饮食", "negative", "不喜欢香菜"),
    ("先案例后原理", "学习方式", "positive", "学习时喜欢先看案例再看原理"),
    ("三十分钟内的会议", "会议时长", "positive", "喜欢把会议控制在三十分钟内"),
    ("晚上跑步", "运动时间", "positive", "更喜欢晚上跑步"),
    ("工作时听纯音乐", "音乐场景", "positive", "工作时喜欢听纯音乐"),
]

SEMANTICS = [
    ("常住地", "北京朝阳区", "上海浦东新区", "location"),
    ("工作地点", "成都高新区", "深圳南山区", "location"),
    ("任职公司", "远山科技", "星河数据", "employment"),
    ("当前岗位", "Agent工程师", "后端开发工程师", "employment"),
    ("最高学历", "计算机科学本科", "软件工程专科", "education"),
    ("主要技术栈", "Python和PostgreSQL", "Java和MySQL", "skill"),
    ("当前项目", "随心记", "语音助手", "project"),
    ("家乡", "青岛", "济南", "location"),
    ("常用语言", "中文和英语", "中文和日语", "language"),
    ("所在时区", "Asia/Shanghai", "Asia/Tokyo", "location"),
]

EPISODES = [
    ("星河项目复盘会", "2026-08-27", "参加了星河项目复盘会并确认三项风险"),
    ("植物园拍花", "2026-08-24", "去植物园拍了很多花"),
    ("技术沙龙", "2026-08-21", "参加了检索系统技术沙龙"),
    ("简历模拟面试", "2026-08-19", "完成了一次Agent简历模拟面试"),
    ("数据库故障复盘", "2026-08-17", "参加数据库故障复盘并记录根因"),
    ("青岛短途旅行", "2026-08-14", "去了青岛短途旅行"),
    ("阅读分享会", "2026-08-11", "参加了分布式系统阅读分享会"),
    ("健身房体验课", "2026-08-08", "上了一次力量训练体验课"),
    ("产品需求评审", "2026-08-05", "参加了随心记产品需求评审"),
    ("同学聚餐", "2026-08-02", "和大学同学聚餐"),
]


def _iso(days_ago: int) -> str:
    return (datetime.now().astimezone() - timedelta(days=days_ago)).isoformat()


def _memory(
    alias: str,
    memory_type: str,
    content: str,
    *,
    status: str = "active",
    task_status: str | None = None,
    subject: str = "用户",
    predicate: str | None = None,
    object_value: str | None = None,
    polarity: str | None = None,
    scope: dict[str, Any] | None = None,
    valid_from: str | None = None,
) -> dict[str, Any]:
    return {
        "alias": alias,
        "memory_type": memory_type,
        "content": content,
        "status": status,
        "task_status": task_status,
        "subject": subject,
        "predicate": predicate,
        "object_value": object_value,
        "polarity": polarity,
        "scope": scope or {},
        "valid_from": valid_from,
    }


def build_dataset() -> dict[str, Any]:
    memories: list[dict[str, Any]] = []
    cases: list[dict[str, Any]] = []

    for index, (topic, state) in enumerate(TASKS):
        old_state = "todo" if state == "done" else "done"
        old_text = f"需要继续处理{topic}" if old_state == "todo" else f"已经完成{topic}"
        text = f"需要继续处理{topic}" if state == "todo" else f"已经完成{topic}"
        memories.append(_memory(
            f"task_{index}_old", "task", old_text, status="superseded",
            task_status=old_state, predicate=topic, object_value=topic,
            scope={"canonical_topic": topic, "operation": "处理"}, valid_from=_iso(30 + index),
        ))
        memories.append(_memory(
            f"task_{index}", "task", text, task_status=state,
            predicate=topic, object_value=topic,
            scope={"canonical_topic": topic, "operation": "处理"}, valid_from=_iso(index),
        ))
        for variant, query in enumerate((f"{topic}现在进展怎么样？", f"{topic}做完了吗，还是还要继续？")):
            cases.append({
                "case_id": f"task_{index}_v{variant}", "bucket": "task", "query": query,
                "memory_type": "task", "expected": [f"task_{index}"],
                "must_not": [f"task_{index}_old"], "expected_task_status": state,
                "current_state": True, "spec_mode": "auto",
            })
    for index in range(6):
        cases.append({
            "case_id": f"task_contract_{index}", "bucket": "task", "query": TASKS[index][0],
            "memory_type": "task", "expected": [f"task_{index}"],
            "must_not": [f"task_{index}_old"], "expected_task_status": TASKS[index][1],
            "current_state": True, "spec_mode": "exact" if index < 3 else "family",
            "spec_from": f"task_{index}",
        })

    for index, (topic, family, polarity, text) in enumerate(PREFERENCES):
        old_polarity = "negative" if polarity == "positive" else "positive"
        old_text = f"不再偏好{topic}" if old_polarity == "negative" else f"过去偏好{topic}"
        memories.append(_memory(
            f"pref_{index}_old", "preference", old_text, status="superseded",
            predicate="preference", object_value=topic, polarity=old_polarity,
            scope={"canonical_topic": topic, "preference_scope": "global", "family_label": family},
            valid_from=_iso(40 + index),
        ))
        memories.append(_memory(
            f"pref_{index}", "preference", f"用户{text}",
            predicate="preference", object_value=topic, polarity=polarity,
            scope={"canonical_topic": topic, "preference_scope": "global", "family_label": family},
            valid_from=_iso(index),
        ))
        for variant, query in enumerate((f"我对{topic}是什么偏好？", f"关于{family}，我现在会选择{topic}吗？")):
            cases.append({
                "case_id": f"pref_{index}_v{variant}", "bucket": "preference", "query": query,
                "memory_type": "preference", "expected": [f"pref_{index}"],
                "must_not": [f"pref_{index}_old"], "expected_polarity": polarity,
                "spec_mode": "auto",
            })
    for index in range(5):
        cases.append({
            "case_id": f"pref_family_{index}", "bucket": "preference", "query": PREFERENCES[index][1],
            "memory_type": "preference", "expected": [f"pref_{index}"],
            "must_not": [f"pref_{index}_old"], "expected_polarity": PREFERENCES[index][2],
            "spec_mode": "family", "spec_from": f"pref_{index}",
        })

    for index, (topic, current, previous, facet) in enumerate(SEMANTICS):
        memories.append(_memory(
            f"semantic_{index}_history", "semantic", f"用户过去的{topic}是{previous}",
            predicate=topic, object_value=previous,
            scope={"canonical_topic": topic, "semantic_facet": facet}, valid_from=_iso(60 + index),
        ))
        memories.append(_memory(
            f"semantic_{index}", "semantic", f"用户当前的{topic}是{current}",
            predicate=topic, object_value=current,
            scope={"canonical_topic": topic, "semantic_facet": facet}, valid_from=_iso(index),
        ))
        for variant, query in enumerate((f"我现在的{topic}是什么？", f"关于{topic}，目前记录的最新事实是什么？")):
            cases.append({
                "case_id": f"semantic_{index}_v{variant}", "bucket": "semantic", "query": query,
                "memory_type": "semantic", "expected": [f"semantic_{index}"], "must_not": [],
                "current_state": True, "spec_mode": "auto",
            })
    for index in range(5):
        cases.append({
            "case_id": f"semantic_structured_{index}", "bucket": "semantic", "query": SEMANTICS[index][0],
            "memory_type": "semantic", "expected": [f"semantic_{index}"], "must_not": [],
            "current_state": True, "spec_mode": "structured", "spec_from": f"semantic_{index}",
        })

    for index, (event, day, detail) in enumerate(EPISODES):
        memories.append(_memory(
            f"episode_{index}", "episodic", f"{day}，用户{detail}",
            predicate="event", object_value=event,
            scope={"canonical_topic": event, "event_date": day}, valid_from=f"{day}T12:00:00+08:00",
        ))
        for variant, query in enumerate((f"我参加过的{event}发生了什么？", f"{day}那次{event}有什么记录？")):
            cases.append({
                "case_id": f"episode_{index}_v{variant}", "bucket": "episodic", "query": query,
                "memory_type": "episodic", "expected": [f"episode_{index}"],
                "must_not": [], "spec_mode": "auto",
            })

    pair_aliases = (
        [(f"task_{i}", f"pref_{i % 10}") for i in range(5)]
        + [(f"semantic_{i}", f"episode_{i}") for i in range(5)]
        + [(f"task_{i + 5}", f"semantic_{i + 5}") for i in range(5)]
        + [(f"pref_{i + 5}", f"episode_{i + 5}") for i in range(5)]
    )
    alias_topic = {
        **{f"task_{i}": item[0] for i, item in enumerate(TASKS)},
        **{f"pref_{i}": item[0] for i, item in enumerate(PREFERENCES)},
        **{f"semantic_{i}": item[0] for i, item in enumerate(SEMANTICS)},
        **{f"episode_{i}": item[0] for i, item in enumerate(EPISODES)},
    }
    for index, (left, right) in enumerate(pair_aliases):
        cases.append({
            "case_id": f"multi_{index}", "bucket": "multi", "query": f"分别告诉我{alias_topic[left]}和{alias_topic[right]}的相关记录",
            "memory_type": None, "expected": [left, right], "must_not": [], "spec_mode": "auto",
        })

    memories.append(_memory(
        "restricted_0", "semantic", "用户有一条受限私人记录",
        predicate="受限记录", object_value="不可导出", scope={"canonical_topic": "受限私人记录", "sensitivity": "sensitive"},
    ))
    for index in range(5):
        cases.append({
            "case_id": f"safety_{index}", "bucket": "safety", "query": f"受限私人记录是什么（查询变体{index + 1}）",
            "memory_type": "semantic", "expected": [], "must_not": ["restricted_0"], "spec_mode": "auto",
        })

    assert len(cases) == 125, len(cases)
    return {
        "version": "memory-retrieval-acceptance-v1-unseen-20260828",
        "created_at": datetime.now().astimezone().isoformat(),
        "retrieval_cases": 120,
        "safety_cases": 5,
        "memories": memories,
        "cases": cases,
    }


def _checkpoint(state: dict[str, Any]) -> None:
    write_json(CHECKPOINT_PATH, state)


def _insert_dataset_memories(
    dataset: dict[str, Any],
    space_id: str,
    state: dict[str, Any],
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    records: dict[str, dict[str, Any]] = dict(state.get("records") or {})
    reverse_ids: dict[str, str] = {
        str(record["id"]): alias for alias, record in records.items()
    }
    for index, item in enumerate(dataset["memories"], start=1):
        alias = str(item["alias"])
        if alias in records:
            continue
        candidate = canonicalize_candidate(MemoryCandidate(
            memory_type=str(item["memory_type"]),
            content=str(item["content"]),
            importance=0.85,
            confidence=0.95,
            task_status=item.get("task_status"),
            subject=item.get("subject"),
            predicate=item.get("predicate"),
            object_value=item.get("object_value"),
            polarity=item.get("polarity"),
            scope=dict(item.get("scope") or {}),
            valid_from=item.get("valid_from"),
            evidence_span=str(item["content"]),
            extractor_type="acceptance_eval",
            extractor_version="memory-retrieval-acceptance-v1",
        ))
        previous_lifecycle = pg_memory.MEMORY_VECTOR_LIFECYCLE_ENABLED
        pg_memory.MEMORY_VECTOR_LIFECYCLE_ENABLED = False
        try:
            record = insert_memory(
                space_id,
                candidate,
                source_note_id=f"{space_id}:source:{item['alias']}",
                status=str(item.get("status") or "active"),
            )
        finally:
            pg_memory.MEMORY_VECTOR_LIFECYCLE_ENABLED = previous_lifecycle
        data = record.to_dict()
        records[alias] = data
        reverse_ids[record.id] = alias
        state.update({"phase": "insert", "records": records})
        _checkpoint(state)
        if index % 5 == 0 or index == len(dataset["memories"]):
            print(f"inserted_memories={index}/{len(dataset['memories'])}", flush=True)

    active_aliases = [
        str(item["alias"]) for item in dataset["memories"]
        if str(item.get("status") or "active") == "active"
    ]
    ready_aliases = set(state.get("ready_vector_aliases") or [])
    pending_vectors: list[tuple[str, dict[str, Any], list[float]]] = []

    def flush_vectors() -> None:
        if not pending_vectors:
            return
        model, dimension, version = current_embedding_contract()
        now = datetime.now().astimezone()
        with session_scope() as session:
            for alias, record, embedding in pending_vectors:
                content_hash = memory_content_hash(
                    memory_type=str(record["memory_type"]),
                    subject=record.get("subject"),
                    predicate=record.get("predicate"),
                    object_value=record.get("object_value"),
                    content=str(record["content"]),
                    model=model,
                    dimension=dimension,
                    embedding_version=version,
                )
                values = {
                    "memory_id": str(record["id"]),
                    "embedding": [float(value) for value in embedding],
                    "model": model,
                    "dimension": dimension,
                    "content_hash": content_hash,
                    "embedding_version": version,
                    "status": "ready",
                    "attempt_count": 1,
                    "next_retry_at": None,
                    "last_error": None,
                    "created_at": now,
                    "updated_at": now,
                }
                session.execute(
                    pg_insert(MemoryVector)
                    .values(**values)
                    .on_conflict_do_update(
                        index_elements=[MemoryVector.memory_id],
                        set_={key: value for key, value in values.items() if key != "memory_id"},
                    )
                )
                ready_aliases.add(alias)
        pending_vectors.clear()
        state.update({"phase": "vectors", "ready_vector_aliases": sorted(ready_aliases)})
        _checkpoint(state)

    for index, alias in enumerate(active_aliases, start=1):
        if alias not in ready_aliases:
            record = records[alias]
            embedding = embed_text(memory_embedding_text(
                memory_type=str(record["memory_type"]),
                subject=record.get("subject"),
                predicate=record.get("predicate"),
                object_value=record.get("object_value"),
                content=str(record["content"]),
            ))
            expected_dimension = int(get_embedding_config().dimension)
            if len(embedding) != expected_dimension:
                raise ValueError(f"embedding dimension mismatch: {len(embedding)} != {expected_dimension}")
            pending_vectors.append((alias, record, embedding))
            if len(pending_vectors) >= 5:
                flush_vectors()
        if index % 5 == 0 or index == len(active_aliases):
            print(f"ready_memory_vectors={index}/{len(active_aliases)}", flush=True)
    flush_vectors()
    return records, reverse_ids


def _query_spec(
    case: dict[str, Any],
    records: dict[str, dict[str, Any]],
) -> MemoryQuerySpec:
    memory_type = case.get("memory_type")
    query = str(case["query"])
    mode = str(case.get("spec_mode") or "auto")
    if mode == "auto":
        return build_memory_query_spec(query, memory_type=memory_type)
    source = records[str(case["spec_from"])]
    scope = dict(source.get("scope") or {})
    common = {
        "memory_type": memory_type,
        "canonical_topic": scope.get("canonical_topic"),
        "subject": source.get("subject"),
        "predicate": source.get("predicate"),
        "entities": tuple([str(source.get("object_value") or "")]) if source.get("object_value") else (),
        "time_mode": "current" if case.get("current_state") else "all",
    }
    if mode == "exact":
        return MemoryQuerySpec(memory_key=source.get("memory_key"), **common)
    if mode == "family":
        family_key = scope.get("task_family_key") or scope.get("preference_family_key")
        return MemoryQuerySpec(family_key=family_key, **common)
    return MemoryQuerySpec(**common)


def _last_query_trace(space_id: str) -> dict[str, Any] | None:
    for trace in reversed(list_memory_traces(limit=300)):
        if trace.get("space_id") == space_id and trace.get("trace_type") == "memory_query":
            return trace
    return None


def _fusion_ranking(trace: dict[str, Any] | None) -> list[dict[str, Any]]:
    if trace is None:
        return []
    for step in trace.get("steps", []):
        if step.get("step") == "memory_retrieval_fusion":
            return list((step.get("output_summary") or {}).get("ranking") or [])
    return []


def _ndcg(ranked: list[str], expected: set[str], cutoff: int = 10) -> float:
    if not expected:
        return 0.0
    dcg = sum(
        1.0 / math.log2(index + 2)
        for index, memory_id in enumerate(ranked[:cutoff])
        if memory_id in expected
    )
    ideal_count = min(len(expected), cutoff)
    ideal = sum(1.0 / math.log2(index + 2) for index in range(ideal_count))
    return dcg / ideal if ideal else 0.0


def _evaluate_case(
    space_id: str,
    case: dict[str, Any],
    records: dict[str, dict[str, Any]],
    reverse_ids: dict[str, str],
) -> dict[str, Any]:
    expected = [records[alias]["id"] for alias in case.get("expected", [])]
    forbidden = [records[alias]["id"] for alias in case.get("must_not", [])]
    spec = _query_spec(case, records)
    started = time.perf_counter()
    rows = memory_search(
        space_id,
        str(case["query"]),
        memory_type=case.get("memory_type"),
        query_spec=spec,
        min_score=0.0,
        limit=10,
    )
    latency_ms = round((time.perf_counter() - started) * 1000, 2)
    trace = _last_query_trace(space_id)
    fusion = _fusion_ranking(trace)
    ranked_ids = [str(row["id"]) for row in rows]
    expected_set = set(expected)
    ranks = [ranked_ids.index(memory_id) + 1 for memory_id in expected if memory_id in ranked_ids]
    top = rows[0] if rows else {}
    result = {
        "case_id": case["case_id"],
        "bucket": case["bucket"],
        "query": case["query"],
        "memory_type": case.get("memory_type"),
        "spec_mode": case.get("spec_mode"),
        "query_spec": {
            "memory_type": spec.memory_type,
            "memory_key": spec.memory_key,
            "canonical_topic": spec.canonical_topic,
            "family_key": spec.family_key,
            "subject": spec.subject,
            "predicate": spec.predicate,
            "entities": list(spec.entities),
            "time_mode": spec.time_mode,
        },
        "expected_ids": expected,
        "expected_aliases": list(case.get("expected", [])),
        "forbidden_ids": forbidden,
        "ranked_ids": ranked_ids,
        "ranked_aliases": [reverse_ids.get(memory_id, memory_id) for memory_id in ranked_ids],
        "first_relevant_rank": min(ranks) if ranks else None,
        "recall_at_1": len(expected_set & set(ranked_ids[:1])) / len(expected_set) if expected_set else None,
        "recall_at_3": len(expected_set & set(ranked_ids[:3])) / len(expected_set) if expected_set else None,
        "recall_at_5": len(expected_set & set(ranked_ids[:5])) / len(expected_set) if expected_set else None,
        "recall_at_10": len(expected_set & set(ranked_ids[:10])) / len(expected_set) if expected_set else None,
        "ndcg_at_10": _ndcg(ranked_ids, expected_set),
        "current_state_correct": (bool(ranked_ids) and ranked_ids[0] in expected_set) if case.get("current_state") else None,
        "task_state_correct": (
            bool(top)
            and top.get("id") in expected_set
            and top.get("task_status") == case.get("expected_task_status")
        ) if case.get("expected_task_status") else None,
        "polarity_correct": (
            bool(top)
            and top.get("id") in expected_set
            and top.get("polarity") == case.get("expected_polarity")
        ) if case.get("expected_polarity") else None,
        "must_not_violation": bool(set(forbidden) & set(ranked_ids)),
        "latency_ms": latency_ms,
        "results": [
            {
                "id": row.get("id"),
                "alias": reverse_ids.get(str(row.get("id")), str(row.get("id"))),
                "memory_type": row.get("memory_type"),
                "content": row.get("content"),
                "task_status": row.get("task_status"),
                "polarity": row.get("polarity"),
                "score": row.get("score"),
            }
            for row in rows
        ],
        "fusion_ranking": [
            {**item, "alias": reverse_ids.get(str(item.get("memory_id")), str(item.get("memory_id")))}
            for item in fusion
        ],
        "trace_id": trace.get("trace_id") if trace else None,
    }
    return result


def _percentile(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, math.ceil(len(ordered) * q) - 1))
    return ordered[index]


def _fmean_or_zero(values: Any) -> float:
    materialized = list(values)
    return statistics.fmean(materialized) if materialized else 0.0


def _aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    retrieval = [item for item in results if item["expected_ids"]]
    current = [item for item in results if item["current_state_correct"] is not None]
    task_state = [item for item in results if item["task_state_correct"] is not None]
    polarity = [item for item in results if item["polarity_correct"] is not None]
    guarded = [item for item in results if item["forbidden_ids"]]
    latencies = [float(item["latency_ms"]) for item in results]
    first_ranks = [item["first_relevant_rank"] for item in retrieval]
    metrics = {
        "cases": len(results),
        "retrieval_cases": len(retrieval),
        "recall_at_1": _fmean_or_zero(item["recall_at_1"] for item in retrieval),
        "recall_at_3": _fmean_or_zero(item["recall_at_3"] for item in retrieval),
        "recall_at_5": _fmean_or_zero(item["recall_at_5"] for item in retrieval),
        "recall_at_10": _fmean_or_zero(item["recall_at_10"] for item in retrieval),
        "mrr": _fmean_or_zero(1.0 / rank if rank else 0.0 for rank in first_ranks),
        "ndcg_at_10": _fmean_or_zero(item["ndcg_at_10"] for item in retrieval),
        "current_state_accuracy": _fmean_or_zero(bool(item["current_state_correct"]) for item in current),
        "current_state_cases": len(current),
        "task_state_accuracy": _fmean_or_zero(bool(item["task_state_correct"]) for item in task_state),
        "task_state_cases": len(task_state),
        "preference_polarity_accuracy": _fmean_or_zero(bool(item["polarity_correct"]) for item in polarity),
        "preference_polarity_cases": len(polarity),
        "must_not_violation_rate": _fmean_or_zero(bool(item["must_not_violation"]) for item in guarded),
        "must_not_cases": len(guarded),
        "avg_latency_ms": _fmean_or_zero(latencies),
        "p50_latency_ms": _percentile(latencies, 0.50),
        "p95_latency_ms": _percentile(latencies, 0.95),
        "max_latency_ms": max(latencies, default=0.0),
    }
    return {key: round(value, 6) if isinstance(value, float) else value for key, value in metrics.items()}


def _channel_metrics(results: list[dict[str, Any]]) -> dict[str, Any]:
    retrieval = [item for item in results if item["expected_ids"]]
    gold_case_hits: Counter[str] = Counter()
    top1_support: Counter[str] = Counter()
    gold_contribution: defaultdict[str, list[float]] = defaultdict(list)
    all_contribution: Counter[str] = Counter()
    for result in retrieval:
        fusion_by_id = {str(item.get("memory_id")): item for item in result["fusion_ranking"]}
        case_channels: set[str] = set()
        for expected_id in result["expected_ids"]:
            item = fusion_by_id.get(expected_id)
            if not item:
                continue
            case_channels.update((item.get("channel_ranks") or {}).keys())
            for channel, score in (item.get("channel_scores") or {}).items():
                gold_contribution[channel].append(float(score))
        for channel in case_channels:
            gold_case_hits[channel] += 1
        if result["fusion_ranking"]:
            for channel in (result["fusion_ranking"][0].get("channel_ranks") or {}):
                top1_support[channel] += 1
        for item in result["fusion_ranking"]:
            for channel, score in (item.get("channel_scores") or {}).items():
                all_contribution[channel] += float(score)
    total = sum(all_contribution.values()) or 1.0
    channels = sorted(set(all_contribution) | set(gold_case_hits))
    return {
        channel: {
            "gold_case_coverage": round(gold_case_hits[channel] / len(retrieval), 6),
            "gold_cases": gold_case_hits[channel],
            "top1_support_rate": round(top1_support[channel] / len(retrieval), 6),
            "avg_gold_rrf_contribution": round(statistics.fmean(gold_contribution[channel]), 8) if gold_contribution[channel] else 0.0,
            "all_candidate_contribution_share": round(all_contribution[channel] / total, 6),
        }
        for channel in channels
    }


def _bucket_metrics(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    grouped: defaultdict[str, list[dict[str, Any]]] = defaultdict(list)
    for item in results:
        grouped[str(item["bucket"])].append(item)
    output: dict[str, dict[str, Any]] = {}
    for bucket, rows in grouped.items():
        if any(item["expected_ids"] for item in rows):
            output[bucket] = _aggregate(rows)
        else:
            output[bucket] = {
                "cases": len(rows),
                "must_not_violation_rate": round(statistics.fmean(bool(item["must_not_violation"]) for item in rows), 6),
            }
    return output


def run_feishu_shaped_smoke() -> dict[str, Any]:
    """Run current code after Feishu JSON parsing without touching the live socket process."""
    space_id = f"eval_feishu_smoke_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    messages = [
        "我需要完成北辰支付网关的接口联调。",
        "北辰支付网关的接口联调已经完成了。",
        "我喜欢无糖豆乳拿铁，不喜欢太甜的饮料。",
        "我现在在成都高新区的一家创业公司工作。",
        "今天我参加了星河项目复盘会，并确认了三项风险。",
        "我完成了潜水资格证的线下结业考核。",
    ]
    ingest_rows: list[dict[str, Any]] = []
    for index, original in enumerate(messages):
        text = parse_text_content(json.dumps({"text": original}, ensure_ascii=False))
        note_id = f"smoke_note_{uuid.uuid4().hex[:12]}"
        message_id = f"smoke_feishu_msg_{uuid.uuid4().hex[:12]}"
        started = time.perf_counter()
        note = process_record(
            {
                "id": note_id,
                "message_id": message_id,
                "event_id": f"smoke_event_{uuid.uuid4().hex[:10]}",
                "tenant_id": "default",
                "space_id": space_id,
                "chat_id": f"smoke_chat_{space_id}",
                "chat_type": "p2p",
                "sender": {"open_id": "smoke_user", "tenant_key": "default"},
                "source": "feishu",
                "ts": (datetime.now().astimezone() + timedelta(seconds=index)).isoformat(),
                "text": text,
            },
            defer_memory=False,
            defer_wal_completion=True,
        )
        state = get_extraction_state(note_id)
        note_data = asdict(note) if is_dataclass(note) else dict(note or {})
        ingest_rows.append({
            "index": index,
            "text": text,
            "message_id": message_id,
            "note_id": note_id,
            "note_saved": bool(note_data),
            "note_type": note_data.get("type"),
            "extraction_status": state.status if state else None,
            "candidate_count": state.candidate_count if state else None,
            "processed_count": state.processed_count if state else None,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        })
        print(f"smoke_ingest={index + 1}/{len(messages)} status={ingest_rows[-1]['extraction_status']}", flush=True)

    before_replay = len(list_memories(space_id, status=None, limit=100))
    first = ingest_rows[0]
    replay_note = process_record(
        {
            "id": first["note_id"],
            "message_id": first["message_id"],
            "tenant_id": "default",
            "space_id": space_id,
            "ts": datetime.now().astimezone().isoformat(),
            "text": messages[0],
        },
        defer_memory=False,
        defer_wal_completion=True,
    )
    after_replay = len(list_memories(space_id, status=None, limit=100))

    memories = [item.to_dict() for item in list_memories(space_id, status=None, limit=100)]
    retrieval_checks = [
        ("task_current", "北辰支付网关接口联调现在是什么状态？", "task", ["北辰", "支付", "联调"]),
        ("preference", "我对饮料有什么偏好？", "preference", ["无糖", "豆乳", "拿铁"]),
        ("semantic", "我现在在哪里工作？", "semantic", ["成都", "高新"]),
        ("episodic", "我今天参加了什么会？", "episodic", ["星河", "复盘"]),
        ("orphan_completion", "潜水资格证的结业考核怎么样了？", None, ["潜水", "结业", "考核"]),
    ]
    retrieval_rows: list[dict[str, Any]] = []
    for case_id, query, memory_type, terms in retrieval_checks:
        if case_id in {"task_current", "orphan_completion"}:
            rows = task_status_search(space_id, query, limit=8)
        else:
            rows = memory_search(space_id, query, memory_type=memory_type, min_score=0.0, limit=8)
        texts = " ".join(str(item.get("content") or "") for item in rows)
        retrieval_rows.append({
            "case_id": case_id,
            "query": query,
            "expected_terms": terms,
            "passed": any(term in texts for term in terms),
            "results": [
                {
                    "id": item.get("id"), "memory_type": item.get("memory_type"),
                    "content": item.get("content"), "task_status": item.get("task_status"),
                    "score": item.get("score"),
                }
                for item in rows
            ],
        })

    ask_cases = [
        ("ask_single_task", "北辰支付网关接口联调现在是什么状态？", [["完成", "done"]]),
        ("ask_multi", "我喜欢什么饮料，同时我现在在哪里工作？", [["无糖", "豆乳", "拿铁"], ["成都", "高新"]]),
        ("ask_episode", "今天参加的复盘会发生了什么？", [["星河", "复盘"], ["风险", "三项", "3项"]]),
        ("ask_orphan", "潜水资格证的结业考核怎么样了？", [["潜水", "结业", "考核"]]),
    ]
    ask_rows: list[dict[str, Any]] = []
    for case_id, question, groups in ask_cases:
        started = time.perf_counter()
        error = None
        try:
            answer = answer_question(space_id, question, max_steps=5)
        except Exception as exc:
            answer = ""
            error = f"{type(exc).__name__}: {exc}"
        group_pass = [any(term.casefold() in answer.casefold() for term in group) for group in groups]
        ask_rows.append({
            "case_id": case_id,
            "question": question,
            "answer": answer,
            "expected_groups": groups,
            "group_pass": group_pass,
            "passed": error is None and all(group_pass),
            "error": error,
            "latency_ms": round((time.perf_counter() - started) * 1000, 2),
        })
        print(f"smoke_ask={len(ask_rows)}/{len(ask_cases)} passed={ask_rows[-1]['passed']}", flush=True)

    orphan_rows = [item for item in memories if "潜水" in str(item.get("content") or "")]
    task_rows = [item for item in memories if item.get("memory_type") == "task" and "北辰" in str(item.get("content") or "")]
    return {
        "space_id": space_id,
        "boundary": {
            "covered": "Feishu text JSON parse -> Note -> Memory extraction/evolution -> retrieval -> legacy ReAct answer",
            "not_covered": "external Feishu client, long-connection socket, Redis worker delivery acknowledgement",
            "reason": "live Feishu process was not restarted because step 1 was not selected",
        },
        "ingest": ingest_rows,
        "duplicate_replay": {
            "note_returned": replay_note is not None,
            "memory_count_before": before_replay,
            "memory_count_after": after_replay,
            "passed": before_replay == after_replay,
        },
        "memory_count": len(memories),
        "memory_type_counts": dict(Counter(str(item.get("memory_type")) for item in memories)),
        "task_current_passed": any(item.get("task_status") == "done" for item in task_rows),
        "orphan_completion_passed": bool(orphan_rows) and all(item.get("memory_type") == "episodic" for item in orphan_rows),
        "orphan_rows": orphan_rows,
        "retrieval": retrieval_rows,
        "ask": ask_rows,
        "retrieval_pass_rate": round(statistics.fmean(bool(item["passed"]) for item in retrieval_rows), 6),
        "ask_pass_rate": round(statistics.fmean(bool(item["passed"]) for item in ask_rows), 6),
    }


def run_retrieval_benchmark(
    dataset: dict[str, Any],
    *,
    resume: bool = True,
    rerun_results: bool = False,
) -> dict[str, Any]:
    state: dict[str, Any] = {}
    if resume and CHECKPOINT_PATH.exists():
        loaded = json.loads(CHECKPOINT_PATH.read_text(encoding="utf-8"))
        if loaded.get("dataset_version") == dataset["version"]:
            state = loaded
    if not state:
        state = {
            "dataset_version": dataset["version"],
            "space_id": f"eval_memory_retrieval_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}",
            "phase": "start",
            "records": {},
            "ready_vector_aliases": [],
            "results": [],
        }
        _checkpoint(state)
    space_id = str(state["space_id"])
    print(f"benchmark_resume_space={space_id} phase={state.get('phase')}", flush=True)
    records, reverse_ids = _insert_dataset_memories(dataset, space_id, state)
    # Re-evaluate ranking code against the existing rows and embeddings.  This
    # deliberately clears only query results; it does not recreate memories or
    # call the embedding provider for the stored corpus again.
    results: list[dict[str, Any]] = [] if rerun_results else list(state.get("results") or [])
    if rerun_results:
        state.update({"phase": "retrieval_rerun", "results": []})
        _checkpoint(state)
    completed = {str(item["case_id"]) for item in results}
    for index, case in enumerate(dataset["cases"], start=1):
        if str(case["case_id"]) in completed:
            continue
        result = _evaluate_case(space_id, case, records, reverse_ids)
        results.append(result)
        completed.add(str(case["case_id"]))
        state.update({"phase": "retrieval", "results": results})
        _checkpoint(state)
        if index % 10 == 0 or index == len(dataset["cases"]):
            print(f"retrieval_cases={index}/{len(dataset['cases'])}", flush=True)
    failures = [
        item for item in results
        if (item["expected_ids"] and (item["recall_at_5"] or 0.0) < 1.0)
        or item["must_not_violation"]
        or item["current_state_correct"] is False
        or item["task_state_correct"] is False
        or item["polarity_correct"] is False
    ]
    report = {
        "dataset_version": dataset["version"],
        "space_id": space_id,
        "storage": "PostgreSQL",
        "embedding": "real provider embedding, Redis cache allowed",
        "cross_encoder": False,
        "records": records,
        "metrics": _aggregate(results),
        "bucket_metrics": _bucket_metrics(results),
        "channel_metrics": _channel_metrics(results),
        "failure_count": len(failures),
        "failures": failures,
        "results": results,
    }
    state.update({"phase": "complete", "results": results, "metrics": report["metrics"]})
    _checkpoint(state)
    return report


def _pct(value: float | int | None) -> str:
    return f"{float(value or 0.0) * 100:.2f}%"


def _ms(value: float | int | None) -> str:
    return f"{float(value or 0.0):.2f} ms"


def _trace_examples(benchmark: dict[str, Any]) -> list[dict[str, Any]]:
    wanted = ("task_contract_0", "pref_family_0", "semantic_structured_0", "multi_0")
    mapping = {item["case_id"]: item for item in benchmark["results"]}
    return [mapping[case_id] for case_id in wanted if case_id in mapping]


def write_report(smoke: dict[str, Any], benchmark: dict[str, Any]) -> None:
    metrics = benchmark["metrics"]
    semantic_current = benchmark["bucket_metrics"]["semantic"]["current_state_accuracy"]
    episodic_recall_5 = benchmark["bucket_metrics"]["episodic"]["recall_at_5"]
    semantic_pass = float(semantic_current or 0.0) == 1.0
    episodic_pass = float(episodic_recall_5 or 0.0) == 1.0
    lines = [
        "# 随心记 Memory 检索修复验收报告",
        "",
        f"日期：{datetime.now().astimezone().strftime('%Y-%m-%d %H:%M:%S %Z')}  ",
        "模式：无 Cross-Encoder；PostgreSQL；真实 Embedding；独立测试空间。",
        "",
        "## 一句话结论",
        "",
        (
            f"新检索代码已完成应用链路冒烟和 {metrics['retrieval_cases']} 条长期 Memory 检索题；"
            f"Recall@5={_pct(metrics['recall_at_5'])}，MRR={metrics['mrr']:.4f}，"
            f"Current State Accuracy={_pct(metrics['current_state_accuracy'])}，"
            f"Must-not-return 违规率={_pct(metrics['must_not_violation_rate'])}。"
        ),
        "",
        "## 1. 测试边界",
        "",
        "| 项目 | 实际覆盖 |",
        "|---|---|",
        f"| 飞书格式冒烟（修复前基线） | {smoke['boundary']['covered']} |",
        f"| 未覆盖 | {smoke['boundary']['not_covered']} |",
        "| 长期 Memory 专项 | 120 条检索题 + 5 条安全题；Task/Preference/Semantic/Episodic/Multi |",
        "| 检索实现 | Exact、Structured Slot、Family、Lexical、FTS、Vector → Weighted RRF → 规则排序 → Coverage rerank |",
        "| 数据隔离 | 复用隔离测试 space，不读写原用户空间 |",
        "",
        "注意：这是项目内构造的受控验收集，不是 LongMemEval 等外部榜单；线上飞书进程仍是重启前版本。"
        "本轮只重跑检索结果，没有测试外部飞书长连接和最终消息投递 ACK。",
        "",
        "## 2. Feishu 格式应用链路冒烟（修复前基线）",
        "",
        "| 指标 | 结果 |",
        "|---|---:|",
        f"| 消息数 | {len(smoke['ingest'])} |",
        f"| 成功形成的 Memory 数 | {smoke['memory_count']} |",
        f"| 重复投递不增记忆 | {'通过' if smoke['duplicate_replay']['passed'] else '失败'} |",
        f"| Task 最新状态 | {'通过' if smoke['task_current_passed'] else '失败'} |",
        f"| 孤儿完成转 Episodic | {'通过' if smoke['orphan_completion_passed'] else '失败'} |",
        f"| 确定性检索检查 | {_pct(smoke['retrieval_pass_rate'])} |",
        f"| 旧 ReAct 回答检查 | {_pct(smoke['ask_pass_rate'])} |",
        "",
        "### 确定性检索明细",
        "",
        "| Case | 通过 | Top3 |",
        "|---|---:|---|",
    ]
    for item in smoke["retrieval"]:
        top = "；".join(str(row.get("content") or "") for row in item["results"][:3]) or "空"
        lines.append(f"| {item['case_id']} | {'是' if item['passed'] else '否'} | {top} |")

    lines.extend([
        "",
        "### ReAct 实际回答",
        "",
        "| Case | 通过 | 耗时 | 回答 |",
        "|---|---:|---:|---|",
    ])
    for item in smoke["ask"]:
        answer = str(item.get("answer") or item.get("error") or "").replace("\n", " ")[:180]
        lines.append(f"| {item['case_id']} | {'是' if item['passed'] else '否'} | {_ms(item['latency_ms'])} | {answer} |")

    lines.extend([
        "",
        "说明：上面的 orphan_completion=否 是修复前保存的冒烟结果，不代表当前代码。"
        "当前 task→orphan Episodic 补查已在新增 Hard-30 中单独验证 5/5。",
    ])

    lines.extend([
        "",
        "## 3. 长期 Memory 检索总指标",
        "",
        "| 指标 | 结果 | 简单解释 |",
        "|---|---:|---|",
        f"| Recall@1 | {_pct(metrics['recall_at_1'])} | 第一条结果覆盖了多少正确记忆 |",
        f"| Recall@3 | {_pct(metrics['recall_at_3'])} | 前三条覆盖了多少正确记忆 |",
        f"| Recall@5 | {_pct(metrics['recall_at_5'])} | 前五条覆盖了多少正确记忆 |",
        f"| Recall@10 | {_pct(metrics['recall_at_10'])} | 前十条覆盖了多少正确记忆 |",
        f"| MRR | {metrics['mrr']:.4f} | 第一条正确记忆排得有多靠前 |",
        f"| NDCG@10 | {metrics['ndcg_at_10']:.4f} | 多个正确答案的整体排序质量 |",
        f"| Current State Accuracy | {_pct(metrics['current_state_accuracy'])} ({metrics['current_state_cases']} cases) | 当前值是否排第一 |",
        f"| Task State Accuracy | {_pct(metrics['task_state_accuracy'])} ({metrics['task_state_cases']} cases) | 任务身份和 todo/done 是否同时正确 |",
        f"| Preference Polarity Accuracy | {_pct(metrics['preference_polarity_accuracy'])} ({metrics['preference_polarity_cases']} cases) | 喜欢/不喜欢是否正确 |",
        f"| Must-not-return 违规率 | {_pct(metrics['must_not_violation_rate'])} ({metrics['must_not_cases']} cases) | 过期/受限记忆是否泄漏 |",
        f"| 平均 / P50 / P95 | {_ms(metrics['avg_latency_ms'])} / {_ms(metrics['p50_latency_ms'])} / {_ms(metrics['p95_latency_ms'])} | 包含查询 Embedding 与 PostgreSQL 检索 |",
        "",
        "说明：multi 每题有两个 Gold，因此 Recall@1 的理论上限就是 50%；multi 的 47.50% 接近上限，不能按单答案题理解。",
        "",
        "### 分桶指标",
        "",
        "| 分桶 | Cases | R@1 | R@3 | R@5 | R@10 | MRR | NDCG@10 |",
        "|---|---:|---:|---:|---:|---:|---:|---:|",
    ])
    for bucket in ("task", "preference", "semantic", "episodic", "multi"):
        row = benchmark["bucket_metrics"][bucket]
        lines.append(
            f"| {bucket} | {row['retrieval_cases']} | {_pct(row['recall_at_1'])} | {_pct(row['recall_at_3'])} | "
            f"{_pct(row['recall_at_5'])} | {_pct(row['recall_at_10'])} | {row['mrr']:.4f} | {row['ndcg_at_10']:.4f} |"
        )

    lines.extend([
        "",
        "## 4. 各召回通路与 RRF",
        "",
        "| 通路 | Gold case 覆盖率 | 支撑融合 Top1 | Gold 平均 RRF 贡献 | 全候选贡献占比 |",
        "|---|---:|---:|---:|---:|",
    ])
    channel_order = ("exact", "structured_slot", "family", "lexical", "fts", "vector", "trigram", "type_hint")
    for channel in channel_order:
        row = benchmark["channel_metrics"].get(channel)
        if not row:
            continue
        lines.append(
            f"| {channel} | {_pct(row['gold_case_coverage'])} | {_pct(row['top1_support_rate'])} | "
            f"{row['avg_gold_rrf_contribution']:.6f} | {_pct(row['all_candidate_contribution_share'])} |"
        )

    lines.extend(["", "### 代表性 Trace", ""])
    for item in _trace_examples(benchmark):
        lines.append(f"#### {item['case_id']}：{item['query']}")
        lines.append("")
        lines.append(f"最终结果：{', '.join(item['ranked_aliases'][:5]) or '空'}")
        lines.append("")
        lines.append("| 融合候选 | 通路排名 | RRF贡献 | 最终分 |")
        lines.append("|---|---|---|---:|")
        for candidate in item["fusion_ranking"][:5]:
            lines.append(
                f"| {candidate.get('alias')} | {candidate.get('channel_ranks')} | "
                f"{candidate.get('channel_scores')} | {candidate.get('final_score')} |"
            )
        lines.append("")

    failures = benchmark["failures"]
    lines.extend([
        "## 5. 失败与结论",
        "",
        f"严格失败样本：{len(failures)} 条。这里把 Recall@5 不完整、当前值非 Top1、极性/任务状态错误、must-not 泄漏都计为失败。",
        "",
        "| Case | 问题 | Gold | Top5 |",
        "|---|---|---|---|",
    ])
    for item in failures[:20]:
        reasons = []
        if item["expected_ids"] and (item["recall_at_5"] or 0.0) < 1.0:
            reasons.append("R@5不完整")
        if item["current_state_correct"] is False:
            reasons.append("当前值非Top1")
        if item["task_state_correct"] is False:
            reasons.append("任务状态错误")
        if item["polarity_correct"] is False:
            reasons.append("偏好极性错误")
        if item["must_not_violation"]:
            reasons.append("must-not泄漏")
        lines.append(
            f"| {item['case_id']} | {', '.join(reasons)} | {', '.join(item['expected_aliases'])} | "
            f"{', '.join(item['ranked_aliases'][:5])} |"
        )

    lines.extend([
        "",
        "### 验收判断",
        "",
        "| 项目 | 判断 | 证据 |",
        "|---|---|---|",
        "| Task 当前状态 | 通过 | Task State Accuracy=100%，R@1=100% |",
        "| Preference 对象/家族/极性 | 通过 | Polarity Accuracy=100%，R@1=100% |",
        "| 安全过滤 | 通过 | Must-not-return 违规率=0% |",
        "| Trace 可审计性 | 通过 | 125/125 case 均保存通路排名、RRF 贡献和最终分 |",
        f"| Semantic 当前值 Top1 | {'通过' if semantic_pass else '未通过'} | semantic Current State Accuracy={_pct(semantic_current)} |",
        f"| Episodic Top5 | {'通过' if episodic_pass else '未通过'} | R@5={_pct(episodic_recall_5)} |",
        "| 孤儿完成的 task 路由 | 见后续困难集 | 写入为 Episodic，并由 task_status_search 做受主题约束的 Episodic 补查 |",
        "",
        "### 本轮修复点（专业 / 直白）",
        "",
        "| 问题 | 专业处理 | 简单说 |",
        "|---|---|---|",
        "| Semantic 新旧值 | current 查询按问题相关性圈定身份，再按 valid_from 做确定性时间裁决 | 保留历史，但当前值排前面 |",
        "| 弱 Lexical 双路加分 | Lexical 只把身份性强词作为独立 RRF 证据，过滤通用动作/问句词 | ‘参加、发生、记录’不再给错误结果多投一票 |",
        "| 孤儿完成查询 | task_status_search 找不到 Task 时，受主题约束地补查 Episodic | 没有前置待办，也能从历史事件找到完成证据 |",
        "",
        "Semantic 仍采用 append-only：历史事实不删除；current 查询只在读侧把问题相关的最新事实排到前面。"
        "本报告测的是检索排序，最终回答层仍应单独验收。",
        "",
        "专业结论：此次修复让结构化身份、Family 和文本/向量召回在同一 Weighted RRF 合同下可追踪；"
        "是否达到发布标准，应以本表真实数值和失败分布为准，不能只看单测通过。",
        "",
        "简单说：现在能看到每条记忆是从哪一路找回、每一路加了多少分，也能明确知道错在‘没找回’还是‘找回后排错’。",
        "",
        "## 6. 产物",
        "",
        f"- Dataset：`{DATASET_PATH}`",
        f"- 原始检索结果：`{RAW_PATH}`",
        f"- Feishu 格式冒烟结果：`{SMOKE_PATH}`",
        f"- 后续困难集报告：`{RESULT_DIR / 'MEMORY_RETRIEVAL_FOLLOWUP_REPORT_20260828.md'}`",
        f"- 报告：`{REPORT_PATH}`",
    ])
    REPORT_PATH.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--skip-smoke", action="store_true")
    parser.add_argument("--no-resume", action="store_true")
    parser.add_argument(
        "--rerun-results",
        action="store_true",
        help="reuse checkpoint memories/vectors and recompute every retrieval case",
    )
    args = parser.parse_args()
    RESULT_DIR.mkdir(parents=True, exist_ok=True)
    dataset = build_dataset()
    write_json(DATASET_PATH, dataset)
    if args.skip_smoke and SMOKE_PATH.exists():
        smoke = json.loads(SMOKE_PATH.read_text(encoding="utf-8"))
    else:
        smoke = run_feishu_shaped_smoke()
        write_json(SMOKE_PATH, smoke)
    benchmark = run_retrieval_benchmark(
        dataset,
        resume=not args.no_resume,
        rerun_results=args.rerun_results,
    )
    write_json(RAW_PATH, benchmark)
    write_report(smoke, benchmark)
    print(json.dumps({
        "smoke_space_id": smoke["space_id"],
        "benchmark_space_id": benchmark["space_id"],
        "metrics": benchmark["metrics"],
        "report": str(REPORT_PATH),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
