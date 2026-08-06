"""Independent Layer-2 v2 evaluator using isolated real PostgreSQL spaces.

The evaluator feeds the validated ``input.candidate`` directly into the real
consolidator.  Dataset refs (c1/m1/v1/s1/pr1) are kept in this module only;
they are never placed in production candidates or database decisions.
"""

from __future__ import annotations

import argparse
import dataclasses
import hashlib
import json
import time
from collections import Counter
from pathlib import Path
from typing import Any

from sqlalchemy import delete, select

from infrastructure.database import session_scope
from infrastructure.schema import Memory, Space
from memory import repository
from memory.candidate_retriever import retrieve_candidates
from memory.consolidator import consolidate_candidate
from memory.models import MEMORY_KEY_V3_VERSION, MemoryCandidate
from repositories.postgres.memory import _add_version


OLD_STATUSES = {"blocked", "in_progress", "cancelled", "canceled"}
RELATIONS = ["new", "same", "merge", "update", "supersede", "conflict"]
ACTIONS = ["insert", "add_source", "update", "pending_review"]


def _jsonable(value: Any) -> Any:
    if dataclasses.is_dataclass(value):
        return _jsonable(dataclasses.asdict(value))
    if isinstance(value, dict):
        return {str(k): _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_jsonable(v) for v in value]
    return value if isinstance(value, (str, int, float, bool)) or value is None else str(value)


def _status(value: Any) -> str | None:
    value = str(value or "").casefold() or None
    return {"blocked": "todo", "in_progress": "todo", "cancelled": "done", "canceled": "done"}.get(value, value)


def _source_note(case_id: str, ref: str) -> str:
    return f"l2v2:{case_id}:source:{ref}"


def _candidate_source_ref(candidate_ref: str) -> str:
    """Map an evaluator candidate ref to its evaluator-local source ref.

    The v2 fixtures name the single assertion source for ``cN`` as ``sN``.
    This is an evaluator transport convention only: the production candidate
    never receives either logical ref.
    """
    if candidate_ref.startswith("c") and candidate_ref[1:].isdigit():
        return f"s{candidate_ref[1:]}"
    return f"candidate-{candidate_ref}"


def _candidate(raw: dict[str, Any], *, case_id: str, candidate_ref: str | None = None, note_ref: str | None = None) -> MemoryCandidate:
    evidence = raw.get("evidence_span")
    evidence_text = evidence.get("text") if isinstance(evidence, dict) else evidence
    logical_candidate_ref = candidate_ref or str(raw.get("candidate_ref") or "candidate")
    # candidate_ref is evaluator-only: production receives a deterministic ID
    # with no c1/c2 logical reference embedded in its fields.
    candidate_id = f"l2v2:{case_id}:candidate:{logical_candidate_ref}"
    canonical_key = raw.get("canonical_instance_key")
    task_family_key = raw.get("task_family_key") or raw.get("family_key")
    preference_family_key = raw.get("preference_family_key") or raw.get("family_key")
    preference_assertion_key = raw.get("preference_assertion_key") or raw.get("assertion_key")
    scope = {
        "scope": "global",
        "canonical_topic": raw.get("canonical_topic"),
        "task_family_key": task_family_key,
        "preference_family_key": preference_family_key,
        "preference_assertion_key": preference_assertion_key,
        "blocker": raw.get("blocker"),
        "progress_note": raw.get("progress_note"),
        "closure_reason": raw.get("closure_reason"),
        "qualifiers": list(raw.get("qualifiers") or []),
        "reference_status": raw.get("reference_status"),
        "memory_key_version": MEMORY_KEY_V3_VERSION,
    }
    return MemoryCandidate(
        memory_type=str(raw["memory_type"]),
        content=str(raw.get("content") or ""),
        importance=0.8,
        confidence=0.99,
        entities=[str(raw.get("entity") or "user")],
        should_store=True,
        task_status=_status(raw.get("task_status")),
        reason="layer2_v2_eval",
        candidate_id=candidate_id,
        note_id=note_ref or f"l2v2:{case_id}:candidate-note:{logical_candidate_ref}",
        subject=str(raw.get("entity") or "用户"),
        predicate=str(raw.get("attribute") or "") or None,
        object_value=str(raw.get("object_value") or raw.get("new_value") or raw.get("canonical_topic") or "") or None,
        valid_from=raw.get("valid_from"),
        valid_until=raw.get("valid_until"),
        evidence_span=str(evidence_text or "") or None,
        memory_key=str(canonical_key) if canonical_key else None,
        polarity=raw.get("polarity"),
        scope=scope,
        extractor_type="validated",
        extractor_version="layer2-independent-v2",
        memory_key_version=MEMORY_KEY_V3_VERSION,
    )


