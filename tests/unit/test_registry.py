"""Agent registry loading and validation (PRD s11, s12.1, s19, Phase 1)."""

from __future__ import annotations

import pytest

from app.agents.prompt_builder import compose_worker_prompt
from app.agents.registry import RegistryError, load_registry

ALL_IDS = {f"A{i:02d}" for i in range(1, 17)}
PROVISIONAL = {"A15", "A16"}


@pytest.fixture(scope="module")
def registry():
    return load_registry(enable_provisional=False)


@pytest.fixture(scope="module")
def full(registry):
    return load_registry(enable_provisional=True)


# -- roster ------------------------------------------------------------------


def test_all_sixteen_profiles_load(full):
    assert len(full) == 16
    assert {p.id for p in full.all_profiles()} == ALL_IDS


def test_only_a15_a16_are_provisional(full):
    assert {p.id for p in full.all_profiles() if p.is_provisional} == PROVISIONAL


def test_provisional_profiles_are_disabled_by_default(registry):
    assert registry.enabled_ids() == ALL_IDS - PROVISIONAL


def test_provisional_profiles_enable_only_on_opt_in(full):
    assert full.enabled_ids() == ALL_IDS


# -- SDS traceability (s11, CLAUDE.md rule 12) -------------------------------


def test_every_active_profile_names_its_source_sds(full):
    for profile in full.all_profiles():
        if profile.status == "active":
            assert profile.source_sds, f"{profile.id} has no source_sds"
            assert profile.source_sds.endswith(".docx")


def test_provisional_profiles_have_no_sds(full):
    """A15/A16 exist because the framework names them, not because an SDS does."""
    for pid in PROVISIONAL:
        assert full.get(pid).source_sds is None


def test_source_documents_are_present_in_the_repo(full):
    from pathlib import Path

    docs = Path("docs/source-specifications")
    for profile in full.all_profiles():
        if profile.source_sds:
            assert (docs / profile.source_sds).is_file(), (
                f"{profile.id} cites a missing source document"
            )


# -- tool policy (s19) -------------------------------------------------------


def test_no_profile_holds_an_amber_or_red_tool(full):
    restricted = {"external_message", "external_submission"}
    for profile in full.all_profiles():
        assert not (set(profile.allowed_tools) & restricted), (
            f"{profile.id} holds a restricted tool"
        )


def test_all_tools_are_in_the_prd_tool_table(full):
    from app.agents.registry import KNOWN_TOOLS

    for profile in full.all_profiles():
        unknown = set(profile.allowed_tools) - KNOWN_TOOLS
        assert not unknown, f"{profile.id} references tools outside s19: {unknown}"


def test_regulatory_intelligence_uses_official_sources_only(full):
    p = full.get("A02")
    assert "web_research_official" in p.allowed_tools
    assert p.evidence_policy == "official_sources_preferred"


def test_governance_profiles_cannot_act_autonomously(full):
    """QA, QC and Validation are advisory in v3 (s11 restrictions)."""
    for agent_id, forbidden in (
        ("A12", "approving_validation_protocol"),
        ("A13", "autonomous_batch_release"),
        ("A14", "autonomous_quality_release"),
    ):
        assert forbidden in full.get(agent_id).forbidden_actions


# -- routing matrix (s12.1) --------------------------------------------------


def test_routing_matrix_matches_the_prd(full):
    expected = {
        "A01": ["A02", "A05", "A11", "A14"],
        "A03": ["A04", "A05"],
        "A09": ["A10", "A12", "A13"],
        "A14": ["A12", "A13", "A15"],
    }
    for agent_id, follow_on in expected.items():
        assert full.get(agent_id).common_follow_on == follow_on


def test_routing_matrix_excludes_disabled_agents(registry):
    """A14 -> A15 must drop out while A15 is disabled."""
    assert "A15" not in registry.routing_matrix()["A14"]
    assert "A15" not in registry.routing_matrix()


# -- escalation and evidence -------------------------------------------------


def test_every_profile_defines_escalation_behaviour(full):
    for profile in full.all_profiles():
        assert profile.escalation_policy.triggers, f"{profile.id} has no triggers"
        assert profile.escalation_policy.action.strip()


def test_every_profile_returns_the_specialist_result_contract(full):
    for profile in full.all_profiles():
        assert profile.output_schema == "SpecialistResult"
        assert profile.route == "specialist"


