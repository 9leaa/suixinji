"""Evidence-first semantic/event memory, isolated from task and preference writers.

Uses the existing transactional workflow store on PostgreSQL and SQLite. All keys
are namespaced fm_*; cards, facts, runs, approvals and versions commit together.
"""

from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import re
import uuid
from zoneinfo import ZoneInfo

from core import settings
from memory import workflow_state as store
from memory.access import memory_access_allowed

TYPES = ("semantic", "episodic")


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def key(*parts):
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False, sort_keys=True, default=str).encode()).hexdigest()[:24]


def items(space, kind):
    return store.list_items(space, "fm_" + kind)


def get(space, kind, ident):
    return store.get(space, "fm_" + kind, ident)


def active_facts(space):
    return [f for f in items(space, "fact") if f["status"] == "active"]


def cards(space):
    return [c for c in items(space, "card") if c["status"] == "active"]


def mask_note(space, note):
    """Suppress forgotten spans in model evidence; never modify the stored Note."""
    spans = [t["evidence_span"] for t in items(space, "tombstone") if t["note_id"] == note.get("id")]
    if not spans:
        return note
    result = dict(note)
    for field in ("text", "original_text", "content"):
        if isinstance(result.get(field), str):
            for span in sorted(spans, key=len, reverse=True):
                result[field] = result[field].replace(span, "[已按用户要求遗忘]")
    # Generated metadata may paraphrase forgotten content, so it cannot be reused.
    result.update(title="", summary="", tags=[], fact_forgetting_applied=True)
    return result


def model_evidence(space, row):
    if "note_id" not in row:
        return row
    masked = mask_note(space, {"id": row["note_id"], "original_text": row["original_text"]})
    return {**row, "original_text": masked["original_text"]}


def _meta(tx):
    return tx.get("fm_meta", "state") or {"id": "state", "revision": 0}


def _bump(tx):
    value = _meta(tx)
    value["revision"] += 1
    tx.put("fm_meta", "state", value)


def _save_card(tx, card, reason):
    card["version"] = card.get("version", 0) + 1
    card["updated_at"] = now_iso()
    tx.put("fm_card", card["id"], card)
    tx.put("fm_version", f"{card['id']}:{card['version']:08}", {**deepcopy(card), "reason": reason})


def evidence_scope(facts):
    scopes = [f.get("scope", {}) for f in facts]
    owners = {s["owner_id"] for s in scopes if s.get("owner_id")}
    if len(owners) > 1:
        raise ValueError("cannot merge different owners")
    result = {"owner_id": next(iter(owners))} if owners else {}
    for field, ordered in (
        ("access_scope", ["public", "shared", "owner", "owner_only", "private", "restricted"]),
        ("sensitivity", ["normal", "sensitive", "high", "secret", "restricted"]),
    ):
        values = [s[field] for s in scopes if s.get(field)]
        if values:
            result[field] = max(values, key=lambda x: ordered.index(x) if x in ordered else 0)
    return result


def _invalidate_sources(tx, note_id, revision):
    changed = set()
    for old in tx.list("fm_fact"):
        if old["note_id"] == note_id and old["source_revision"] != revision and old["status"] == "active":
            old["status"] = "source_changed"
            tx.put("fm_fact", old["id"], old)
            changed.add(old["id"])
    for card in tx.list("fm_card"):
        if card["status"] == "active" and set(card["source_fact_ids"]) & changed:
            card["status"] = "invalidated"
            _save_card(tx, card, "source_changed")
            for source in set(card["source_fact_ids"]) - changed:
                other = tx.get("fm_fact", source)
                if other and other["status"] == "active":
                    other["processed_at"] = None
                    tx.put("fm_fact", source, other)
    for question in tx.list("fm_question"):
        if question["status"] == "open" and set(question["source_fact_ids"]) & changed:
            question["status"] = "cancelled"
            tx.put("fm_question", question["id"], question)
    return changed


def sync_source(space, note_id, text):
    """Observe a source revision before extraction, including empty replacements."""
    revision = key(text)
    with store.transaction(space) as tx:
        previous = tx.get("fm_source", note_id)
        invalidated = _invalidate_sources(tx, note_id, revision)
        changed = bool(invalidated or (previous and previous["revision"] != revision))
        if not previous or changed:
            tx.put("fm_source", note_id, {"id": note_id, "revision": revision})
            _bump(tx)
    return changed


