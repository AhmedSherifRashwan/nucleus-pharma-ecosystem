"""Inter-agent communication protocol (PRD s13).

Substantive collaboration uses structured messages or persisted artifacts, never
unrestricted conversation. Specialists may request follow-up work; they do not
autonomously spawn unlimited new agents.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from pydantic import Field

from app.schemas.common import ConfidenceLevel, Severity, StrictModel
from app.schemas.specialist import EvidenceRef


class AgentMessage(StrictModel):
    message_id: str = Field(default_factory=lambda: f"MSG-{uuid.uuid4().hex[:12]}")
    from_agent: str = Field(pattern=r"^A\d{2}$")
    to_agent: str = Field(pattern=r"^A\d{2}$")
    run_id: str
    task_id: str | None = None
    priority: Severity = "medium"
    purpose: str = Field(min_length=1)
    question_or_request: str | None = None
    background: str | None = None
    finding: str | None = None
    evidence_refs: list[EvidenceRef] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    confidence: ConfidenceLevel = "medium"
    implication: str | None = None
    action_requested: str | None = None
    created_at: datetime = Field(default_factory=lambda: datetime.now(UTC))
