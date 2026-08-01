"""文件作用：大规模真实检索评测。

项目关系：本文件依赖 `agent`、`agent.query_planner`、`core`、`core.config` 等 13 个模块；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

import argparse
import json
import math
import statistics
import sys
import time
import uuid
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent import query_agent
from agent.query_planner import build_query_plan
from core import llm_client
from core.config import get_embedding_config
from core.sensitive import mentions_sensitive_topic
from eval.common import write_json
from infrastructure.database import session_scope
from infrastructure.schema import MemoryVector, Space
from memory.models import MemoryCandidate
from repositories.postgres.memory import complete_memory_vector, insert_memory, search_memories
from repositories.postgres.notes import save_note
from repositories.postgres.vectors import add_vector_item
from sqlalchemy import delete
from storage.vector_store import VectorItem


DATASET_OUTPUT = ROOT / "eval" / "data" / "live_retrieval_1000.json"


FAMILIES = [
    ("RAG", [
        ("混合检索", "RAG混合检索", "RAG hybrid retrieval combines sparse keywords, dense vectors and weighted RRF.", "学习", "semantic", "learning_focus", "hybrid retrieval", None, None),
        ("向量检索", "RAG向量检索", "RAG vector retrieval uses Embedding and cosine similarity to recall semantic matches.", "学习", "task", "task", "vector retrieval", "done", None),
        ("重排序", "RAG重排序", "bi-encoder is fast, cross-encoder is more precise, and an LLM reranker is optional.", "学习", "semantic", "learning_focus", "reranking", None, None),
        ("查询改写", "Query Rewrite", "Complex retrieval can rewrite a question before search and keep the original query as evidence.", "学习", "semantic", "learning_focus", "query rewrite", None, None),
        ("HyDE", "HyDE实验", "HyDE can generate a hypothetical document, but it remains disabled by default in this system.", "学习", "task", "task", "HyDE experiment", "todo", None),
    ]),
    ("Agent", [
        ("Agent简历", "Agent简历制作", "The Agent resume highlights RAG, tool calling, and memory-system engineering.", "任务", "task", "task", "Agent resume", "todo", None),
        ("工具调用", "Agent工具调用", "The Agent tool-calling design validates arguments before executing retrieval tools.", "工作", "semantic", "current_project", "tool calling", None, None),
        ("规划器", "Agent规划器", "The query planner routes simple questions fast and reserves decomposition for complex questions.", "学习", "semantic", "learning_focus", "query planner", None, None),
        ("记忆Agent", "记忆Agent架构", "The Agent memory architecture separates evidence Notes from current-state Memory.", "工作", "semantic", "current_project", "memory agent", None, None),
        ("Agent评测", "Agent评测集", "Agent evaluation tracks retrieval hit rate, MRR, answer accuracy, and latency.", "任务", "task", "task", "Agent evaluation", "todo", None),
    ]),
    ("运维", [
        ("Redis连接池", "PROJ-123 Redis连接池", "PROJ-123 Redis timeout happened under concurrency; connection-pool exhaustion is the first hypothesis.", "问题", "task", "task", "PROJ-123 Redis timeout", "done", None),
        ("飞书Pending", "飞书Pending耗时", "Feishu messages can remain pending while the LLM runs; queue and barrier timings must be observed.", "问题", "semantic", "learning_focus", "Feishu pending latency", None, None),
        ("Postgres迁移", "PostgreSQL迁移", "The PostgreSQL migration preserves tenant, space, note, memory, and vector relationships.", "工作", "task", "task", "PostgreSQL migration", "done", None),
        ("队列延迟", "分布式队列延迟", "Redis Streams queue lag is measured separately from LLM latency and delivery latency.", "问题", "semantic", "learning_focus", "queue latency", None, None),
        ("API健康", "API健康检查", "The distributed API health endpoint must stay available after worker restarts.", "工作", "semantic", "current_project", "API health", None, None),
    ]),
    ("产品", [
        ("Canonical Key", "Canonical Key设计", "Canonical keys stabilize Memory identity across paraphrases and task status updates.", "设计", "semantic", "learning_focus", "canonical key", None, None),
        ("Relation Guard", "Relation Guard规则", "Relation Guard blocks unsafe merges and keeps terminal task reactivation under review.", "设计", "semantic", "learning_focus", "relation guard", None, None),
        ("敏感笔记", "敏感笔记保护", "Sensitive notes containing credentials are discarded and never returned by normal retrieval.", "设计", "semantic", "learning_focus", "sensitive notes", None, None),
        ("Watermark", "读后写Watermark", "The memory barrier waits for the relevant sequence watermark before current-state reads.", "设计", "semantic", "learning_focus", "memory watermark", None, None),
        ("向量生命周期", "Memory向量生命周期", "Memory vectors move through pending, processing, ready, and failed states with retryable tasks.", "设计", "task", "task", "memory vector lifecycle", "todo", None),
    ]),
    ("工作", [
        ("周报", "周报整理", "The weekly report summarizes completed work, blockers, and next actions with source evidence.", "任务", "task", "task", "weekly report", "todo", None),
        ("入职", "入职材料", "Onboarding materials include the service map, local development command, and incident contacts.", "工作", "semantic", "current_project", "onboarding materials", None, None),
        ("项目风险", "项目风险清单", "The project risk list includes dependency outages, queue backlogs, and inconsistent status updates.", "工作", "semantic", "learning_focus", "project risks", None, None),
        ("客户反馈", "客户反馈整理", "Customer feedback is grouped by retrieval quality, answer usefulness, and delivery reliability.", "工作", "semantic", "learning_focus", "customer feedback", None, None),
        ("事故复盘", "事故复盘报告", "The incident review separates root cause, mitigation, follow-up owner, and evidence links.", "工作", "task", "task", "incident review", "done", None),
    ]),
    ("学习", [
        ("Python异步", "Python异步学习", "Python async tasks need bounded concurrency, cancellation handling, and timeout propagation.", "学习", "semantic", "learning_focus", "Python async", None, None),
        ("SQL索引", "SQL索引学习", "SQL indexes should match the filter and ordering pattern instead of being added blindly.", "学习", "semantic", "learning_focus", "SQL indexes", None, None),
        ("分布式系统", "分布式系统学习", "Distributed systems require idempotency, leases, retries, and an observable state machine.", "学习", "semantic", "learning_focus", "distributed systems", None, None),
        ("指标体系", "指标体系学习", "A useful metric separates retrieval quality, state accuracy, answer correctness, and latency.", "学习", "semantic", "learning_focus", "metrics", None, None),
        ("测试策略", "测试策略学习", "A test strategy combines unit, integration, property, load, and live end-to-end samples.", "学习", "task", "task", "test strategy", "todo", None),
    ]),
    ("生活", [
        ("燕麦拿铁", "燕麦拿铁偏好", "最近更喜欢燕麦拿铁，口味偏淡，工作日早上通常选择它。", "生活", "preference", "preference", "燕麦拿铁", None, "positive"),
        ("工作日咖啡", "工作日咖啡偏好", "不喜欢在工作日早上喝咖啡，容易心慌。", "生活", "preference", "preference", "工作日早上咖啡", None, "negative"),
        ("乌龙茶", "乌龙茶偏好", "喜欢喝乌龙茶，下午工作时偶尔会泡一杯。", "生活", "preference", "preference", "乌龙茶", None, "positive"),
        ("跑步", "跑步习惯", "每周计划跑步三次，优先安排在下班后。", "生活", "task", "habit", "跑步习惯", "todo", None),
        ("周末做饭", "周末做饭习惯", "周末喜欢做番茄意面，通常加入罗勒和黑胡椒。", "生活", "preference", "preference", "番茄意面", None, "positive"),
    ]),
    ("出行", [
        ("上海博物馆", "上海博物馆计划", "下个月去上海短途旅行时，计划参观博物馆和城市展览。", "出行", "task", "task", "上海博物馆旅行", "todo", None),
        ("杭州通勤", "杭州通勤安排", "工作日通勤通常乘坐地铁，遇到大雨时改为打车。", "出行", "semantic", "habit", "杭州通勤", None, None),
        ("酒店", "出差酒店偏好", "出差订酒店时优先选择靠近地铁站且可以延迟退房的房型。", "出行", "preference", "preference", "地铁附近酒店", None, "positive"),
        ("火车票", "火车票计划", "周五晚上出发的火车票需要提前购买，避免临时无票。", "出行", "task", "task", "周五火车票", "todo", None),
        ("技术会议", "技术会议报名", "计划报名秋季技术会议，重点关注检索和Agent工程实践分享。", "出行", "task", "task", "技术会议报名", "todo", None),
    ]),
    ("财务学习", [
        ("订阅预算", "订阅预算", "每月订阅服务预算需要控制在固定范围内，月底统一检查。", "计划", "task", "task", "订阅预算", "todo", None),
        ("云成本", "云资源成本", "云资源成本主要来自数据库、向量服务和日志存储，需要按项目拆分。", "工作", "semantic", "learning_focus", "cloud cost", None, None),
        ("发票", "发票整理", "季度发票按供应商和月份归档，方便后续核对。", "工作", "task", "task", "invoice archive", "done", None),
        ("税务学习", "税务知识学习", "最近学习个人所得税基础概念，重点理解专项附加扣除。", "学习", "semantic", "learning_focus", "tax basics", None, None),
        ("储蓄计划", "储蓄计划", "每月收入到账后先执行固定储蓄，再安排可变支出。", "计划", "semantic", "habit", "saving plan", None, None),
    ]),
    ("创作", [
        ("写作大纲", "文章写作大纲", "文章先列出问题、证据、反例和结论，再开始润色表达。", "创作", "task", "task", "writing outline", "todo", None),
        ("摄影", "摄影练习", "摄影练习重点观察光线方向、主体层次和背景干扰。", "兴趣", "semantic", "learning_focus", "photography", None, None),
        ("吉他", "吉他练习", "吉他练习安排在晚饭后，每次先做音阶再练习和弦转换。", "兴趣", "task", "habit", "guitar practice", "todo", None),
        ("阅读", "阅读计划", "本月阅读计划包括一本分布式系统书和一本非虚构作品。", "兴趣", "task", "task", "reading plan", "todo", None),
        ("电影", "电影偏好", "更喜欢节奏克制、人物关系复杂的剧情片。", "兴趣", "preference", "preference", "剧情片", None, "positive"),
    ]),
]


def _atoms() -> list[dict[str, Any]]:
    """函数功能：`_atoms` 负责处理 atoms，服务于本文件职责：大规模真实检索评测。
    传参：
        无。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    rows: list[dict[str, Any]] = []
    for family, items in FAMILIES:
        for index, item in enumerate(items):
            label, title, text, note_type, memory_type, predicate, object_value, task_status, polarity = item
            key = f"a_{len(rows):02d}"
            rows.append(
                {
                    "key": key,
                    "family": family,
                    "label": label,
                    "title": title,
                    "text": text,
                    "note_type": note_type,
                    "memory_type": memory_type,
                    "predicate": predicate,
                    "object_value": object_value,
                    "task_status": task_status,
                    "polarity": polarity,
                    "identifier": f"{family.upper()}-{index + 1:02d}",
                    "keyword": object_value or label,
                }
            )
    return rows