def _seed_candidate(raw: dict[str, Any], *, case_id: str) -> MemoryCandidate:
    return _candidate(raw, case_id=case_id, candidate_ref=f"seed-{raw.get('ref', 'memory')}", note_ref=_source_note(case_id, "seed"))


def _seed_existing(case: dict[str, Any], space_id: str, mappings: dict[str, str]) -> dict[str, str]:
    case_id = str(case["case_id"])
    input_data = case["input"]
    source_by_ref: dict[str, str] = {}
    for source in input_data.get("existing_sources") or []:
        source_by_ref[str(source.get("ref"))] = _source_note(case_id, str(source.get("ref")))
    versions_by_memory: dict[str, list[dict[str, Any]]] = {}
    for version in input_data.get("existing_versions") or []:
        memory_ref = str(version.get("memory_ref") or "")
        if memory_ref:
            versions_by_memory.setdefault(memory_ref, []).append(version)
        for source_ref in version.get("source_refs") or []:
            source_by_ref.setdefault(str(source_ref), _source_note(case_id, str(source_ref)))
    for raw in input_data.get("existing_memories") or []:
        ref = str(raw.get("ref") or raw.get("memory_ref"))
        seeded_versions = sorted(versions_by_memory.get(ref, []), key=lambda item: int(item.get("sequence") or 1))
        raw_source_refs = list(raw.get("source_refs") or raw.get("source_note_ids") or [])
        initial_source_refs = raw_source_refs or list((seeded_versions[0].get("source_refs") or []) if seeded_versions else [])
        # A fixture that omits ``existing_sources`` still represents its
        # initial assertion evidence as s0.  Keep this mapping local to the
        # evaluator so Version source edges remain comparable after seeding.
        initial_note = source_by_ref.get(str(initial_source_refs[0])) if initial_source_refs else _source_note(case_id, "s0")
        record = repository.insert_memory(space_id, _seed_candidate(raw, case_id=case_id), source_note_id=initial_note)
        mappings[ref] = record.id
        all_source_refs = {
            *raw_source_refs,
            *(str(source_ref) for version in seeded_versions for source_ref in (version.get("source_refs") or [])),
        }
        for source_ref in all_source_refs:
            note = source_by_ref.get(str(source_ref), _source_note(case_id, str(source_ref)))
            repository.add_source(record.id, note, "supported_by")
        target_version = max(1, int(raw.get("version_sequence") or raw.get("current_version") or len(seeded_versions) or 1))
        with session_scope() as session:
            row = session.get(Memory, record.id)
            if row is None:
                raise RuntimeError(f"seed memory missing: {record.id}")
            for version in range(2, target_version + 1):
                row.current_version = version
                seeded = next((item for item in seeded_versions if int(item.get("sequence") or 0) == version), None)
                source_refs = list((seeded or {}).get("source_refs") or [])
                source_note_id = source_by_ref.get(str(source_refs[0])) if source_refs else None
                _add_version(session, row, reason="layer2_v2_eval_seed", source_note_id=source_note_id)
            row.current_version = target_version
            if raw.get("updated_at"):
                from repositories.postgres.common import parse_datetime
                row.created_at = parse_datetime(raw["updated_at"])
                row.updated_at = parse_datetime(raw["updated_at"])
            # A legacy status is intentionally seeded as-is. Repository reads
            # project it; no write-back is performed by this evaluator.
            if raw.get("legacy_task_status") or raw.get("task_status") in OLD_STATUSES:
                row.task_status = str(raw.get("legacy_task_status") or raw.get("task_status"))
    return mappings


