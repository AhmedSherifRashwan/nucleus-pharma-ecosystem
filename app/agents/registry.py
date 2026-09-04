"""Versioned agent registry (PRD s11, Phase 1).

The 16 specialists are YAML validated into AgentProfile objects at load time.
Loading FAILS if any profile is malformed, references an unknown tool, points at
a missing prompt, or names an unknown follow-on agent. A silently degraded
registry is worse than a refusal to start (s2 fail closed).
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any

import yaml

from app.config import get_settings
from app.schemas.agent import AgentProfile

# PRD s19 is the authoritative tool list. A profile may not reference anything
# outside it; AMBER/RED tools are never assigned to a specialist profile.
KNOWN_TOOLS: set[str] = {
    "project_search",
    "read_artifact",
    "save_artifact",
    "save_evidence",
    "calculator",
    "web_research_official",
}

RESTRICTED_TOOLS: set[str] = {"external_message", "external_submission"}

KNOWN_KNOWLEDGE_SCOPES: set[str] = {
    "project_documents",
    "regulatory_sources",
    "formulation_data",
    "analytical_data",
    "stability_data",
    "manufacturing_data",
    "quality_records",
    "commercial_data",
    "evidence_store",
}


class RegistryError(RuntimeError):
    """The registry cannot be loaded in a trustworthy state."""


class AgentRegistry:
    def __init__(self, profiles: dict[str, AgentProfile], prompts: dict[str, str]):
        self._profiles = profiles
        self._prompts = prompts

    # -- lookup -------------------------------------------------------------

    def get(self, agent_id: str) -> AgentProfile:
        try:
            return self._profiles[agent_id]
        except KeyError as exc:
            raise KeyError(f"unknown agent id: {agent_id}") from exc

    def get_by_slug(self, slug: str) -> AgentProfile:
        for profile in self._profiles.values():
            if profile.slug == slug:
                return profile
        raise KeyError(f"unknown agent slug: {slug}")

    def exists(self, agent_id: str) -> bool:
        return agent_id in self._profiles

    def all_profiles(self) -> list[AgentProfile]:
        return sorted(self._profiles.values(), key=lambda p: p.id)

    def enabled_profiles(self) -> list[AgentProfile]:
        return [p for p in self.all_profiles() if p.enabled]

    def enabled_ids(self) -> set[str]:
        return {p.id for p in self.enabled_profiles()}

    def domain_prompt(self, agent_id: str) -> str:
        return self._prompts.get(agent_id, "")

    def prompt_sha256(self, agent_id: str) -> str:
        return hashlib.sha256(self._prompts.get(agent_id, "").encode()).hexdigest()

    # -- planner input ------------------------------------------------------

    def catalog(self) -> list[dict[str, Any]]:
        """Compact catalog for the planner. Never includes prompts."""
        return [p.catalog_entry() for p in self.enabled_profiles()]

    def routing_matrix(self) -> dict[str, list[str]]:
        """s12.1 follow-on map, filtered to enabled profiles."""
        enabled = self.enabled_ids()
        return {
            p.id: [a for a in p.common_follow_on if a in enabled] for p in self.enabled_profiles()
        }

    def __len__(self) -> int:
        return len(self._profiles)


def load_registry(
    definitions_path: str | Path | None = None,
    prompts_path: str | Path | None = None,
    *,
    enable_provisional: bool | None = None,
) -> AgentRegistry:
    settings = get_settings()
    defs_dir = Path(definitions_path or settings.agent_definitions_path)
    prompts_dir = Path(prompts_path or settings.worker_prompts_path)
    allow_provisional = (
        settings.enable_provisional_profiles if enable_provisional is None else enable_provisional
    )

    if not defs_dir.is_dir():
        raise RegistryError(f"agent definitions directory not found: {defs_dir}")

    profiles: dict[str, AgentProfile] = {}
    prompts: dict[str, str] = {}
    problems: list[str] = []
    slugs: dict[str, str] = {}

    files = sorted(defs_dir.glob("*.yaml")) + sorted(defs_dir.glob("*.yml"))
    if not files:
        raise RegistryError(f"no agent definitions found in {defs_dir}")

    for path in files:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except yaml.YAMLError as exc:
            problems.append(f"{path.name}: invalid YAML - {exc}")
            continue
        if not isinstance(raw, dict):
            problems.append(f"{path.name}: expected a mapping at the top level")
            continue

        try:
            profile = AgentProfile.model_validate(raw)
        except Exception as exc:
            problems.append(f"{path.name}: validation failed - {exc}")
            continue

        if profile.id in profiles:
            problems.append(f"{path.name}: duplicate agent id {profile.id}")
            continue
        if profile.slug in slugs:
            problems.append(
                f"{path.name}: duplicate slug '{profile.slug}' (also {slugs[profile.slug]})"
            )
            continue
        slugs[profile.slug] = path.name

        for tool in profile.allowed_tools:
            if tool in RESTRICTED_TOOLS:
                problems.append(
                    f"{path.name} ({profile.id}): '{tool}' is an AMBER/RED tool and "
                    "must not be assigned to a specialist profile (s19)"
                )
            elif tool not in KNOWN_TOOLS:
                problems.append(f"{path.name} ({profile.id}): unknown tool '{tool}'")

        for scope in profile.knowledge_scopes:
            if scope not in KNOWN_KNOWLEDGE_SCOPES:
                problems.append(f"{path.name} ({profile.id}): unknown knowledge scope '{scope}'")

        # An active profile must name the client document it derives from (s11).
        if profile.status == "active" and not profile.source_sds:
            problems.append(
                f"{path.name} ({profile.id}): an active profile must record its source_sds"
            )

        prompt_path = prompts_dir / profile.prompt_file
        if not prompt_path.is_file():
            problems.append(f"{path.name} ({profile.id}): prompt not found: {prompt_path}")
        else:
            prompts[profile.id] = prompt_path.read_text(encoding="utf-8")

        # Provisional profiles stay off unless explicitly enabled (s11).
        if profile.is_provisional and not allow_provisional:
            profile = profile.model_copy(update={"enabled": False})

        profiles[profile.id] = profile

    for profile in profiles.values():
        for target in profile.common_follow_on:
            if target not in profiles:
                problems.append(f"{profile.id}: common_follow_on names unknown agent {target}")

    if problems:
        raise RegistryError("agent registry failed validation:\n  - " + "\n  - ".join(problems))

    return AgentRegistry(profiles, prompts)


_registry: AgentRegistry | None = None


def get_registry() -> AgentRegistry:
    global _registry
    if _registry is None:
        _registry = load_registry()
    return _registry


def reset_registry() -> None:
    global _registry
    _registry = None