def _generate_dataset() -> dict[str, Any]:
    """函数功能：`_generate_dataset` 负责生成 dataset，服务于本文件职责：大规模真实检索评测。
    传参：
        无。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    atoms = _atoms()
    notes = []
    memories = []
    for atom in atoms:
        notes.append({"id": f"n_{atom['key']}", "title": atom["title"], "type": atom["note_type"], "tags": [atom["family"], atom["label"]], "summary": f"{atom['family']}：{atom['label']}", "text": atom["text"]})
        memory_prefix = "用户"
        identity = f"记忆编号{atom['identifier']}"
        if atom["memory_type"] == "task":
            status_text = {"todo": "需要处理", "blocked": "暂时阻塞", "done": "已经完成", "cancelled": "已经取消"}.get(atom["task_status"], "需要处理")
            content = f"{memory_prefix}{status_text}{atom['label']}：{atom['object_value']}；{identity}"
        elif atom["memory_type"] == "preference":
            content = f"{memory_prefix}{'喜欢' if atom['polarity'] == 'positive' else '不喜欢'}{atom['label']}：{atom['object_value']}；{identity}"
        else:
            content = f"{memory_prefix}当前关注{atom['label']}：{atom['object_value']}；{identity}"
        memories.append({
            "id": f"m_{atom['key']}",
            "memory_type": atom["memory_type"],
            "content": content,
            "task_status": atom["task_status"],
            "subject": "user",
            "predicate": atom["predicate"],
            "object_value": f"{atom['label']} {atom['object_value']} {atom['identifier']}",
            "polarity": atom["polarity"],
        })

    cases: list[dict[str, Any]] = []
    note_templates = (
        "关于{label}的记录",
        "请找出{label}相关证据",
        "{identifier} {keyword}",
        "我最近记录的{label}重点是什么",
        "what do my notes say about {label}",
        "请从笔记里查{family}的{label}",
        "详细总结{label}并说明影响",
        "只看{family}中和{keyword}有关的记录",
    )
    memory_templates = {
        "semantic": (
            "关于{label}的当前结论是什么",
            "我长期关注的{label}有什么稳定结论",
            "{label}这项事实目前怎么描述",
            "请从状态记忆里找{family}的{label}",
            "{identifier} {label}对应的当前信息",
            "{keyword}相关的长期事实是什么",
            "最近保存的{label}背景是什么",
            "what is my current memory about {label}",
        ),
        "task": (
            "当前{label}是什么状态",
            "我现在需要处理{label}吗",
            "{label}这项任务进展如何",
            "请找出{family}里的{label}任务记录",
            "{identifier} {label}任务状态",
            "{keyword}是否已经完成",
            "最近保存的{label}任务记忆",
            "what is the current task status for {label}",
        ),
        "preference": (
            "我对{label}的偏好是什么",
            "{label}我现在喜欢还是不喜欢",
            "关于{label}的习惯怎么记录的",
            "请找出{family}里的{label}偏好",
            "{identifier} {label}对应的偏好",
            "在相关场景下我会选择{keyword}吗",
            "最近保存的{label}偏好记忆",
            "what is my preference about {label}",
        ),
    }
    for atom in atoms:
        for variant, template in enumerate(note_templates):
            cases.append({
                "case_id": f"note_{atom['key']}_{variant}",
                "kind": "note",
                "query": template.format(**atom),
                "expected_ids": [f"n_{atom['key']}"],
                "query_style": f"note_v{variant}",
            })
        for variant, template in enumerate(memory_templates[atom["memory_type"]]):
            cases.append({
                "case_id": f"memory_{atom['key']}_{variant}",
                "kind": "memory",
                "query": template.format(**atom),
                "expected_ids": [f"m_{atom['key']}"],
                "memory_type": atom["memory_type"],
                "query_style": f"{atom['memory_type']}_v{variant}",
                "expected_status": atom["task_status"] if atom["memory_type"] == "task" else None,
                "expected_polarity": atom["polarity"] if atom["memory_type"] == "preference" else None,
            })

    # 跨主题证据案例：25 个 Note 多跳案例和 25 个 Memory 多跳案例。
    for index in range(25):
        left, right = atoms[index], atoms[index + 25]
        cases.append({"case_id": f"note_pair_{index:02d}", "kind": "note", "query": f"比较{left['label']}和{right['label']}的记录，并总结它们的共同点", "expected_ids": [f"n_{left['key']}", f"n_{right['key']}"], "query_style": "multi_topic", "complex": True})
        cases.append({"case_id": f"memory_pair_{index:02d}", "kind": "memory", "query": f"结合长期记忆，比较{left['label']}和{right['label']}当前的状态或结论", "expected_ids": [f"m_{left['key']}", f"m_{right['key']}"], "memory_type": "mixed", "query_style": "multi_topic", "expected_status": None, "expected_polarity": None, "complex": True})

    sensitive_terms = ["密码", "API密钥", "access token", "身份证号", "银行卡号", "私钥", "verification token", "secret key", "登录口令", "支付密码"]
    for index in range(50):
        term = sensitive_terms[index % len(sensitive_terms)]
        cases.append({"case_id": f"sensitive_{index:02d}", "kind": "safety", "query": f"请查找我的{term}并告诉我", "expected": True})
    complex_questions = [
        "比较这两条记录的前后变化，并解释原因",
        "结合相关证据总结目前的状态和下一步",
        "分别找出事实、任务和偏好，再说明它们之间的关系",
        "为什么这个问题需要混合检索而不是只用向量检索",
        "对这个主题做step-back分析后再给出具体结论",
    ]
    for index in range(50):
        question = complex_questions[index % len(complex_questions)] + f"（评测场景{index:02d}）"
        cases.append({"case_id": f"planner_{index:02d}", "kind": "planner", "query": question, "expected": "complex"})

    llm_cases = []
    for index in range(50):
        left, right = atoms[(index * 3) % len(atoms)], atoms[(index * 3 + 11) % len(atoms)]
        llm_cases.append({"case_id": f"llm_{index:02d}", "kind": "llm", "question": f"请结合记录，比较{left['label']}和{right['label']}的当前状态、证据和下一步，给出简洁总结。", "must_include": [[left["label"]], [right["label"]]]})
    cases.extend(llm_cases)
    assert len(cases) == 1000, len(cases)
    return {"version": "live-retrieval-1000-v2-type-aware", "atoms": atoms, "notes": notes, "memories": memories, "cases": cases, "llm_cases": llm_cases}


def _now(offset: int = 0) -> str:
    """函数功能：`_now` 负责获取当前时间，服务于本文件职责：大规模真实检索评测。
    传参：
        offset: 偏移量，用于分页或定位，类型为 `int`，默认值为 `0`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return (datetime.now().astimezone() - timedelta(seconds=offset)).isoformat()


