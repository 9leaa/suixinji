"""文件作用：Note 数据访问。

项目关系：本文件依赖 `core.sensitive`、`core.settings`、`infrastructure.database`、`infrastructure.schema` 等 5 个模块；被 `agent.query_agent`、`apps.handlers`、`eval.large_live_retrieval_eval`、`eval.live_retrieval_eval`。
"""



from __future__ import annotations

from dataclasses import asdict
from datetime import datetime
from typing import Any

import re

from sqlalchemy import delete, func, or_, select, update
from sqlalchemy.dialects.postgresql import insert

from infrastructure.database import session_scope
from infrastructure.schema import Note, NoteEmbedding, NoteRelation, NoteTag
from core.sensitive import contains_sensitive_data
from core.settings import (
    NOTE_HYBRID_RRF_K,
    NOTE_TRIGRAM_ENABLED,
    RETRIEVAL_WEIGHTED_RRF_ENABLED,
)
from repositories.postgres.common import DEFAULT_TENANT_ID, ensure_tenant_space, parse_datetime


def _iso(value: datetime | None) -> str | None:
    """函数功能：`_iso` 负责处理 iso，服务于本文件职责：Note 数据访问。
    传参：
        value: 待转换、校验或计算的值，类型为 `datetime | None`。
    返回结果说明：
        返回 `str | None`；未命中或无需处理时可返回 `None`。
    """
    return value.isoformat() if value is not None else None


def _as_note(row: Note, tags: list[str], related: list[str]) -> dict[str, Any]:
    """函数功能：`_as_note` 负责处理 as note，服务于本文件职责：Note 数据访问。
    传参：
        row: row 参数，由调用方传入，类型为 `Note`。
        tags: tags 参数，由调用方传入，类型为 `list[str]`。
        related: related 参数，由调用方传入，类型为 `list[str]`。
    返回结果说明：
        返回 `dict[str, Any]`，表示结构化结果、载荷或状态映射。
    """
    metadata = dict(row.metadata_json or {})
    return {
        "id": row.id,
        "message_id": row.message_id,
        "space_id": row.space_id,
        "tenant_id": row.tenant_id,
        "ts": row.created_at.isoformat(),
        "title": row.title,
        "tags": tags,
        "type": row.note_type,
        "summary": row.summary,
        "text": row.text,
        "related": related,
        "enrichment_status": row.enrichment_status,
        "enrichment_attempts": row.enrichment_attempts,
        "enrichment_error": row.enrichment_error,
        "enrichment_started_at": _iso(row.enrichment_started_at),
        "enrichment_updated_at": _iso(row.enrichment_updated_at),
        "sensitivity": row.sensitivity,
        **metadata,
    }


def _load_parts(session: Any, note_ids: list[str]) -> tuple[dict[str, list[str]], dict[str, list[str]]]:
    """函数功能：`_load_parts` 负责加载 parts，服务于本文件职责：Note 数据访问。
    传参：
        session: 数据库会话或运行会话对象，由调用方管理生命周期，类型为 `Any`。
        note_ids: note ids 参数，由调用方传入，类型为 `list[str]`。
    返回结果说明：
        返回 `tuple[dict[str, list[str]], dict[str, list[str]]]`，表示由多个相关值组成的结果。
    """
    tags: dict[str, list[str]] = {note_id: [] for note_id in note_ids}
    related: dict[str, list[str]] = {note_id: [] for note_id in note_ids}
    if not note_ids:
        return tags, related
    for note_id, tag in session.execute(select(NoteTag.note_id, NoteTag.tag).where(NoteTag.note_id.in_(note_ids))):
        tags[str(note_id)].append(str(tag))
    for note_id, target_id in session.execute(
        select(NoteRelation.source_note_id, NoteRelation.target_note_id).where(NoteRelation.source_note_id.in_(note_ids))
    ):
        related[str(note_id)].append(str(target_id))
    return tags, related


