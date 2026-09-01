"""Isolated LongMemEval-Oracle adapter for Suixinji Ask V2.

This runner deliberately stores each benchmark case in its own PostgreSQL
space, writes only the released evidence sessions as Notes, executes the real
Ask V2 workflow, writes reproducible receipts, then deletes the test space.
It does not write to any real Feishu/user space.

Oracle is an answer-generation/evidence-use evaluation: all retained sessions
are gold evidence, so its session recall is not a distractor-retrieval score.
Use LongMemEval-S separately for retrieval-under-distractors.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import statistics
import sys
import time
import uuid
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parents[3]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from sqlalchemy import delete

from agent.ask_workflow import answer_question_v2
from agent.query_agent import answer_question
from core import settings
from infrastructure.database import session_scope
from infrastructure.schema import Space
from repositories.postgres.notes import save_note
from storage.note_storage import NoteMetadata


ROOT = Path(__file__).resolve().parent
DEFAULT_DATASET = ROOT / "longmemeval_oracle.json"
RESULTS_DIR = ROOT / "results"


def _safe_id(value: str) -> str:
    return re.sub(r"[^a-zA-Z0-9_-]", "-", value)[:96]


def _case_space_id(case_id: str) -> str:
    digest = hashlib.sha256(case_id.encode("utf-8")).hexdigest()[:16]
    return f"eval-longmemeval-oracle-{digest}"


def _to_iso(value: str) -> str:
    try:
        return datetime.strptime(value, "%Y/%m/%d (%a) %H:%M").replace(tzinfo=timezone.utc).isoformat()
    except (TypeError, ValueError):
        return datetime.now(timezone.utc).isoformat()


def _session_text(session: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for turn in session:
        role = str(turn.get("role") or "unknown").strip()
        content = " ".join(str(turn.get("content") or "").split())
        if content:
            lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _note_id(case_id: str, session_id: str) -> str:
    digest = hashlib.sha256(f"{case_id}:{session_id}".encode("utf-8")).hexdigest()[:20]
    return f"lme-note-{digest}"


def _write_case_notes(case: dict[str, Any], space_id: str) -> dict[str, str]:
    mapping: dict[str, str] = {}
    sessions = case.get("haystack_sessions") or []
    session_ids = case.get("haystack_session_ids") or []
    dates = case.get("haystack_dates") or []
    if not (len(sessions) == len(session_ids) == len(dates)):
        raise ValueError(f"invalid LongMemEval session arrays: {case.get('question_id')}")
    for session, session_id, date in zip(sessions, session_ids, dates, strict=True):
        session_key = str(session_id)
        note_id = _note_id(str(case["question_id"]), session_key)
        # A released case can reference the same session more than once. It
        # remains one Note in the isolated space, while every reference maps
        # back to that same deterministic Note ID.
        if session_key in mapping:
            continue
        text = _session_text(session)
        saved = save_note(NoteMetadata(
            id=note_id,
            message_id=f"lme-message-{_safe_id(str(case['question_id']))}-{_safe_id(str(session_id))}",
            space_id=space_id,
            ts=_to_iso(str(date)),
            title=f"LongMemEval session {session_id}",
            tags=["benchmark", "longmemeval", str(case.get("question_type") or "")],
            type="benchmark_session",
            summary=text[:600],
            text=text,
            related=[],
            enrichment_status="ready",
        ))
        if not saved:
            raise RuntimeError(f"note insert conflict: {note_id}")
        mapping[session_key] = note_id
    return mapping


def _cleanup_space(space_id: str) -> None:
    with session_scope() as session:
        session.execute(delete(Space).where(Space.id == space_id))


def _normalise(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").casefold())


def _gold_substring(answer: str, gold: str) -> bool:
    """A transparent strict proxy only; not the official LLM judge."""
    # Legacy answers append a provenance block.  IDs in that block must never
    # satisfy a numeric Gold answer such as ``3`` by coincidence.
    answer_body = str(answer or "").split("\n\n来源", 1)[0]
    answer_value = _normalise(answer_body)
    gold_text = str(gold or "").strip()
    gold_value = _normalise(gold_text)
    if re.fullmatch(r"\d+(?:\.\d+)?", gold_text):
        return bool(re.search(rf"(?<!\d){re.escape(gold_text)}(?!\d)", answer_body))
    return bool(gold_value and gold_value in answer_value)


def _stratified_cases(cases: list[dict[str, Any]], limit: int | None) -> list[dict[str, Any]]:
    if limit is None or limit <= 0 or limit >= len(cases):
        return cases
    by_type: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for case in cases:
        by_type[str(case.get("question_type") or "unknown")].append(case)
    selected: list[dict[str, Any]] = []
    cursor: dict[str, int] = {name: 0 for name in by_type}
    names = sorted(by_type)
    while len(selected) < limit:
        progressed = False
        for name in names:
            if len(selected) >= limit:
                break
            index = cursor[name]
            if index >= len(by_type[name]):
                continue
            selected.append(by_type[name][index])
            cursor[name] += 1
            progressed = True
        if not progressed:
            break
    return selected


def _recall_at(retrieved: list[str], expected: set[str], limit: int) -> bool:
    return bool(expected.intersection(retrieved[:limit]))


def _legacy_note_ids(answer: str) -> list[str]:
    return re.findall(r"(?:^|\n)- note:([^｜\s]+)", str(answer or ""))


def _run_case(case: dict[str, Any], *, keep_space: bool, compare_react: bool) -> dict[str, Any]:
    case_id = str(case["question_id"])
    space_id = _case_space_id(case_id)
    _cleanup_space(space_id)
    started = time.perf_counter()
    try:
        session_to_note = _write_case_notes(case, space_id)
        question = str(case["question"])
        legacy: dict[str, Any] = {}
        if compare_react:
            legacy_started = time.perf_counter()
            previous_v2_enabled = settings.ASK_V2_ENABLED
            previous_v2_shadow = settings.ASK_V2_SHADOW
            try:
                # Compare the old answer path itself, rather than charging it
                # for the production Shadow planner's observational cost.
                settings.ASK_V2_ENABLED = False
                settings.ASK_V2_SHADOW = False
                legacy_answer = answer_question(
                    space_id,
                    question,
                    message_id=f"lme-react-{_safe_id(case_id)}-{uuid.uuid4().hex}",
                )
                legacy_ids = _legacy_note_ids(legacy_answer)
                legacy = {
                    "react_answer": legacy_answer,
                    "react_retrieved_note_ids": legacy_ids,
                    "react_evidence_recall_at_1": _recall_at(legacy_ids, set(session_to_note.values()), 1),
                    "react_evidence_recall_at_3": _recall_at(legacy_ids, set(session_to_note.values()), 3),
                    "react_evidence_recall_at_5": _recall_at(legacy_ids, set(session_to_note.values()), 5),
                    "react_gold_substring_match": _gold_substring(legacy_answer, str(case.get("answer") or "")),
                    "react_elapsed_seconds": round(time.perf_counter() - legacy_started, 3),
                    "react_error": None,
                }
            except Exception as exc:
                legacy = {
                    "react_answer": None,
                    "react_retrieved_note_ids": [],
                    "react_elapsed_seconds": round(time.perf_counter() - legacy_started, 3),
                    "react_error": f"{type(exc).__name__}: {exc}",
                }
            finally:
                settings.ASK_V2_ENABLED = previous_v2_enabled
                settings.ASK_V2_SHADOW = previous_v2_shadow
        v2_started = time.perf_counter()
        outcome = answer_question_v2(space_id, str(case["question"]))
        expected = {
            session_to_note[session_id]
            for session_id in case.get("answer_session_ids") or []
            if session_id in session_to_note
        }
        retrieved = [str(item.get("id") or item.get("note_id") or "") for item in outcome.selected_records]
        return {
            "question_id": case_id,
            "question_type": case.get("question_type"),
            "question": case.get("question"),
            "gold_answer": case.get("answer"),
            "answer": outcome.answer,
            "answer_source": outcome.answer_source,
            "plan": outcome.plan.model_dump(),
            "resolution_statuses": [bundle.resolution.status for bundle in outcome.bundles],
            "expected_note_ids": sorted(expected),
            "retrieved_note_ids": retrieved,
            "evidence_recall_at_1": _recall_at(retrieved, expected, 1),
            "evidence_recall_at_3": _recall_at(retrieved, expected, 3),
            "evidence_recall_at_5": _recall_at(retrieved, expected, 5),
            "gold_substring_match": _gold_substring(outcome.answer, str(case.get("answer") or "")),
            "elapsed_seconds": round(time.perf_counter() - v2_started, 3),
            "total_case_seconds": round(time.perf_counter() - started, 3),
            "error": None,
            **legacy,
        }
    except Exception as exc:
        return {
            "question_id": case_id,
            "question_type": case.get("question_type"),
            "question": case.get("question"),
            "error": f"{type(exc).__name__}: {exc}",
            "elapsed_seconds": round(time.perf_counter() - started, 3),
        }
    finally:
        if not keep_space:
            _cleanup_space(space_id)


def _ratio(rows: list[dict[str, Any]], key: str) -> float | None:
    values = [bool(row.get(key)) for row in rows if not row.get("error")]
    return round(sum(values) / len(values), 4) if values else None


def _summary(rows: list[dict[str, Any]], *, dataset: str) -> dict[str, Any]:
    valid = [row for row in rows if not row.get("error")]
    plan_intents = Counter(
        unit["intent"]
        for row in valid
        for unit in row.get("plan", {}).get("units", [])
    )
    by_type: dict[str, dict[str, Any]] = {}
    for question_type in sorted({str(row.get("question_type")) for row in rows}):
        bucket = [row for row in rows if str(row.get("question_type")) == question_type]
        by_type[question_type] = {
            "cases": len(bucket),
            "completed": sum(not row.get("error") for row in bucket),
            "evidence_recall_at_1": _ratio(bucket, "evidence_recall_at_1"),
            "evidence_recall_at_5": _ratio(bucket, "evidence_recall_at_5"),
            "gold_substring_match": _ratio(bucket, "gold_substring_match"),
        }
    output = {
        "benchmark": "LongMemEval-Oracle cleaned",
        "dataset": dataset,
        "evaluation_scope": "isolated evidence-session Notes + real Ask V2; no memory extraction; Oracle has no distractor sessions",
        "cases": len(rows),
        "completed": len(valid),
        "failed": len(rows) - len(valid),
        "evidence_recall_at_1": _ratio(rows, "evidence_recall_at_1"),
        "evidence_recall_at_3": _ratio(rows, "evidence_recall_at_3"),
        "evidence_recall_at_5": _ratio(rows, "evidence_recall_at_5"),
        "gold_substring_match": _ratio(rows, "gold_substring_match"),
        "mean_latency_seconds": round(statistics.mean(row["elapsed_seconds"] for row in valid), 3) if valid else None,
        "planner_intent_counts": dict(sorted(plan_intents.items())),
        "by_question_type": by_type,
        "limitations": [
            "Gold substring match is a strict transparent proxy, not LongMemEval's official LLM judge.",
            "Oracle retains only evidence sessions, so its evidence recall must not be presented as distractor-retrieval recall.",
            "No extracted Memory objects are created; this isolates AskPlan, Note retrieval, evidence expansion and answer synthesis.",
        ],
    }
    if any("react_elapsed_seconds" in row for row in rows):
        react_valid = [row for row in rows if not row.get("react_error")]
        output["react"] = {
            "completed": len(react_valid),
            "failed": len(rows) - len(react_valid),
            "evidence_recall_at_1": _ratio(rows, "react_evidence_recall_at_1"),
            "evidence_recall_at_3": _ratio(rows, "react_evidence_recall_at_3"),
            "evidence_recall_at_5": _ratio(rows, "react_evidence_recall_at_5"),
            "gold_substring_match": _ratio(rows, "react_gold_substring_match"),
            "mean_latency_seconds": round(statistics.mean(row["react_elapsed_seconds"] for row in react_valid), 3) if react_valid else None,
            "note": "Legacy ReAct was run with V2 and V2 Shadow disabled only inside this benchmark process.",
        }
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument("--limit", type=int, default=30, help="0 means all 500 cases")
    parser.add_argument("--output-dir", type=Path, default=RESULTS_DIR)
    parser.add_argument("--keep-spaces", action="store_true", help="debug only; never use for a normal run")
    parser.add_argument("--compare-react", action="store_true", help="run legacy ReAct and Ask V2 against the same isolated Notes")
    args = parser.parse_args()

    cases = json.loads(args.dataset.read_text(encoding="utf-8"))
    selected = _stratified_cases(cases, args.limit)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    label = "react_vs_ask_v2" if args.compare_react else "ask_v2"
    rows_path = args.output_dir / f"longmemeval_oracle_{label}_{stamp}.jsonl"
    rows: list[dict[str, Any]] = []
    for index, case in enumerate(selected, start=1):
        row = _run_case(case, keep_space=args.keep_spaces, compare_react=args.compare_react)
        rows.append(row)
        with rows_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(row, ensure_ascii=False) + "\n")
        print(json.dumps({"progress": f"{index}/{len(selected)}", "question_id": row["question_id"], "error": row.get("error")}, ensure_ascii=False), flush=True)
    summary = _summary(rows, dataset=str(args.dataset))
    summary_path = rows_path.with_suffix(".summary.json")
    summary_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"rows": str(rows_path), "summary": str(summary_path), **summary}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