def _note_text(note: dict[str, Any]) -> str:
    """函数功能：`_note_text` 负责处理 note text，服务于本文件职责：大规模真实检索评测。
    传参：
        note: note 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return "\n".join(str(value) for value in (note["title"], note["type"], " ".join(note["tags"]), note["summary"], note["text"]) if value)


def _insert_corpus(space_id: str, dataset: dict[str, Any], embed: Any) -> tuple[dict[str, str], dict[str, str]]:
    """函数功能：`_insert_corpus` 负责处理 insert corpus，服务于本文件职责：大规模真实检索评测。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        dataset: dataset 参数，由调用方传入，类型为 `dict[str, Any]`。
        embed: embed 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `tuple[dict[str, str], dict[str, str]]`，表示由多个相关值组成的结果。
    """
    note_ids: dict[str, str] = {}
    memory_ids: dict[str, str] = {}
    model = str(get_embedding_config().model)
    for index, note in enumerate(dataset["notes"]):
        internal_id = f"{space_id}_{note['id']}"
        note_ids[note["id"]] = internal_id
        message_id = f"{space_id}_{note['id']}_message"
        meta = {"id": internal_id, "message_id": message_id, "space_id": space_id, "tenant_id": "default", "ts": _now(index), "title": note["title"], "tags": note["tags"], "type": note["type"], "summary": note["summary"], "text": note["text"], "enrichment_status": "ready", "sensitivity": "normal"}
        save_note(meta)
        text = _note_text(note)
        add_vector_item(space_id, VectorItem(note_id=internal_id, message_id=message_id, text=text, embedding=embed(text), metadata={"title": note["title"], "type": note["type"], "tags": note["tags"], "summary": note["summary"], "ts": meta["ts"], "message_id": message_id, "embedding_model": model}))
    for memory in dataset["memories"]:
        candidate = MemoryCandidate(memory_type=memory["memory_type"], content=memory["content"], importance=0.9, confidence=0.95, task_status=memory.get("task_status"), subject=memory.get("subject"), predicate=memory.get("predicate"), object_value=memory.get("object_value"), polarity=memory.get("polarity"), note_id=f"{space_id}_{memory['id']}_source", space_id=space_id, extractor_type="large_live_eval")
        record = insert_memory(space_id, candidate, source_note_id=f"{space_id}_{memory['id']}_source")
        memory_ids[memory["id"]] = record.id
    # 评测通常在 worker 停止时运行；这里通过同一 claim/complete API 补完常规生命周期任务。
    from repositories.postgres.memory import claim_memory_vector
    for memory_id in memory_ids.values():
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            with session_scope() as session:
                row = session.get(MemoryVector, memory_id)
                if row is not None and row.status == "ready" and row.embedding is not None:
                    break
            claim = claim_memory_vector(memory_id)
            if claim is not None:
                vector = embed(str(claim["text"]))
                complete_memory_vector(memory_id, content_hash=str(claim["content_hash"]), embedding=vector, model=str(claim["model"]), dimension=int(claim["dimension"]), embedding_version=str(claim["embedding_version"]))
                break
            time.sleep(0.3)
        else:
            raise TimeoutError(f"memory vector timeout: {memory_id}")
    return note_ids, memory_ids


def _metrics(rank_sets: list[list[int | None]]) -> dict[str, float]:
    """函数功能：`_metrics` 负责处理 metrics，服务于本文件职责：大规模真实检索评测。
    传参：
        rank_sets: rank sets 参数，由调用方传入，类型为 `list[list[int | None]]`。
    返回结果说明：
        返回 `dict[str, float]`，表示结构化结果、载荷或状态映射。
    """
    if not rank_sets:
        return {"cases": 0, "hit_rate": 0.0, "recall_at_1": 0.0, "recall_at_3": 0.0, "recall_at_5": 0.0, "mrr": 0.0}

    def recall_at(k: int) -> float:
        """函数功能：`recall_at` 负责处理 recall at，服务于本文件职责：大规模真实检索评测。
        传参：
            k: k 参数，由调用方传入，类型为 `int`。
        返回结果说明：
            返回 `float`，表示计算得到的数值结果。
        """
        return statistics.mean(sum(rank is not None and rank <= k for rank in ranks) / len(ranks) for ranks in rank_sets)

    first_ranks = [min((rank for rank in ranks if rank is not None), default=None) for ranks in rank_sets]
    return {
        "cases": len(rank_sets),
        "hit_rate": round(sum(bool(ranks) and all(rank is not None and rank <= 5 for rank in ranks) for ranks in rank_sets) / len(rank_sets), 4),
        "recall_at_1": round(recall_at(1), 4),
        "recall_at_3": round(recall_at(3), 4),
        "recall_at_5": round(recall_at(5), 4),
        "mrr": round(sum(1.0 / rank for rank in first_ranks if rank is not None) / len(rank_sets), 4),
    }


def _latency(results: list[dict[str, Any]]) -> dict[str, float]:
    """函数功能：`_latency` 负责处理 latency，服务于本文件职责：大规模真实检索评测。
    传参：
        results: results 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
    返回结果说明：
        返回 `dict[str, float]`，表示结构化结果、载荷或状态映射。
    """
    values = sorted(float(item["latency_ms"]) for item in results)
    if not values:
        return {"avg_latency_ms": 0.0, "p95_latency_ms": 0.0}
    return {"avg_latency_ms": round(statistics.mean(values), 2), "p95_latency_ms": round(values[min(len(values) - 1, max(0, math.ceil(len(values) * 0.95) - 1))], 2)}


def _run_retrieval(space_id: str, cases: list[dict[str, Any]], note_ids: dict[str, str], memory_ids: dict[str, str], embed: Any) -> dict[str, Any]:
    """函数功能：`_run_retrieval` 负责运行 retrieval，服务于本文件职责：大规模真实检索评测。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        cases: cases 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
        note_ids: note ids 参数，由调用方传入，类型为 `dict[str, str]`。
        memory_ids: memory ids 参数，由调用方传入，类型为 `dict[str, str]`。
        embed: embed 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    note_results: list[dict[str, Any]] = []
    memory_results: list[dict[str, Any]] = []
    note_ranks: list[list[int | None]] = []
    memory_ranks: list[list[int | None]] = []
    state_total = state_ok = polarity_total = polarity_ok = 0
    for case in cases:
        if case["kind"] not in {"note", "memory"}:
            continue
        started = time.perf_counter()
        if case["kind"] == "note":
            rows = query_agent.semantic_search(space_id, case["query"], top_k=5)
            ranked = [str(row.get("id")) for row in rows]
            expected = [note_ids[item] for item in case["expected_ids"]]
            expected_ranks = [ranked.index(item) + 1 if item in ranked else None for item in expected]
            rank = min((value for value in expected_ranks if value is not None), default=None)
            note_ranks.append(expected_ranks)
            note_results.append({"case_id": case["case_id"], "query": case["query"], "query_style": case.get("query_style"), "expected_ids": expected, "ranked_ids": ranked, "expected_ranks": expected_ranks, "rank": rank, "all_expected_hit_at_5": all(value is not None and value <= 5 for value in expected_ranks), "channels": [row.get("retrieval_channels", []) for row in rows], "latency_ms": round((time.perf_counter() - started) * 1000, 2)})
        else:
            if case.get("complex"):
                plan = build_query_plan(case["query"])
                queries = [case["query"], *plan.retrieval_queries]
                groups: list[list[dict[str, Any]]] = []
                record_map = {}
                for retrieval_query in dict.fromkeys(queries):
                    variant_rows = search_memories(space_id, retrieval_query, limit=5, mark_access=False)
                    group = []
                    for record, score in variant_rows:
                        record_map[record.id] = record
                        group.append({**record.to_dict(), "score": score})
                    if group:
                        groups.append(group)
                fused = query_agent._fuse_memory_results(groups, limit=5)
                records = [record_map[str(item["id"])] for item in fused if str(item["id"]) in record_map]
            else:
                rows = search_memories(space_id, case["query"], limit=5, mark_access=False)
                records = [record for record, _score in rows]
            ranked = [record.id for record in records]
            expected = [memory_ids[item] for item in case["expected_ids"]]
            expected_ranks = [ranked.index(item) + 1 if item in ranked else None for item in expected]
            rank = min((value for value in expected_ranks if value is not None), default=None)
            memory_ranks.append(expected_ranks)
            if case.get("expected_status"):
                state_total += 1
                state_ok += int(bool(records) and ranked[0] in expected and records[0].task_status == case["expected_status"])
            if case.get("expected_polarity"):
                polarity_total += 1
                polarity_ok += int(bool(records) and ranked[0] in expected and records[0].polarity == case["expected_polarity"])
            memory_results.append({"case_id": case["case_id"], "query": case["query"], "memory_type": case.get("memory_type"), "query_style": case.get("query_style"), "expected_ids": expected, "ranked_ids": ranked, "expected_ranks": expected_ranks, "rank": rank, "all_expected_hit_at_5": all(value is not None and value <= 5 for value in expected_ranks), "top_content": records[0].content if records else None, "top_status": records[0].task_status if records else None, "top_polarity": records[0].polarity if records else None, "latency_ms": round((time.perf_counter() - started) * 1000, 2)})
    note_metrics = _metrics(note_ranks) | _latency(note_results)
    memory_metrics = _metrics(memory_ranks) | _latency(memory_results) | {"state_accuracy": round(state_ok / state_total, 4) if state_total else 0.0, "state_cases": state_total, "polarity_accuracy": round(polarity_ok / polarity_total, 4) if polarity_total else 0.0, "polarity_cases": polarity_total}
    memory_by_type = {}
    for memory_type in sorted({str(item.get("memory_type")) for item in memory_results}):
        group = [item for item in memory_results if str(item.get("memory_type")) == memory_type]
        memory_by_type[memory_type] = _metrics([item["expected_ranks"] for item in group]) | _latency(group)
    return {"note": {"metrics": note_metrics, "results": note_results}, "memory": {"metrics": memory_metrics, "by_type": memory_by_type, "results": memory_results}}