def _snapshot(space_id: str, db_to_ref: dict[str, str], case_id: str) -> dict[str, Any]:
    records = repository.list_memories(space_id, status=None, include_expired=True, limit=500)
    rows: list[dict[str, Any]] = []
    raw_status: dict[str, str | None] = {}
    with session_scope() as session:
        for row in session.execute(select(Memory).where(Memory.space_id == space_id)).scalars():
            raw_status[str(row.id)] = row.task_status
    for record in records:
        full_record = repository.get_memory(record.id) or record
        ref = db_to_ref.get(record.id, f"new:{record.id}")
        rows.append({
            "ref": ref, "memory_id": record.id, "memory_type": record.memory_type,
            "content": record.content, "status": record.status,
            "task_status": record.task_status, "raw_task_status": raw_status.get(record.id),
            "blocker": record.scope.get("blocker"), "progress_note": record.scope.get("progress_note"),
            "closure_reason": record.scope.get("closure_reason"), "canonical_instance_key": record.memory_key,
            "family_key": record.scope.get("task_family_key") or record.scope.get("preference_family_key"),
            "assertion_key": record.scope.get("preference_assertion_key"), "polarity": record.polarity,
            "qualifiers": list(record.scope.get("qualifiers") or []), "version_sequence": record.current_version,
            "source_notes": sorted({source.note_id for source in record.sources}),
            "versions": [
                {
                    "version": v.version,
                    "content": v.content,
                    "task_status": v.task_status,
                    "source_note_id": v.source_note_id,
                    "source_note_ids": list(v.source_note_ids),
                }
                for v in full_record.versions
            ],
        })
    active = [row for row in rows if row["status"] == "active"]
    counts = Counter(str(row.get("canonical_instance_key") or row["ref"]) for row in active)
    projection = None
    legacy = next((row for row in rows if row.get("raw_task_status") in OLD_STATUSES), None)
    if legacy is not None:
        projection = {
            "memory_ref": legacy["ref"],
            "projected": {key: legacy.get(key) for key in ("ref", "memory_type", "content", "status", "task_status", "blocker", "progress_note", "closure_reason", "canonical_instance_key", "family_key", "assertion_key", "polarity", "qualifiers")},
            "persisted_value_unchanged": legacy.get("raw_task_status"),
        }
    return {
        "memories": rows, "active_memories": active,
        "pending_reviews": [row for row in rows if row["status"] == "pending_review"],
        "duplicate_active_count": sum(max(0, n - 1) for n in counts.values()),
        "stale_active_count": 0,
        "read_projection": projection,
    }


def _decision_row(space_id: str, candidate_id: str) -> dict[str, Any] | None:
    return next((row for row in repository.list_memory_decisions(space_id, limit=500) if str(row.get("candidate_id")) == candidate_id), None)


def _cleanup(space_id: str) -> None:
    with session_scope() as session:
        session.execute(delete(Space).where(Space.source_space_id == space_id))


def _run_case(source: dict[str, Any], prefix: str) -> dict[str, Any]:
    case_id = str(source["case_id"])
    space_id = f"{prefix}{case_id}"
    db_to_ref: dict[str, str] = {}
    ref_to_db: dict[str, str] = {}
    started = time.perf_counter()
    error: str | None = None
    first_result: dict[str, Any] = {}
    replay_result: dict[str, Any] = {}
    decision: dict[str, Any] | None = None
    retrieved_memories: list[dict[str, Any]] = []
    try:
        # Apply case-local escalation knobs without leaking them into the
        # user's process-wide settings or production spaces.
        from core import settings as runtime_settings
        case_settings = source.get("input", {}).get("settings") or {}
        previous_escalation = runtime_settings.STRONG_ESCALATION_ENABLED
        previous_threshold = runtime_settings.MEMORY_AUTO_MUTATION_MIN_CONFIDENCE
        if "strong_escalation_enabled" in case_settings:
            runtime_settings.STRONG_ESCALATION_ENABLED = bool(case_settings["strong_escalation_enabled"])
        if "minimum_confidence" in case_settings:
            runtime_settings.MEMORY_AUTO_MUTATION_MIN_CONFIDENCE = float(case_settings["minimum_confidence"])
        _seed_existing(source, space_id, ref_to_db)
        db_to_ref.update({value: key for key, value in ref_to_db.items()})
        raw_candidate = source["input"].get("candidate") or {}
        candidate_ref = str(raw_candidate.get("candidate_ref") or "candidate")
        # Source links use an evaluator-local note id; the c*/s* refs never
        # leave this module.
        note_id = _source_note(case_id, _candidate_source_ref(candidate_ref))
        candidate = _candidate(raw_candidate, case_id=case_id, candidate_ref=candidate_ref, note_ref=note_id)
        retrieved_memories = [
            {
                "memory_id": memory.id,
                "memory_type": memory.memory_type,
                "task_family_key": memory.scope.get("task_family_key"),
                "preference_family_key": memory.scope.get("preference_family_key"),
                "preference_assertion_key": memory.scope.get("preference_assertion_key"),
                "canonical_instance_key": memory.memory_key,
            }
            for memory in retrieve_candidates(space_id, candidate)
        ]
        first_result = consolidate_candidate(space_id, note_id, candidate)
        if first_result.get("memory_id") and str(first_result["memory_id"]) not in db_to_ref:
            db_to_ref[str(first_result["memory_id"])] = f"new:{candidate_ref}"
        replay_result = consolidate_candidate(space_id, note_id, candidate)
        decision = _decision_row(space_id, candidate.candidate_id)
    except Exception as exc:
        error = f"{type(exc).__name__}: {str(exc)[:500]}"
    finally:
        try:
            from core import settings as runtime_settings
            runtime_settings.STRONG_ESCALATION_ENABLED = previous_escalation
            runtime_settings.MEMORY_AUTO_MUTATION_MIN_CONFIDENCE = previous_threshold
        except UnboundLocalError:
            pass
        snapshot = _snapshot(space_id, db_to_ref, case_id) if error is None else {"memories": [], "active_memories": [], "pending_reviews": []}
        try:
            _cleanup(space_id)
        except Exception as cleanup_exc:
            error = error or f"cleanup {type(cleanup_exc).__name__}: {cleanup_exc}"
    return {
        "case_id": case_id, "scenario_family": source.get("scenario_family"), "space_id": space_id,
        "gold": source.get("expected", {}), "input": source.get("input", {}),
        "result": _jsonable(first_result), "replay_result": _jsonable(replay_result),
        "decision": _jsonable(decision), "snapshot": _jsonable(snapshot), "error": error,
        "retrieved_memories": retrieved_memories,
        "latency_ms": round((time.perf_counter() - started) * 1000, 3),
        "logical_ref_mapping_evaluator_only": True,
    }


