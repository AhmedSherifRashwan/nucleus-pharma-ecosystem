"""Load project documents so specialists can retrieve them (PRD s14, s17.3).

    uv run python scripts/ingest_documents.py --project "Midostaurin" docs/*.pdf

Until a document is ingested, `project_search` returns nothing and specialists
reason from general principles — which they then label honestly as assumptions.
That is the difference between a report that cites the FDA guidance and one that
says the guidance could not be retrieved.

A project is identified by name. Documents attach to it, and every run on that
project can search them.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
import uuid
from pathlib import Path

from sqlalchemy import select

from app.agents.registry import KNOWN_KNOWLEDGE_SCOPES
from app.audit.logger import configure_logging, get_logger
from app.config import get_settings
from app.ingest.extract import SUPPORTED
from app.ingest.service import DEFAULT_SCOPES, discover, ingest_path
from app.storage.db import dispose_engine, session_scope
from app.storage.models import Project, SourceDocument

log = get_logger(__name__)


async def resolve_project(name: str | None, project_id: str | None) -> tuple[uuid.UUID, str]:
    """Find or create the project these documents belong to."""
    async with session_scope() as session:
        if project_id:
            project = await session.get(Project, uuid.UUID(project_id))
            if project is None:
                raise ValueError(f"no project {project_id}")
            return project.id, project.name

        if not name:
            raise ValueError("give --project NAME or --project-id UUID")

        found = (
            await session.execute(select(Project).where(Project.name == name))
        ).scalar_one_or_none()
        if found is not None:
            return found.id, found.name

        project = Project(name=name)
        session.add(project)
        await session.flush()
        log.info("project_created", name=name)
        return project.id, project.name


async def list_documents(project_id: uuid.UUID) -> list[dict]:
    async with session_scope() as session:
        rows = (
            (
                await session.execute(
                    select(SourceDocument)
                    .where(SourceDocument.project_id == project_id)
                    .order_by(SourceDocument.created_at)
                )
            )
            .scalars()
            .all()
        )
    return [
        {
            "title": d.title,
            "filename": d.filename,
            "version": d.version,
            "scopes": d.knowledge_scopes,
            "ingested_at": d.ingested_at.strftime("%Y-%m-%d %H:%M") if d.ingested_at else None,
        }
        for d in rows
    ]


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Ingest project documents for specialist retrieval."
    )
    parser.add_argument("paths", nargs="*", help="files or directories to ingest")
    parser.add_argument("--project", help="project name (created if it does not exist)")
    parser.add_argument("--project-id", help="existing project id")
    parser.add_argument(
        "--scope",
        action="append",
        help=f"knowledge scope, repeatable. Default {DEFAULT_SCOPES[0]}. "
        f"Known: {', '.join(sorted(KNOWN_KNOWLEDGE_SCOPES))}",
    )
    parser.add_argument("--list", action="store_true", help="list what the project already has")
    parser.add_argument(
        "--keep-old-versions",
        action="store_true",
        help="keep earlier versions searchable (they can then be cited alongside)",
    )
    args = parser.parse_args()

    configure_logging(get_settings().log_level, "console")

    try:
        project_id, project_name = await resolve_project(args.project, args.project_id)

        if args.list:
            existing = await list_documents(project_id)
            print(json.dumps({"project": project_name, "documents": existing}, indent=1))
            return 0

        if not args.paths:
            parser.error("give at least one file or directory, or use --list")

        scopes = tuple(args.scope) if args.scope else DEFAULT_SCOPES
        unknown = [s for s in scopes if s not in KNOWN_KNOWLEDGE_SCOPES]
        if unknown:
            raise ValueError(
                f"unknown knowledge scope(s): {', '.join(unknown)}. "
                f"Known: {', '.join(sorted(KNOWN_KNOWLEDGE_SCOPES))}"
            )

        documents: list[dict] = []
        total_chunks = 0
        for raw in args.paths:
            target = Path(raw).expanduser()
            if not target.exists():
                documents.append({"path": raw, "status": "failed", "detail": "no such path"})
                continue
            if target.is_dir() and not discover(target):
                documents.append(
                    {
                        "path": raw,
                        "status": "failed",
                        "detail": f"no supported files ({', '.join(sorted(SUPPORTED))})",
                    }
                )
                continue
            report = await ingest_path(
                target,
                project_id=project_id,
                scopes=scopes,
                replace=not args.keep_old_versions,
            )
            for result in report.results:
                documents.append(
                    {
                        "path": str(result.path),
                        "title": result.title,
                        "status": result.status,
                        "chunks": result.chunks,
                        **({"detail": result.detail} if result.detail else {}),
                    }
                )
            total_chunks += report.total_chunks

        failed = [d for d in documents if d["status"] == "failed"]
        summary = {
            "project": project_name,
            "documents": documents,
            "chunks": total_chunks,
            "failed": len(failed),
        }
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1
    finally:
        await dispose_engine()

    print(json.dumps(summary, indent=1))
    # A batch where nothing landed is a failure, even if each file failed quietly.
    return 1 if failed and not any(d["status"] != "failed" for d in documents) else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