# -- planner catalog ---------------------------------------------------------


def test_catalog_never_leaks_prompts(registry):
    for entry in registry.catalog():
        assert "prompt" not in " ".join(entry.keys()).lower()


def test_catalog_excludes_provisional_profiles(registry):
    assert not ({e["agent_id"] for e in registry.catalog()} & PROVISIONAL)


# -- prompt composition ------------------------------------------------------


def test_every_prompt_composes_without_placeholders(full):
    for profile in full.all_profiles():
        prompt = compose_worker_prompt(profile, full.domain_prompt(profile.id))
        assert "{{" not in prompt, f"{profile.id} left a placeholder"


def test_prompt_states_the_ecosystem_name(full):
    prompt = compose_worker_prompt(full.get("A01"), full.domain_prompt("A01"))
    assert "Neucleus" in prompt


def test_prompt_carries_the_evidence_basis_rule(full):
    """The prompt must state the rule the schema enforces (s15, FR-007)."""
    for profile in full.all_profiles():
        prompt = compose_worker_prompt(profile, full.domain_prompt(profile.id))
        assert "basis" in prompt
        assert "MUST carry at least one evidence reference" in prompt


def test_prompt_states_the_untrusted_data_boundary(full):
    for profile in full.all_profiles():
        prompt = compose_worker_prompt(profile, full.domain_prompt(profile.id))
        assert "UNTRUSTED DATA" in prompt
        assert "never obeyed" in prompt


def test_prompt_carries_profile_specific_prohibitions(full):
    prompt = compose_worker_prompt(full.get("A13"), full.domain_prompt("A13"))
    assert "autonomous_batch_release" in prompt


def test_every_domain_prompt_has_real_content(full):
    for profile in full.all_profiles():
        assert len(full.domain_prompt(profile.id)) > 400, f"{profile.id} prompt is thin"


# -- failure modes -----------------------------------------------------------


def _write_profile(tmp_path, body: str, prompt: bool = True):
    (tmp_path / "prompts").mkdir(exist_ok=True)
    if prompt:
        (tmp_path / "prompts" / "p.md").write_text("x" * 500)
    (tmp_path / "A99_test.yaml").write_text(body)
    return tmp_path, tmp_path / "prompts"


BASE = """
id: A99
slug: test_profile
name: Test Profile
version: "1.0.0"
status: active
source_sds: AI_Framework_Generic_Softgel_Agents.docx
operating_persona: Tester.
purpose: A purpose long enough to pass.
mission: A mission long enough to pass.
prompt_file: p.md
"""


def test_unknown_tool_fails_loading(tmp_path):
    defs, prompts = _write_profile(tmp_path, BASE + "allowed_tools: [not_a_tool]\n")
    with pytest.raises(RegistryError, match="unknown tool"):
        load_registry(defs, prompts)


def test_restricted_tool_fails_loading(tmp_path):
    defs, prompts = _write_profile(tmp_path, BASE + "allowed_tools: [external_submission]\n")
    with pytest.raises(RegistryError, match="AMBER/RED"):
        load_registry(defs, prompts)


def test_unknown_knowledge_scope_fails_loading(tmp_path):
    defs, prompts = _write_profile(tmp_path, BASE + "knowledge_scopes: [everything]\n")
    with pytest.raises(RegistryError, match="unknown knowledge scope"):
        load_registry(defs, prompts)


def test_missing_prompt_fails_loading(tmp_path):
    defs, prompts = _write_profile(tmp_path, BASE, prompt=False)
    with pytest.raises(RegistryError, match="prompt not found"):
        load_registry(defs, prompts)


def test_active_profile_without_sds_fails_loading(tmp_path):
    body = BASE.replace("source_sds: AI_Framework_Generic_Softgel_Agents.docx\n", "")
    defs, prompts = _write_profile(tmp_path, body)
    with pytest.raises(RegistryError, match="must record its source_sds"):
        load_registry(defs, prompts)


def test_unknown_follow_on_agent_fails_loading(tmp_path):
    defs, prompts = _write_profile(tmp_path, BASE + "common_follow_on: [A77]\n")
    with pytest.raises(RegistryError, match="unknown agent A77"):
        load_registry(defs, prompts)