def save_note(meta: Any) -> bool:
    """函数功能：`save_note` 负责保存 note，服务于本文件职责：Note 数据访问。
    传参：
        meta: meta 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    values = asdict(meta) if not isinstance(meta, dict) else dict(meta)
    space_id = str(values["space_id"])
    tenant_id = str(values.get("tenant_id") or DEFAULT_TENANT_ID)
    standard_keys = {
        "id", "message_id", "space_id", "ts", "title", "tags", "type", "summary", "text", "related",
        "enrichment_status", "enrichment_attempts", "enrichment_error", "enrichment_started_at",
        "enrichment_updated_at", "sensitivity", "tenant_id",
    }
    metadata = {key: value for key, value in values.items() if key not in standard_keys}
    with session_scope() as session:
        space_id = ensure_tenant_space(session, space_id, tenant_id=tenant_id)
        note_id = session.execute(
            insert(Note)
            .values(
                id=str(values["id"]),
                message_id=str(values["message_id"]),
                tenant_id=tenant_id,
                space_id=space_id,
                created_at=parse_datetime(values.get("ts")),
                title=str(values.get("title") or ""),
                note_type=str(values.get("type") or "other"),
                summary=str(values.get("summary") or ""),
                text=str(values.get("text") or ""),
                metadata_json=metadata,
                enrichment_status=str(values.get("enrichment_status") or "ready"),
                enrichment_attempts=int(values.get("enrichment_attempts") or 0),
                enrichment_error=values.get("enrichment_error"),
                enrichment_started_at=parse_datetime(values["enrichment_started_at"]) if values.get("enrichment_started_at") else None,
                enrichment_updated_at=parse_datetime(values["enrichment_updated_at"]) if values.get("enrichment_updated_at") else None,
                sensitivity=str(values.get("sensitivity") or "normal"),
            )
            .on_conflict_do_nothing(constraint="uq_notes_space_message")
            .returning(Note.id)
        ).scalar_one_or_none()
        if note_id is None:
            return False
        for tag in values.get("tags") or []:
            session.execute(insert(NoteTag).values(note_id=note_id, tag=str(tag)).on_conflict_do_nothing())
        for related_id in values.get("related") or []:
            session.execute(
                insert(NoteRelation)
                .values(source_note_id=note_id, target_note_id=str(related_id), relation="related")
                .on_conflict_do_nothing()
            )
        return True


def load_index(space_id: str) -> list[dict[str, Any]]:
    """函数功能：`load_index` 负责加载 index，服务于本文件职责：Note 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    with session_scope() as session:
        rows = list(session.execute(select(Note).where(Note.space_id == space_id).order_by(Note.created_at, Note.id)).scalars())
        tags, related = _load_parts(session, [row.id for row in rows])
        return [_as_note(row, tags[row.id], related[row.id]) for row in rows]