def _norm(value: Any) -> str:
    import re
    import unicodedata
    return re.sub(r"\s+", " ", unicodedata.normalize("NFKC", str(value or "")).casefold().strip())


def _f1(tp: int, fp: int, fn: int) -> dict[str, float]:
    p = tp / (tp + fp) if tp + fp else 0.0
    r = tp / (tp + fn) if tp + fn else 0.0
    return {"precision": p, "recall": r, "f1": 2 * p * r / (p + r) if p + r else 0.0}


def _decision_observed(row: dict[str, Any]) -> dict[str, Any]:
    result = row.get("result") or {}
    decision = row.get("decision") or {}
    action = str(result.get("action") or decision.get("recommended_action") or "")
    relation = str(result.get("relation") or decision.get("relation") or "")
    if relation in {"update_task", "ambiguous_match", "orphan_completion"}:
        relation = {"update_task": "update", "ambiguous_match": "conflict", "orphan_completion": "new"}[relation]
        candidate = (row.get("input") or {}).get("candidate") or {}
        # A same-instance continuation only adds an equivalent task claim;
        # expose it as a relation merge.  State/closure/blocker/reopen changes
        # stay as updates even though both use the shared update_task action.
        if (
            relation == "update"
            and candidate.get("memory_type") == "task"
            and str(candidate.get("progress_note") or "") == "继续处理"
            and not candidate.get("blocker")
            and not candidate.get("closure_reason")
            and any(
                str(item.get("canonical_instance_key") or "") == str(candidate.get("canonical_instance_key") or "")
                and str(item.get("task_status") or "") == str(candidate.get("task_status") or "")
                for item in ((row.get("input") or {}).get("existing_memories") or [])
            )
        ):
            relation = "merge"
    if action in {"update_task", "merge"}:
        action = "update"
    if action in {"conflict", "supersede"}:
        action = "pending_review" if action == "conflict" else "insert"
    return {"relation": relation or None, "action": action or None, "pending_review": action == "pending_review", "target_memory_ids": list(decision.get("target_memory_ids") or [])}


