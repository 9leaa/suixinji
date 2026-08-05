"""文件作用：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。

项目关系：本文件依赖 `agent.query_planner`、`core`、`core.config`、`core.sensitive` 等 10 个模块；被 暂无静态导入方或仅作为入口脚本执行。
"""



from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import statistics
import sys
import threading
import time
import zipfile
from collections import Counter, defaultdict
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, is_dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_ZIP = ROOT / "suixinji_memory_benchmark_massive_v2_package.zip"
RESULTS_DIR = ROOT / "eval" / "results"


def _bootstrap_env(argv: list[str]) -> argparse.Namespace:
    """函数功能：`_bootstrap_env` 负责初始化运行环境 env，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        argv: argv 参数，由调用方传入，类型为 `list[str]`。
    返回结果说明：
        返回 `argparse.Namespace` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument(
        "--extractor-mode",
        choices=("rules", "hybrid", "llm"),
        default=os.getenv("SUIXINJI_MEMORY_EXTRACTOR_MODE", "llm").strip().lower() or "llm",
    )
    parser.add_argument("--llm-timeout-seconds", type=int, default=30)
    parser.add_argument("--llm-timeout-retries", type=int, default=1)
    args, _unknown = parser.parse_known_args(argv)

    safe_env = {
        "STORAGE_BACKEND": "local",
        "TASK_QUEUE_BACKEND": "local",
        "COORDINATION_BACKEND": "local",
        "SUIXINJI_AGENT_HOOKS_ENABLED": "false",
        "SUIXINJI_MEMORY_EXTRACTOR_MODE": args.extractor_mode,
        "SUIXINJI_MEMORY_RETRIEVAL_MODE": "hybrid",
        "SUIXINJI_MEMORY_CLAUSE_EXTRACTION_ENABLED": "true",
        "SUIXINJI_MEMORY_EXTRACTOR_SCHEMA_V3_ENABLED": "true",
        "SUIXINJI_MEMORY_CANONICAL_KEY_V3_ENABLED": "true",
        "SUIXINJI_MEMORY_RELATION_GUARD_V3_ENABLED": "true",
        "SUIXINJI_QUERY_INTENT_MODEL_ENABLED": "false",
        "SUIXINJI_QUERY_ROUTER_LLM_ON_UNCERTAIN": "false",
        "SUIXINJI_QUERY_ROUTER_LLM_ON_LOW_RECALL": "false",
        "SUIXINJI_OBSERVABILITY_DISABLED": "1",
        "SUIXINJI_MEMORY_EXTRACTION_LLM_TIMEOUT_SECONDS": str(max(1, args.llm_timeout_seconds)),
        "SUIXINJI_MEMORY_EXTRACTION_LLM_MAX_RETRIES": str(min(1, max(0, args.llm_timeout_retries))),
    }
    for key, value in safe_env.items():
        os.environ[key] = value
    return args


BOOTSTRAP_ARGS = _bootstrap_env(sys.argv[1:])

if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from agent.query_planner import build_query_plan
from core import llm_client, settings
from core.config import get_chat_config
from core.sensitive import contains_sensitive_data, redact_sensitive_text
from memory import extractor as memory_extractor
from memory import repository as memory_repository
from memory.candidate_validator import validate_candidates
from memory.extractor import extract_candidates
from memory.models import MemoryCandidate, normalize_content
from memory.repository import insert_memory, list_memories, search_memories, soft_delete_memory
from memory.service import process_note_memory


EXTRACTION_SUITES = {
    "extraction_atomic",
    "extraction_multiclause",
    "negative_noise",
    "sensitive_block",
    # 多样化 V3 原生套件。
    "atomic_extraction",
    "multiclause_scope",
    "negative_hypothetical_third_party",
}
SERVICE_SUITES = {
    "relation_same_add_source",
    "task_lifecycle",
    "preference_supersede",
    "semantic_pending_review",
    "deletion_idempotency",
    "cross_user_isolation",
    # 多样化 V3 原生套件。
    "same_add_source",
    "preference_semantic_change",
    "read_after_write_provisional",
    "session_coreference",
}
RETRIEVAL_SUITES = {"retrieval_single", "retrieval_long_context", "retrieval_single_hop"}
ROUTING_SUITES = {
    "query_routing",
    "complex_planner",
    "adversarial_query_routing",
    "complex_retrieval_planning",
}
ALL_SUITES = sorted(EXTRACTION_SUITES | SERVICE_SUITES | RETRIEVAL_SUITES | ROUTING_SUITES)


def _json_default(value: Any) -> Any:
    """函数功能：`_json_default` 负责处理 JSON 数据 default，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        value: 待转换、校验或计算的值，类型为 `Any`。
    返回结果说明：
        返回 `Any` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    if is_dataclass(value):
        return asdict(value)
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, set):
        return sorted(value)
    return str(value)


def _now_tag() -> str:
    """函数功能：`_now_tag` 负责获取当前时间 tag，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        无。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def _utc_now() -> str:
    """函数功能：`_utc_now` 负责处理 UTC 时间 now，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        无。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return datetime.now(timezone.utc).isoformat()


def _rate(num: float, den: float) -> float:
    """函数功能：`_rate` 负责计算比率，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        num: num 参数，由调用方传入，类型为 `float`。
        den: den 参数，由调用方传入，类型为 `float`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
    """
    return round(float(num) / float(den), 6) if den else 0.0


def _pct(value: float) -> str:
    """函数功能：`_pct` 负责格式化百分比，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        value: 待转换、校验或计算的值，类型为 `float`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return f"{100 * value:.1f}%"


def _pctl(values: list[float], pct: float) -> float:
    """函数功能：`_pctl` 负责计算百分位数，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        values: values 参数，由调用方传入，类型为 `list[float]`。
        pct: pct 参数，由调用方传入，类型为 `float`。
    返回结果说明：
        返回 `float`，表示计算得到的数值结果。
    """
    if not values:
        return 0.0
    ordered = sorted(values)
    position = (len(ordered) - 1) * pct
    low = int(position)
    high = min(low + 1, len(ordered) - 1)
    fraction = position - low
    return round(ordered[low] + (ordered[high] - ordered[low]) * fraction, 4)


def _as_text(value: Any) -> str:
    """函数功能：`_as_text` 负责处理 as text，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        value: 待转换、校验或计算的值，类型为 `Any`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, default=_json_default)


def _norm(value: Any) -> str:
    """函数功能：`_norm` 负责处理 norm，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        value: 待转换、校验或计算的值，类型为 `Any`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return normalize_content(_as_text(value))


def _space_for_case(case: dict[str, Any], raw_space_id: str | None = None) -> str:
    """函数功能：`_space_for_case` 负责处理 space for case，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
        raw_space_id: raw space id 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    raw = raw_space_id or str(case.get("space_id") or "default")
    return f"massive:{case['case_id']}:{raw}"


def _note_id_for_case(case: dict[str, Any], raw_note_id: str | None) -> str:
    """函数功能：`_note_id_for_case` 负责处理 note id for case，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
        raw_note_id: raw note id 参数，由调用方传入，类型为 `str | None`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return f"{case['case_id']}:{raw_note_id or 'note'}"


def _safe_error(exc: Exception) -> str:
    """函数功能：`_safe_error` 负责处理 safe error，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        exc: 当前捕获的异常对象，类型为 `Exception`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return redact_sensitive_text(f"{type(exc).__name__}: {exc}")[:500]


def _candidate_blob(candidate: MemoryCandidate) -> str:
    """函数功能：`_candidate_blob` 负责处理 candidate blob，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return " ".join(
        _as_text(item)
        for item in (
            candidate.content,
            candidate.subject,
            candidate.predicate,
            candidate.object_value,
            candidate.memory_key,
            candidate.evidence_span,
            candidate.entities,
            candidate.scope,
        )
        if item
    )


def _record_blob(memory: Any) -> str:
    """函数功能：`_record_blob` 负责记录 blob，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        memory: memory 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `str`，通常是格式化后的文本、标识或路径。
    """
    return " ".join(
        _as_text(item)
        for item in (
            getattr(memory, "content", ""),
            getattr(memory, "subject", ""),
            getattr(memory, "predicate", ""),
            getattr(memory, "object_value", ""),
            getattr(memory, "memory_key", ""),
            getattr(memory, "scope", {}),
        )
        if item
    )


def _expected_terms(expected: dict[str, Any]) -> list[str]:
    """函数功能：`_expected_terms` 负责处理 expected terms，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        expected: expected 参数，由调用方传入，类型为 `dict[str, Any]`。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
    terms = []
    for key in ("entity", "object", "object_value", "subject"):
        value = str(expected.get(key) or "").strip()
        if value and value != "用户":
            terms.append(value)
    content = str(expected.get("content") or "").strip()
    if content:
        for token in ("牛奶", "咖啡", "海鲜", "苹果", "香蕉", "香菜", "奶茶", "绿茶", "酸奶"):
            if token in content:
                terms.append(token)
        for token in ("Python", "Agent", "RAG", "Redis", "PostgreSQL", "Docker", "React", "FastAPI", "Go", "Java"):
            if token in content:
                terms.append(token)
    canonical_key = str(expected.get("canonical_key") or "")
    if canonical_key:
        parts = [part for part in canonical_key.split(":") if part and part not in {"current", "temporary", "set", "prefer", "avoid"}]
        terms.extend(parts[-2:])
    return list(dict.fromkeys(term for term in terms if term))


