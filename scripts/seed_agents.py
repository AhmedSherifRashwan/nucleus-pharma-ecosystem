"""Seed agent profile and prompt version records (PRD Phase 1, FR-018).

Idempotent. Every run records the exact profile and prompt version used, so
these snapshots must exist before any run executes.
"""

from __future__ import annotations

import asyncio
import hashlib
import re
import sys
from pathlib import Path

from sqlalchemy import select

from app.agents.registry import load_registry
from app.audit.logger import configure_logging, get_logger
from app.config import get_settings
from app.storage.db import dispose_engine, session_scope
from app.storage.models import AgentProfileRecord, PromptVersion

log = get_logger(__name__)


async def seed() -> int:
    settings = get_settings()
    registry = load_registry(enable_provisional=True)

    created = updated = prompts_created = 0

    async with session_scope() as session:
        for profile in registry.all_profiles():
            enabled = profile.enabled and (
                not profile.is_provisional or settings.enable_provisional_profiles
            )
            record = await session.get(AgentProfileRecord, profile.id)
            snapshot = profile.model_dump(mode="json")
            if record is None:
                session.add(
                    AgentProfileRecord(
                        id=profile.id,
                        slug=profile.slug,
                        name=profile.name,
                        version=profile.version,
                        operating_persona=profile.operating_persona,
                        purpose=profile.purpose,
                        source_sds=profile.source_sds,
                        status=profile.status,
                        enabled=enabled,
                        profile_snapshot=snapshot,
                    )
                )
                created += 1
            else:
                record.slug = profile.slug
                record.name = profile.name
                record.version = profile.version
                record.operating_persona = profile.operating_persona
                record.purpose = profile.purpose
                record.source_sds = profile.source_sds
                record.status = profile.status
                record.enabled = enabled
                record.profile_snapshot = snapshot
                updated += 1

            # The domain prompt is versioned with its profile.
            text = registry.domain_prompt(profile.id)
            digest = hashlib.sha256(text.encode()).hexdigest()
            name = f"workers/profiles/{profile.id}"
            existing = await session.scalar(
                select(PromptVersion).where(
                    PromptVersion.name == name, PromptVersion.version == profile.version
                )
            )
            if existing is None:
                session.add(
                    PromptVersion(name=name, version=profile.version, content=text, sha256=digest)
                )
                prompts_created += 1
            elif existing.sha256 != digest:
                # A released prompt must not drift without a version bump (s21).
                print(
                    f"ERROR: prompt '{name}' v{profile.version} changed without a "
                    "version bump. Bump the profile version instead of editing a "
                    "released prompt.",
                    file=sys.stderr,
                )
                return 1

        # Shared prompts. The version is declared in the file so it travels with
        # the content it versions: a seeder-side constant drifts from the text.
        shared = [("workers/common", Path(settings.worker_prompts_path) / "common_v1.md")]
        for name, path in shared:
            if not path.is_file():
                continue
            content = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(content.encode()).hexdigest()
            version = prompt_version(content)
            existing = await session.scalar(
                select(PromptVersion).where(
                    PromptVersion.name == name, PromptVersion.version == version
                )
            )
            if existing is None:
                session.add(
                    PromptVersion(name=name, version=version, content=content, sha256=digest)
                )
                prompts_created += 1
            elif existing.sha256 != digest:
                # A recorded version is immutable: a specialist result cites the
                # prompt that produced it, so editing one in place would make the
                # audit trail describe work that never happened.
                print(
                    f"ERROR: shared prompt '{name}' v{version} changed without a version "
                    f"bump. Edit the '<!-- version: -->' line at the top of {path.name}.",
                    file=sys.stderr,
                )
                return 1

    log.info(
        "seed_complete",
        created=created,
        updated=updated,
        prompts_created=prompts_created,
    )
    print(
        f"Seeded: {created} new profiles, {updated} updated, {prompts_created} new prompt versions."
    )
    return 0


PROMPT_VERSION = re.compile(r"<!--\s*version:\s*([0-9]+\.[0-9]+\.[0-9]+)\s*-->")


def prompt_version(content: str, default: str = "1.0.0") -> str:
    """The version declared at the top of a prompt file."""
    match = PROMPT_VERSION.search(content[:200])
    return match.group(1) if match else default


async def main() -> int:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_format)
    try:
        return await seed()
    finally:
        await dispose_engine()


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
