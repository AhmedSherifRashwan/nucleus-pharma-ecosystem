"""Single-specialist runs (PRD s9.1).

When the user names the specialist, there is nothing for the executive planner to
decide. Building the plan in code instead of asking a model for it makes the run
faster, cheaper and exactly what was asked for - the planner cannot reinterpret a
direct instruction into a different team.

Everything downstream is unchanged: the same envelope, the same evidence rules,
the same challenge and quality gates, the same report.
"""

from __future__ import annotations

from app.agents.registry import AgentRegistry
from app.schemas.common import OutputType
from app.schemas.planning import Plan, PlannedTask

DIRECT_TASK_ID = "T1"
MAX_OBJECTIVE_CHARS = 2000


def build_direct_plan(
    agent_id: str,
    request: str,
    *,
    registry: AgentRegistry,
    max_steps: int = 12,
    output_type: OutputType = "brief",
) -> Plan:
    """One task for one named specialist."""
    profile = registry.get(agent_id)
    objective = (request or "").strip()
    if not objective:
        raise ValueError(f"a direct request to {agent_id} needs a task to perform")

    return Plan(
        objective=f"{profile.name}: {objective[:MAX_OBJECTIVE_CHARS]}",
        output_type=output_type,
        tasks=[
            PlannedTask(
                task_id=DIRECT_TASK_ID,
                agent_id=agent_id,
                objective=objective[:MAX_OBJECTIVE_CHARS],
                required_output=(
                    "A specialist assessment answering the request, with findings, "
                    "evidence references, assumptions and anything you need but do "
                    "not have."
                ),
                depends_on=[],
                context_refs=["user_request"],
                max_steps=max_steps,
            )
        ],
        # The user chose the specialist; that is the scope decision. The plan gate
        # would be asking them to approve what they just asked for. The AMBER/RED
        # decision gate after synthesis is untouched.
        plan_approval_required=False,
        approval_rationale=f"Direct request to {agent_id} ({profile.name}).",
    )