def _operation_matches(expected: dict[str, Any], candidate: MemoryCandidate) -> bool:
    """函数功能：`_operation_matches` 负责处理 operation matches，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        expected: expected 参数，由调用方传入，类型为 `dict[str, Any]`。
        candidate: candidate 参数，由调用方传入，类型为 `MemoryCandidate`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    operation = str(expected.get("operation") or "").strip().lower()
    if not operation:
        return True
    if operation in {"prefer", "like"}:
        return candidate.polarity in {None, "positive"} and not any(token in candidate.content for token in ("不喜欢", "讨厌", "不爱"))
    if operation in {"avoid", "dislike"}:
        return candidate.polarity == "negative" or any(token in candidate.content for token in ("不喜欢", "讨厌", "不爱", "不碰"))
    if operation in {"created", "create", "todo"}:
        return candidate.task_status in {None, "todo"}
    if operation in {"completed", "done"}:
        return candidate.task_status == "done" or any(token in candidate.content for token in ("完成", "做完"))
    if operation in {"cancelled", "canceled"}:
        return candidate.task_status == "done" and candidate.scope.get("closure_reason") == "cancelled"
    return True


def _match_expected_candidates(
    expected: list[dict[str, Any]],
    actual: list[MemoryCandidate],
) -> tuple[int, int]:
    """函数功能：`_match_expected_candidates` 负责处理 match expected candidates，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        expected: expected 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
        actual: actual 参数，由调用方传入，类型为 `list[MemoryCandidate]`。
    返回结果说明：
        返回 `tuple[int, int]`，表示由多个相关值组成的结果。
    """
    used: set[int] = set()
    operation_hits = 0
    matches = 0
    for exp in expected:
        exp_type = str(exp.get("memory_type") or "")
        terms = [_norm(term) for term in _expected_terms(exp)]
        best_index = None
        for index, candidate in enumerate(actual):
            if index in used or candidate.memory_type != exp_type:
                continue
            blob = _norm(_candidate_blob(candidate))
            if terms and not any(term and term in blob for term in terms):
                continue
            best_index = index
            break
        if best_index is None:
            for index, candidate in enumerate(actual):
                if index in used or candidate.memory_type != exp_type:
                    continue
                best_index = index
                break
        if best_index is None:
            continue
        used.add(best_index)
        matches += 1
        if _operation_matches(exp, actual[best_index]):
            operation_hits += 1
    return matches, operation_hits


class LLMEventTracker:
    """类功能：`LLMEventTracker` 封装与“评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(self, path: Path) -> None:
        """函数功能：`LLMEventTracker.__init__` 在类 `LLMEventTracker` 中负责初始化实例状态，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            path: 文件系统路径，类型为 `Path`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.path = path
        self.counters: Counter[str] = Counter()
        self.tokens: Counter[str] = Counter()
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def log_event(self, action: str, **kwargs: Any) -> None:
        """函数功能：`LLMEventTracker.log_event` 在类 `LLMEventTracker` 中负责记录日志 event，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            action: action 参数，由调用方传入，类型为 `str`。
            **kwargs: kwargs 参数，由调用方传入，类型为 `Any`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        status = str(kwargs.get("status") or "")
        extra = kwargs.get("extra") if isinstance(kwargs.get("extra"), dict) else {}
        item = {
            "ts": _utc_now(),
            "action": action,
            "status": status,
            "level": kwargs.get("level"),
            "record_id": kwargs.get("record_id"),
            "duration_ms": kwargs.get("duration_ms"),
            "error": kwargs.get("error"),
            "extra": extra,
        }
        with self._lock:
            if action == "llm.complete_json":
                self.counters["llm_events"] += 1
                self.counters[f"llm_status_{status}"] += 1
                self.tokens["prompt_tokens"] += int(extra.get("prompt_tokens") or 0)
                self.tokens["completion_tokens"] += int(extra.get("completion_tokens") or 0)
                self.tokens["total_tokens"] += int(extra.get("total_tokens") or 0)
            elif action == "memory.extractor.llm_failed":
                self.counters["memory_extractor_llm_failed"] += 1
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(item, ensure_ascii=False, default=_json_default) + "\n")

    def summary(self) -> dict[str, Any]:
        """函数功能：`LLMEventTracker.summary` 在类 `LLMEventTracker` 中负责处理 summary，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            无。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        with self._lock:
            return {
                **dict(self.counters),
                "tokens": dict(self.tokens),
            }


class Metrics:
    """类功能：`Metrics` 封装与“评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(self, *, max_failures: int) -> None:
        """函数功能：`Metrics.__init__` 在类 `Metrics` 中负责初始化实例状态，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            max_failures: max failures 参数，由调用方传入，类型为 `int`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.max_failures = max_failures
        self._lock = threading.RLock()
        self.suites: dict[str, dict[str, Any]] = defaultdict(
            lambda: {
                "cases": 0,
                "passed": 0,
                "failed": 0,
                "skipped": 0,
                "errors": 0,
                "rates": defaultdict(lambda: [0.0, 0.0]),
                "counters": Counter(),
                "values": defaultdict(list),
                "failures": [],
            }
        )

    def record_case(
        self,
        suite: str,
        *,
        passed: bool,
        latency_ms: float,
        skipped: bool = False,
        error: str | None = None,
        detail: dict[str, Any] | None = None,
    ) -> None:
        """函数功能：`Metrics.record_case` 在类 `Metrics` 中负责记录 case，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            suite: suite 参数，由调用方传入，类型为 `str`。
            passed: passed 参数，由调用方传入，类型为 `bool`。
            latency_ms: latency ms 参数，由调用方传入，类型为 `float`。
            skipped: skipped 参数，由调用方传入，类型为 `bool`，默认值为 `False`。
            error: 当前捕获的异常对象，类型为 `str | None`，默认值为 `None`。
            detail: detail 参数，由调用方传入，类型为 `dict[str, Any] | None`，默认值为 `None`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        with self._lock:
            item = self.suites[suite]
            item["cases"] += 1
            item["values"]["case_latency_ms"].append(float(latency_ms))
            if skipped:
                item["skipped"] += 1
                return
            if error:
                item["errors"] += 1
            if passed:
                item["passed"] += 1
            else:
                item["failed"] += 1
                if len(item["failures"]) < self.max_failures:
                    item["failures"].append(detail or {"error": error})

    def add_rate(self, suite: str, name: str, numerator: float, denominator: float = 1.0) -> None:
        """函数功能：`Metrics.add_rate` 在类 `Metrics` 中负责计算比率 add，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            suite: suite 参数，由调用方传入，类型为 `str`。
            name: name 参数，由调用方传入，类型为 `str`。
            numerator: numerator 参数，由调用方传入，类型为 `float`。
            denominator: denominator 参数，由调用方传入，类型为 `float`，默认值为 `1.0`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        if denominator <= 0:
            return
        with self._lock:
            rate = self.suites[suite]["rates"][name]
            rate[0] += float(numerator)
            rate[1] += float(denominator)

    def add_counter(self, suite: str, name: str, value: int = 1) -> None:
        """函数功能：`Metrics.add_counter` 在类 `Metrics` 中负责处理 add counter，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            suite: suite 参数，由调用方传入，类型为 `str`。
            name: name 参数，由调用方传入，类型为 `str`。
            value: 待转换、校验或计算的值，类型为 `int`，默认值为 `1`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        with self._lock:
            self.suites[suite]["counters"][name] += int(value)

    def add_value(self, suite: str, name: str, value: float) -> None:
        """函数功能：`Metrics.add_value` 在类 `Metrics` 中负责处理 add value，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            suite: suite 参数，由调用方传入，类型为 `str`。
            name: name 参数，由调用方传入，类型为 `str`。
            value: 待转换、校验或计算的值，类型为 `float`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        with self._lock:
            self.suites[suite]["values"][name].append(float(value))

    def finalize(self) -> dict[str, Any]:
        """函数功能：`Metrics.finalize` 在类 `Metrics` 中负责处理 finalize，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            无。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        with self._lock:
            snapshot = {suite: item for suite, item in sorted(self.suites.items())}
        suites: dict[str, Any] = {}
        total_cases = 0
        total_passed = 0
        total_failed = 0
        total_skipped = 0
        total_errors = 0
        for suite, item in snapshot.items():
            cases = item["cases"]
            total_cases += cases
            total_passed += item["passed"]
            total_failed += item["failed"]
            total_skipped += item["skipped"]
            total_errors += item["errors"]
            rates = {key: _rate(value[0], value[1]) for key, value in sorted(item["rates"].items())}
            values = {
                key: {
                    "count": len(vals),
                    "avg": round(statistics.mean(vals), 4) if vals else 0.0,
                    "p50": round(statistics.median(vals), 4) if vals else 0.0,
                    "p95": _pctl(vals, 0.95),
                    "p99": _pctl(vals, 0.99),
                    "max": round(max(vals), 4) if vals else 0.0,
                }
                for key, vals in sorted(item["values"].items())
            }
            counters = dict(item["counters"])
            self._add_derived_routing(counters, rates)
            suites[suite] = {
                "cases": cases,
                "passed": item["passed"],
                "failed": item["failed"],
                "skipped": item["skipped"],
                "errors": item["errors"],
                "pass_rate": _rate(item["passed"], cases - item["skipped"]),
                "rates": rates,
                "counters": counters,
                "values": values,
                "failures": item["failures"],
            }
        return {
            "global": {
                "cases": total_cases,
                "passed": total_passed,
                "failed": total_failed,
                "skipped": total_skipped,
                "errors": total_errors,
                "pass_rate": _rate(total_passed, total_cases - total_skipped),
            },
            "suites": suites,
        }

    @staticmethod
    def _add_derived_routing(counters: dict[str, Any], rates: dict[str, Any]) -> None:
        """函数功能：`Metrics._add_derived_routing` 在类 `Metrics` 中负责处理 add derived routing，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            counters: counters 参数，由调用方传入，类型为 `dict[str, Any]`。
            rates: rates 参数，由调用方传入，类型为 `dict[str, Any]`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        tp = int(counters.get("routing_tp", 0))
        fp = int(counters.get("routing_fp", 0))
        fn = int(counters.get("routing_fn", 0))
        tn = int(counters.get("routing_tn", 0))
        if tp + fp + fn + tn <= 0:
            return
        precision = _rate(tp, tp + fp)
        recall = _rate(tp, tp + fn)
        rates["complex_precision"] = precision
        rates["complex_recall"] = recall
        rates["complex_f1"] = _rate(2 * precision * recall, precision + recall)
        rates["predicted_complex_rate"] = _rate(tp + fp, tp + fp + fn + tn)
        rates["gold_complex_rate"] = _rate(tp + fn, tp + fp + fn + tn)
        rates["false_complex_rate"] = _rate(fp, fp + tn)
        rates["false_simple_rate"] = _rate(fn, tp + fn)


class MassiveBenchmarkRunner:
    """类功能：`MassiveBenchmarkRunner` 封装与“评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总”相关的数据结构、状态或行为。
    传参：类构造参数以 `__init__`、dataclass 字段或父类约定为准。
    返回结果说明：实例方法按各自 docstring 返回；类本身用于创建可复用对象或类型约束。
    """
    def __init__(self, args: argparse.Namespace) -> None:
        """函数功能：`MassiveBenchmarkRunner.__init__` 在类 `MassiveBenchmarkRunner` 中负责初始化实例状态，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            args: args 参数，由调用方传入，类型为 `argparse.Namespace`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.args = args
        self.run_tag = _now_tag()
        self.run_dir = Path(args.output_dir or RESULTS_DIR / f"massive_memory_benchmark_{self.run_tag}")
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.rows_path = self.run_dir / "rows.jsonl"
        self.summary_json_path = self.run_dir / "summary.json"
        self.summary_md_path = self.run_dir / "summary.md"
        self.db_path = Path(args.db_path or self.run_dir / "memory_benchmark.sqlite")
        self.metrics = Metrics(max_failures=args.max_failures)
        self.llm_tracker = LLMEventTracker(self.run_dir / "llm_events.jsonl")
        self.suite_seen: Counter[str] = Counter()
        self.filtered_cases = 0
        self.submitted_cases = 0
        self.processed_cases = 0
        self.started_llm_notes = 0
        self.started_at = time.perf_counter()
        self.metadata: dict[str, Any] = {}
        self._state_lock = threading.RLock()
        memory_repository.DB_PATH = self.db_path
        llm_client.log_event = self.llm_tracker.log_event
        memory_extractor.log_event = self.llm_tracker.log_event

    def run(self) -> dict[str, Any]:
        """函数功能：`MassiveBenchmarkRunner.run` 在类 `MassiveBenchmarkRunner` 中负责运行，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            无。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        if self.args.verify_sha256:
            self._verify_zip_sha256()
        rows_fh = self.rows_path.open("w", encoding="utf-8", buffering=1) if self.args.write_rows else None
        try:
            if self.args.workers <= 1:
                self._run_sequential(rows_fh)
            else:
                self._run_parallel(rows_fh)
        finally:
            if rows_fh is not None:
                rows_fh.close()

        summary = self._build_summary()
        self.summary_json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=_json_default), encoding="utf-8")
        self.summary_md_path.write_text(self._render_markdown(summary), encoding="utf-8")
        return summary

    def _run_sequential(self, rows_fh: Any | None) -> None:
        """函数功能：`MassiveBenchmarkRunner._run_sequential` 在类 `MassiveBenchmarkRunner` 中负责运行 sequential，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            rows_fh: rows fh 参数，由调用方传入，类型为 `Any | None`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        for case in self._iter_cases():
            if not self._accept_case(case):
                continue
            outcome = self._run_case(case)
            self._record_outcome(outcome, rows_fh)
            if self.args.fail_fast and not outcome.get("passed", False) and not outcome.get("skipped", False):
                break

    def _run_parallel(self, rows_fh: Any | None) -> None:
        """函数功能：`MassiveBenchmarkRunner._run_parallel` 在类 `MassiveBenchmarkRunner` 中负责运行 parallel，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            rows_fh: rows fh 参数，由调用方传入，类型为 `Any | None`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        pending: set[Future[dict[str, Any]]] = set()
        max_pending = max(1, int(self.args.workers) * 2)
        stop_submitting = False
        with ThreadPoolExecutor(max_workers=max(1, int(self.args.workers))) as pool:
            for case in self._iter_cases():
                if stop_submitting:
                    break
                if not self._accept_case(case):
                    continue
                pending.add(pool.submit(self._run_case, case))
                if len(pending) < max_pending:
                    continue
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                if self._handle_done_futures(done, rows_fh):
                    stop_submitting = True
                    break
            while pending:
                done, pending = wait(pending, return_when=FIRST_COMPLETED)
                if self._handle_done_futures(done, rows_fh) and self.args.fail_fast:
                    for future in pending:
                        future.cancel()
                    break

    def _handle_done_futures(self, futures: set[Future[dict[str, Any]]], rows_fh: Any | None) -> bool:
        """函数功能：`MassiveBenchmarkRunner._handle_done_futures` 在类 `MassiveBenchmarkRunner` 中负责处理 done futures，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            futures: futures 参数，由调用方传入，类型为 `set[Future[dict[str, Any]]]`。
            rows_fh: rows fh 参数，由调用方传入，类型为 `Any | None`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        should_stop = False
        for future in futures:
            try:
                outcome = future.result()
            except Exception as exc:
                outcome = {"case_id": "future_error", "suite": "unknown", "passed": False, "error": _safe_error(exc)}
            self._record_outcome(outcome, rows_fh)
            if self.args.fail_fast and not outcome.get("passed", False) and not outcome.get("skipped", False):
                should_stop = True
        return should_stop

    def _accept_case(self, case: dict[str, Any]) -> bool:
        """函数功能：`MassiveBenchmarkRunner._accept_case` 在类 `MassiveBenchmarkRunner` 中负责处理 accept case，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        if not self._should_run_case(case):
            return False
        if self.args.limit and self.submitted_cases >= self.args.limit:
            return False
        self.filtered_cases += 1
        self.suite_seen[case["suite"]] += 1
        self.submitted_cases += 1
        return True

    def _record_outcome(self, outcome: dict[str, Any], rows_fh: Any | None) -> None:
        """函数功能：`MassiveBenchmarkRunner._record_outcome` 在类 `MassiveBenchmarkRunner` 中负责记录 outcome，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            outcome: outcome 参数，由调用方传入，类型为 `dict[str, Any]`。
            rows_fh: rows fh 参数，由调用方传入，类型为 `Any | None`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        self.processed_cases += 1
        if rows_fh is not None:
            rows_fh.write(json.dumps(outcome, ensure_ascii=False, default=_json_default) + "\n")
            rows_fh.flush()
        if self.args.progress_every and self.processed_cases % self.args.progress_every == 0:
            self._print_progress()

    def _verify_zip_sha256(self) -> None:
        """函数功能：`MassiveBenchmarkRunner._verify_zip_sha256` 在类 `MassiveBenchmarkRunner` 中负责处理 verify zip sha256，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        manifest = self._read_manifest()
        expected = str(manifest.get("sha256") or "")
        if not expected:
            return
        main_file = str(manifest.get("main_file") or "")
        digest = hashlib.sha256()
        with zipfile.ZipFile(self.args.zip_path) as zf:
            with zf.open(main_file) as fh:
                for chunk in iter(lambda: fh.read(1024 * 1024), b""):
                    digest.update(chunk)
        actual = digest.hexdigest()
        if actual != expected:
            raise RuntimeError(f"dataset sha256 mismatch: expected={expected} actual={actual}")

    def _read_manifest(self) -> dict[str, Any]:
        """函数功能：`MassiveBenchmarkRunner._read_manifest` 在类 `MassiveBenchmarkRunner` 中负责读取 manifest，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            无。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        with zipfile.ZipFile(self.args.zip_path) as zf:
            manifest_names = [
                name
                for name in zf.namelist()
                if name.endswith("_manifest.json") and not name.startswith("__MACOSX/")
            ]
            if len(manifest_names) != 1:
                raise RuntimeError(
                    f"expected exactly one benchmark manifest, found: {manifest_names}"
                )
            data = zf.read(manifest_names[0])
        manifest = json.loads(data.decode("utf-8"))
        self.metadata = manifest
        return manifest

    def _iter_cases(self) -> Iterator[dict[str, Any]]:
        """函数功能：`MassiveBenchmarkRunner._iter_cases` 在类 `MassiveBenchmarkRunner` 中负责处理 iter cases，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            无。
        返回结果说明：
            返回 `Iterator[dict[str, Any]]` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        manifest = self.metadata or self._read_manifest()
        main_file = str(manifest.get("main_file") or "suixinji_memory_benchmark_massive_v2_210k.jsonl.gz")
        with zipfile.ZipFile(self.args.zip_path) as zf:
            with zf.open(main_file) as raw:
                opener = gzip.open(raw, "rt", encoding="utf-8") if main_file.endswith(".gz") else io.TextIOWrapper(raw, encoding="utf-8")
                with opener as fh:
                    for line_no, line in enumerate(fh, 1):
                        obj = json.loads(line)
                        if line_no == 1 and obj.get("record_type") == "metadata":
                            continue
                        yield obj

    def _should_run_case(self, case: dict[str, Any]) -> bool:
        """函数功能：`MassiveBenchmarkRunner._should_run_case` 在类 `MassiveBenchmarkRunner` 中负责运行 case，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        suite = str(case.get("suite") or "")
        if self.args.suite and suite not in self.args.suite:
            return False
        if self.args.split and str(case.get("split") or "") not in self.args.split:
            return False
        if self.args.limit_per_suite and self.suite_seen[suite] >= self.args.limit_per_suite:
            return False
        return True

    def _run_case(self, case: dict[str, Any]) -> dict[str, Any]:
        """函数功能：`MassiveBenchmarkRunner._run_case` 在类 `MassiveBenchmarkRunner` 中负责运行 case，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        suite = str(case.get("suite") or "")
        case_id = str(case.get("case_id") or "")
        started = time.perf_counter()
        try:
            if suite in EXTRACTION_SUITES:
                detail = self._evaluate_extraction_case(case)
            elif suite in SERVICE_SUITES:
                detail = self._evaluate_service_case(case)
            elif suite in RETRIEVAL_SUITES:
                detail = self._evaluate_retrieval_case(case)
            elif suite in ROUTING_SUITES:
                detail = self._evaluate_routing_case(case)
            else:
                detail = {"passed": False, "error": f"unsupported suite: {suite}"}
            latency_ms = (time.perf_counter() - started) * 1000
            self.metrics.record_case(
                suite,
                passed=bool(detail.get("passed")),
                latency_ms=latency_ms,
                skipped=bool(detail.get("skipped")),
                error=detail.get("error"),
                detail={"case_id": case_id, **{k: v for k, v in detail.items() if k != "passed"}},
            )
            return {"case_id": case_id, "suite": suite, "latency_ms": round(latency_ms, 4), **detail}
        except Exception as exc:
            latency_ms = (time.perf_counter() - started) * 1000
            error = _safe_error(exc)
            self.metrics.record_case(
                suite,
                passed=False,
                latency_ms=latency_ms,
                error=error,
                detail={"case_id": case_id, "error": error},
            )
            if self.args.fail_fast:
                raise
            return {"case_id": case_id, "suite": suite, "passed": False, "latency_ms": round(latency_ms, 4), "error": error}

    def _reserve_llm_note(self, text: str) -> bool:
        """函数功能：`MassiveBenchmarkRunner._reserve_llm_note` 在类 `MassiveBenchmarkRunner` 中负责预约 llm note，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            text: 输入文本内容，类型为 `str`。
        返回结果说明：
            返回 `bool`，表示判断、写入或处理是否成功。
        """
        if self.args.extractor_mode == "rules":
            return True
        if contains_sensitive_data(text):
            return True
        with self._state_lock:
            if self.args.max_llm_cases and self.started_llm_notes >= self.args.max_llm_cases:
                return False
            self.started_llm_notes += 1
            return True

    def _evaluate_extraction_case(self, case: dict[str, Any]) -> dict[str, Any]:
        """函数功能：`MassiveBenchmarkRunner._evaluate_extraction_case` 在类 `MassiveBenchmarkRunner` 中负责处理 evaluate extraction case，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        suite = str(case["suite"])
        passed = True
        note_details = []
        for note in case.get("timeline") or []:
            text = str(note.get("text") or "")
            expected_candidates = list(note.get("expected_candidates") or [])
            expected_count = int((case.get("expected") or {}).get("candidate_count", len(expected_candidates)))
            expected_admit = bool(note.get("expected_admit") or expected_count > 0)
            sensitive = contains_sensitive_data(text) or suite == "sensitive_block"
            if not self._reserve_llm_note(text):
                return {"passed": False, "skipped": True, "reason": "max_llm_cases_reached"}

            started = time.perf_counter()
            if sensitive:
                raw_candidates: list[MemoryCandidate] = []
                valid_candidates: list[MemoryCandidate] = []
                rejections = []
            else:
                raw_candidates = extract_candidates(_note_id_for_case(case, note.get("note_id")), text)
                valid_candidates, rejections = validate_candidates(raw_candidates, note_text=text)
            elapsed_ms = (time.perf_counter() - started) * 1000
            self.metrics.add_value(suite, "extraction_latency_ms", elapsed_ms)
            for candidate in raw_candidates:
                self.metrics.add_counter(suite, f"raw_candidate_{candidate.extractor_type}")
            for candidate in valid_candidates:
                self.metrics.add_counter(suite, f"valid_candidate_{candidate.extractor_type}")

            actual_count = len(valid_candidates)
            actual_admit = actual_count > 0
            matched, operation_hits = _match_expected_candidates(expected_candidates, valid_candidates)
            expected_type_counts = Counter(str(item.get("memory_type") or "") for item in expected_candidates)
            actual_type_counts = Counter(candidate.memory_type for candidate in valid_candidates)
            type_exact = expected_type_counts == actual_type_counts
            count_ok = actual_count == expected_count
            admission_ok = actual_admit == expected_admit
            no_false_memory = not expected_candidates and actual_count == 0
            precision_den = actual_count if actual_count else 0

            self.metrics.add_rate(suite, "admission_accuracy", int(admission_ok))
            self.metrics.add_rate(suite, "candidate_count_accuracy", int(count_ok))
            self.metrics.add_rate(suite, "type_exact_accuracy", int(type_exact))
            self.metrics.add_rate(suite, "candidate_recall", matched, len(expected_candidates))
            self.metrics.add_rate(suite, "candidate_precision", matched, precision_den)
            self.metrics.add_rate(suite, "operation_accuracy", operation_hits, len(expected_candidates))
            if not expected_candidates:
                self.metrics.add_rate(suite, "false_memory_free_rate", int(no_false_memory))
                self.metrics.add_rate(suite, "false_memory_rate", int(actual_count > 0))
            if sensitive:
                self.metrics.add_rate(suite, "sensitive_block_rate", int(actual_count == 0))

            note_passed = admission_ok and count_ok and type_exact and matched == len(expected_candidates)
            passed = passed and note_passed
            note_details.append(
                {
                    "note_id": note.get("note_id"),
                    "expected_count": expected_count,
                    "actual_count": actual_count,
                    "matched": matched,
                    "raw_count": len(raw_candidates),
                    "rejected_count": len(rejections),
                    "actual_types": dict(actual_type_counts),
                    "extractor_types": dict(Counter(candidate.extractor_type for candidate in raw_candidates)),
                    "passed": note_passed,
                }
            )
        return {"passed": passed, "notes": note_details[:3]}

    def _evaluate_service_case(self, case: dict[str, Any]) -> dict[str, Any]:
        """函数功能：`MassiveBenchmarkRunner._evaluate_service_case` 在类 `MassiveBenchmarkRunner` 中负责处理 evaluate service case，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        suite = str(case["suite"])
        if self.args.skip_service_suites:
            return {"passed": False, "skipped": True, "reason": "service suites skipped"}
        if suite == "read_after_write_provisional":
            # 该 runner 同步调用 process_note_memory，无法观察生产中 Note 持久写入后、异步富化前的窗口；这个窗口由 V3 契约测试覆盖。
            return {
                "passed": False,
                "skipped": True,
                "reason": "requires asynchronous note-ingest boundary unavailable in synchronous runner",
            }
        if suite == "session_coreference":
            # answer_question 会在内部创建 hook context，因此 runner 无法安全注入基准的前序轮次 session。
            return {
                "passed": False,
                "skipped": True,
                "reason": "requires injectable query session context unavailable in public query API",
            }
        timeline = list(case.get("timeline") or [])
        processed = []
        spaces: set[str] = set()
        for note in timeline:
            text = str(note.get("text") or "")
            if not self._reserve_llm_note(text):
                return {"passed": False, "skipped": True, "reason": "max_llm_cases_reached"}
            runtime_space = _space_for_case(case, str(note.get("space_id") or case.get("space_id") or "default"))
            spaces.add(runtime_space)
            runtime_note = {
                "id": _note_id_for_case(case, str(note.get("note_id") or "")),
                "space_id": runtime_space,
                "tenant_id": "massive_benchmark",
                "user_id": str(note.get("actor") or case.get("user_id") or ""),
                "text": text,
            }
            started = time.perf_counter()
            report = process_note_memory(runtime_note)
            self.metrics.add_value(suite, "service_note_latency_ms", (time.perf_counter() - started) * 1000)
            processed.append(
                {
                    "note_id": note.get("note_id"),
                    "space_id": runtime_space,
                    "candidates": report.get("candidates"),
                    "status": report.get("extraction_status"),
                    "actions": [item.get("action") for item in report.get("results") or [] if isinstance(item, dict)],
                    "relations": [item.get("relation") for item in report.get("results") or [] if isinstance(item, dict)],
                }
            )
        if suite == "deletion_idempotency":
            return self._evaluate_deletion_case(case, spaces, processed)
        if suite == "cross_user_isolation":
            return self._evaluate_cross_user_case(case, processed)
        return self._evaluate_stateful_memory_case(case, spaces, processed)

    def _evaluate_stateful_memory_case(
        self,
        case: dict[str, Any],
        spaces: set[str],
        processed: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """函数功能：`MassiveBenchmarkRunner._evaluate_stateful_memory_case` 在类 `MassiveBenchmarkRunner` 中负责处理 evaluate stateful memory case，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
            spaces: spaces 参数，由调用方传入，类型为 `set[str]`。
            processed: processed 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        suite = str(case["suite"])
        expected = case.get("expected") or {}
        memories = [memory for space in spaces for memory in list_memories(space, status=None, limit=100)]
        active = [memory for memory in memories if memory.status == "active"]
        active_open_tasks = [
            memory
            for memory in active
            if memory.memory_type == "task" and memory.task_status == "todo"
        ]
        pending = [memory for memory in memories if memory.status == "pending_review"]
        destructive = [memory for memory in memories if memory.status in {"deleted", "superseded", "conflicted"}]
        max_sources = max((len(memory.sources) for memory in memories), default=0)
        max_version = max((memory.current_version for memory in memories), default=0)
        duplicate_active = self._duplicate_active_count(active)
        stale_active_tasks = len(active_open_tasks) if int(expected.get("active_task_count", 1)) == 0 else 0

        passed_checks = []
        if "active_memory_count" in expected:
            ok = len(active) == int(expected["active_memory_count"])
            self.metrics.add_rate(suite, "active_memory_count_accuracy", int(ok))
            passed_checks.append(ok)
        if "active_task_count" in expected:
            ok = len(active_open_tasks) == int(expected["active_task_count"])
            self.metrics.add_rate(suite, "active_open_task_count_accuracy", int(ok))
            passed_checks.append(ok)
        if "duplicate_active_count" in expected:
            ok = duplicate_active <= int(expected["duplicate_active_count"])
            self.metrics.add_rate(suite, "duplicate_active_free_rate", int(ok))
            passed_checks.append(ok)
        if "duplicate_active_task_count" in expected:
            duplicate_tasks = self._duplicate_active_count([memory for memory in active if memory.memory_type == "task"])
            ok = duplicate_tasks <= int(expected["duplicate_active_task_count"])
            self.metrics.add_rate(suite, "duplicate_active_task_free_rate", int(ok))
            passed_checks.append(ok)
        if "stale_active_task_count" in expected:
            ok = stale_active_tasks <= int(expected["stale_active_task_count"])
            self.metrics.add_rate(suite, "stale_active_task_free_rate", int(ok))
            passed_checks.append(ok)
        if "source_count" in expected:
            ok = max_sources >= int(expected["source_count"])
            self.metrics.add_rate(suite, "source_preservation_rate", int(ok))
            passed_checks.append(ok)
        if "version_count" in expected:
            ok = max_version >= int(expected["version_count"])
            self.metrics.add_rate(suite, "version_preservation_rate", int(ok))
            passed_checks.append(ok)
        actual_actions = [
            str(action)
            for item in processed
            for action in item.get("actions") or []
            if action
        ]
        actual_relations = [
            str(relation)
            for item in processed
            for relation in item.get("relations") or []
            if relation
        ]
        expected_actions = [str(action) for action in expected.get("action_sequence") or []]
        expected_relations = [str(relation) for relation in expected.get("relation_sequence") or []]
        if expected_actions:
            ok = actual_actions == expected_actions
            self.metrics.add_rate(suite, "action_sequence_accuracy", int(ok))
            passed_checks.append(ok)
        if expected_relations:
            ok = actual_relations == expected_relations
            self.metrics.add_rate(suite, "relation_sequence_accuracy", int(ok))
            passed_checks.append(ok)
        if expected.get("action"):
            ok = bool(actual_actions) and actual_actions[-1] == str(expected["action"])
            self.metrics.add_rate(suite, "final_action_accuracy", int(ok))
            passed_checks.append(ok)
        if expected.get("relation"):
            ok = bool(actual_relations) and actual_relations[-1] == str(expected["relation"])
            self.metrics.add_rate(suite, "final_relation_accuracy", int(ok))
            passed_checks.append(ok)
        if expected.get("old_memory_final_status"):
            expected_old_status = str(expected["old_memory_final_status"])
            ok = expected_old_status == "absent" or any(memory.status == expected_old_status for memory in memories)
            self.metrics.add_rate(suite, "old_memory_status_accuracy", int(ok))
            passed_checks.append(ok)
        if expected.get("new_memory_final_status"):
            expected_new_status = str(expected["new_memory_final_status"])
            ok = any(memory.status == expected_new_status for memory in memories)
            self.metrics.add_rate(suite, "new_memory_status_accuracy", int(ok))
            passed_checks.append(ok)
        if expected.get("destructive_action_allowed") is False or expected.get("auto_supersede_allowed") is False:
            ok = len(destructive) == 0
            self.metrics.add_rate(suite, "destructive_guard_rate", int(ok))
            passed_checks.append(ok)
        if expected.get("new_memory_final_status") == "pending_review" or expected.get("candidate_status") == "pending_review":
            ok = len(pending) >= 1
            self.metrics.add_rate(suite, "pending_review_detection_rate", int(ok))
            passed_checks.append(ok)

        expected_active = list(expected.get("active_memories") or [])
        if expected_active:
            matched = self._match_expected_records(expected_active, active)
            self.metrics.add_rate(suite, "active_memory_recall", matched, len(expected_active))
            passed_checks.append(matched == len(expected_active))

        return {
            "passed": all(passed_checks) if passed_checks else True,
            "processed": processed[:3],
            "actions": actual_actions,
            "relations": actual_relations,
            "memory_counts": {
                "total": len(memories),
                "active": len(active),
                "pending_review": len(pending),
                "destructive": len(destructive),
                "active_open_tasks": len(active_open_tasks),
                "duplicate_active": duplicate_active,
                "max_sources": max_sources,
                "max_version": max_version,
            },
        }

    @staticmethod
    def _duplicate_active_count(memories: list[Any]) -> int:
        """函数功能：`MassiveBenchmarkRunner._duplicate_active_count` 在类 `MassiveBenchmarkRunner` 中负责计数 duplicate active，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            memories: memories 参数，由调用方传入，类型为 `list[Any]`。
        返回结果说明：
            返回 `int`，表示计算得到的数值结果。
        """
        groups: Counter[str] = Counter()
        for memory in memories:
            key = getattr(memory, "memory_key", None) or _norm(getattr(memory, "content", ""))
            groups[f"{getattr(memory, 'memory_type', '')}:{key}"] += 1
        return sum(count - 1 for count in groups.values() if count > 1)

    def _match_expected_records(self, expected: list[dict[str, Any]], actual: list[Any]) -> int:
        """函数功能：`MassiveBenchmarkRunner._match_expected_records` 在类 `MassiveBenchmarkRunner` 中负责处理 match expected records，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            expected: expected 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
            actual: actual 参数，由调用方传入，类型为 `list[Any]`。
        返回结果说明：
            返回 `int`，表示计算得到的数值结果。
        """
        used: set[int] = set()
        matched = 0
        for exp in expected:
            exp_type = str(exp.get("memory_type") or "")
            terms = [_norm(term) for term in _expected_terms(exp)]
            for index, memory in enumerate(actual):
                if index in used or memory.memory_type != exp_type:
                    continue
                blob = _norm(_record_blob(memory))
                if terms and not any(term and term in blob for term in terms):
                    continue
                used.add(index)
                matched += 1
                break
        return matched

    def _evaluate_deletion_case(
        self,
        case: dict[str, Any],
        spaces: set[str],
        processed: list[dict[str, Any]],
    ) -> dict[str, Any]:
        """函数功能：`MassiveBenchmarkRunner._evaluate_deletion_case` 在类 `MassiveBenchmarkRunner` 中负责处理 evaluate deletion case，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
            spaces: spaces 参数，由调用方传入，类型为 `set[str]`。
            processed: processed 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        suite = str(case["suite"])
        expected = case.get("expected") or {}
        space = next(iter(spaces), _space_for_case(case))
        active_before = list_memories(space, status="active", limit=100)
        duplicate_before = self._duplicate_active_count(active_before)
        for memory in active_before:
            soft_delete_memory(memory.id)
        query = ((case.get("query") or {}).get("text") or "")
        returned = search_memories(space, query, min_score=self.args.retrieval_min_score, limit=10, mark_access=False)
        deleted_non_retrieval_ok = len(returned) == 0
        created_ok = len(active_before) == int(expected.get("created_memory_count", len(active_before)))
        duplicate_ok = duplicate_before <= int(expected.get("duplicate_active_count", 0))
        self.metrics.add_rate(suite, "idempotent_same_note_rate", int(created_ok and duplicate_ok))
        self.metrics.add_rate(suite, "deleted_non_retrieval_rate", int(deleted_non_retrieval_ok))
        return {
            "passed": created_ok and duplicate_ok and deleted_non_retrieval_ok,
            "processed": processed[:3],
            "active_before_delete": len(active_before),
            "duplicate_before_delete": duplicate_before,
            "returned_after_delete": len(returned),
        }

    def _evaluate_cross_user_case(self, case: dict[str, Any], processed: list[dict[str, Any]]) -> dict[str, Any]:
        """函数功能：`MassiveBenchmarkRunner._evaluate_cross_user_case` 在类 `MassiveBenchmarkRunner` 中负责处理 evaluate cross user case，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
            processed: processed 参数，由调用方传入，类型为 `list[dict[str, Any]]`。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        suite = str(case["suite"])
        expected = case.get("expected") or {}
        checks = []
        details = []
        for query in expected.get("queries") or []:
            runtime_space = _space_for_case(case, str(query.get("space_id") or "default"))
            rows = search_memories(
                runtime_space,
                str(query.get("text") or ""),
                min_score=0.0,
                limit=5,
                mark_access=False,
            )
            returned = [memory for memory, _score in rows]
            gold = str(query.get("gold") or "")
            gold_hit = any(gold and gold in memory.content for memory in returned)
            leak = any(memory.space_id != runtime_space for memory in returned)
            checks.append(gold_hit and not leak)
            self.metrics.add_rate(suite, "cross_space_isolation_rate", int(not leak))
            self.metrics.add_rate(suite, "cross_user_gold_hit_rate", int(gold_hit))
            details.append(
                {
                    "space_id": runtime_space,
                    "gold": gold,
                    "gold_hit": gold_hit,
                    "leak": leak,
                    "returned": [memory.content for memory in returned[:3]],
                }
            )
        return {"passed": all(checks), "processed": processed[:3], "queries": details}

    def _evaluate_retrieval_case(self, case: dict[str, Any]) -> dict[str, Any]:
        """函数功能：`MassiveBenchmarkRunner._evaluate_retrieval_case` 在类 `MassiveBenchmarkRunner` 中负责处理 evaluate retrieval case，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        suite = str(case["suite"])
        expected = case.get("expected") or {}
        query = str((case.get("query") or {}).get("text") or "")
        space = _space_for_case(case)
        expected_to_actual, actual_to_expected = self._seed_candidate_pool(case, space)
        started = time.perf_counter()
        plan = build_query_plan(query)
        results = self._planned_search(space, query, plan)
        self.metrics.add_value(suite, "retrieval_latency_ms", (time.perf_counter() - started) * 1000)
        ranked_expected_ids = [actual_to_expected.get(memory.id, memory.id) for memory, _score in results]
        gold_ids = [str(item) for item in expected.get("gold_memory_ids") or []]
        must_not = {str(item) for item in expected.get("must_not_return_ids") or []}
        targets = [int(item) for item in expected.get("recall_at_k_targets") or [1, 3, 5]]
        for k in targets:
            hit = any(gold_id in ranked_expected_ids[:k] for gold_id in gold_ids)
            self.metrics.add_rate(suite, f"recall_at_{k}", int(hit))
        if gold_ids:
            first_rank = next((idx + 1 for idx, memory_id in enumerate(ranked_expected_ids) if memory_id in gold_ids), None)
            self.metrics.add_rate(suite, "mrr", 1 / first_rank if first_rank else 0.0)
        leakage = bool(must_not & set(ranked_expected_ids))
        self.metrics.add_rate(suite, "must_not_return_free_rate", int(not leakage))
        expected_route = (case.get("query") or {}).get("expected_route") or {}
        expected_complexity = str(expected_route.get("complexity") or "")
        if expected_complexity:
            self._record_routing_confusion(suite, expected_complexity, plan.complexity)
            self.metrics.add_rate(suite, "route_complexity_accuracy", int(plan.complexity == expected_complexity))
        passed = (not gold_ids or any(gold_id in ranked_expected_ids[: max(targets)] for gold_id in gold_ids)) and not leakage
        return {
            "passed": passed,
            "query": query,
            "expected_to_actual_count": len(expected_to_actual),
            "top_ids": ranked_expected_ids[:10],
            "gold_ids": gold_ids,
            "plan": asdict(plan),
        }

    def _seed_candidate_pool(self, case: dict[str, Any], space: str) -> tuple[dict[str, str], dict[str, str]]:
        """函数功能：`MassiveBenchmarkRunner._seed_candidate_pool` 在类 `MassiveBenchmarkRunner` 中负责写入测试种子数据 candidate pool，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
            space: space 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            返回 `tuple[dict[str, str], dict[str, str]]`，表示由多个相关值组成的结果。
        """
        expected = case.get("expected") or {}
        expected_to_actual: dict[str, str] = {}
        actual_to_expected: dict[str, str] = {}
        for index, item in enumerate(expected.get("candidate_pool") or []):
            expected_id = str(item.get("memory_id") or f"pool_{index}")
            candidate = self._candidate_from_pool_item(case, item, expected_id)
            record = insert_memory(
                space,
                candidate,
                source_note_id=f"seed:{case['case_id']}:{expected_id}",
                status=str(item.get("status") or "active"),
            )
            expected_to_actual[expected_id] = record.id
            actual_to_expected[record.id] = expected_id
        return expected_to_actual, actual_to_expected

    @staticmethod
    def _candidate_from_pool_item(case: dict[str, Any], item: dict[str, Any], expected_id: str) -> MemoryCandidate:
        """函数功能：`MassiveBenchmarkRunner._candidate_from_pool_item` 在类 `MassiveBenchmarkRunner` 中负责处理 candidate from pool item，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
            item: item 参数，由调用方传入，类型为 `dict[str, Any]`。
            expected_id: expected id 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            返回 `MemoryCandidate` 类型结果；具体字段和语义由调用方按该对象约定使用。
        """
        memory_type = str(item.get("memory_type") or "semantic")
        entity = str(item.get("entity") or item.get("object_value") or "").strip() or None
        attribute = str(item.get("attribute") or "").strip() or None
        operation = str(item.get("operation") or "").strip().lower()
        task_status = None
        if operation in {"created", "create", "todo"}:
            task_status = "todo"
        elif operation in {"completed", "done"}:
            task_status = "done"
        elif operation in {"cancelled", "canceled"}:
            task_status = "done"
        return MemoryCandidate(
            memory_type=memory_type,
            content=str(item.get("content") or ""),
            importance=float(item.get("importance") or 0.7),
            confidence=float(item.get("confidence") or 0.8),
            entities=[entity] if entity else [],
            candidate_id=f"seed_{case['case_id']}_{expected_id}",
            note_id=f"seed:{case['case_id']}:{expected_id}",
            subject=str(item.get("subject") or "用户"),
            predicate=attribute,
            object_value=entity,
            task_status=task_status if memory_type == "task" else None,
            reason="massive_benchmark_seed",
        )

    def _planned_search(self, space: str, query: str, plan: Any) -> list[tuple[Any, float]]:
        """函数功能：`MassiveBenchmarkRunner._planned_search` 在类 `MassiveBenchmarkRunner` 中负责搜索 planned，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            space: space 参数，由调用方传入，类型为 `str`。
            query: 检索或查询文本，类型为 `str`。
            plan: plan 参数，由调用方传入，类型为 `Any`。
        返回结果说明：
            返回 `list[tuple[Any, float]]`，表示按条件筛选、构造或查询得到的列表。
        """
        queries = [query, *list(plan.retrieval_queries or ())]
        merged: dict[str, tuple[Any, float]] = {}
        for query_index, item_query in enumerate(dict.fromkeys(q for q in queries if q)):
            rows = search_memories(
                space,
                item_query,
                min_score=self.args.retrieval_min_score,
                limit=50,
                mark_access=False,
            )
            for rank, (memory, score) in enumerate(rows):
                combined = float(score) + 0.01 / (rank + 1) + 0.001 / (query_index + 1)
                current = merged.get(memory.id)
                if current is None or combined > current[1]:
                    merged[memory.id] = (memory, combined)
        return sorted(merged.values(), key=lambda item: item[1], reverse=True)

    def _evaluate_routing_case(self, case: dict[str, Any]) -> dict[str, Any]:
        """函数功能：`MassiveBenchmarkRunner._evaluate_routing_case` 在类 `MassiveBenchmarkRunner` 中负责处理 evaluate routing case，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            case: case 参数，由调用方传入，类型为 `dict[str, Any]`。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        suite = str(case["suite"])
        query = str((case.get("query") or {}).get("text") or "")
        expected = case.get("expected") or {}
        expected_route = expected.get("gold_route") or (case.get("query") or {}).get("expected_route") or {}
        expected_complexity = str(expected.get("gold_complexity") or expected_route.get("complexity") or "")
        started = time.perf_counter()
        plan = build_query_plan(query)
        self.metrics.add_value(suite, "planner_latency_ms", (time.perf_counter() - started) * 1000)
        expansion = bool(plan.retrieval_queries or plan.use_query_rewrite or plan.use_decomposition or plan.use_step_back)
        expansion_required = bool(expected.get("expansion_required") or expected.get("should_call_complex_planner"))
        min_variants = int(expected.get("minimum_retrieval_variants") or 0)
        complexity_ok = plan.complexity == expected_complexity
        expansion_ok = not expansion_required or expansion
        variants_ok = len(plan.retrieval_queries) >= min_variants
        self.metrics.add_rate(suite, "complexity_accuracy", int(complexity_ok))
        self.metrics.add_rate(suite, "expansion_coverage", int(expansion_ok))
        if min_variants:
            self.metrics.add_rate(suite, "minimum_variant_coverage", int(variants_ok))
        self._record_routing_confusion(suite, expected_complexity, plan.complexity)
        passed = complexity_ok and expansion_ok and variants_ok
        return {
            "passed": passed,
            "query": query,
            "expected_complexity": expected_complexity,
            "actual_complexity": plan.complexity,
            "variant_count": len(plan.retrieval_queries),
            "plan": asdict(plan),
        }

    def _record_routing_confusion(self, suite: str, expected: str, actual: str) -> None:
        """函数功能：`MassiveBenchmarkRunner._record_routing_confusion` 在类 `MassiveBenchmarkRunner` 中负责记录 routing confusion，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            suite: suite 参数，由调用方传入，类型为 `str`。
            expected: expected 参数，由调用方传入，类型为 `str`。
            actual: actual 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        if expected not in {"simple", "complex"} or actual not in {"simple", "complex"}:
            return
        if expected == "complex" and actual == "complex":
            self.metrics.add_counter(suite, "routing_tp")
        elif expected == "simple" and actual == "complex":
            self.metrics.add_counter(suite, "routing_fp")
        elif expected == "complex" and actual == "simple":
            self.metrics.add_counter(suite, "routing_fn")
        else:
            self.metrics.add_counter(suite, "routing_tn")

    def _print_progress(self) -> None:
        """函数功能：`MassiveBenchmarkRunner._print_progress` 在类 `MassiveBenchmarkRunner` 中负责处理 print progress，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            无。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        elapsed = max(time.perf_counter() - self.started_at, 0.001)
        rate = self.processed_cases / elapsed
        print(
            f"[massive-benchmark] processed={self.processed_cases} submitted={self.submitted_cases} "
            f"rate={rate:.2f}/s llm_notes_started={self.started_llm_notes} "
            f"suites={dict(self.suite_seen)}",
            flush=True,
        )

    def _build_summary(self) -> dict[str, Any]:
        """函数功能：`MassiveBenchmarkRunner._build_summary` 在类 `MassiveBenchmarkRunner` 中负责构建 summary，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            无。
        返回结果说明：
            返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
        """
        config = get_chat_config("fast")
        metrics = self.metrics.finalize()
        elapsed_seconds = round(time.perf_counter() - self.started_at, 3)
        return {
            "benchmark": "suixinji_massive_memory_benchmark_v2",
            "created_at": _utc_now(),
            "elapsed_seconds": elapsed_seconds,
            "dataset": {
                "zip_path": str(self.args.zip_path),
                "manifest": self.metadata,
                "filtered_cases": self.filtered_cases,
                "submitted_cases": self.submitted_cases,
                "processed_cases": self.processed_cases,
                "suite_seen": dict(self.suite_seen),
            },
            "runtime": {
                "extractor_mode": self.args.extractor_mode,
                "workers": self.args.workers,
                "memory_extractor_import_mode": memory_extractor.MEMORY_EXTRACTOR_MODE,
                "llm_timeout_seconds": settings.MEMORY_EXTRACTION_LLM_TIMEOUT_SECONDS,
                "llm_timeout_retries": settings.MEMORY_EXTRACTION_LLM_MAX_RETRIES,
                "fast_model": config.model,
                "base_url_set": bool(config.base_url),
                "storage_backend": settings.STORAGE_BACKEND,
                "task_queue_backend": settings.TASK_QUEUE_BACKEND,
                "coordination_backend": settings.COORDINATION_BACKEND,
                "db_path": str(self.db_path),
                "rows_path": str(self.rows_path if self.args.write_rows else ""),
            },
            "llm": self.llm_tracker.summary(),
            "metrics": metrics,
            "outputs": {
                "run_dir": str(self.run_dir),
                "summary_json": str(self.summary_json_path),
                "summary_md": str(self.summary_md_path),
                "llm_events": str(self.llm_tracker.path),
            },
        }

    def _render_markdown(self, summary: dict[str, Any]) -> str:
        """函数功能：`MassiveBenchmarkRunner._render_markdown` 在类 `MassiveBenchmarkRunner` 中负责渲染 markdown，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            summary: summary 参数，由调用方传入，类型为 `dict[str, Any]`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        runtime = summary["runtime"]
        global_metrics = summary["metrics"]["global"]
        lines = [
            "# Suixinji Massive Memory Benchmark V2",
            "",
            "Synthetic offline benchmark. Do not present these numbers as production traffic quality.",
            "",
            "## Run",
            "",
            f"- Created: `{summary['created_at']}`",
            f"- Elapsed: `{summary['elapsed_seconds']}s`",
            f"- Extractor mode: `{runtime['extractor_mode']}`",
            f"- Imported extractor mode: `{runtime['memory_extractor_import_mode']}`",
            f"- Fast model: `{runtime['fast_model']}`",
            f"- LLM timeout/retry: `{runtime['llm_timeout_seconds']}s / {runtime['llm_timeout_retries']}`",
            f"- SQLite DB: `{runtime['db_path']}`",
            "",
            "## Global",
            "",
            f"- Cases: `{global_metrics['cases']}`",
            f"- Passed: `{global_metrics['passed']}`",
            f"- Failed: `{global_metrics['failed']}`",
            f"- Skipped: `{global_metrics['skipped']}`",
            f"- Errors: `{global_metrics['errors']}`",
            f"- Pass rate: `{_pct(global_metrics['pass_rate'])}`",
            "",
            "## LLM",
            "",
        ]
        llm_summary = summary.get("llm") or {}
        token_summary = llm_summary.get("tokens") or {}
        for key in sorted(k for k in llm_summary if k != "tokens"):
            lines.append(f"- {key}: `{llm_summary[key]}`")
        for key in sorted(token_summary):
            lines.append(f"- {key}: `{token_summary[key]}`")

        lines.extend(
            [
                "",
                "## Suites",
                "",
                "| Suite | Cases | Pass | Key Metrics | p95 ms |",
                "|---|---:|---:|---|---:|",
            ]
        )
        for suite, item in summary["metrics"]["suites"].items():
            rates = item.get("rates") or {}
            key_metrics = self._suite_key_metrics(suite, rates)
            p95 = ((item.get("values") or {}).get("case_latency_ms") or {}).get("p95", 0.0)
            lines.append(
                f"| `{suite}` | {item['cases']} | {_pct(item['pass_rate'])} | {key_metrics} | {p95:.2f} |"
            )

        lines.extend(["", "## Failure Samples", ""])
        for suite, item in summary["metrics"]["suites"].items():
            failures = item.get("failures") or []
            if not failures:
                continue
            lines.append(f"### {suite}")
            for failure in failures[:10]:
                lines.append(f"- `{failure.get('case_id', 'unknown')}`: `{_as_text(failure)[:500]}`")
            lines.append("")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _suite_key_metrics(suite: str, rates: dict[str, Any]) -> str:
        """函数功能：`MassiveBenchmarkRunner._suite_key_metrics` 在类 `MassiveBenchmarkRunner` 中负责处理 suite key metrics，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
        传参：
            suite: suite 参数，由调用方传入，类型为 `str`。
            rates: rates 参数，由调用方传入，类型为 `dict[str, Any]`。
        返回结果说明：
            返回 `str`，通常是格式化后的文本、标识或路径。
        """
        preferred = {
            "extraction_atomic": ["candidate_recall", "candidate_precision", "admission_accuracy"],
            "extraction_multiclause": ["candidate_recall", "candidate_precision", "candidate_count_accuracy"],
            "negative_noise": ["false_memory_free_rate", "admission_accuracy"],
            "sensitive_block": ["sensitive_block_rate"],
            "relation_same_add_source": ["active_memory_count_accuracy", "source_preservation_rate"],
            "task_lifecycle": ["active_open_task_count_accuracy", "stale_active_task_free_rate", "version_preservation_rate"],
            "preference_supersede": ["pending_review_detection_rate", "destructive_guard_rate"],
            "semantic_pending_review": ["pending_review_detection_rate", "destructive_guard_rate"],
            "deletion_idempotency": ["idempotent_same_note_rate", "deleted_non_retrieval_rate"],
            "cross_user_isolation": ["cross_space_isolation_rate", "cross_user_gold_hit_rate"],
            "retrieval_single": ["recall_at_1", "recall_at_3", "mrr"],
            "retrieval_long_context": ["recall_at_1", "recall_at_3", "mrr"],
            "query_routing": ["complexity_accuracy", "complex_f1", "false_complex_rate"],
            "complex_planner": ["complexity_accuracy", "expansion_coverage", "complex_f1"],
        }.get(suite, [])
        chunks = []
        for key in preferred:
            if key in rates:
                chunks.append(f"{key}={_pct(rates[key])}")
        return "<br>".join(chunks) if chunks else "-"


def build_parser() -> argparse.ArgumentParser:
    """函数功能：`build_parser` 负责构建 parser，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        无。
    返回结果说明：
        返回 `argparse.ArgumentParser` 类型结果；具体字段和语义由调用方按该对象约定使用。
    """
    parser = argparse.ArgumentParser(description="Run Suixinji massive Memory benchmark V2.")
    parser.add_argument("--zip-path", type=Path, default=DEFAULT_ZIP)
    parser.add_argument("--suite", action="append", choices=ALL_SUITES)
    parser.add_argument("--split", action="append", choices=("train", "dev", "test", "challenge"))
    parser.add_argument("--limit", type=int, default=0, help="Maximum total filtered cases to run.")
    parser.add_argument("--limit-per-suite", type=int, default=0)
    parser.add_argument("--workers", type=int, default=1, help="Concurrent case workers. Use 4-16 for LLM-heavy runs.")
    parser.add_argument("--extractor-mode", choices=("rules", "hybrid", "llm"), default=BOOTSTRAP_ARGS.extractor_mode)
    parser.add_argument("--max-llm-cases", type=int, default=0, help="0 means unlimited.")
    parser.add_argument("--llm-timeout-seconds", type=int, default=BOOTSTRAP_ARGS.llm_timeout_seconds)
    parser.add_argument("--llm-timeout-retries", type=int, default=BOOTSTRAP_ARGS.llm_timeout_retries)
    parser.add_argument(
        "--retrieval-min-score",
        type=float,
        default=0.0,
        help="Use 0.0 by default to evaluate ranking over the synthetic candidate pool.",
    )
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--db-path", type=Path)
    parser.add_argument("--write-rows", action="store_true", default=True)
    parser.add_argument("--no-write-rows", action="store_false", dest="write_rows")
    parser.add_argument("--skip-service-suites", action="store_true")
    parser.add_argument("--verify-sha256", action="store_true", default=True)
    parser.add_argument("--no-verify-sha256", action="store_false", dest="verify_sha256")
    parser.add_argument("--progress-every", type=int, default=1000)
    parser.add_argument("--max-failures", type=int, default=200)
    parser.add_argument("--fail-fast", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    """函数功能：`main` 负责作为命令行入口解析参数并调度执行，服务于本文件职责：评测脚本，负责 run massive memory benchmark 场景的数据构造、执行或指标汇总。
    传参：
        argv: argv 参数，由调用方传入，类型为 `list[str] | None`，默认值为 `None`。
    返回结果说明：
        返回 `int`，表示计算得到的数值结果。
    """
    args = build_parser().parse_args(argv)
    if not args.zip_path.exists():
        raise SystemExit(f"Dataset zip not found: {args.zip_path}")
    runner = MassiveBenchmarkRunner(args)
    summary = runner.run()
    print(json.dumps(summary["outputs"], ensure_ascii=False, indent=2))
    global_metrics = summary["metrics"]["global"]
    print(
        "completed "
        f"cases={global_metrics['cases']} "
        f"passed={global_metrics['passed']} "
        f"failed={global_metrics['failed']} "
        f"skipped={global_metrics['skipped']} "
        f"pass_rate={_pct(global_metrics['pass_rate'])}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
