"""Isolated adapter from validated JSONL cases to the real consolidator."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from memory import repository
from memory.consolidator import consolidate_candidate
from memory.models import MEMORY_KEY_V3_VERSION, MemoryCandidate

from .mappings import normalize_action, normalize_relation


def _task_status(value: Any) -> str | None:
    status = str(value or "").casefold() or None
    return {"in_progress": "todo", "blocked": "todo", "cancelled": "done", "canceled": "done"}.get(status, status)


def _candidate(raw: dict[str, Any]) -> MemoryCandidate:
    scope = {
        "scope": "global",
        "operation": raw.get("operation"),
        "canonical_topic": raw.get("canonical_topic"),
        "old_value": raw.get("old_value"),
        "new_value": raw.get("new_value"),
        "observed_at": raw.get("observed_at"),
        "memory_key_version": MEMORY_KEY_V3_VERSION,
    }
    object_value = raw.get("new_value") or raw.get("attribute") or raw.get("canonical_topic")
    return MemoryCandidate(
        memory_type=str(raw["memory_type"]),
        content=str(raw.get("content") or raw.get("note_text") or ""),
        importance=0.8,
        confidence=0.99,
        task_status=_task_status(raw.get("task_status")),
        reason=raw.get("reason"),
        candidate_id=str(raw["candidate_id"]),
        note_id=str(raw.get("note_id") or raw["candidate_id"]),
        subject=raw.get("entity"),
        predicate=raw.get("attribute"),
        object_value=object_value,
        valid_from=raw.get("valid_from"),
        valid_until=raw.get("valid_until"),
        evidence_span=raw.get("evidence_span"),
        memory_key=raw.get("memory_key"),
        polarity=raw.get("polarity"),
        scope=scope,
        extractor_type="validated",
        extractor_version="layer2-validated-candidate-v1",
        memory_key_version=MEMORY_KEY_V3_VERSION,
    )


def _seed_candidate(raw: dict[str, Any]) -> MemoryCandidate:
    return _candidate(
        {
            **raw,
            "candidate_id": f"seed:{raw.get('memory_ref', 'memory')}",
            "note_id": (raw.get("source_note_ids") or [f"seed:{raw.get('memory_ref', 'memory')}"])[0],
        }
    )


@dataclass
class Stage2EvalAdapter:
    case: dict[str, Any]
    db_path: Path
    run_id: str
    logical_ref_to_db_id: dict[str, str] = field(default_factory=dict)
    db_id_to_logical_ref: dict[str, str] = field(default_factory=dict)
    candidate_by_id: dict[str, MemoryCandidate] = field(default_factory=dict)
    errors: list[dict[str, Any]] = field(default_factory=list)

    @property
    def case_id(self) -> str:
        return str(self.case["case_id"])

    @property
    def space_id(self) -> str:
        return f"space_eval:{self.run_id}:{self.case_id}"

    def reset_case(self) -> None:
        repository.DB_PATH = self.db_path
        repository.init_db()
        self.logical_ref_to_db_id.clear()
        self.db_id_to_logical_ref.clear()
        self.candidate_by_id.clear()
        self.errors.clear()

    def seed_existing_memories(self) -> None:
        for raw in self.case["input"].get("existing_memories", []):
            ref = str(raw["memory_ref"])
            record = repository.insert_memory(
                self.space_id,
                _seed_candidate(raw),
                source_note_id=(raw.get("source_note_ids") or [f"seed:{ref}"])[0],
            )
            self.logical_ref_to_db_id[ref] = record.id
            self.db_id_to_logical_ref[record.id] = ref
            for note_id in (raw.get("source_note_ids") or [])[1:]:
                repository.add_source(record.id, str(note_id), "supported_by")
            self._set_seed_version(record.id, raw)

    def _set_seed_version(self, memory_id: str, raw: dict[str, Any]) -> None:
        target_version = max(1, int(raw.get("version_sequence") or 1))
        with repository._connect() as conn:  # test-only adapter uses the repository schema.
            row = conn.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
            if row is None:
                raise RuntimeError(f"seed memory missing: {memory_id}")
            for version in range(2, target_version + 1):
                repository._add_version(
                    conn,
                    memory_id,
                    version,
                    str(row["content"]),
                    str(row["status"]),
                    task_status=row["task_status"],
                    confidence=float(row["confidence"]),
                    importance=float(row["importance"]),
                    valid_from=row["valid_from"],
                    valid_until=row["valid_until"],
                    reason="layer2_eval_seed",
                    source_note_id=None,
                )
            conn.execute(
                "UPDATE memories SET current_version = ?, created_at = ?, updated_at = ? WHERE id = ?",
                (target_version, raw.get("updated_at") or row["created_at"], raw.get("updated_at") or row["updated_at"], memory_id),
            )

    def build_candidate(self, raw: dict[str, Any]) -> MemoryCandidate:
        candidate = _candidate(raw)
        self.candidate_by_id[candidate.candidate_id] = candidate
        return candidate

    def _map_result_ids(self, candidate_id: str, result: dict[str, Any]) -> None:
        for key in ("memory_id", "target_memory_id"):
            memory_id = result.get(key)
            if not memory_id or memory_id in self.db_id_to_logical_ref:
                continue
            if key == "memory_id" and result.get("action") in {"insert", "pending_review", "supersede"}:
                ref = f"new:{candidate_id}"
                self.logical_ref_to_db_id[ref] = str(memory_id)
                self.db_id_to_logical_ref[str(memory_id)] = ref

    def _decision_for(self, candidate_id: str) -> dict[str, Any] | None:
        decisions = repository.list_memory_decisions(self.space_id, limit=500)
        rows = [row for row in decisions if row["candidate_id"] == candidate_id]
        return rows[0] if rows else None

    def consolidate(self, raw: dict[str, Any]) -> dict[str, Any]:
        candidate = self.build_candidate(raw)
        try:
            result = consolidate_candidate(self.space_id, candidate.note_id or raw["candidate_id"], candidate)
            self._map_result_ids(candidate.candidate_id, result)
            decision = self._decision_for(candidate.candidate_id)
            return {"result": result, "decision": decision}
        except Exception as exc:  # preserve the case for failure diagnosis.
            error = {"type": type(exc).__name__, "message": str(exc), "candidate_id": candidate.candidate_id}
            self.errors.append(error)
            return {"result": {}, "decision": None, "error": error}

    def normalize_decision(self, raw: dict[str, Any], observed: dict[str, Any]) -> dict[str, Any]:
        result = observed.get("result") or {}
        decision = observed.get("decision") or {}
        target_ids = decision.get("target_memory_ids") or []
        target_refs = [self.db_id_to_logical_ref.get(str(item), f"db:{item}") for item in target_ids]
        result_action = result.get("action") or decision.get("recommended_action")
        result_relation = result.get("relation") or decision.get("relation")
        candidate_id = str(raw["candidate_id"])
        memory_id = result.get("memory_id")
        target_ref = None
        if memory_id:
            target_ref = self.db_id_to_logical_ref.get(str(memory_id))
            if target_ref is None and result_action in {"insert", "pending_review", "supersede"}:
                target_ref = f"new:{candidate_id}"
        if normalize_action(result_action) == "pending_review":
            target_ref = None
        return {
            "candidate_id": candidate_id,
            "matched_memory_refs": target_refs,
            "task_identity_match": bool(target_refs) if raw["memory_type"] == "task" else None,
            "relation": normalize_relation(result_relation, result_action),
            "action": normalize_action(result_action),
            "target_memory_ref": target_refs[0] if target_refs else target_ref,
            "final_memory_type": None,
            "final_task_status": None,
            "create_version": None,
            "expected_version_sequence": None,
            "source_link_added": result.get("source_added"),
            "pending_review": normalize_action(result_action) == "pending_review",
            "reason": decision.get("reason") or result.get("reason"),
            "error": observed.get("error"),
        }

    def snapshot_state(self) -> dict[str, Any]:
        records = repository.list_memories(self.space_id, status=None, include_expired=True, limit=500)
        all_records = []
        for record in records:
            ref = self.db_id_to_logical_ref.get(record.id, f"db:{record.id}")
            all_records.append(
                {
                    "memory_ref": ref,
                    "memory_id": record.id,
                    "memory_type": record.memory_type,
                    "memory_key": record.memory_key,
                    "entity": record.subject,
                    "attribute": record.predicate,
                    "operation": record.scope.get("operation"),
                    "canonical_topic": record.scope.get("canonical_topic"),
                    "task_status": record.task_status,
                    "old_value": record.scope.get("old_value"),
                    "new_value": record.scope.get("new_value"),
                    "content": record.content,
                    "status": record.status,
                    "version_sequence": record.current_version,
                    "source_note_ids": sorted({source.note_id for source in record.sources}),
                    "valid_from": record.valid_from,
                    "valid_until": record.valid_until,
                    "polarity": record.polarity,
                    "updated_at": record.updated_at,
                }
            )
        active = [record for record in all_records if record["status"] == "active"]
        counts: dict[str, int] = {}
        for record in active:
            counts[record["memory_key"] or record["memory_ref"]] = counts.get(record["memory_key"] or record["memory_ref"], 0) + 1
        duplicate_active_count = sum(max(0, count - 1) for count in counts.values())
        return {
            "all_memories": all_records,
            "active_memories": active,
            "pending_review_memories": [record for record in all_records if record["status"] == "pending_review"],
            "expected_active_memory_refs": [record["memory_ref"] for record in active],
            "duplicate_active_count": duplicate_active_count,
            "stale_active_count": 0,
        }

    def run(self) -> dict[str, Any]:
        self.reset_case()
        self.seed_existing_memories()
        normalized_decisions = []
        by_id = {raw["candidate_id"]: raw for raw in self.case["input"].get("incoming_candidates", [])}
        for candidate_id in self.case["input"].get("processing_order", []):
            raw = by_id[str(candidate_id)]
            observed = self.consolidate(raw)
            normalized_decisions.append(self.normalize_decision(raw, observed))
        state = self.snapshot_state()
        raw_by_id = {str(row["candidate_id"]): row for row in self.case["input"].get("incoming_candidates", [])}
        for decision in normalized_decisions:
            raw = raw_by_id.get(str(decision["candidate_id"]), {})
            refs = [ref for ref in state["active_memories"] if ref["memory_ref"] == decision.get("target_memory_ref")]
            if refs:
                decision["final_memory_type"] = refs[0]["memory_type"]
                decision["final_task_status"] = refs[0]["task_status"]
                decision["create_version"] = decision.get("action") in {"insert", "update"}
                decision["expected_version_sequence"] = refs[0]["version_sequence"]
                decision["source_link_added"] = str(raw.get("note_id") or raw.get("candidate_id")) in refs[0].get("source_note_ids", [])
            else:
                decision["create_version"] = False
                decision["source_link_added"] = False
        return {
            "case_id": self.case_id,
            "dataset": self.case.get("dataset"),
            "space_id": self.space_id,
            "gold": self.case["expected_output"],
            "predicted_decisions": normalized_decisions,
            "predicted_state": state,
            "errors": self.errors,
        }