def _query_notes(
    space_id: str,
    *,
    note_type: str | None = None,
    tags: list[str] | None = None,
    match_all_tags: bool = True,
    created_after: datetime | None = None,
    enrichment_statuses: tuple[str, ...] | None = None,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """函数功能：`_query_notes` 负责查询 notes，服务于本文件职责：Note 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_type: note type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        tags: tags 参数，由调用方传入，类型为 `list[str] | None`，默认值为 `None`。
        match_all_tags: match all tags 参数，由调用方传入，类型为 `bool`，默认值为 `True`。
        created_after: created after 参数，由调用方传入，类型为 `datetime | None`，默认值为 `None`。
        enrichment_statuses: enrichment statuses 参数，由调用方传入，类型为 `tuple[str, ...] | None`，默认值为 `None`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `30`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    requested_tags = sorted(set(tags or []))
    statement = select(Note).where(Note.space_id == space_id, Note.sensitivity == "normal")
    if note_type:
        statement = statement.where(Note.note_type == note_type)
    if created_after is not None:
        statement = statement.where(Note.created_at >= created_after)
    if enrichment_statuses:
        statement = statement.where(Note.enrichment_status.in_(enrichment_statuses))
    if requested_tags:
        tag_ids = select(NoteTag.note_id).where(NoteTag.tag.in_(requested_tags)).group_by(NoteTag.note_id)
        if match_all_tags:
            tag_ids = tag_ids.having(func.count(func.distinct(NoteTag.tag)) == len(requested_tags))
        statement = statement.where(Note.id.in_(tag_ids))
    bounded_limit = max(1, min(int(limit), 500))
    statement = statement.order_by(Note.created_at.desc(), Note.id.desc()).limit(bounded_limit * 4)
    with session_scope() as session:
        rows = list(session.execute(statement).scalars())
        tags_by_id, related = _load_parts(session, [row.id for row in rows])
        notes = [_as_note(row, tags_by_id[row.id], related[row.id]) for row in rows]
        return [note for note in notes if not contains_sensitive_data(str(note.get("text") or ""))][:bounded_limit]


def query_notes_by_type(space_id: str, note_type: str, *, limit: int = 30) -> list[dict[str, Any]]:
    """函数功能：`query_notes_by_type` 负责查询 notes by type，服务于本文件职责：Note 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_type: note type 参数，由调用方传入，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `30`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    return _query_notes(space_id, note_type=note_type, limit=limit)


def query_notes_by_tags(
    space_id: str,
    tags: list[str],
    *,
    note_type: str | None = None,
    match_all_tags: bool = True,
    limit: int = 30,
) -> list[dict[str, Any]]:
    """函数功能：`query_notes_by_tags` 负责查询 notes by tags，服务于本文件职责：Note 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        tags: tags 参数，由调用方传入，类型为 `list[str]`。
        note_type: note type 参数，由调用方传入，类型为 `str | None`，默认值为 `None`。
        match_all_tags: match all tags 参数，由调用方传入，类型为 `bool`，默认值为 `True`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `30`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    return _query_notes(
        space_id,
        note_type=note_type,
        tags=tags,
        match_all_tags=match_all_tags,
        limit=limit,
    )


def list_recent_notes(space_id: str, *, created_after: datetime, limit: int = 30) -> list[dict[str, Any]]:
    """函数功能：`list_recent_notes` 负责列出 recent notes，服务于本文件职责：Note 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        created_after: created after 参数，由调用方传入，类型为 `datetime`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `30`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    return _query_notes(space_id, created_after=created_after, limit=limit)


def list_provisional_notes(space_id: str, *, limit: int = 200) -> list[dict[str, Any]]:
    """函数功能：`list_provisional_notes` 负责列出 provisional notes，服务于本文件职责：Note 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `200`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    return _query_notes(
        space_id,
        enrichment_statuses=("provisional", "enriching", "failed"),
        limit=limit,
    )


_NOTE_QUERY_FILLERS = (
    "请问", "帮我", "告诉我", "查一下", "看一下", "什么", "哪个", "哪些",
    "是否", "有没有", "相关内容", "相关记录", "刚才", "上次",
)


def _note_query_terms(text: str) -> list[str]:
    """函数功能：`_note_query_terms` 负责查询 terms，服务于本文件职责：Note 数据访问。
    传参：
        text: 输入文本内容，类型为 `str`。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
    value = " ".join(str(text or "").split()).strip().casefold()
    for filler in _NOTE_QUERY_FILLERS:
        value = value.replace(filler, " ")
    terms: list[str] = []

    def add(token: str) -> None:
        """函数功能：`add` 负责处理 add，服务于本文件职责：Note 数据访问。
        传参：
            token: token 参数，由调用方传入，类型为 `str`。
        返回结果说明：
            无返回值；主要通过副作用、状态更新、持久化写入或断言体现结果。
        """
        token = str(token or "").strip()
        if len(token) >= 2 and token not in terms:
            terms.append(token)

    for run in re.findall(r"[a-z0-9][a-z0-9+#._-]*|[\u3400-\u9fff]+", value):
        add(run)
        if re.fullmatch(r"[\u3400-\u9fff]+", run):
            for width in (4, 3, 2):
                if len(run) < width:
                    continue
                for start in range(len(run) - width + 1):
                    add(run[start : start + width])
    compact = re.sub(r"\s+", "", value)
    add(compact)
    return terms[:18]


def _weighted_note_rrf(
    channels: list[tuple[str, list[Note]]],
    *,
    limit: int,
) -> list[tuple[Note, float, list[str]]]:
    """函数功能：`_weighted_note_rrf` 负责处理 weighted note rrf，服务于本文件职责：Note 数据访问。
    传参：
        channels: channels 参数，由调用方传入，类型为 `list[tuple[str, list[Note]]]`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`。
    返回结果说明：
        返回 `list[tuple[Note, float, list[str]]]`，表示按条件筛选、构造或查询得到的列表。
    """
    weights = {
        "exact": 1.55,
        "fts": 1.00,
        "lexical": 0.85,
        "trigram": 0.60,
        "vector": 0.95,
    }
    rrf_k = max(1, int(NOTE_HYBRID_RRF_K))
    rows: dict[str, Note] = {}
    scores: dict[str, float] = {}
    channels_by_id: dict[str, list[str]] = {}
    for channel, ranked in channels:
        weight = weights.get(channel, 1.0) if RETRIEVAL_WEIGHTED_RRF_ENABLED else 1.0
        for rank, row in enumerate(ranked, start=1):
            rows[row.id] = row
            scores[row.id] = scores.get(row.id, 0.0) + weight / (rrf_k + rank)
            channels_by_id.setdefault(row.id, []).append(channel)
    ordered = sorted(
        rows.values(),
        key=lambda row: (scores.get(row.id, 0.0), row.created_at, row.id),
        reverse=True,
    )
    return [
        (row, scores.get(row.id, 0.0), channels_by_id.get(row.id, []))
        for row in ordered[: max(1, min(int(limit), 50))]
    ]


def hybrid_search_notes(
    space_id: str,
    query: str,
    *,
    query_embedding: list[float] | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    """函数功能：`hybrid_search_notes` 负责搜索 notes，服务于本文件职责：Note 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        query: 检索或查询文本，类型为 `str`。
        query_embedding: query embedding 参数，由调用方传入，类型为 `list[float] | None`，默认值为 `None`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `8`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    cleaned = " ".join(str(query or "").split()).strip()
    if not cleaned:
        return []
    bounded_limit = max(1, min(int(limit), 30))
    retrieval_limit = max(20, min(100, bounded_limit * 4))
    document = func.concat_ws(" ", Note.title, Note.summary, Note.text)
    terms = _note_query_terms(cleaned)
    channels: list[tuple[str, list[Note]]] = []
    base = select(Note).where(Note.space_id == space_id, Note.sensitivity == "normal")

    with session_scope() as session:
        exact = list(
            session.execute(
                base.where(document.ilike(f"%{cleaned[:120]}%"))
                .order_by(Note.created_at.desc(), Note.id.desc())
                .limit(retrieval_limit)
            ).scalars()
        )
        if exact:
            channels.append(("exact", exact))

        # FTS 适合空格分词文本，尤其是英文；中文场景可能为空，因此下方有界词法通道始终可作为补充。
        fts_query = " ".join(terms) or cleaned
        tsquery = func.websearch_to_tsquery("simple", fts_query[:500])
        fts = list(
            session.execute(
                base.where(func.to_tsvector("simple", document).op("@@")(tsquery))
                .order_by(
                    func.ts_rank_cd(func.to_tsvector("simple", document), tsquery).desc(),
                    Note.created_at.desc(),
                )
                .limit(retrieval_limit)
            ).scalars()
        )
        if fts:
            channels.append(("fts", fts))

        if terms:
            conditions = [document.ilike(f"%{term[:80]}%") for term in terms]
            lexical = list(
                session.execute(
                    base.where(or_(*conditions))
                    .order_by(Note.created_at.desc(), Note.id.desc())
                    .limit(retrieval_limit)
                ).scalars()
            )
            if lexical:
                channels.append(("lexical", lexical))

            if NOTE_TRIGRAM_ENABLED:
                trigram_conditions = [Note.text.op("%") (term[:120]) for term in terms[:8]]
                trigram = list(
                    session.execute(
                        base.where(or_(*trigram_conditions))
                        .order_by(Note.created_at.desc(), Note.id.desc())
                        .limit(retrieval_limit)
                    ).scalars()
                )
                if trigram:
                    channels.append(("trigram", trigram))

        if query_embedding and len(query_embedding) == 1024:
            distance = NoteEmbedding.embedding.cosine_distance(query_embedding)
            vector = list(
                session.execute(
                    base.join(NoteEmbedding, NoteEmbedding.note_id == Note.id)
                    .where(NoteEmbedding.dimensions == len(query_embedding))
                    .order_by(distance, Note.created_at.desc())
                    .limit(retrieval_limit)
                ).scalars()
            )
            if vector:
                channels.append(("vector", vector))

        ranked = _weighted_note_rrf(channels, limit=bounded_limit * 2)
        note_ids = [row.id for row, _score, _channels in ranked]
        tags_by_id, related = _load_parts(session, note_ids)
        output: list[dict[str, Any]] = []
        for row, score, retrieval_channels in ranked:
            if contains_sensitive_data(row.text):
                continue
            note = _as_note(row, tags_by_id[row.id], related[row.id])
            note["score"] = round(float(score), 6)
            note["retrieval_channels"] = retrieval_channels
            output.append(note)
        return output[:bounded_limit]


def search_notes_memory_fallback(space_id: str, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
    """函数功能：`search_notes_memory_fallback` 负责搜索 notes memory fallback，服务于本文件职责：Note 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        query: 检索或查询文本，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `8`。
    返回结果说明：
        返回 `list[dict[str, Any]]`，表示按条件筛选、构造或查询得到的列表。
    """
    cleaned = " ".join(str(query or "").split()).strip()
    if not cleaned:
        return []
    bounded_limit = max(1, min(int(limit), 20))
    document = func.concat_ws(" ", Note.title, Note.summary, Note.text)
    tsquery = func.websearch_to_tsquery("simple", cleaned)
    fts_condition = func.to_tsvector("simple", document).op("@@")(tsquery)
    with session_scope() as session:
        rows = list(
            session.execute(
                select(Note)
                .where(Note.space_id == space_id, Note.sensitivity == "normal", fts_condition)
                .order_by(Note.created_at.desc(), Note.id.desc())
                .limit(bounded_limit * 3)
            ).scalars()
        )
        if not rows:
            # simple dictionary 不切中文；这里保留小而确定性的词法 fallback，而不是依赖可选扩展或直接返回空结果。
            fragments = [cleaned]
            fragments.extend(part for part in cleaned.split() if len(part) >= 2)
            conditions = [document.ilike(f"%{fragment[:80]}%") for fragment in dict.fromkeys(fragments) if fragment]
            if conditions:
                rows = list(
                    session.execute(
                        select(Note)
                        .where(Note.space_id == space_id, Note.sensitivity == "normal", or_(*conditions))
                        .order_by(Note.created_at.desc(), Note.id.desc())
                        .limit(bounded_limit * 3)
                    ).scalars()
                )
        tags_by_id, related = _load_parts(session, [row.id for row in rows])
        return [
            _as_note(row, tags_by_id[row.id], related[row.id])
            for row in rows
            if not contains_sensitive_data(row.text)
        ][:bounded_limit]


def get_note_relations(space_id: str, note_id: str, *, limit: int = 5) -> dict[str, Any] | None:
    """函数功能：`get_note_relations` 负责获取 note relations，服务于本文件职责：Note 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `5`。
    返回结果说明：
        返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
    """
    bounded_limit = max(1, min(int(limit), 20))
    with session_scope() as session:
        source = session.execute(
            select(Note).where(Note.space_id == space_id, Note.id == note_id, Note.sensitivity == "normal")
        ).scalar_one_or_none()
        if source is None or contains_sensitive_data(source.text):
            return None
        relation_rows = list(
            session.execute(
                select(NoteRelation.source_note_id, NoteRelation.target_note_id).where(
                    or_(NoteRelation.source_note_id == note_id, NoteRelation.target_note_id == note_id)
                )
            )
        )
        outbound_ids = [str(target) for source_id, target in relation_rows if str(source_id) == note_id][:bounded_limit]
        inbound_ids = [str(source_id) for source_id, target in relation_rows if str(target) == note_id][:bounded_limit]
        related_ids = list(dict.fromkeys([*outbound_ids, *inbound_ids]))
        related_rows = list(
            session.execute(
                select(Note).where(
                    Note.space_id == space_id,
                    Note.id.in_(related_ids),
                    Note.sensitivity == "normal",
                )
            ).scalars()
        ) if related_ids else []
        all_rows = [source, *related_rows]
        tags_by_id, _related = _load_parts(session, [row.id for row in all_rows])
        notes = {
            row.id: _as_note(row, tags_by_id[row.id], outbound_ids if row.id == note_id else [])
            for row in all_rows
            if not contains_sensitive_data(row.text)
        }
        return {
            "source": notes.get(note_id),
            "outbound": [notes[item_id] for item_id in outbound_ids if item_id in notes],
            "inbound": [notes[item_id] for item_id in inbound_ids if item_id in notes],
        }


def list_space_ids() -> list[str]:
    """函数功能：`list_space_ids` 负责列出 space ids，服务于本文件职责：Note 数据访问。
    传参：
        无。
    返回结果说明：
        返回 `list[str]`，表示按条件筛选、构造或查询得到的列表。
    """
    with session_scope() as session:
        return list(session.execute(select(Note.space_id).distinct().order_by(Note.space_id)).scalars())


def find_note(space_id: str, note_id: str) -> dict[str, Any] | None:
    """函数功能：`find_note` 负责查找 note，服务于本文件职责：Note 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
    返回结果说明：
        返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
    """
    with session_scope() as session:
        row = session.execute(select(Note).where(Note.space_id == space_id, Note.id == note_id)).scalar_one_or_none()
        if row is None:
            return None
        tags, related = _load_parts(session, [row.id])
        return _as_note(row, tags[row.id], related[row.id])


def find_note_content(space_id: str, note_id: str) -> dict[str, Any] | None:
    """函数功能：`find_note_content` 负责查找 note content，服务于本文件职责：Note 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
    返回结果说明：
        返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
    """
    with session_scope() as session:
        tags = (
            select(func.array_agg(NoteTag.tag))
            .where(NoteTag.note_id == Note.id)
            .correlate(Note)
            .scalar_subquery()
        )
        row = session.execute(
            select(
                Note.id,
                Note.message_id,
                Note.space_id,
                Note.tenant_id,
                Note.created_at,
                Note.title,
                Note.note_type,
                Note.summary,
                Note.text,
                Note.sensitivity,
                tags.label("tags"),
            ).where(Note.space_id == space_id, Note.id == note_id)
        ).one_or_none()
        if row is None:
            return None
        return {
            "id": str(row.id),
            "message_id": str(row.message_id),
            "space_id": str(row.space_id),
            "tenant_id": str(row.tenant_id),
            "ts": row.created_at.isoformat(),
            "title": str(row.title),
            "tags": list(row.tags or []),
            "type": str(row.note_type),
            "summary": str(row.summary),
            "text": str(row.text),
            "sensitivity": str(row.sensitivity),
        }


def note_exists(space_id: str, message_id: str) -> bool:
    """函数功能：`note_exists` 负责处理 note exists，服务于本文件职责：Note 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        message_id: 外部或本地消息标识，用于入口幂等和追踪，类型为 `str`。
    返回结果说明：
        返回 `bool`，表示判断、写入或处理是否成功。
    """
    with session_scope() as session:
        return session.execute(select(Note.id).where(Note.space_id == space_id, Note.message_id == message_id).limit(1)).scalar_one_or_none() is not None


def update_note_metadata(space_id: str, note_id: str, **updates: Any) -> dict[str, Any] | None:
    """函数功能：`update_note_metadata` 负责更新 note metadata，服务于本文件职责：Note 数据访问。
    传参：
        space_id: 业务空间标识，用于隔离不同会话或租户下的数据，类型为 `str`。
        note_id: Note 标识，用于定位原始记录，类型为 `str`。
        **updates: updates 参数，由调用方传入，类型为 `Any`。
    返回结果说明：
        返回 `dict[str, Any] | None`，表示结构化结果、载荷或状态映射。
    """
    column_map = {
        "title": "title", "type": "note_type", "summary": "summary", "text": "text",
        "enrichment_status": "enrichment_status", "enrichment_attempts": "enrichment_attempts",
        "enrichment_error": "enrichment_error", "sensitivity": "sensitivity",
    }
    values = {column_map[key]: value for key, value in updates.items() if key in column_map}
    for key in ("enrichment_started_at", "enrichment_updated_at"):
        if key in updates:
            values[key] = parse_datetime(updates[key]) if updates[key] else None
    with session_scope() as session:
        if values:
            session.execute(update(Note).where(Note.space_id == space_id, Note.id == note_id).values(**values))
        if "tags" in updates:
            session.execute(delete(NoteTag).where(NoteTag.note_id == note_id))
            for tag in updates["tags"] or []:
                session.add(NoteTag(note_id=note_id, tag=str(tag)))
        if "related" in updates:
            session.execute(delete(NoteRelation).where(NoteRelation.source_note_id == note_id))
            for target in updates["related"] or []:
                session.add(NoteRelation(source_note_id=note_id, target_note_id=str(target), relation="related"))
    return find_note(space_id, note_id)


def list_pending_enrichments(*, limit: int = 100, max_attempts: int = 3) -> list[dict[str, str]]:
    """函数功能：`list_pending_enrichments` 负责列出 pending enrichments，服务于本文件职责：Note 数据访问。
    传参：
        limit: 数量上限，用于限制返回、扫描或处理规模，类型为 `int`，默认值为 `100`。
        max_attempts: max attempts 参数，由调用方传入，类型为 `int`，默认值为 `3`。
    返回结果说明：
        返回 `list[dict[str, str]]`，表示按条件筛选、构造或查询得到的列表。
    """
    with session_scope() as session:
        rows = session.execute(
            select(Note.space_id, Note.id)
            .where(
                Note.enrichment_status.in_(["provisional", "enriching", "failed"]),
                Note.enrichment_attempts < max_attempts,
                Note.sensitivity == "normal",
            )
            .order_by(Note.created_at)
            .limit(max(1, int(limit)))
        )
        return [{"space_id": str(space_id), "note_id": str(note_id)} for space_id, note_id in rows]
