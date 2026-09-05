"""Ingest project documents so specialists can retrieve them (PRD s14, s17.3, s19).

`project_search` returned nothing on every run because nothing had ever been
ingested — so specialists reasoned from general principles and labelled the result
honestly. This is the other half of that tool.

Two rules shape it. A document is stored with its own text, not a summary of it,
because a specialist must be able to quote what it cites. And re-ingesting the
same file supersedes the old version rather than duplicating it, so a search
cannot return two contradictory copies of the same guidance.
"""

from __future__ import annotations

import hashlib
import shutil
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import delete, select

from app.audit.logger import get_logger
from app.config import get_settings
from app.ingest.extract import SUPPORTED, ExtractedDocument, UnsupportedDocument, extract
from app.storage.db import session_scope
from app.storage.models import DocumentChunk, SourceDocument
from app.storage.repositories import AuditRepository, EventType

log = get_logger(__name__)

DEFAULT_SCOPES = ("project_documents",)


@dataclass
class IngestResult:
    path: Path
    title: str
    document_id: uuid.UUID | None = None
    chunks: int = 0
    status: str = "ingested"  # ingested | replaced | unchanged | failed
    detail: str | None = None

    @property
    def ok(self) -> bool:
        return self.status in ("ingested", "replaced", "unchanged")


@dataclass
class IngestReport:
    results: list[IngestResult] = field(default_factory=list)

    @property
    def ingested(self) -> list[IngestResult]:
        return [r for r in self.results if r.status in ("ingested", "replaced")]

    @property
    def failed(self) -> list[IngestResult]:
        return [r for r in self.results if r.status == "failed"]

    @property
    def total_chunks(self) -> int:
        return sum(r.chunks for r in self.results)


def discover(target: Path) -> list[Path]:
    """Every supported file under a path, or the file itself."""
    if target.is_file():
        return [target]
    return sorted(
        p
        for p in target.rglob("*")
        if p.is_file() and p.suffix.lower() in SUPPORTED and not p.name.startswith(".")
    )


def _digest(path: Path) -> str:
    sha = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            sha.update(block)
    return sha.hexdigest()


def _store_copy(path: Path, document_id: uuid.UUID) -> str:
    """Keep a copy, so a cited document survives the original moving."""
    base = Path(get_settings().artifact_local_path).expanduser().resolve() / "documents"
    base.mkdir(parents=True, exist_ok=True)
    target = base / f"{document_id}{path.suffix.lower()}"
    shutil.copy2(path, target)
    return str(target)


async def ingest_file(
    path: Path,
    *,
    project_id: uuid.UUID,
    title: str | None = None,
    scopes: tuple[str, ...] = DEFAULT_SCOPES,
    replace: bool = True,
) -> IngestResult:
    try:
        extracted: ExtractedDocument = extract(path)
    except UnsupportedDocument as exc:
        return IngestResult(path=path, title=path.stem, status="failed", detail=str(exc))
    except Exception as exc:  # noqa: BLE001 - one bad file must not stop a batch
        return IngestResult(
            path=path, title=path.stem, status="failed", detail=f"{type(exc).__name__}: {exc}"
        )

    if not extracted.chunks:
        return IngestResult(
            path=path,
            title=extracted.title,
            status="failed",
            detail="no retrievable text after extraction",
        )

    digest = _digest(path)
    document_title = title or extracted.title

    async with session_scope() as session:
        existing = (
            (
                await session.execute(
                    select(SourceDocument).where(
                        SourceDocument.project_id == project_id,
                        SourceDocument.filename == path.name,
                    )
                )
            )
            .scalars()
            .all()
        )

        current = next((d for d in existing if d.sha256 == digest), None)
        if current is not None:
            return IngestResult(
                path=path,
                title=current.title,
                document_id=current.id,
                chunks=0,
                status="unchanged",
                detail="already ingested, unchanged since",
            )

        version = max((d.version for d in existing), default=0) + 1
        if existing and replace:
            # A superseded guidance document must not still be searchable: two
            # versions of the same source is how a specialist cites the wrong one.
            for old in existing:
                await session.execute(
                    delete(DocumentChunk).where(DocumentChunk.document_id == old.id)
                )
                await session.delete(old)

        document = SourceDocument(
            project_id=project_id,
            title=document_title,
            filename=path.name,
            content_type=extracted.content_type,
            sha256=digest,
            version=version,
            knowledge_scopes=list(scopes),
            ingested_at=datetime.now(UTC),
        )
        session.add(document)
        await session.flush()
        document.storage_path = _store_copy(path, document.id)

        for chunk in extracted.chunks:
            session.add(
                DocumentChunk(
                    document_id=document.id,
                    project_id=project_id,
                    chunk_index=chunk.index,
                    content=chunk.content,
                    locator=chunk.locator,
                    token_count=len(chunk.content) // 4,
                )
            )

        await AuditRepository(session).record(
            event_type=EventType.DOCUMENT_INGESTED,
            actor_type="user",
            project_id=project_id,
            metadata={
                "title": document_title,
                "filename": path.name,
                "chunks": len(extracted.chunks),
                "version": version,
                "sha256": digest,
                "scopes": list(scopes),
            },
        )
        result = IngestResult(
            path=path,
            title=document_title,
            document_id=document.id,
            chunks=len(extracted.chunks),
            status="replaced" if existing and replace else "ingested",
        )

    log.info(
        "document_ingested",
        title=document_title,
        chunks=result.chunks,
        version=version,
        status=result.status,
    )
    return result


async def ingest_path(
    target: Path,
    *,
    project_id: uuid.UUID,
    scopes: tuple[str, ...] = DEFAULT_SCOPES,
    replace: bool = True,
) -> IngestReport:
    report = IngestReport()
    for path in discover(target):
        report.results.append(
            await ingest_file(path, project_id=project_id, scopes=scopes, replace=replace)
        )
    return report
