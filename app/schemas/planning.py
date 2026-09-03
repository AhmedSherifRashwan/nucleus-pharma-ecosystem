"""Executive planning contracts (PRD s9.1)."""

from __future__ import annotations

from pydantic import Field, field_validator, model_validator

from app.schemas.common import OutputType, StrictModel


class PlannedTask(StrictModel):
    """One specialist task within a Plan. Never submit this as the top-level object."""

    task_id: str = Field(min_length=1, max_length=32)
    agent_id: str = Field(pattern=r"^A\d{2}$")
    objective: str = Field(min_length=10)
    required_output: str = Field(min_length=3)
    depends_on: list[str] = Field(default_factory=list)
    context_refs: list[str] = Field(default_factory=list)
    max_steps: int = Field(default=12, ge=1, le=50)

    @model_validator(mode="after")
    def _no_self_dependency(self) -> PlannedTask:
        if self.task_id in self.depends_on:
            raise ValueError(f"task {self.task_id} depends on itself")
        return self


class Plan(StrictModel):
    """The complete execution plan: the TOP-LEVEL object the planner submits.

    Contains `objective`, `tasks` (a LIST of task objects) and the identified
    information gaps. Do not submit a single task in place of the plan.
    """

    objective: str = Field(min_length=5)
    output_type: OutputType = "brief"
    tasks: list[PlannedTask] = Field(min_length=1)
    information_gaps: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    estimated_complexity: str | None = None
    plan_approval_required: bool = False
    approval_rationale: str | None = None
    # Recomputed deterministically from dependencies by the validator; any
    # planner-supplied ordering is advisory only (s8).
    execution_groups: list[list[str]] = Field(default_factory=list)

    @property
    def selected_agents(self) -> list[str]:
        seen: list[str] = []
        for task in self.tasks:
            if task.agent_id not in seen:
                seen.append(task.agent_id)
        return seen

    @field_validator("tasks")
    @classmethod
    def _unique_task_ids(cls, v: list[PlannedTask]) -> list[PlannedTask]:
        ids = [t.task_id for t in v]
        if len(set(ids)) != len(ids):
            raise ValueError("duplicate task ids in plan")
        return v


class RunBudget(StrictModel):
    """Enforced in code, never by prompt instruction (s12.2, s22)."""

    max_specialists: int = Field(default=6, ge=1, le=16)
    max_parallel_specialists: int = Field(default=4, ge=1, le=16)
    max_followup_rounds: int = Field(default=1, ge=0, le=3)
    max_specialist_steps: int = Field(default=12, ge=1, le=50)
    max_runtime_seconds: int = Field(default=1200, ge=60)
