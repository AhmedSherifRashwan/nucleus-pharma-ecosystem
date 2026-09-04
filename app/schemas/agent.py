"""Versioned specialist profile contract (PRD s11, s41.1 rule 5).

The 16 agents are configuration, not code: a profile carries the prompt, tools,
permissions, knowledge scopes, limits and escalation rules that one generic
runtime executes. Adding A17 later is a new YAML plus prompt plus tests, never a
new service (NFR maintainability).

Each profile records the client SDS it derives from. Role scope must never be
silently broadened from that source document (CLAUDE.md rule 12).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

ProfileStatus = Literal["active", "provisional", "retired"]

# Which OpenClaw route executes this profile (s7.3). Never a provider model ID.
ProfileRoute = Literal["specialist", "executive", "local"]


class EscalationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    triggers: list[str] = Field(default_factory=list)
    action: str = "Surface to the orchestrator; do not proceed on missing evidence."


class AgentProfile(BaseModel):
    """One of the 16 logical specialists."""

    model_config = ConfigDict(extra="forbid")

    id: str = Field(pattern=r"^A\d{2}$")
    slug: str = Field(pattern=r"^[a-z][a-z0-9_]*$")
    name: str
    version: str = Field(pattern=r"^\d+\.\d+\.\d+$")
    status: ProfileStatus = "active"
    # The client document this role derives from. None only for provisional
    # profiles, which is exactly why they stay disabled (s11).
    source_sds: str | None = None

    route: ProfileRoute = "specialist"

    operating_persona: str = Field(min_length=3)
    purpose: str = Field(min_length=10)
    mission: str = Field(min_length=10)

    core_responsibilities: list[str] = Field(default_factory=list)
    key_inputs: list[str] = Field(default_factory=list)
    key_outputs: list[str] = Field(default_factory=list)
    knowledge_base: list[str] = Field(default_factory=list)
    functional_modules: list[str] = Field(default_factory=list)
    quality_controls: list[str] = Field(default_factory=list)

    # s12.1 routing matrix: which follow-on specialists commonly consume this
    # profile's output. Advisory input to the planner, never a hard rule.
    common_follow_on: list[str] = Field(default_factory=list)
    lifecycle_stage: str | None = None

    allowed_tools: list[str] = Field(default_factory=list)
    knowledge_scopes: list[str] = Field(default_factory=list)
    forbidden_actions: list[str] = Field(default_factory=list)

    output_schema: Literal["SpecialistResult"] = "SpecialistResult"
    evidence_policy: str = "evidence_required_for_material_conclusions"
    escalation_policy: EscalationPolicy = Field(default_factory=EscalationPolicy)

    max_steps: int = Field(default=12, ge=1, le=50)
    max_output_tokens: int = Field(default=8000, ge=256, le=64000)

    prompt_file: str
    enabled: bool = True

    @field_validator("allowed_tools", "knowledge_scopes", "forbidden_actions")
    @classmethod
    def _no_duplicates(cls, v: list[str]) -> list[str]:
        if len(set(v)) != len(v):
            raise ValueError("duplicate entries are not allowed")
        return v

    @field_validator("common_follow_on")
    @classmethod
    def _valid_agent_ids(cls, v: list[str]) -> list[str]:
        for agent_id in v:
            if not (len(agent_id) == 3 and agent_id[0] == "A" and agent_id[1:].isdigit()):
                raise ValueError(f"invalid agent id in common_follow_on: {agent_id}")
        return v

    @property
    def is_provisional(self) -> bool:
        return self.status == "provisional"

    def catalog_entry(self) -> dict[str, Any]:
        """Compact description given to the planner. Never the full prompt."""
        return {
            "agent_id": self.id,
            "name": self.name,
            "operating_persona": self.operating_persona,
            "purpose": self.purpose,
            "key_inputs": self.key_inputs,
            "key_outputs": self.key_outputs,
            "lifecycle_stage": self.lifecycle_stage,
            "common_follow_on": self.common_follow_on,
            "restrictions": self.forbidden_actions,
            "status": self.status,
        }