def ingest_fact(
    space,
    *,
    note_id,
    original_text,
    content,
    memory_type,
    evidence_span=None,
    entities=(),
    mentioned_at=None,
    time=None,
    reference_state="not_applicable",
    scope=None,
    enqueue=True,
):
    if memory_type not in TYPES:
        raise ValueError("fact memory accepts only semantic/episodic")
    span = evidence_span or original_text
    if not content.strip() or not span or span not in original_text:
        raise ValueError("fact requires verbatim source evidence")
    revision = key(original_text)
    ident = "fact_" + key(note_id, revision, span, memory_type, content.strip())
    with store.transaction(space) as tx:
        existing = tx.get("fm_fact", ident)
        if existing and existing["status"] != "source_changed":
            return existing
        _invalidate_sources(tx, note_id, revision)
        tombstone = any(
            t["note_id"] == note_id and (t["evidence_span"] in span or span in t["evidence_span"]) for t in tx.list("fm_tombstone")
        )
        fact = dict(
            id=ident,
            space_id=space,
            note_id=note_id,
            source_revision=revision,
            original_text=original_text,
            content=content.strip(),
            evidence_span=span,
            evidence_offset=original_text.index(span),
            memory_type=memory_type,
            entities=list(entities),
            mentioned_at=str(mentioned_at or now_iso()),
            ingested_at=now_iso(),
            time=time or {},
            reference_state=reference_state,
            scope=scope or {},
            status="forgotten" if tombstone else "active",
            processed_at=None,
        )
        tx.put("fm_fact", ident, fact)
        for card in tx.list("fm_card"):
            if card["status"] == "active" and score(content, card["content"]) > 0.15:
                card["freshness"] = "stale"
                tx.put("fm_card", card["id"], card)
        _bump(tx)
    if enqueue and fact["status"] == "active":
        enqueue_due(space)
    return fact


def ingest_candidate(candidate, note):
    def value(name, default=None):
        return note.get(name, default) if isinstance(note, dict) else getattr(note, name, default)
    fact = ingest_fact(
        candidate.space_id,
        note_id=value("id"),
        original_text=value("text", ""),
        content=candidate.content,
        memory_type=candidate.memory_type,
        evidence_span=candidate.evidence_span,
        entities=candidate.entities,
        mentioned_at=value("ts") or value("created_at"),
        time={"start": candidate.valid_from, "end": candidate.valid_until, "imprecise": candidate.scope.get("time_evidence", {})},
        reference_state=candidate.scope.get("reference_status", "not_applicable"),
        scope={k: candidate.scope[k] for k in ("owner_id", "sensitivity", "access_scope") if k in candidate.scope},
    )
    reference_ids = candidate.scope.get("context_fact_ids", [])
    if reference_ids or candidate.scope.get("antecedent_note_id"):
        fact["related_fact_ids"] = reference_ids
        fact["reference_evidence"] = {
            k: candidate.scope[k]
            for k in ("context_fact_ids", "antecedent_note_id", "antecedent_evidence_span", "resolution_confidence")
            if k in candidate.scope
        }
        store.put(candidate.space_id, "fm_fact", fact["id"], fact)
    return {"action": "fact_stored", "fact_id": fact["id"], "memory_id": fact["id"]}


def _tokens(text):
    text = str(text).casefold()
    words = re.findall(r"[a-z0-9_-]+", text)
    for run in re.findall(r"[\u4e00-\u9fff]+", text):
        words.extend(run[i : i + 2] for i in range(max(1, len(run) - 1)))
    return set(words)


def score(query, text):
    q, t = _tokens(query), _tokens(text)
    return len(q & t) / max(1, math.sqrt(len(q) * len(t)))


def search_memory(space, query, *, kind=None, limit=30, access_context=None, deduplicate=True):
    rows = active_facts(space) + cards(space)
    rows = [r for r in rows if (not kind or r["memory_type"] == kind) and memory_access_allowed(r, access_context)]
    # A card and its verbatim source are one retrieval result. Keep the card
    # (inspect expands its sources), without collapsing distinct repeated events.
    def normalized(r):
        return re.sub(r"\s+", "", r["content"]).rstrip("。.!！")
    represented = {
        (fid, r["memory_type"], normalized(r)) for r in rows if r.get("assertion_kind") == "stated" for fid in r.get("source_fact_ids", [])
    }
    edited_sources = {fid for r in rows if r.get("review_status") == "edited" for fid in r.get("source_fact_ids", [])}
    if deduplicate:
        rows = [
            r
            for r in rows
            if "source_fact_ids" in r or (r["id"] not in edited_sources and (r["id"], r["memory_type"], normalized(r)) not in represented)
        ]
    from memory.fact_retrieval import rank

    return [model_evidence(space, r) for r in rank(space, query, rows, limit)]