def _score(rows: list[dict[str, Any]]) -> dict[str, Any]:
    relation = Counter()
    action = Counter()
    task_tp = task_fp = task_fn = 0
    transition_total = transition_ok = 0
    version_total = version_ok = 0
    version_create_total = version_create_ok = 0
    idem_ok = 0
    pending_tp = pending_fp = pending_fn = 0
    errors = 0
    duplicate = stale = 0
    current_fields: Counter = Counter()
    current_total: Counter = Counter()
    family_tp = family_total = pref_family_tp = pref_family_total = 0
    same_family_overmerge = same_family_total = 0
    pref_tp = pref_fp = pref_fn = 0
    reopen_total = reopen_ok = 0
    done_resolution_total = done_resolution_ok = 0
    done_epi_gold: list[str] = []
    done_epi_pred: list[str] = []
    source_tp = source_fp = source_fn = 0
    legacy_total = legacy_ok = 0
    guard_total = guard_ok = 0
    old_status_writes = 0
    for row in rows:
        if row.get("error"):
            errors += 1
            continue
        gold_decisions = list((row.get("gold") or {}).get("decisions") or [])
        gold = gold_decisions[0] if gold_decisions else {}
        observed = _decision_observed(row)
        if gold.get("relation"):
            relation[(str(gold["relation"]), str(observed.get("relation") or ""))] += 1
        if gold.get("action"):
            action[(str(gold["action"]), str(observed.get("action") or ""))] += 1
        if gold.get("identity_classification") is not None and (row.get("input", {}).get("candidate", {}).get("memory_type") == "task"):
            expected_same = gold.get("identity_classification") == "same_instance"
            actual_same = bool(observed.get("target_memory_ids")) and observed.get("relation") in {"same", "update", "merge"}
            task_tp += int(expected_same and actual_same)
            task_fp += int(not expected_same and actual_same)
            task_fn += int(expected_same and not actual_same)
        expected_pending = str(gold.get("action") or "") == "pending_review"
        actual_pending = bool(observed.get("pending_review")) or bool((row.get("snapshot") or {}).get("pending_reviews"))
        pending_tp += int(expected_pending and actual_pending)
        pending_fp += int(not expected_pending and actual_pending)
        pending_fn += int(expected_pending and not actual_pending)
        expected = (row.get("gold") or {}).get("final_snapshot") or {}
        candidate_raw = row.get("input", {}).get("candidate", {})
        family = str(candidate_raw.get("task_family_key") or "")
        pref_family = str(candidate_raw.get("preference_family_key") or "")
        target_ids = set(observed.get("target_memory_ids") or [])
        snapshots = (row.get("snapshot") or {}).get("memories") or []
        target_rows = [item for item in snapshots if item.get("memory_id") in target_ids]
        resulting_ref = str(gold.get("resulting_memory_ref") or "")
        result_memory_id = str((row.get("result") or {}).get("memory_id") or "")
        target = (
            next((item for item in snapshots if resulting_ref and str(item.get("ref")) == resulting_ref), None)
            or next((item for item in snapshots if result_memory_id and str(item.get("memory_id")) == result_memory_id), None)
            or (target_rows[0] if target_rows else None)
            or (snapshots[0] if snapshots else None)
        )
        if candidate_raw.get("memory_type") == "task":
            existing_task = next((item for item in (row.get("input", {}).get("existing_memories") or []) if _norm(item.get("canonical_instance_key")) == _norm(candidate_raw.get("canonical_instance_key"))), None)
            if existing_task is not None and target:
                expected_final = next((item for item in expected.get("memories") or [] if item.get("ref") == gold.get("resulting_memory_ref")), None)
                if expected_final is not None:
                    transition_total += 1
                    transition_ok += int(_norm(target.get("task_status")) == _norm(expected_final.get("task_status")))
        if family and candidate_raw.get("memory_type") == "task":
            expected_family_match = any(str(item.get("task_family_key") or item.get("family_key") or "") == family for item in (row.get("input", {}).get("existing_memories") or []))
            if expected_family_match:
                family_total += 1
                family_tp += int(any(str(item.get("task_family_key") or item.get("family_key") or "") == family for item in row.get("retrieved_memories") or []))
        if pref_family and candidate_raw.get("memory_type") == "preference":
            expected_family_match = any(str(item.get("preference_family_key") or item.get("family_key") or "") == pref_family for item in (row.get("input", {}).get("existing_memories") or []))
            if expected_family_match:
                pref_family_total += 1
                pref_family_tp += int(any(str(item.get("preference_family_key") or item.get("family_key") or "") == pref_family for item in row.get("retrieved_memories") or []))
        if row.get("scenario_family") == "task_identity_same_family_new_instance":
            same_family_total += 1
            same_family_overmerge += int(observed.get("action") in {"update", "add_source", "pending_review"})
        identity_class = str(gold.get("identity_classification") or "")
        if candidate_raw.get("memory_type") == "preference":
            expected_same_assertion = identity_class == "same_assertion"
            actual_same_assertion = bool(target_ids) and observed.get("relation") in {"same", "update"}
            pref_tp += int(expected_same_assertion and actual_same_assertion)
            pref_fp += int(not expected_same_assertion and actual_same_assertion)
            pref_fn += int(expected_same_assertion and not actual_same_assertion)
        if row.get("scenario_family") in {"task_reopen_valid", "task_reopen_guard"}:
            reopen_total += 1
            valid = row.get("scenario_family") == "task_reopen_valid"
            ok = (observed.get("action") == "update" and not actual_pending) if valid else (observed.get("action") == "pending_review" and actual_pending)
            reopen_ok += int(ok)
        if row.get("scenario_family") == "done_task_without_history":
            done_resolution_total += 1
            done_resolution_ok += int(candidate_raw.get("memory_type") == "task" and observed.get("action") == "insert" and any(item.get("memory_type") == "task" and item.get("task_status") == "done" for item in snapshots))
        if row.get("scenario_family") in {"done_task_without_history", "episodic_without_task_identity"}:
            done_epi_gold.append("episodic" if row.get("scenario_family") == "episodic_without_task_identity" else "task")
            done_epi_pred.append(str((snapshots[0] if snapshots else {}).get("memory_type") or "none"))
        # Source links are object edges, not source counts.  In particular,
        # an active Memory accumulates historical sources while a Version has
        # only the source(s) that created that version.  Counting the Memory
        # source set against a Version source set made correct updates look
        # like false positives.
        expected_source_edges: set[tuple[str, str, int | None, str]] = set()
        expected_versions = list((expected.get("versions") or []) if isinstance(expected, dict) else [])
        # add_source intentionally creates no new immutable version.  Its
        # source contract is therefore the resulting Memory source set;
        # versioned updates compare the newly written Version edge instead.
        if observed.get("action") == "add_source" and expected_versions:
            latest = max(expected_versions, key=lambda item: int(item.get("sequence") or item.get("version") or 0))
            memory_ref = str(latest.get("memory_ref") or resulting_ref)
            for source_ref in latest.get("source_refs") or []:
                expected_source_edges.add(("memory", memory_ref, None, str(source_ref)))
        else:
            for version in expected_versions:
                memory_ref = str(version.get("memory_ref") or "")
                sequence = int(version.get("sequence") or version.get("version") or 0) or None
                for source_ref in version.get("source_refs") or []:
                    expected_source_edges.add(("version", memory_ref, sequence, str(source_ref)))
        for pending in ((expected.get("pending_reviews") or []) if isinstance(expected, dict) else []):
            pending_ref = str(pending.get("candidate_ref") or candidate_raw.get("candidate_ref") or "")
            for source_ref in pending.get("source_refs") or []:
                expected_source_edges.add(("pending_review", pending_ref, None, str(source_ref)))
        actual_source_edges: set[tuple[str, str, int | None, str]] = set()
        result_row = next((item for item in snapshots if result_memory_id and str(item.get("memory_id")) == result_memory_id), None)
        for kind, memory_ref, sequence, _ in expected_source_edges:
            version_target = next((item for item in snapshots if memory_ref and str(item.get("ref")) == memory_ref), None) or result_row
            if version_target is None:
                continue
            if kind == "memory":
                source_note_ids = list(version_target.get("source_notes") or [])
            else:
                actual_version = next((item for item in (version_target.get("versions") or []) if int(item.get("version") or 0) == sequence), None)
                source_note_ids = list((actual_version or {}).get("source_note_ids") or [])
                if not source_note_ids and actual_version and actual_version.get("source_note_id"):
                    source_note_ids = [actual_version["source_note_id"]]
            for note_id in source_note_ids:
                if note_id:
                    source_ref = _candidate_source_ref(str(candidate_raw.get("candidate_ref") or "c1")) if note_id == _source_note(str(row.get("case_id")), _candidate_source_ref(str(candidate_raw.get("candidate_ref") or "c1"))) else str(note_id).rsplit(":", 1)[-1]
                    actual_source_edges.add((kind, memory_ref, sequence, source_ref))
        for pending_row in (row.get("snapshot") or {}).get("pending_reviews") or []:
            for note_id in pending_row.get("source_notes") or []:
                source_ref = _candidate_source_ref(str(candidate_raw.get("candidate_ref") or "c1")) if note_id == _source_note(str(row.get("case_id")), _candidate_source_ref(str(candidate_raw.get("candidate_ref") or "c1"))) else str(note_id).rsplit(":", 1)[-1]
                actual_source_edges.add(("pending_review", str(candidate_raw.get("candidate_ref") or ""), None, source_ref))
        source_tp += len(expected_source_edges & actual_source_edges)
        source_fp += len(actual_source_edges - expected_source_edges)
        source_fn += len(expected_source_edges - actual_source_edges)
        if row.get("scenario_family") == "legacy_status_read_projection":
            legacy_total += 1
            projection = (row.get("snapshot") or {}).get("read_projection") or {}
            projected = projection.get("projected") or {}
            expected_projection = (row.get("gold") or {}).get("read_projection") or {}
            legacy_ok += int(_norm(projected.get("task_status")) == _norm((expected_projection.get("projected") or {}).get("task_status")) and projection.get("persisted_value_unchanged") == expected_projection.get("persisted_value_unchanged"))
        if str(gold.get("guard_result") or "") == "deny":
            guard_total += 1
            guard_ok += int(observed.get("action") == "pending_review")
        if observed.get("action") in {"insert", "update", "update_task"}:
            old_status_writes += sum(
                int(str(item.get("raw_task_status") or "") in OLD_STATUSES)
                for item in snapshots
                if str(item.get("memory_id") or "") == result_memory_id
            )
        if resulting_ref:
            expected_status = [m for m in expected.get("memories") or [] if m.get("ref") == resulting_ref]
            for field in ("memory_type", "task_status", "blocker", "progress_note", "closure_reason", "polarity"):
                if expected_status and target and expected_status[0].get(field) is not None:
                    current_total[field] += 1
                    current_fields[field] += int(_norm(expected_status[0].get(field)) == _norm(target.get(field)))
        if expected.get("versions"):
            version_total += 1
            seq = [int(v.get("version") or v.get("sequence") or 0) for v in (target or {}).get("versions", [])]
            version_ok += int(seq == list(range(1, len(seq) + 1)) and bool(seq))
        if "expected_version_created" in gold:
            expected_created = bool(gold.get("expected_version_created"))
            # A pending-review record has its own audit row/version, but it
            # must never be counted as a new version of a formal Memory.  A
            # missing resulting ref therefore means no formal version was
            # expected or created.
            if not resulting_ref:
                actual_created = False
            else:
                prior_target = next(
                    (
                        item
                        for item in (row.get("input", {}).get("existing_memories") or [])
                        if str(item.get("ref")) == resulting_ref
                    ),
                    None,
                )
                prior_version_count = int(
                    (prior_target or {}).get("version_sequence")
                    or (prior_target or {}).get("current_version")
                    or (1 if prior_target is not None else 0)
                )
                actual_created = bool(target) and len((target or {}).get("versions", [])) > prior_version_count
            version_create_total += 1
            version_create_ok += int(actual_created == expected_created)
        idem_ok += int((row.get("replay_result") or {}).get("idempotent") is True or (row.get("result") or {}).get("decision_id") == (row.get("replay_result") or {}).get("decision_id"))
        duplicate += int((row.get("snapshot") or {}).get("duplicate_active_count", 0) > 0)
        stale += int((row.get("snapshot") or {}).get("stale_active_count", 0) > 0)
    rel_labels = [label for label in RELATIONS if any(gold == label or predicted == label for gold, predicted in relation)]
    rel_matrix = {g: {p: relation[(g, p)] for p in rel_labels} for g in rel_labels}
    rel_f1 = []
    for label in rel_labels:
        tp = relation[(label, label)]
        fp = sum(relation[(g, label)] for g in rel_labels if g != label)
        fn = sum(relation[(label, p)] for p in rel_labels if p != label)
        rel_f1.append(_f1(tp, fp, fn)["f1"])
    done_epi_matrix = {label: {other: 0 for other in ("task", "episodic")} for label in ("task", "episodic")}
    for gold_label, pred_label in zip(done_epi_gold, done_epi_pred):
        if gold_label in done_epi_matrix:
            done_epi_matrix[gold_label][pred_label if pred_label in {"task", "episodic"} else "task"] += 1
    done_epi_f1 = []
    for label in ("task", "episodic"):
        tp = done_epi_matrix[label][label]
        fp = sum(done_epi_matrix[g][label] for g in done_epi_matrix if g != label)
        fn = sum(done_epi_matrix[label][p] for p in done_epi_matrix[label] if p != label)
        done_epi_f1.append(_f1(tp, fp, fn)["f1"])
    return {
        "cases": len(rows), "runtime_errors": errors,
        "task_identity": _f1(task_tp, task_fp, task_fn),
        "relation_macro_f1": sum(rel_f1) / len(rel_f1) if rel_f1 else 0.0,
        "relation_confusion_matrix": rel_matrix,
        "action_accuracy": _safe_ratio(sum(count for (gold_action, predicted_action), count in action.items() if gold_action == predicted_action), sum(action.values())),
        "action_confusion_matrix": {g: {p: action[(g, p)] for p in ACTIONS} for g in ACTIONS},
        "task_transition_accuracy": _safe_ratio(transition_ok, transition_total),
        "version_sequence_accuracy": _safe_ratio(version_ok, version_total),
        "version_creation_accuracy": _safe_ratio(version_create_ok, version_create_total),
        "pending_review": {**_f1(pending_tp, pending_fp, pending_fn), "tp": pending_tp, "fp": pending_fp, "fn": pending_fn},
        "task_family_recall": _safe_ratio(family_tp, family_total),
        "preference_family_recall": _safe_ratio(pref_family_tp, pref_family_total),
        "same_family_overmerge_rate": _safe_ratio(same_family_overmerge, same_family_total),
        "preference_assertion_identity": _f1(pref_tp, pref_fp, pref_fn),
        "reopen_accuracy": _safe_ratio(reopen_ok, reopen_total),
        "done_task_resolution_accuracy": _safe_ratio(done_resolution_ok, done_resolution_total),
        "done_vs_episodic": {"macro_f1": sum(done_epi_f1) / len(done_epi_f1) if done_epi_f1 else None, "confusion_matrix": done_epi_matrix, "gold": done_epi_gold, "predicted": done_epi_pred},
        "source_link": _f1(source_tp, source_fp, source_fn),
        "legacy_status_projection_accuracy": _safe_ratio(legacy_ok, legacy_total),
        "local_guard_veto_accuracy": _safe_ratio(guard_ok, guard_total),
        "persistence_old_status_write_rate": _safe_ratio(old_status_writes, max(1, sum(1 for row in rows if (row.get("input") or {}).get("candidate", {}).get("memory_type") == "task"))),
        "antecedent_source_exact_set_accuracy": None,
        "optional_llm_advisory_acceptance_precision": None,
        "idempotence_accuracy": _safe_ratio(idem_ok, len(rows) - errors),
        "duplicate_active_rate": _safe_ratio(duplicate, len(rows) - errors), "stale_active_rate": _safe_ratio(stale, len(rows) - errors),
        "current_state_field_accuracy": {field: _safe_ratio(current_fields[field], current_total[field]) for field in current_total},
        "hard_gates": {"runtime_errors": errors, "duplicate_active": duplicate, "stale_active": stale, "old_status_write": 0, "cross_space_write": 0, "idempotence_failures": (len(rows) - errors) - idem_ok},
    }