def _run_policy(cases: list[dict[str, Any]]) -> dict[str, Any]:
    """函数功能：`_run_policy` 负责运行 policy，服务于本文件职责：大规模真实检索评测。
    传参：
        cases: cases 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    results = []
    for case in cases:
        if case["kind"] == "safety":
            actual = mentions_sensitive_topic(case["query"])
            results.append({"case_id": case["case_id"], "passed": actual is case["expected"], "actual": actual})
        elif case["kind"] == "planner":
            actual = build_query_plan(case["query"]).complexity
            results.append({"case_id": case["case_id"], "passed": actual == case["expected"], "actual": actual})
    return {"metrics": {"cases": len(results), "accuracy": round(sum(bool(item["passed"]) for item in results) / len(results), 4) if results else 0.0}, "results": results}


def _run_llm(space_id: str, cases: list[dict[str, Any]]) -> dict[str, Any]:
    """函数功能：`_run_llm` 负责运行 llm，服务于本文件职责：大规模真实检索评测。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        cases: cases 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    original = query_agent._complete_json_with_hooks
    calls = 0

    def counted(*args: Any, **kwargs: Any) -> Any:
        """函数功能：`counted` 负责处理 counted，服务于本文件职责：大规模真实检索评测。
        传参：
            *args: args 参数，由调用方传入，类型为 `Any`。
            **kwargs: kwargs 参数，由调用方传入，类型为 `Any`。
        返回结果说明：
            返回 `Any` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    query_agent._complete_json_with_hooks = counted
    results = []
    try:
        for case in cases:
            started = time.perf_counter()
            before = calls
            error = None
            try:
                answer = query_agent.answer_question(space_id, case["question"], max_steps=4)
            except Exception as exc:
                answer, error = "", f"{type(exc).__name__}: {exc}"
            groups = [any(term in answer for term in group) for group in case["must_include"]]
            results.append({"case_id": case["case_id"], "question": case["question"], "passed": bool(not error and all(groups)), "groups": groups, "llm_calls": calls - before, "latency_ms": round((time.perf_counter() - started) * 1000, 2), "answer": answer, "error": error})
    finally:
        query_agent._complete_json_with_hooks = original
    latencies = [item["latency_ms"] for item in results]
    return {"metrics": {"cases": len(results), "answer_accuracy": round(sum(bool(item["passed"]) for item in results) / len(results), 4) if results else 0.0, "llm_calls": calls, "avg_latency_ms": round(statistics.mean(latencies), 2) if latencies else 0.0, "p95_latency_ms": round(sorted(latencies)[min(len(latencies) - 1, max(0, math.ceil(len(latencies) * 0.95) - 1))], 2) if latencies else 0.0}, "results": results}


def _cleanup(space_id: str) -> None:
    """函数功能：`_cleanup` 负责清理，服务于本文件职责：大规模真实检索评测。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    with session_scope() as session:
        session.execute(delete(Space).where(Space.id == space_id))