def inspect_memory(space, ids):
    result = []
    for ident in ids:
        row = get(space, "fact", ident) or get(space, "card", ident)
        if row and row["status"] == "active":
            result.append(row)
            for source in row.get("source_fact_ids", []):
                fact = get(space, "fact", source)
                if fact and fact["status"] == "active":
                    result.append(fact)
    return [model_evidence(space, r) for r in {r["id"]: r for r in result}.values()]


def due(space, *, now=None):
    current = now or datetime.now(timezone.utc)
    local = current.astimezone(ZoneInfo("Asia/Shanghai"))
    result = []
    facts = active_facts(space)
    unresolved_ids = {fid for q in items(space, "question") if q["status"] == "open" for fid in q["source_fact_ids"]}
    unreviewed_types = {c["memory_type"] for c in cards(space) if c.get("review_status") == "unreviewed"}
    for kind in TYPES:
        waiting = [f for f in facts if f["memory_type"] == kind and not f["processed_at"]]
        state = get(space, "schedule", kind) or {}
        needs_digest = kind in unreviewed_types or any(f["id"] in unresolved_ids and f["memory_type"] == kind for f in facts)
        if (
            (waiting or needs_digest)
            and local.hour >= settings.FACT_MEMORY_DAILY_HOUR
            and state.get("last_daily") != local.date().isoformat()
        ):
            result.append((kind, "daily"))
        elif len(waiting) >= settings.FACT_MEMORY_THRESHOLD:
            result.append((kind, "threshold"))
    return result


def enqueue_due(space, *, now=None):
    if not settings.MEMORY_AUTO_CONSOLIDATION_ENABLED:
        return []
    scheduled = due(space, now=now)
    from memory.scheduler import is_evaluation_space

    if is_evaluation_space(space):
        return scheduled
    if settings.STORAGE_BACKEND != "postgres":
        return scheduled  # Local runtime calls maintenance directly.
    from repositories.postgres.dispatch import enqueue_task

    for kind, trigger in scheduled:
        pending = [f["id"] for f in active_facts(space) if f["memory_type"] == kind and not f["processed_at"]]
        day = (now or datetime.now(timezone.utc)).astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
        enqueue_task(
            task_type="memory",
            space_id=space,
            idempotency_key="fact-consolidate:" + key(space, kind, pending, trigger, day),
            payload={"operation": "fact_consolidate", "memory_type": kind, "trigger": trigger},
        )
    return scheduled


def _snapshot(space, kind):
    with store.transaction(space) as tx:
        facts = sorted(
            (f for f in tx.list("fm_fact") if f["status"] == "active"), key=lambda f: (f.get("mentioned_at", ""), f["ingested_at"], f["id"])
        )
        memories = [c for c in tx.list("fm_card") if c["status"] == "active"]
        pending = [q for q in tx.list("fm_question") if q["status"] == "open"]
        return {
            "revision": _meta(tx)["revision"],
            "facts": facts,
            "memories": memories,
            "questions": pending,
            "new_facts": [f for f in facts if f["memory_type"] == kind and not f["processed_at"]],
        }


