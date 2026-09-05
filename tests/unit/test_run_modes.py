"""How much of the ecosystem a request engages (PRD s9.1)."""

from __future__ import annotations

import pytest

from app.orchestration.direct import build_direct_plan
from app.orchestration.modes import detect_mode
from app.orchestration.planner import full_ecosystem_instruction

IDS = {"A01", "A03", "A07", "A14"}
SLUGS = {"qtpp": "A03", "shell_formulation": "A07", "quality_assurance": "A14"}


def decide(text: str, **kw):
    return detect_mode(text, known_ids=IDS, slug_to_id=SLUGS, **kw)


# -- standard is the default -------------------------------------------------


def test_an_ordinary_request_is_standard():
    d = decide("Provide a QTPP for Midostaurin 25mg softgels.")
    assert d.mode == "standard"
    assert d.agent_id is None


def test_a_broad_question_is_not_silently_escalated():
    """Inferring 'they probably want everyone' would spend an hour on a guess."""
    d = decide(
        "Give me a complete assessment covering regulatory, formulation, "
        "analytical, stability and quality for this product."
    )
    assert d.mode == "standard"


# -- full ecosystem ----------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "Use all 16 agents to assess this.",
        "use all sixteen agents please",
        "Run the whole ecosystem on this question.",
        "I want the full ecosystem for this.",
        "every specialist should look at this",
        "all agents please",
    ],
)
def test_an_explicit_request_for_everyone_is_honoured(text):
    d = decide(text)
    assert d.mode == "full", text
    assert d.matched_on


def test_the_full_instruction_names_every_enabled_agent():
    from app.agents.registry import get_registry

    registry = get_registry()
    text = full_ecosystem_instruction(registry)
    for profile in registry.enabled_profiles():
        assert profile.id in text
    assert "Do not narrow the team" in text


# -- direct ------------------------------------------------------------------


def test_a_slash_command_by_id_routes_to_that_specialist():
    d = decide("/A07 assess cross-linking risk for a PEG 400 fill")
    assert d.mode == "direct"
    assert d.agent_id == "A07"
    assert d.request == "assess cross-linking risk for a PEG 400 fill"


def test_a_slash_command_by_name_routes_the_same_way():
    d = decide("/shell-formulation assess cross-linking risk")
    assert (d.mode, d.agent_id) == ("direct", "A07")
    assert d.request == "assess cross-linking risk"


def test_the_command_prefix_never_reaches_the_specialist():
    """The specialist gets the task, not the routing instruction."""
    assert not decide("/qtpp draft the QTPP").request.startswith("/")


def test_an_unknown_command_is_left_as_an_ordinary_request():
    d = decide("/not-an-agent do something")
    assert d.mode == "standard"
    assert d.agent_id is None


def test_a_direct_request_with_no_task_is_still_detected():
    """Caught at intake with a clear message rather than silently planned."""
    d = decide("/A07")
    assert d.mode == "direct" and d.request == ""


def test_an_explicit_argument_beats_the_text():
    d = decide("use all 16 agents", explicit_agent="A03")
    assert (d.mode, d.agent_id) == ("direct", "A03")


# -- the direct plan ---------------------------------------------------------


def test_a_direct_plan_has_one_task_for_the_named_specialist():
    from app.agents.registry import get_registry

    plan = build_direct_plan("A07", "Assess cross-linking risk.", registry=get_registry())
    assert len(plan.tasks) == 1
    assert plan.tasks[0].agent_id == "A07"
    assert plan.tasks[0].objective == "Assess cross-linking risk."
    assert plan.selected_agents == ["A07"]


def test_a_direct_plan_does_not_ask_the_user_to_approve_their_own_choice():
    from app.agents.registry import get_registry

    plan = build_direct_plan("A07", "Assess the shell.", registry=get_registry())
    assert plan.plan_approval_required is False


def test_a_direct_plan_needs_a_task():
    from app.agents.registry import get_registry

    with pytest.raises(ValueError, match="needs a task"):
        build_direct_plan("A07", "   ", registry=get_registry())
