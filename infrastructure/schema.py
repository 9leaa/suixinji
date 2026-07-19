"""SQLAlchemy schema for the shared PostgreSQL data store."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="feishu")
    source_user_id: Mapped[str | None] = mapped_column(String(255))
    profile_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "source", "source_user_id", name="uq_users_source_identity"),)


class Space(Base):
    __tablename__ = "spaces"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    source: Mapped[str] = mapped_column(String(64), nullable=False, default="feishu")
    source_space_id: Mapped[str | None] = mapped_column(String(255))
    space_type: Mapped[str] = mapped_column(String(64), nullable=False, default="chat")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    processed_sequence_no: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    note_watermark: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    memory_watermark: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0, server_default="0")
    memory_gap_sequence_no: Mapped[int | None] = mapped_column(BigInteger)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "source", "source_space_id", name="uq_spaces_source_identity"),)


class SpaceMember(Base):
    __tablename__ = "space_members"
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(32), nullable=False, default="member")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class InboxMessage(Base):
    __tablename__ = "inbox_messages"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    source: Mapped[str] = mapped_column(String(64), nullable=False)
    source_message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    source_event_id: Mapped[str | None] = mapped_column(String(255))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[str | None] = mapped_column(String(255))
    chat_type: Mapped[str | None] = mapped_column(String(64))
    sender_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending")
    sensitivity: Mapped[str] = mapped_column(String(64), nullable=False, default="normal")
    sequence_no: Mapped[int] = mapped_column(BigInteger, nullable=False)
    note_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending")
    memory_status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending")
    note_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    memory_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("tenant_id", "source", "source_message_id", name="uq_inbox_tenant_source_message"),
        UniqueConstraint("space_id", "sequence_no", name="uq_inbox_space_sequence"),
        Index("ix_inbox_space_status_sequence", "space_id", "status", "sequence_no"),
        Index("ix_inbox_space_note_sequence", "space_id", "note_status", "sequence_no"),
        Index("ix_inbox_space_memory_sequence", "space_id", "memory_status", "sequence_no"),
    )


class OutboxEvent(Base):
    __tablename__ = "outbox_events"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(128), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(64), nullable=False)
    aggregate_id: Mapped[str] = mapped_column(String(255), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    publish_attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="pending", server_default="pending")
    claimed_by: Mapped[str | None] = mapped_column(String(255))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=10, server_default="10")
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_outbox_unpublished", "published_at", "created_at"),
        Index("ix_outbox_status_next_created", "status", "next_attempt_at", "created_at"),
    )


class Task(Base):
    __tablename__ = "tasks"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    task_type: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False)
    source_message_id: Mapped[str | None] = mapped_column(String(255))
    idempotency_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="queued")
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    defer_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    next_retry_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    claimed_by: Mapped[str | None] = mapped_column(String(255))
    lease_token: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    __table_args__ = (
        Index("ix_tasks_space_status_created", "space_id", "status", "created_at"),
        Index("ix_tasks_status_lease", "status", "lease_expires_at"),
    )


class TaskAttempt(Base):
    __tablename__ = "task_attempts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    task_id: Mapped[str] = mapped_column(ForeignKey("tasks.id", ondelete="CASCADE"), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(255), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_type: Mapped[str | None] = mapped_column(String(255))
    error_summary: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("task_id", "attempt_no", name="uq_task_attempt_no"),)


class Note(Base):
    __tablename__ = "notes"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    message_id: Mapped[str] = mapped_column(String(255), nullable=False)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    note_type: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    enrichment_status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready")
    enrichment_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    enrichment_error: Mapped[str | None] = mapped_column(Text)
    enrichment_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    enrichment_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sensitivity: Mapped[str] = mapped_column(String(64), nullable=False, default="normal")
    __table_args__ = (
        UniqueConstraint("space_id", "message_id", name="uq_notes_space_message"),
        Index("ix_notes_space_created", "space_id", "created_at"),
        Index("ix_notes_space_type_created", "space_id", "note_type", "created_at"),
        Index("ix_notes_space_enrichment_created", "space_id", "enrichment_status", "created_at"),
    )


class NoteTag(Base):
    __tablename__ = "note_tags"
    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True)
    tag: Mapped[str] = mapped_column(String(255), primary_key=True)
    __table_args__ = (Index("ix_note_tags_tag_note", "tag", "note_id"),)


class NoteRelation(Base):
    __tablename__ = "note_relations"
    source_note_id: Mapped[str] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True)
    target_note_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    relation: Mapped[str] = mapped_column(String(64), primary_key=True, default="related")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    __table_args__ = (Index("ix_note_relations_target", "target_note_id"),)


class NoteEmbedding(Base):
    __tablename__ = "note_embeddings"
    note_id: Mapped[str] = mapped_column(ForeignKey("notes.id", ondelete="CASCADE"), primary_key=True)
    model: Mapped[str] = mapped_column(String(255), primary_key=True)
    dimensions: Mapped[int] = mapped_column(Integer, nullable=False)
    embedding: Mapped[list[float]] = mapped_column(Vector(1024), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class Memory(Base):
    __tablename__ = "memories"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False)
    memory_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_content: Mapped[str] = mapped_column(Text, nullable=False, default="")
    importance: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    task_status: Mapped[str | None] = mapped_column(String(64))
    subject: Mapped[str | None] = mapped_column(Text)
    predicate: Mapped[str | None] = mapped_column(Text)
    object_value: Mapped[str | None] = mapped_column(Text)
    memory_key: Mapped[str | None] = mapped_column(String(512))
    memory_key_version: Mapped[str] = mapped_column(String(64), nullable=False, default="memory-key-v2", server_default="memory-key-v2")
    polarity: Mapped[str | None] = mapped_column(String(32))
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_confirmed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_accessed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    access_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    current_version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    __table_args__ = (
        Index("ix_memories_space_status_type", "space_id", "status", "memory_type"),
        Index("ix_memories_space_key_status", "space_id", "memory_key", "status"),
        Index("ix_memories_space_status_updated", "space_id", "status", "updated_at"),
    )


class MemoryCandidateRow(Base):
    __tablename__ = "memory_candidates"
    candidate_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, default="default")
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False)
    note_id: Mapped[str] = mapped_column(String(255), nullable=False)
    memory_type: Mapped[str] = mapped_column(String(64), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    normalized_content: Mapped[str] = mapped_column(Text, nullable=False)
    memory_key: Mapped[str] = mapped_column(String(512), nullable=False)
    memory_key_version: Mapped[str] = mapped_column(String(64), nullable=False, default="memory-key-v2", server_default="memory-key-v2")
    subject: Mapped[str | None] = mapped_column(Text)
    predicate: Mapped[str | None] = mapped_column(Text)
    object_value: Mapped[str | None] = mapped_column(Text)
    task_status: Mapped[str | None] = mapped_column(String(64))
    polarity: Mapped[str | None] = mapped_column(String(32))
    scope_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    entities_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    importance: Mapped[float] = mapped_column(Float, nullable=False)
    evidence_span: Mapped[str | None] = mapped_column(Text)
    should_store: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    extractor_type: Mapped[str] = mapped_column(String(32), nullable=False, default="rules")
    extractor_version: Mapped[str] = mapped_column(String(128), nullable=False, default="memory-extractor-v1")
    model: Mapped[str | None] = mapped_column(String(255))
    prompt_hash: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="extracted")
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    decision_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint("note_id", "candidate_id", name="uq_memory_candidate_note"),
        Index("ix_memory_candidates_space_status", "space_id", "status", "updated_at"),
    )


class MemorySource(Base):
    __tablename__ = "memory_sources"
    memory_id: Mapped[str] = mapped_column(ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True)
    note_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    relation: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryVersion(Base):
    __tablename__ = "memory_versions"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    memory_id: Mapped[str] = mapped_column(ForeignKey("memories.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    task_status: Mapped[str | None] = mapped_column(String(64))
    confidence: Mapped[float | None] = mapped_column(Float)
    importance: Mapped[float | None] = mapped_column(Float)
    valid_from: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    valid_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reason: Mapped[str | None] = mapped_column(Text)
    source_note_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("memory_id", "version", name="uq_memory_version"),)


class MemoryVector(Base):
    __tablename__ = "memory_vectors"
    memory_id: Mapped[str] = mapped_column(ForeignKey("memories.id", ondelete="CASCADE"), primary_key=True)
    embedding: Mapped[list[float] | None] = mapped_column(Vector(1024))
    model: Mapped[str | None] = mapped_column(String(255))
    dimension: Mapped[int | None] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(128))
    embedding_version: Mapped[str | None] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(32), nullable=False, default="ready", server_default="ready")
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryExtractionState(Base):
    __tablename__ = "memory_extraction_states"
    note_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    candidate_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    processed_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class MemoryConsolidationRun(Base):
    __tablename__ = "memory_consolidation_runs"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False)
    cadence: Mapped[str] = mapped_column(String(32), nullable=False)
    period_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("space_id", "cadence", "period_key", name="uq_memory_consolidation_period"),)


class MemoryDecision(Base):
    __tablename__ = "memory_decisions"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False)
    note_id: Mapped[str] = mapped_column(String(255), nullable=False)
    candidate_id: Mapped[str] = mapped_column(String(255), nullable=False)
    relation: Mapped[str] = mapped_column(String(64), nullable=False)
    target_memory_ids_json: Mapped[list[str]] = mapped_column(JSONB, nullable=False, default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    evidence_json: Mapped[list[Any]] = mapped_column(JSONB, nullable=False, default=list)
    recommended_action: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_memory_ids_json: Mapped[list[str] | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    policy_version: Mapped[str] = mapped_column(String(128), nullable=False, default="memory-policy-v1")
    adjudicator_version: Mapped[str] = mapped_column(String(128), nullable=False, default="memory-adjudicator-v1")
    model: Mapped[str | None] = mapped_column(String(255))
    prompt_hash: Mapped[str | None] = mapped_column(String(128))
    input_hash: Mapped[str | None] = mapped_column(String(128))
    target_snapshot_version: Mapped[int | None] = mapped_column(Integer)
    retry_of_decision_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryRelation(Base):
    __tablename__ = "memory_relations"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False)
    source_memory_id: Mapped[str] = mapped_column(ForeignKey("memories.id", ondelete="CASCADE"), nullable=False)
    target_memory_id: Mapped[str] = mapped_column(ForeignKey("memories.id", ondelete="CASCADE"), nullable=False)
    relation: Mapped[str] = mapped_column(String(64), nullable=False)
    decision_id: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    __table_args__ = (UniqueConstraint("source_memory_id", "target_memory_id", "relation", "decision_id", name="uq_memory_relation"),)


class MemoryTrace(Base):
    __tablename__ = "memory_traces"
    trace_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False)
    note_id: Mapped[str | None] = mapped_column(String(255))
    trace_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class SummarySubscriptionRow(Base):
    __tablename__ = "summary_subscriptions"
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    chat_id: Mapped[str] = mapped_column(String(255), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    time: Mapped[str] = mapped_column(String(5), nullable=False)
    range_key: Mapped[str] = mapped_column(String(32), nullable=False, default="today")
    last_sent_date: Mapped[str | None] = mapped_column(String(10))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)


class SummaryRun(Base):
    __tablename__ = "summary_runs"
    id: Mapped[str] = mapped_column(String(255), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False)
    range_key: Mapped[str] = mapped_column(String(32), nullable=False)
    period_key: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    result_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("space_id", "range_key", "period_key", name="uq_summary_run_period"),)


class Delivery(Base):
    __tablename__ = "deliveries"
    delivery_key: Mapped[str] = mapped_column(String(512), primary_key=True)
    delivery_type: Mapped[str] = mapped_column(String(64), nullable=False)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False)
    message_id: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    reserved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempt_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    error: Mapped[str | None] = mapped_column(Text)


class DeliveryAttempt(Base):
    __tablename__ = "delivery_attempts"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    delivery_key: Mapped[str] = mapped_column(ForeignKey("deliveries.delivery_key", ondelete="CASCADE"), nullable=False)
    attempt_no: Mapped[int] = mapped_column(Integer, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("delivery_key", "attempt_no", name="uq_delivery_attempt_no"),)


class AgentRun(Base):
    __tablename__ = "agent_runs"
    run_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False)
    space_id: Mapped[str] = mapped_column(ForeignKey("spaces.id", ondelete="CASCADE"), nullable=False)
    user_id: Mapped[str | None] = mapped_column(String(255))
    message_id: Mapped[str | None] = mapped_column(String(255))
    run_type: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_type: Mapped[str | None] = mapped_column(String(255))


class AgentStep(Base):
    __tablename__ = "agent_steps"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.run_id", ondelete="CASCADE"), nullable=False)
    step_no: Mapped[int] = mapped_column(Integer, nullable=False)
    step_type: Mapped[str] = mapped_column(String(64), nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    safe_input_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    safe_output_json: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    error_type: Mapped[str | None] = mapped_column(String(255))
    __table_args__ = (UniqueConstraint("run_id", "step_no", name="uq_agent_step_no"),)


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[str] = mapped_column(ForeignKey("agent_runs.run_id", ondelete="CASCADE"), nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    request_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    input_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    estimated_cost: Mapped[Decimal] = mapped_column(Numeric(14, 6), nullable=False, default=0)