def consolidate(space, memory_type, *, trigger="manual", planner=None, now=None):
    if trigger != "manual" and not settings.MEMORY_AUTO_CONSOLIDATION_ENABLED:
        return {"status": "skipped", "reason": "automatic_consolidation_disabled"}
    if memory_type not in TYPES:
        raise ValueError("unknown memory type")
    clock = now or datetime.now(timezone.utc)
    run_id = "run_" + uuid.uuid4().hex[:20]
    lease_key = "consolidation"  # Both types can touch shared evidence; serialize per space.
    with store.transaction(space) as tx:
        lease = tx.get("fm_lease", lease_key)
        if lease and datetime.fromisoformat(lease["until"]) > datetime.now(timezone.utc):
            return {"status": "busy", "run_id": lease["run_id"]}
        tx.put("fm_lease", lease_key, {"run_id": run_id, "until": (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()})
    snapshot = _snapshot(space, memory_type)
    batch = snapshot["new_facts"][: settings.FACT_MEMORY_BATCH_SIZE]
    run = dict(
        id=run_id,
        memory_type=memory_type,
        trigger=trigger,
        started_at=now_iso(),
        fact_ids=[f["id"] for f in batch],
        status="running",
        trace=[],
    )
    store.put(space, "fm_run", run_id, run)
    try:
        if batch:
            if planner is None:
                from memory.fact_agent import plan

                planner = plan
            plan = planner(space, memory_type, batch, snapshot, run["trace"])
            apply_plan(space, plan, snapshot=snapshot, run_id=run_id, batch_ids=run["fact_ids"])
        else:
            plan = {"actions": []}
        with store.transaction(space) as tx:
            run.update(status="completed", finished_at=now_iso(), plan=plan)
            tx.put("fm_run", run_id, run)
            schedule = tx.get("fm_schedule", memory_type) or {}
            remaining = [
                f for f in tx.list("fm_fact") if f["status"] == "active" and f["memory_type"] == memory_type and not f["processed_at"]
            ]
            if trigger == "daily" and not remaining:
                schedule["last_daily"] = clock.astimezone(ZoneInfo("Asia/Shanghai")).date().isoformat()
            schedule["last_run"] = run_id
            tx.put("fm_schedule", memory_type, schedule)
            questions = [q for q in tx.list("fm_question") if q["status"] == "open"]
            inferences = [
                c
                for c in tx.list("fm_card")
                if c["status"] == "active" and c["assertion_kind"] == "inferred" and c["review_status"] == "unreviewed"
            ]
            digest = dict(id=run_id, run_id=run_id, created_at=now_iso(), questions=questions, inferences=inferences, trigger=trigger)
            tx.put("fm_digest", run_id, digest)
        if trigger == "daily" and not remaining and not any(reason == "daily" for _, reason in due(space, now=clock)):
            queue_digest_notification(space, digest)
        return {**run, "digest": digest}
    except Exception as exc:
        run.update(status="failed", error=f"{type(exc).__name__}: {exc}", finished_at=now_iso())
        store.put(space, "fm_run", run_id, run)
        raise
    finally:
        with store.transaction(space) as tx:
            lease = tx.get("fm_lease", lease_key)
            if lease and lease["run_id"] == run_id:
                tx.put("fm_lease", lease_key, {"run_id": run_id, "until": now_iso()})


class _PreviewRollback(Exception):
    pass


def validate_plan(space, plan, *, snapshot, run_id, batch_ids):
    """Run the exact write contract in a rollback-only transaction."""
    try:
        apply_plan(space, plan, snapshot=snapshot, run_id=run_id, batch_ids=batch_ids, preview=True)
    except _PreviewRollback:
        pass


def apply_plan(space, plan, *, snapshot, run_id, batch_ids, preview=False):
    """Validate the whole proposal and commit all changes atomically."""
    available_facts = {f["id"]: f for f in snapshot["facts"]}
    available_cards = {c["id"]: c for c in snapshot["memories"]}
    actions = plan.get("actions", [])
    covered = {s for a in actions for s in a.get("source_fact_ids", [])}
    if not set(batch_ids) <= covered:
        raise ValueError("plan must account for every new fact, using defer if necessary")
    touched = set()
    with store.transaction(space) as tx:
        lease = tx.get("fm_lease", "consolidation")
        if not lease or lease["run_id"] != run_id or datetime.fromisoformat(lease["until"]) <= datetime.now(timezone.utc):
            raise ValueError("consolidation lease expired or replaced")
        edited = [c for c in tx.list("fm_card") if c["status"] == "active" and c.get("review_status") == "edited"]
        # New facts arriving during the call are allowed; referenced evidence and targets
        # are checked individually. A parallel user edit must not be overwritten.
        for action in actions:
            op = action["action"]
            if op not in {"create", "add_evidence", "revise", "merge", "split", "link", "retract", "resolve_reference", "defer"}:
                raise ValueError("unknown memory tool")
            sources = action.get("source_fact_ids", [])
            if not sources or not set(sources) <= available_facts.keys() or not action.get("reason"):
                raise ValueError("action requires known evidence and reason")
            for source in sources:
                current = tx.get("fm_fact", source)
                if not current or current["status"] != "active" or current["source_revision"] != available_facts[source]["source_revision"]:
                    raise ValueError("source changed or forgotten")
            targets = action.get("target_ids", [])
            if op in {"create", "revise", "merge", "split", "add_evidence"}:
                protected = [c for c in edited if set(sources) & set(c["source_fact_ids"])]
                if protected and not (op == "add_evidence" and targets and set(targets) <= {c["id"] for c in protected}):
                    raise ValueError(
                        "user-edited evidence cannot derive another card; keep the edited card "
                        "and organize only new sources separately: "
                        + json.dumps(
                            [{"id": c["id"], "content": c["content"], "protected_source_ids": c["source_fact_ids"]} for c in protected],
                            ensure_ascii=False,
                        )
                    )
            selected = []
            if op == "link":
                # Relations connect facts and/or cards. A new fact does not
                # need a synthetic card merely to link it to an existing one.
                endpoints = list(dict.fromkeys(sources + targets))
                if len(endpoints) < 2:
                    raise ValueError("link requires two distinct endpoints")
                for ident in targets:
                    old = available_cards.get(ident) or available_facts.get(ident)
                    current = tx.get("fm_card", ident) or tx.get("fm_fact", ident)
                    if not old or not current or current["status"] != "active":
                        raise ValueError("link endpoint is unavailable")
                    if ident not in touched and current.get("version") != old.get("version"):
                        raise ValueError("target version changed")
                tx.put(
                    "fm_relation",
                    key(endpoints),
                    dict(
                        id=key(endpoints),
                        target_ids=targets,
                        source_fact_ids=sources,
                        endpoint_ids=endpoints,
                        relation=action.get("relation", "related"),
                        reason=action["reason"],
                        created_at=now_iso(),
                    ),
                )
                continue
            for ident in targets:
                if ident in touched:
                    raise ValueError(f"duplicate target {ident}; combine its changes into one action")
                old = available_cards.get(ident)
                current = tx.get("fm_card", ident)
                if not old or not current or current["status"] != "active" or current["version"] != old["version"]:
                    raise ValueError("target version changed")
                if current.get("user_locked") and op not in {"add_evidence", "link"}:
                    raise ValueError("user-reviewed memory cannot be automatically rewritten")
                selected.append(deepcopy(current))
            if op not in {"create", "defer", "resolve_reference"} and not selected:
                raise ValueError("tool requires target")
            if op == "merge" and len(selected) < 2:
                raise ValueError("tool requires multiple targets")
            if op == "merge" and len({c["memory_type"] for c in selected}) != 1:
                raise ValueError("different memory types must be linked, not merged")
            if op == "defer":
                question = action.get("question", "").strip()
                if not question:
                    raise ValueError("defer requires a question")
                ident = "question_" + key(sources, question)
                tx.put(
                    "fm_question",
                    ident,
                    dict(
                        id=ident,
                        question=question,
                        source_fact_ids=sources,
                        candidate_ids=targets,
                        status="open",
                        created_at=now_iso(),
                        run_id=run_id,
                    ),
                )
            elif op == "resolve_reference":
                text = action.get("resolution", "").strip()
                if not text:
                    raise ValueError("reference resolution requires text")
                for ident in action.get("resolve_fact_ids", []):
                    if ident not in sources:
                        raise ValueError("resolved fact must be included in evidence")
                    fact = tx.get("fm_fact", ident)
                    fact["resolution"] = {"text": text, "source_fact_ids": sources, "reason": action["reason"], "resolved_at": now_iso()}
                    fact["reference_state"] = "resolved"
                    tx.put("fm_fact", ident, fact)
                for ident in action.get("question_ids", []):
                    question = tx.get("fm_question", ident)
                    if not question or not set(question["source_fact_ids"]) <= set(sources):
                        raise ValueError("question is outside resolution evidence")
                    question.update(status="resolved", resolution=text, resolved_at=now_iso())
                    tx.put("fm_question", ident, question)
            elif op == "retract":
                for card in selected:
                    card["status"] = "retracted"
                    _save_card(tx, card, action["reason"])
            elif op == "add_evidence":
                for card in selected:
                    card["source_fact_ids"] = list(dict.fromkeys(card["source_fact_ids"] + sources))
                    card["scope"] = evidence_scope([tx.get("fm_fact", s) for s in card["source_fact_ids"]])
                    if action.get("content") and action["content"].strip() != card["content"]:
                        if card.get("user_locked"):
                            raise ValueError("user-reviewed memory cannot be automatically rewritten")
                        card["content"] = action["content"].strip()
                    card["freshness"] = "fresh"
                    card["consolidated_at"] = now_iso()
                    _save_card(tx, card, action["reason"])
            else:
                outputs = action.get("outputs") if op == "split" else [action]
                if not outputs or (op == "split" and len(outputs) < 2):
                    raise ValueError("split requires at least two outputs")
                if op == "merge" and len({c["assertion_kind"] for c in selected}) != 1:
                    raise ValueError("do not merge inferred and stated memories")
                for index, output in enumerate(outputs):
                    content = output.get("content", "").strip()
                    kind = output.get("memory_type")
                    assertion = output.get("assertion_kind", "stated")
                    if not content or kind not in TYPES or assertion not in {"stated", "inferred"}:
                        raise ValueError("invalid card content/type/assertion kind")
                    output_sources = output.get("source_fact_ids", sources)
                    if not output_sources or not set(output_sources) <= set(sources):
                        raise ValueError("output evidence outside action")
                    base = selected[0] if selected and op in {"revise", "merge"} else {}
                    if base and assertion != base["assertion_kind"]:
                        raise ValueError("assertion class changes require separate card")
                    evidence_ids = list(dict.fromkeys(base.get("source_fact_ids", []) + output_sources))
                    if op == "merge":
                        evidence_ids = list(dict.fromkeys(evidence_ids + [s for c in selected for s in c["source_fact_ids"]]))
                    owners = {available_facts[s].get("scope", {}).get("owner_id") for s in evidence_ids if s in available_facts}
                    if len(owners - {None}) > 1:
                        raise ValueError("cannot merge different owners")
                    # A rejected inference with unchanged evidence must not silently return.
                    rejected = [c for c in tx.list("fm_card") if c["status"] == "rejected"]
                    if assertion == "inferred" and any(set(evidence_ids) <= set(c["source_fact_ids"]) for c in rejected):
                        raise ValueError("inference rejected by user for this evidence")
                    card = {
                        **base,
                        "id": base.get("id") or "fm_" + key(run_id, len(touched), index, content),
                        "space_id": space,
                        "memory_type": kind,
                        "content": content,
                        "status": "active",
                        "assertion_kind": assertion,
                        "source_fact_ids": evidence_ids,
                        "entities": output.get("entities", base.get("entities", [])),
                        "scope": evidence_scope([tx.get("fm_fact", s) for s in evidence_ids]),
                        "created_at": base.get("created_at", now_iso()),
                        "consolidated_at": now_iso(),
                        "run_id": run_id,
                        "freshness": "fresh",
                        "review_status": "unreviewed" if assertion == "inferred" else "not_required",
                    }
                    if assertion == "inferred":
                        card["inferred_at"] = now_iso()
                    _save_card(tx, card, action["reason"])
                if op in {"merge", "split"}:
                    retired = selected[1:] if op == "merge" else selected
                    for old in retired:
                        old.update(status="superseded", replaced_by=card["id"] if op == "merge" else None)
                        _save_card(tx, old, action["reason"])
            touched.update(targets)
        for ident in batch_ids:
            fact = tx.get("fm_fact", ident)
            fact["processed_at"] = now_iso()
            tx.put("fm_fact", ident, fact)
        _bump(tx)
        if preview:
            raise _PreviewRollback()


def review_memory(space, ident, *, decision, content=None, expected_version):
    if decision not in {"confirm", "edit", "reject"}:
        raise ValueError("unknown review decision")
    with store.transaction(space) as tx:
        card = tx.get("fm_card", ident)
        if not card or card["status"] != "active" or card["version"] != expected_version:
            raise ValueError("memory missing or changed; refresh before reviewing")
        if decision == "edit":
            if not content or not content.strip():
                raise ValueError("edited content is required")
            card["content"] = content.strip()
            protected = set(card["source_fact_ids"])
            for derived in tx.list("fm_card"):
                if (
                    derived["id"] != ident
                    and derived["status"] == "active"
                    and not derived.get("user_locked")
                    and protected & set(derived["source_fact_ids"])
                ):
                    derived.update(status="superseded", review_superseded_by=ident)
                    _save_card(tx, derived, "shared_evidence_user_edited")
                    for fid in set(derived["source_fact_ids"]) - protected:
                        source = tx.get("fm_fact", fid)
                        if source and source["status"] == "active":
                            source["processed_at"] = None
                            tx.put("fm_fact", fid, source)
        # Confirming a correction must not turn its original sources back into
        # current evidence for another derived card.
        review_status = (
            "edited"
            if decision == "confirm" and card.get("review_status") == "edited"
            else {"confirm": "confirmed", "edit": "edited", "reject": "rejected"}[decision]
        )
        card.update(review_status=review_status, reviewed_at=now_iso(), user_locked=True)
        if decision == "reject":
            card["status"] = "rejected"
        # Even confirmed inference keeps its provenance label; it never becomes
        # an original user statement merely because someone clicked confirm.
        _save_card(tx, card, "user_" + decision)
        _bump(tx)
        return card


def answer_question(space, ident, answer):
    if not answer.strip():
        raise ValueError("answer cannot be empty")
    with store.transaction(space) as tx:
        question = tx.get("fm_question", ident)
        if not question or question["status"] != "open":
            raise ValueError("question missing or already answered")
        question.update(status="answered", answer=answer.strip(), answered_at=now_iso())
        tx.put("fm_question", ident, question)
        related = [tx.get("fm_fact", f) for f in question["source_fact_ids"]]
        related = [f for f in related if f and f["status"] == "active"]
        for fact in related:
            fact["processed_at"] = None
            tx.put("fm_fact", fact["id"], fact)
        _bump(tx)
    if related:
        text = f"针对“{question['question']}”，用户补充：{answer.strip()}"
        # This is new user evidence with an explicit link, not a rewrite of the old note.
        fact = ingest_fact(
            space,
            note_id="review:" + ident,
            original_text=text,
            content=text,
            memory_type=related[0]["memory_type"],
            scope=related[0].get("scope"),
            enqueue=False,
        )
        with store.transaction(space) as tx:
            stored = tx.get("fm_fact", fact["id"])
            stored["question_id"] = ident
            stored["related_fact_ids"] = question["source_fact_ids"]
            tx.put("fm_fact", stored["id"], stored)
    return question


def request_forget(space, query):
    if not query.strip():
        raise ValueError("deletion topic cannot be empty")
    matches = search_memory(space, query, limit=200, deduplicate=False)
    candidate_ids = [r["id"] for r in matches]
    request = dict(
        id="delete_" + uuid.uuid4().hex[:16],
        query=query,
        candidates=[
            {
                "id": r["id"],
                "content": r["content"],
                "kind": r.get("assertion_kind", "source_fact"),
                "version": r.get("version"),
                "source_revision": r.get("source_revision"),
            }
            for r in matches
        ],
        candidate_ids=candidate_ids,
        approved_ids=[],
        status="pending",
        created_at=now_iso(),
    )
    store.put(space, "fm_delete", request["id"], request)
    return request


def approve_forget(space, request_id, selected_ids):
    if not selected_ids:
        raise ValueError("select at least one memory")
    with store.transaction(space) as tx:
        request = tx.get("fm_delete", request_id)
        if not request or not set(selected_ids) <= set(request["candidate_ids"]):
            raise ValueError("ids must come from the preview")
        previews = {r["id"]: r for r in request["candidates"]}
        facts_to_forget = set()
        for ident in selected_ids:
            if ident in request["approved_ids"]:
                continue
            card = tx.get("fm_card", ident)
            fact = tx.get("fm_fact", ident)
            row = card or fact
            if not row or row["status"] != "active":
                raise ValueError("selected record is no longer active")
            if card and card["version"] != previews[ident]["version"]:
                raise ValueError("memory changed after deletion preview")
            facts_to_forget.update(card["source_fact_ids"] if card else [ident])
        for ident in facts_to_forget:
            fact = tx.get("fm_fact", ident)
            fact["status"] = "forgotten"
            tx.put("fm_fact", ident, fact)
            tx.put(
                "fm_tombstone",
                key(fact["note_id"], fact["evidence_span"]),
                {"note_id": fact["note_id"], "evidence_span": fact["evidence_span"], "created_at": now_iso()},
            )
        for card in tx.list("fm_card"):
            if card["status"] != "active" or not set(card["source_fact_ids"]) & facts_to_forget:
                continue
            card["status"] = "forgotten" if card["id"] in selected_ids else "invalidated"
            _save_card(tx, card, "approved_forget")
            for source in set(card["source_fact_ids"]) - facts_to_forget:
                fact = tx.get("fm_fact", source)
                if fact and fact["status"] == "active":
                    fact["processed_at"] = None
                    tx.put("fm_fact", source, fact)
        for question in tx.list("fm_question"):
            if set(question["source_fact_ids"]) & facts_to_forget:
                question["status"] = "cancelled"
                tx.put("fm_question", question["id"], question)
        request["approved_ids"] = sorted(set(request["approved_ids"]) | set(selected_ids))
        request["status"] = "completed" if set(request["approved_ids"]) == set(request["candidate_ids"]) else "partial"
        tx.put("fm_delete", request_id, request)
        _bump(tx)
        return request


def query_hits(space, query, *, memory_type=None, limit=10, access_context=None):
    found = search_memory(space, query, kind=memory_type, limit=limit * 4, access_context=access_context)
    return format_hits(space, found)[:limit]


def recent_episodes(space, *, limit=10, access_context=None):
    visible_cards = [c for c in cards(space) if c["memory_type"] == "episodic" and memory_access_allowed(c, access_context)]
    represented = {fid for c in visible_cards for fid in c["source_fact_ids"]}
    rows = visible_cards + [
        f
        for f in active_facts(space)
        if f["memory_type"] == "episodic" and f["id"] not in represented and memory_access_allowed(f, access_context)
    ]
    rows.sort(key=lambda r: str(r.get("mentioned_at") or r.get("created_at", "")), reverse=True)
    return format_hits(space, rows[:limit])


def format_hits(space, found):
    result = []
    for row in found:
        inference = row.get("assertion_kind") == "inferred"
        label = "【系统归纳，待检查】" if inference and row.get("review_status") == "unreviewed" else "【系统归纳】" if inference else ""
        if row.get("reference_state") == "unresolved":
            label += "【对象待确定】"
        if row.get("freshness") == "stale":
            label += "【待重新整理】"
        if row.get("user_locked"):
            label += "【用户已纠正，以此内容为准】"
        source_ids = row.get("source_fact_ids", [row["id"]])
        sources = [get(space, "fact", ident) for ident in source_ids]
        result.append(
            {
                **row,
                "content": label + row["content"],
                "current_version": row.get("version", 1),
                "task_status": None,
                "confidence": 0.8,
                "importance": 0.6,
                "sources": [{"note_id": s["note_id"], "relation": "supported_by"} for s in sources if s and s["status"] == "active"],
                "scope": {
                    **row.get("scope", {}),
                    "assertion_kind": row.get("assertion_kind", "stated"),
                    "inferred_at": row.get("inferred_at"),
                    "review_status": row.get("review_status"),
                },
            }
        )
    return result


def digest_text(digest):
    lines = ["记忆整理完成"]
    for q in digest["questions"]:
        lines.append(f"待确认 {q['id']}：{q['question']}\n回复：/memory facts answer {q['id']} 你的说明")
    for c in digest["inferences"]:
        lines.append(f"系统归纳（{c['inferred_at']}）：{c['content']}\n请检查；可在记忆整理面板确认、修改或否定。ID：{c['id']}")
    return "\n\n".join(lines)


def queue_digest_notification(space, digest):
    if not digest["questions"] and not digest["inferences"]:
        return
    fact_ids = [f for q in digest["questions"] for f in q["source_fact_ids"]]
    fact_ids += [f for c in digest["inferences"] for f in c["source_fact_ids"]]
    for ident in fact_ids:
        fact = get(space, "fact", ident)
        if fact and store.get(space, "reply_route", fact["note_id"]):
            store.put(
                space,
                "notification",
                "fact-digest:" + digest["id"],
                {"id": "fact-digest:" + digest["id"], "note_id": fact["note_id"], "status": "queued", "text": digest_text(digest)},
            )
            from memory.runtime_workflow import flush_notifications

            flush_notifications(space)
            return


def manual_run(space, kind=None):
    result = []
    for t in [kind] if kind else TYPES:
        while True:
            run = consolidate(space, t, trigger="manual")
            result.append(run)
            if run["status"] != "completed" or not any(f["memory_type"] == t and not f["processed_at"] for f in active_facts(space)):
                break
    digests = [r["digest"] for r in result if "digest" in r]
    return {"runs": result, "message": digest_text(digests[-1]) if digests else "整理正在进行，请稍后查看。"}
