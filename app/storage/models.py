"""SQLAlchemy models for the PostgreSQL system of record (PRD s14).

Sixteen application tables. LangGraph's checkpoint tables live in the same
database but are owned by PostgresSaver and are never modelled or migrated here
(s14.1).

There is no organizations or users table: v3 is a single named user (s3.2), so
identity is the Slack user id recorded on approvals and decisions.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    pass


def _pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


# ---------------------------------------------------------------------------
# Projects and runs
# ---------------------------------------------------------------------------


class Project(Base, TimestampMixin):
    """Long-lived project objective and Slack thread mapping (s17.2)."""

    __tablename__ = "projects"
    __table_args__ = (
        UniqueConstraint("slack_channel_id", "slack_thread_ts", name="uq_project_thread"),
    )

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    objective: Mapped[str | None] = mapped_column(Text)
    context_summary: Mapped[str | None] = mapped_column(Text)
    slack_channel_id: Mapped[str | None] = mapped_column(String(64))
    slack_thread_ts: Mapped[str | None] = mapped_column(String(64))
    state: Mapped[str] = mapped_column(String(32), default="active", nullable=False)

    runs: Mapped[list[Run]] = relationship(back_populates="project")


class Run(Base, TimestampMixin):
    """One orchestrated execution (s14)."""

    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_run_idempotency"),
        Index("ix_runs_status", "status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    run_ref: Mapped[str] = mapped_column(String(32), unique=True, nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )

    slack_channel_id: Mapped[str | None] = mapped_column(String(64))
    slack_thread_ts: Mapped[str | None] = mapped_column(String(64))
    slack_user_id: Mapped[str | None] = mapped_column(String(64))
    idempotency_key: Mapped[str | None] = mapped_column(String(255))

    user_request: Mapped[str] = mapped_column(Text, nullable=False)
    requested_output_format: Mapped[str] = mapped_column(String(32), default="brief")

    # queued | planning | awaiting_approval | running | challenging | synthesizing
    # | quality_gate | awaiting_decision | reporting | complete | review_required
    # | failed | cancelled
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    current_stage: Mapped[str | None] = mapped_column(String(64))

    plan: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    executive_brief: Mapped[dict[str, Any] | None] = mapped_column(JSONType)
    budgets: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    challenge_round: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # s20: which gateway route and auth mode produced this run.
    executive_route: Mapped[str | None] = mapped_column(String(64))
    specialist_route: Mapped[str | None] = mapped_column(String(64))
    auth_mode: Mapped[str | None] = mapped_column(String(32))

    final_summary: Mapped[str | None] = mapped_column(Text)
    error_state: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    project: Mapped[Project | None] = relationship(back_populates="runs")
    tasks: Mapped[list[RunTask]] = relationship(back_populates="run", cascade="all, delete-orphan")


class RunJob(Base, TimestampMixin):
    """Durable single-worker job queue (s31).

    A run may have several jobs over its lifetime: the initial orchestrate job
    and a resume job per human decision.
    """

    __tablename__ = "run_jobs"
    __table_args__ = (Index("ix_run_jobs_claim", "status", "available_at"),)

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # QUEUED CLAIMED RUNNING WAITING_APPROVAL REPORTING COMPLETE FAILED CANCELLED
    status: Mapped[str] = mapped_column(String(32), default="queued", nullable=False)
    job_type: Mapped[str] = mapped_column(String(32), default="orchestrate", nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    idempotency_key: Mapped[str | None] = mapped_column(String(255), unique=True)

    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    available_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    locked_by: Mapped[str | None] = mapped_column(String(128))
    last_error: Mapped[str | None] = mapped_column(Text)


class RunTask(Base, TimestampMixin):
    """A planner-created specialist task and its dependencies (s14)."""

    __tablename__ = "run_tasks"
    __table_args__ = (
        UniqueConstraint("run_id", "planner_task_id", "attempt", name="uq_run_task_attempt"),
        Index("ix_run_tasks_run_status", "run_id", "status"),
    )

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    planner_task_id: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    agent_version: Mapped[str | None] = mapped_column(String(32))

    objective: Mapped[str] = mapped_column(Text, nullable=False)
    required_output: Mapped[str | None] = mapped_column(Text)
    depends_on: Mapped[list[str]] = mapped_column(JSONType, default=list)
    context_refs: Mapped[list[str]] = mapped_column(JSONType, default=list)

    execution_group: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    is_followup: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    attempt: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    max_steps: Mapped[int] = mapped_column(Integer, default=12, nullable=False)
    error: Mapped[str | None] = mapped_column(Text)

    run: Mapped[Run] = relationship(back_populates="tasks")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class AgentProfileRecord(Base, TimestampMixin):
    """Versioned A01-A16 registry metadata (s14, FR-018)."""

    __tablename__ = "agent_profiles"

    id: Mapped[str] = mapped_column(String(8), primary_key=True)
    slug: Mapped[str] = mapped_column(String(64), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    operating_persona: Mapped[str | None] = mapped_column(Text)
    purpose: Mapped[str | None] = mapped_column(Text)
    source_sds: Mapped[str | None] = mapped_column(String(255))
    # active | provisional | retired
    status: Mapped[str] = mapped_column(String(32), default="active", nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    profile_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)


class PromptVersion(Base, TimestampMixin):
    """Versioned executive and specialist prompts (s14, FR-018)."""

    __tablename__ = "prompt_versions"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_prompt_version"),)

    id: Mapped[uuid.UUID] = _pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False)
    version: Mapped[str] = mapped_column(String(32), nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)


# ---------------------------------------------------------------------------
# Work products
# ---------------------------------------------------------------------------


class SpecialistResultRecord(Base, TimestampMixin):
    """Structured specialist outputs (s14)."""

    __tablename__ = "specialist_results"

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("run_tasks.id", ondelete="SET NULL"), index=True
    )
    planner_task_id: Mapped[str] = mapped_column(String(32), nullable=False)
    agent_id: Mapped[str] = mapped_column(String(8), nullable=False, index=True)
    agent_version: Mapped[str | None] = mapped_column(String(32))

    gateway_route: Mapped[str | None] = mapped_column(String(64))
    model_id: Mapped[str | None] = mapped_column(String(128))

    status: Mapped[str] = mapped_column(String(32), nullable=False)
    confidence: Mapped[str | None] = mapped_column(String(32))
    executive_summary: Mapped[str | None] = mapped_column(Text)
    structured_result: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    escalation_required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    duration_ms: Mapped[int | None] = mapped_column(Integer)


class EvidenceItem(Base, TimestampMixin):
    """Provenance-backed evidence (s15). A first-class entity, not prose."""

    __tablename__ = "evidence_items"
    __table_args__ = (UniqueConstraint("evidence_ref", name="uq_evidence_ref"),)

    id: Mapped[uuid.UUID] = _pk()
    evidence_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )

    source_type: Mapped[str] = mapped_column(String(32), nullable=False)
    source_title: Mapped[str] = mapped_column(Text, nullable=False)
    source_url_or_document_id: Mapped[str | None] = mapped_column(Text)
    retrieved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    excerpt_or_locator: Mapped[str | None] = mapped_column(Text)
    # s15.1 levels 1 (authoritative) to 4 (unverified).
    reliability_level: Mapped[int] = mapped_column(Integer, nullable=False)
    originating_agent: Mapped[str | None] = mapped_column(String(8))
    hash_or_snapshot_ref: Mapped[str | None] = mapped_column(String(128))
    notes: Mapped[str | None] = mapped_column(Text)


class Artifact(Base, TimestampMixin):
    """Uploaded/generated files and report versions (s14, s18.3)."""

    __tablename__ = "artifacts"
    __table_args__ = (UniqueConstraint("artifact_ref", name="uq_artifact_ref"),)

    id: Mapped[uuid.UUID] = _pk()
    artifact_ref: Mapped[str] = mapped_column(String(64), nullable=False)
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), default="work_product", nullable=False)
    title: Mapped[str] = mapped_column(Text, nullable=False)
    content_type: Mapped[str] = mapped_column(String(128), default="text/markdown")
    storage_backend: Mapped[str] = mapped_column(String(32), default="local")
    storage_path: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    created_by_agent: Mapped[str | None] = mapped_column(String(8))
    # Lifecycle: draft | current | superseded | approved. A revision supersedes
    # the draft it replaces, so the report generator can tell which QTPP is the
    # QTPP rather than printing both.
    status: Mapped[str] = mapped_column(String(16), default="current", nullable=False)
    supersedes_ref: Mapped[str | None] = mapped_column(String(64))
    superseded_by_ref: Mapped[str | None] = mapped_column(String(64))
    # s18.3: reports are versioned, never overwritten.
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    template_version: Mapped[str | None] = mapped_column(String(64))
    generator_version: Mapped[str | None] = mapped_column(String(64))


class Assumption(Base, TimestampMixin):
    """Explicit assumptions and their approval state (s14)."""

    __tablename__ = "assumptions"

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    statement: Mapped[str] = mapped_column(Text, nullable=False)
    originating_agent: Mapped[str | None] = mapped_column(String(8))
    # proposed | approved | rejected | superseded
    state: Mapped[str] = mapped_column(String(32), default="proposed", nullable=False)
    approved_by: Mapped[str | None] = mapped_column(String(64))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rationale: Mapped[str | None] = mapped_column(Text)


class Conflict(Base, TimestampMixin):
    """Substantive inter-agent disagreement (s14). Never silently averaged."""

    __tablename__ = "conflicts"

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    agent_ids: Mapped[list[str]] = mapped_column(JSONType, default=list)
    severity: Mapped[str] = mapped_column(String(32), default="medium", nullable=False)
    material: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    # open | resolved | escalated | accepted
    state: Mapped[str] = mapped_column(String(32), default="open", nullable=False)
    resolution: Mapped[str | None] = mapped_column(Text)


class Risk(Base, TimestampMixin):
    """Risk register entries (s14)."""

    __tablename__ = "risks"

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    description: Mapped[str] = mapped_column(Text, nullable=False)
    likelihood: Mapped[str] = mapped_column(String(32), default="medium", nullable=False)
    impact: Mapped[str] = mapped_column(String(32), default="medium", nullable=False)
    mitigation: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(String(128))
    originating_agent: Mapped[str | None] = mapped_column(String(8))


class Approval(Base, TimestampMixin):
    """Plan / action / final human approvals (s16, FR-011)."""

    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = _pk()
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # plan | tool | final_decision | scope_change
    gate: Mapped[str] = mapped_column(String(32), nullable=False)
    decision_class: Mapped[str] = mapped_column(String(16), default="amber", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    requested_payload: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    decided_by: Mapped[str | None] = mapped_column(String(64))
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    rationale: Mapped[str | None] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Decision(Base, TimestampMixin):
    """Durable human decision register with rationale (s14, FR-020)."""

    __tablename__ = "decisions"

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("runs.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    decision_text: Mapped[str] = mapped_column(Text, nullable=False)
    decision_class: Mapped[str] = mapped_column(String(16), default="amber", nullable=False)
    rationale: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(String(64))
    conditions: Mapped[list[str]] = mapped_column(JSONType, default=list)
    evidence_refs: Mapped[list[str]] = mapped_column(JSONType, default=list)
    superseded_by_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("decisions.id", ondelete="SET NULL")
    )


class AuditEvent(Base):
    """Append-only event log (s14, s21, FR-019). Never updated or deleted."""

    __tablename__ = "audit_events"
    __table_args__ = (Index("ix_audit_run_time", "run_id", "created_at"),)

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL")
    )
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.id", ondelete="SET NULL"))
    task_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("run_tasks.id", ondelete="SET NULL")
    )
    actor_type: Mapped[str] = mapped_column(String(32), nullable=False)
    actor_id: Mapped[str | None] = mapped_column(String(128))
    event_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    agent_version: Mapped[str | None] = mapped_column(String(32))
    gateway_route: Mapped[str | None] = mapped_column(String(64))
    model_id: Mapped[str | None] = mapped_column(String(128))
    event_metadata: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class SourceDocument(Base, TimestampMixin):
    """Project document metadata, chunks and access scope (s14, s17.3)."""

    __tablename__ = "source_documents"

    id: Mapped[uuid.UUID] = _pk()
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    title: Mapped[str] = mapped_column(Text, nullable=False)
    filename: Mapped[str | None] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(String(128))
    storage_backend: Mapped[str] = mapped_column(String(32), default="local")
    storage_path: Mapped[str | None] = mapped_column(Text)
    sha256: Mapped[str | None] = mapped_column(String(64))
    version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)
    knowledge_scopes: Mapped[list[str]] = mapped_column(JSONType, default=list)
    ingested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DocumentChunk(Base, TimestampMixin):
    """Retrieval chunks for a source document (s14 'chunks if using retrieval')."""

    __tablename__ = "document_chunks"
    __table_args__ = (UniqueConstraint("document_id", "chunk_index", name="uq_document_chunk"),)

    id: Mapped[uuid.UUID] = _pk()
    document_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("source_documents.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("projects.id", ondelete="SET NULL"), index=True
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    locator: Mapped[str | None] = mapped_column(Text)
    token_count: Mapped[int | None] = mapped_column(Integer)