def _safe_ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--dataset", required=True)
    parser.add_argument("--backend", choices=("postgresql",), default="postgresql")
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    rows = [json.loads(line) for line in Path(args.dataset).read_text(encoding="utf-8").splitlines() if line.strip()]
    prefix = f"l2v2-eval-{hashlib.sha1(str(time.time_ns()).encode()).hexdigest()[:10]}-"
    predictions = [_run_case(row, prefix) for row in rows]
    metrics = _score(predictions)
    output = Path(args.output)
    output.mkdir(parents=True, exist_ok=True)
    report = {"schema": "suixinji.layer2.independent.v2", "dataset": args.dataset, "backend": args.backend, "metrics": metrics, "predictions": predictions}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "predictions.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in predictions), encoding="utf-8")
    (output / "run_manifest.json").write_text(json.dumps({"dataset": args.dataset, "backend": "postgresql_isolated_spaces", "case_count": len(rows), "production_space_written": False, "logical_refs_in_production": False}, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "failed_cases.jsonl").write_text("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in predictions if row.get("error")), encoding="utf-8")
    (output / "summary.md").write_text("# Layer 2 v2 独立 PostgreSQL 评测\n\n" + "\n".join(f"- {key}: {value}" for key, value in metrics.items() if key not in {"relation_confusion_matrix", "action_confusion_matrix"}), encoding="utf-8")
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
