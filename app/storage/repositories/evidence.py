"""Evidence persistence (PRD s15).

Evidence is a first-class entity, not a URL buried in model prose. Reliability is
an integer level 1-4 per s15.1.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.storage.models import Artifact, EvidenceItem

VALID_RELIABILITY = (1, 2, 3, 4)


class EvidenceRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def next_evidence_ref(self) -> str:
        count = await self.session.scalar(select(func.count()).select_from(EvidenceItem))
        return f"E-{(count or 0) + 1}"

    async def create(
        self,
        *,
        source_type: str,
        source_title: str,
        reliability_level: int,
        excerpt_or_locator: str | None = None,
        source_url_or_document_id: str | None = None,
        project_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        originating_agent: str | None = None,
        notes: str | None = None,
        evidence_ref: str | None = None,
    ) -> EvidenceItem:
        if reliability_level not in VALID_RELIABILITY:
            raise ValueError(f"reliability_level must be one of {VALID_RELIABILITY} (PRD s15.1)")
        item = EvidenceItem(
            evidence_ref=evidence_ref or await self.next_evidence_ref(),
            source_type=source_type[:32],
            source_title=source_title,
            source_url_or_document_id=source_url_or_document_id,
            retrieved_at=datetime.now(UTC),
            excerpt_or_locator=excerpt_or_locator,
            reliability_level=reliability_level,
            project_id=project_id,
            run_id=run_id,
            originating_agent=originating_agent,
            hash_or_snapshot_ref=(
                hashlib.sha256(excerpt_or_locator.encode()).hexdigest()
                if excerpt_or_locator
                else None
            ),
            notes=notes,
        )
        self.session.add(item)
        await self.session.flush()
        return item

    async def get_by_ref(self, evidence_ref: str) -> EvidenceItem | None:
        stmt = select(EvidenceItem).where(EvidenceItem.evidence_ref == evidence_ref)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_run(self, run_id: uuid.UUID) -> list[EvidenceItem]:
        stmt = (
            select(EvidenceItem)
            .where(EvidenceItem.run_id == run_id)
            .order_by(EvidenceItem.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def refs_exist(self, refs: list[str]) -> set[str]:
        """Which of these evidence refs actually exist.

        The report QA step (s32.3) must verify that every ID cited in a report
        resolves to a real record.
        """
        if not refs:
            return set()
        stmt = select(EvidenceItem.evidence_ref).where(EvidenceItem.evidence_ref.in_(refs))
        return {r for (r,) in (await self.session.execute(stmt)).all()}


class ArtifactRepository:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def next_artifact_ref(self, prefix: str = "ART") -> str:
        count = await self.session.scalar(
            select(func.count()).select_from(Artifact).where(Artifact.kind != "report")
            if prefix == "ART"
            else select(func.count()).select_from(Artifact)
        )
        return f"{prefix}-{(count or 0) + 1}"

    async def create(
        self,
        *,
        title: str,
        kind: str = "work_product",
        content_type: str = "text/markdown",
        storage_path: str | None = None,
        size_bytes: int | None = None,
        sha256: str | None = None,
        project_id: uuid.UUID | None = None,
        run_id: uuid.UUID | None = None,
        created_by_agent: str | None = None,
        artifact_ref: str | None = None,
        status: str = "current",
        supersedes_ref: str | None = None,
        version: int = 1,
        template_version: str | None = None,
        generator_version: str | None = None,
    ) -> Artifact:
        artifact = Artifact(
            artifact_ref=artifact_ref or await self.next_artifact_ref(),
            title=title,
            kind=kind,
            content_type=content_type,
            storage_path=storage_path,
            size_bytes=size_bytes,
            sha256=sha256,
            project_id=project_id,
            run_id=run_id,
            created_by_agent=created_by_agent,
            status=status,
            supersedes_ref=supersedes_ref,
            version=version,
            template_version=template_version,
            generator_version=generator_version,
        )
        self.session.add(artifact)
        await self.session.flush()
        return artifact

    async def supersede(self, *, old_ref: str, new_ref: str, run_id: uuid.UUID) -> bool:
        """Mark one work product as replaced by another.

        Both must belong to the same run: a revision cannot reach across runs.
        The row is never deleted - the audit trail keeps the original (s14).
        """
        old = await self.get_by_ref(old_ref)
        new = await self.get_by_ref(new_ref)
        if old is None or new is None:
            return False
        if old.run_id != run_id or new.run_id != run_id:
            return False
        old.status = "superseded"
        old.superseded_by_ref = new_ref
        new.supersedes_ref = old_ref
        await self.session.flush()
        return True

    async def current_work_products(self, run_id: uuid.UUID) -> list[Artifact]:
        """Work products that still represent the run's position (s18.

        Superseded drafts are excluded: they belong to the audit appendix, not
        to the report's current scientific conclusions.
        """
        stmt = (
            select(Artifact)
            .where(
                Artifact.run_id == run_id,
                Artifact.kind == "work_product",
                Artifact.status != "superseded",
            )
            .order_by(Artifact.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def superseded_work_products(self, run_id: uuid.UUID) -> list[Artifact]:
        stmt = (
            select(Artifact)
            .where(
                Artifact.run_id == run_id,
                Artifact.kind == "work_product",
                Artifact.status == "superseded",
            )
            .order_by(Artifact.created_at)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def next_report_version(self, run_id: uuid.UUID) -> int:
        """s18.3: a report is versioned, never overwritten."""
        current = await self.session.scalar(
            select(func.max(Artifact.version)).where(
                Artifact.run_id == run_id, Artifact.kind == "report"
            )
        )
        return (current or 0) + 1

    async def get_by_ref(self, artifact_ref: str) -> Artifact | None:
        stmt = select(Artifact).where(Artifact.artifact_ref == artifact_ref)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_run(self, run_id: uuid.UUID) -> list[Artifact]:
        stmt = select(Artifact).where(Artifact.run_id == run_id).order_by(Artifact.created_at)
        return list((await self.session.execute(stmt)).scalars().all())