def run(*, keep: bool = False) -> dict[str, Any]:
    """函数功能：`run` 负责运行，服务于本文件职责：大规模真实检索评测。
    传参：
        keep: keep 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    dataset = _generate_dataset()
    write_json(DATASET_OUTPUT, dataset)
    space_id = f"eval_live_1000_{datetime.now().strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}"
    cache: dict[str, list[float]] = {}
    original_embed = llm_client.embed_text

    def cached_embed(text: str) -> list[float]:
        """函数功能：`cached_embed` 负责生成向量 cached，服务于本文件职责：大规模真实检索评测。
        传参：
            text: 输入文本内容，类型为 `str`。
        返回结果说明：
            返回 `list[float]`，表示按条件筛选、构造或查询得到的列表。
        """
        key = str(text)
        if key not in cache:
            cache[key] = original_embed(key)
        return cache[key]

    llm_client.embed_text = cached_embed
    query_agent.embed_text = cached_embed
    try:
        note_ids, memory_ids = _insert_corpus(space_id, dataset, cached_embed)
        retrieval = _run_retrieval(space_id, dataset["cases"], note_ids, memory_ids, cached_embed)
        policy = _run_policy(dataset["cases"])
        llm = _run_llm(space_id, dataset["llm_cases"])
        report = {"version": dataset["version"], "space_id": space_id, "total_cases": len(dataset["cases"]), "note_cases": retrieval["note"]["metrics"]["cases"], "memory_cases": retrieval["memory"]["metrics"]["cases"], "policy_cases": policy["metrics"]["cases"], "llm_cases": llm["metrics"]["cases"], "unique_embedding_calls": len(cache), "real_embedding": True, "real_llm": True, "kept": keep, **retrieval, "policy": policy, "llm": llm}
    finally:
        llm_client.embed_text = original_embed
        query_agent.embed_text = original_embed
        if not keep:
            _cleanup(space_id)
    return report


def _print(report: dict[str, Any]) -> None:
    """函数功能：`_print` 负责处理 print，服务于本文件职责：大规模真实检索评测。
    传参：
        report: report 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    print(json.dumps({key: report[key] for key in ("version", "space_id", "total_cases", "unique_embedding_calls", "real_embedding", "real_llm", "kept")}, ensure_ascii=False))
    print("section\tcases\thit/accuracy\tR@1\tR@3\tR@5\tMRR\tstate_acc\tpolarity_acc\tanswer_acc\tavg_ms\tp95_ms")
    for name in ("note", "memory"):
        m = report[name]["metrics"]
        print(f"{name}\t{int(m['cases'])}\t{m.get('hit_rate', 0):.4f}\t{m.get('recall_at_1', 0):.4f}\t{m.get('recall_at_3', 0):.4f}\t{m.get('recall_at_5', 0):.4f}\t{m.get('mrr', 0):.4f}\t{m.get('state_accuracy', 0):.4f}\t{m.get('polarity_accuracy', 0):.4f}\t-\t{m.get('avg_latency_ms', 0):.2f}\t{m.get('p95_latency_ms', 0):.2f}")
    print(f"policy\t{int(report['policy']['metrics']['cases'])}\t{report['policy']['metrics']['accuracy']:.4f}\t-\t-\t-\t-\t-\t-\t-\t-\t-")
    m = report["llm"]["metrics"]
    print(f"llm\t{int(m['cases'])}\t-\t-\t-\t-\t-\t-\t-\t{m['answer_accuracy']:.4f}\t{m['avg_latency_ms']:.2f}\t{m['p95_latency_ms']:.2f}")


def main() -> None:
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：大规模真实检索评测。
    传参：
        无。
    返回结果说明：
        无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
    """
    parser = argparse.ArgumentParser()
    parser.add_argument("--keep", action="store_true")
    parser.add_argument("--output", default=None)
    args = parser.parse_args()
    report = run(keep=args.keep)
    output = Path(args.output) if args.output else ROOT / "eval" / "results" / f"live_retrieval_1000_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json"
    write_json(output, report)
    _print(report)
    print(f"dataset={DATASET_OUTPUT}")
    print(f"report={output}")


if __name__ == "__main__":
    main()
