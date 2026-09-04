"""Generic specialist runtime (PRD s10, s15, Phase 5)."""

from __future__ import annotations

import pytest

from app.agents.registry import load_registry
from app.orchestration.specialist_runner import (
    SpecialistRunner,
    build_envelope,
    build_task_message,
    find_evidence_violations,
    summarise_for_downstream,
)
from app.providers.openclaw_client import AgentRoute, RouteInfo, Usage
from app.schemas.specialist import EvidenceRef, Finding, SpecialistResult


@pytest.fixture(scope="module")
def registry():
    return load_registry(enable_provisional=True)


def _result(**kw) -> SpecialistResult:
    base = {
        "task_id": "T1",
        "agent_id": "A07",
        "status": "complete",
        "executive_summary": "Shell assessment complete.",
    }
    return SpecialistResult(**{**base, **kw})


class FakeClient:
    """Stub gateway. Returns queued results and records the prompts sent."""

    def __init__(self, *results):
        self._results = list(results)
        self.calls: list[dict] = []

    async def run_specialist(self, *, system, user, result_schema, idempotency_key=None):
        self.calls.append({"system": system, "user": user, "idempotency_key": idempotency_key})
        result = self._results.pop(0)
        info = RouteInfo(
            route=AgentRoute.SPECIALIST,
            agent_id="pharma-specialist",
            auth_mode="subscription",
            provider="claude-cli",
            model="claude-sonnet-5",
        )
        return result, Usage(duration_ms=100, steps=1), info


def _runner(registry, *results) -> tuple[SpecialistRunner, FakeClient]:
    client = FakeClient(*results)
    return SpecialistRunner(registry=registry, client=client), client


def _envelope(registry, agent_id="A07", **kw):
    return build_envelope(
        run_id="run-1",
        task_id=kw.pop("task_id", "T1"),
        profile=registry.get(agent_id),
        objective=kw.pop("objective", "Assess shell-fill compatibility for this fill."),
        required_output=kw.pop("required_output", "A compatibility assessment."),
        **kw,
    )


# -- envelope construction (s10.1) -------------------------------------------


def test_envelope_inherits_profile_policy(registry):
    env = _envelope(registry, "A07")
    profile = registry.get("A07")
    assert env.allowed_tools == profile.allowed_tools
    assert env.knowledge_scopes == profile.knowledge_scopes
    assert env.restrictions == profile.forbidden_actions
    assert env.escalation_rules == profile.escalation_policy.triggers


def test_envelope_clamps_steps_to_the_profile_limit(registry):
    env = _envelope(registry, "A07", max_steps=999)
    assert env.max_steps == registry.get("A07").max_steps


# -- task message (s10.1 context isolation) ----------------------------------


def test_message_marks_upstream_work_as_fallible(registry):
    env = _envelope(
        registry,
        upstream_findings=[
            {
                "agent_id": "A03",
                "agent_name": "QTPP",
                "task_id": "T0",
                "status": "complete",
                "confidence": "high",
                "executive_summary": "QTPP approved.",
                "evidence_refs": ["E-1"],
            }
        ],
    )
    msg = build_task_message(env, registry.get("A07"))
    assert "not instructions to you" in msg
    assert "they may be wrong" in msg
    assert "conflicts_or_questions" in msg
    assert "QTPP approved." in msg


def test_message_omits_upstream_block_when_no_dependencies(registry):
    msg = build_task_message(_envelope(registry), registry.get("A07"))
    assert "upstream_specialist_findings" not in msg


def test_message_lists_only_the_profile_tools(registry):
    msg = build_task_message(_envelope(registry, "A03"), registry.get("A03"))
    profile = registry.get("A03")
    for tool in profile.allowed_tools:
        assert tool in msg
    # A03 has no web research; it must not be offered one.
    assert "web_research_official" not in msg


def test_message_warns_against_answering_from_recollection(registry):
    """A specialist with no tools must disclose the gap, not fake retrieval."""
    profile = registry.get("A07").model_copy(update={"allowed_tools": []})
    env = build_envelope(
        run_id="r",
        task_id="T1",
        profile=profile,
        objective="Assess something material here.",
        required_output="Assessment",
    )
    msg = build_task_message(env, profile)
    assert "answering from recollection" in msg


# -- evidence enforcement (s15, FR-007) --------------------------------------


def test_fact_without_evidence_is_a_violation():
    r = _result(findings=[Finding(statement="Bloom is 180.", basis="fact")])
    assert len(find_evidence_violations(r)) == 1


def test_labelled_assumption_is_not_a_violation():
    r = _result(findings=[Finding(statement="Assume gelatin.", basis="assumption")])
    assert find_evidence_violations(r) == []


async def test_violation_triggers_one_correction(registry):
    bad = _result(findings=[Finding(statement="Bloom is 180.", basis="fact")])
    good = _result(
        findings=[
            Finding(
                statement="Bloom is 180.",
                basis="fact",
                evidence_refs=[EvidenceRef(evidence_id="E-1")],
            )
        ]
    )
    runner, client = _runner(registry, bad, good)
    outcome = await runner.run(registry.get("A07"), _envelope(registry))

    assert outcome.corrected
    assert not outcome.has_evidence_violations
    assert len(client.calls) == 2
    correction = client.calls[1]["user"]
    assert "violates the evidence policy" in correction
    assert "Do not invent an evidence reference" in correction


async def test_correction_offers_relabelling_as_an_honest_alternative(registry):
    """The fix may be to admit it is an assumption, not to manufacture a citation."""
    bad = _result(findings=[Finding(statement="Likely cross-linking.", basis="inference")])
    relabelled = _result(findings=[Finding(statement="Likely cross-linking.", basis="assumption")])
    runner, client = _runner(registry, bad, relabelled)
    outcome = await runner.run(registry.get("A07"), _envelope(registry))

    assert not outcome.has_evidence_violations
    assert "change its `basis` to 'assumption'" in client.calls[1]["user"]


async def test_persistent_violations_are_reported_not_hidden(registry):
    """The runner never relabels a claim on the specialist's behalf."""
    bad = _result(findings=[Finding(statement="Bloom is 180.", basis="fact")])
    still_bad = _result(findings=[Finding(statement="Bloom is 180.", basis="fact")])
    runner, client = _runner(registry, bad, still_bad)
    outcome = await runner.run(registry.get("A07"), _envelope(registry))

    assert outcome.has_evidence_violations
    assert outcome.result.findings[0].basis == "fact"  # unchanged, not falsified
    assert len(client.calls) == 2  # bounded: exactly one correction


async def test_clean_result_makes_one_call(registry):
    clean = _result(findings=[Finding(statement="Assume gelatin.", basis="assumption")])
    runner, client = _runner(registry, clean)
    outcome = await runner.run(registry.get("A07"), _envelope(registry))
    assert not outcome.corrected
    assert len(client.calls) == 1


async def test_enforcement_can_be_disabled(registry):
    bad = _result(findings=[Finding(statement="Bloom is 180.", basis="fact")])
    runner, client = _runner(registry, bad)
    outcome = await runner.run(registry.get("A07"), _envelope(registry), enforce_evidence=False)
    assert len(client.calls) == 1
    assert not outcome.has_evidence_violations


# -- identity and safety -----------------------------------------------------


async def test_task_identity_is_owned_by_the_platform(registry):
    """A model that returns the wrong ids must not corrupt the run record."""
    wrong = _result(task_id="whatever", agent_id="A01")
    runner, _ = _runner(registry, wrong)
    outcome = await runner.run(registry.get("A07"), _envelope(registry, task_id="T9"))
    assert outcome.result.task_id == "T9"
    assert outcome.result.agent_id == "A07"


async def test_disabled_profile_refuses_to_run(registry):
    """A15/A16 are provisional; running one would breach s11."""
    provisional = registry.get("A15").model_copy(update={"enabled": False})
    runner, _ = _runner(registry, _result())
    with pytest.raises(ValueError, match="disabled"):
        await runner.run(
            provisional,
            build_envelope(
                run_id="r",
                task_id="T1",
                profile=provisional,
                objective="Assess GMP compliance here.",
                required_output="Assessment",
            ),
        )


async def test_envelope_profile_mismatch_is_refused(registry):
    runner, _ = _runner(registry, _result())
    with pytest.raises(ValueError, match="targets A07 but profile is A08"):
        await runner.run(registry.get("A08"), _envelope(registry, "A07"))


async def test_idempotency_key_carries_run_and_task(registry):
    """s31.2: the key identifies the work, so a retry is not duplicated."""
    runner, client = _runner(registry, _result())
    await runner.run(registry.get("A07"), _envelope(registry, task_id="T4"))
    assert client.calls[0]["idempotency_key"] == "run-1:T4"


# -- downstream summary (s10.3) ----------------------------------------------


async def test_downstream_summary_omits_working_detail(registry):
    clean = _result(
        executive_summary="Cross-linking is the leading hypothesis.",
        findings=[Finding(statement="Detailed internal reasoning.", basis="assumption")],
        evidence_refs=[EvidenceRef(evidence_id="E-1")],
    )
    runner, _ = _runner(registry, clean)
    outcome = await runner.run(registry.get("A07"), _envelope(registry))

    summary = summarise_for_downstream(outcome)
    assert summary["executive_summary"] == "Cross-linking is the leading hypothesis."
    assert summary["evidence_refs"] == ["E-1"]
    assert summary["agent_name"] == "Softgel Shell Formulation Expert"
    # The next specialist gets conclusions, not the full working detail.
    assert "findings" not in summary
    assert "Detailed internal reasoning" not in str(summary)
