"""Back up and restore everything a run depends on (PRD s14, Phase 15).

Three things have to travel for a machine to carry on where another left off: the
database (runs, evidence, conflicts, approvals, the audit trail), the artifacts on
disk (specialist work products, delivered reports, ingested documents), and enough
metadata to prove the two belong together.

The code travels through git and the gateway configuration through
`openclaw.template.json`, so neither is here. Secrets are never included: a backup
that carries credentials is a credential you have lost track of.

    uv run python scripts/backup.py create --to ~/Backups
    uv run python scripts/backup.py restore --from ~/Backups/nucleus-2026-09-09.tar.gz
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.exc import ProgrammingError

from app.audit.logger import configure_logging, get_logger
from app.config import get_settings
from app.storage.db import dispose_engine, session_scope
from app.storage.models import Artifact, EvidenceItem, Run, SourceDocument

log = get_logger(__name__)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MANIFEST = "manifest.json"
DUMP = "database.dump"
ARTIFACTS = "artifacts"

# Homebrew keeps postgresql@17 keg-only, so its tools are often off PATH.
PG_SEARCH = (
    "/opt/homebrew/opt/postgresql@17/bin",
    "/usr/local/opt/postgresql@17/bin",
    "/opt/homebrew/bin",
    "/usr/local/bin",
)


class BackupError(RuntimeError):
    """Something is wrong with the archive or the machine. Never proceed on doubt."""


def pg_tool(name: str) -> str:
    found = shutil.which(name)
    if found:
        return found
    for directory in PG_SEARCH:
        candidate = Path(directory) / name
        if candidate.is_file():
            return str(candidate)
    raise BackupError(
        f"{name} not found. It ships with postgresql@17, which Homebrew keeps off "
        f'PATH: export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"'
    )


def database_url() -> str:
    return get_settings().database_url


def connection_args(url: str) -> tuple[list[str], str]:
    """psql-style arguments and the database name, from the SQLAlchemy URL."""
    trimmed = url.split("://", 1)[-1]
    credentials, _, location = trimmed.rpartition("@")
    user, _, password = credentials.partition(":")
    hostport, _, name = location.partition("/")
    host, _, port = hostport.partition(":")
    args = ["-h", host or "localhost", "-p", port or "5432"]
    if user:
        args += ["-U", user]
    if password:
        os.environ["PGPASSWORD"] = password
    return args, name.split("?", 1)[0]


async def census() -> dict[str, int]:
    """What the archive should contain, so a restore can be checked against it.

    A database with no schema yet is the normal state on a new machine, so that
    reads as empty rather than as an error.
    """
    counts = {"runs": 0, "evidence_items": 0, "artifacts": 0, "source_documents": 0}
    try:
        async with session_scope() as session:
            for label, model in (
                ("runs", Run),
                ("evidence_items", EvidenceItem),
                ("artifacts", Artifact),
                ("source_documents", SourceDocument),
            ):
                counts[label] = int(
                    await session.scalar(select(func.count()).select_from(model)) or 0
                )
    except ProgrammingError:
        return counts
    return counts


def git_commit() -> str | None:
    result = subprocess.run(
        ["git", "rev-parse", "HEAD"], cwd=PROJECT_ROOT, capture_output=True, text=True
    )
    return result.stdout.strip() or None


def artifact_dir() -> Path:
    return Path(get_settings().artifact_local_path).expanduser().resolve()


async def create(destination: Path) -> Path:
    url = database_url()
    args, name = connection_args(url)
    counts = await census()

    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y-%m-%d-%H%M")
    archive = destination / f"nucleus-{stamp}.tar.gz"

    with tempfile.TemporaryDirectory() as scratch:
        staging = Path(scratch)

        dump = staging / DUMP
        result = subprocess.run(
            [pg_tool("pg_dump"), *args, "-Fc", "-f", str(dump), name],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise BackupError(f"pg_dump failed: {result.stderr.strip()[:300]}")

        source = artifact_dir()
        if source.is_dir():
            shutil.copytree(source, staging / ARTIFACTS, dirs_exist_ok=True)

        manifest = {
            "created_at": datetime.now(UTC).isoformat(),
            "database": name,
            "git_commit": git_commit(),
            "counts": counts,
            "database_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
            "artifact_files": sum(1 for _ in (staging / ARTIFACTS).rglob("*") if _.is_file())
            if (staging / ARTIFACTS).is_dir()
            else 0,
        }
        (staging / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")

        with tarfile.open(archive, "w:gz") as tar:
            for item in sorted(staging.iterdir()):
                tar.add(item, arcname=item.name)

    log.info("backup_created", archive=str(archive), **counts)
    print(json.dumps({"archive": str(archive), **manifest}, indent=1))
    return archive


async def restore(archive: Path, *, force: bool) -> None:
    if not archive.is_file():
        raise BackupError(f"no archive at {archive}")

    url = database_url()
    args, name = connection_args(url)

    with tempfile.TemporaryDirectory() as scratch:
        staging = Path(scratch)
        with tarfile.open(archive, "r:gz") as tar:
            # Refuse a path that would write outside the staging directory.
            for member in tar.getmembers():
                target = (staging / member.name).resolve()
                if not str(target).startswith(str(staging.resolve())):
                    raise BackupError(f"archive contains an unsafe path: {member.name}")
            # `data` refuses absolute paths, traversal and special files.
            tar.extractall(staging, filter="data")

        manifest_path = staging / MANIFEST
        if not manifest_path.is_file():
            raise BackupError("archive has no manifest; refusing to restore blind")
        manifest = json.loads(manifest_path.read_text())

        dump = staging / DUMP
        digest = hashlib.sha256(dump.read_bytes()).hexdigest()
        if digest != manifest.get("database_sha256"):
            raise BackupError("the database dump does not match its manifest checksum")

        counts = await census()
        occupied = sum(counts.values())
        if occupied and not force:
            raise BackupError(
                f"{name} already holds data ({counts}). Restoring would merge two "
                "histories into one audit trail. Use --force only on a database you "
                "mean to overwrite."
            )

        await dispose_engine()
        result = subprocess.run(
            [pg_tool("pg_restore"), *args, "-d", name, "--clean", "--if-exists", str(dump)],
            capture_output=True,
            text=True,
        )
        # pg_restore warns about objects it cannot drop on a fresh database, which
        # is expected. A permission or connection error is not, and swallowing it
        # leaves an empty database that looks restored.
        stderr = result.stderr.strip()
        fatal = [
            line
            for line in stderr.splitlines()
            if "error:" in line.lower() and "does not exist" not in line.lower()
        ]
        if fatal:
            raise BackupError(
                "pg_restore could not write to the database:\n  "
                + "\n  ".join(fatal[:3])
                + "\n\nIf this is 'permission denied for schema public', the database "
                "is not owned by the user in DATABASE_URL. Recreate it with "
                "`createdb -O <user> <name>` — bootstrap.py does this correctly."
            )
        if stderr:
            log.info("pg_restore_notes", detail=stderr[:300])

        source = staging / ARTIFACTS
        if source.is_dir():
            target = artifact_dir()
            target.mkdir(parents=True, exist_ok=True)
            shutil.copytree(source, target, dirs_exist_ok=True)

    await dispose_engine()
    restored = await census()
    expected = manifest.get("counts", {})
    mismatch = {
        k: (expected.get(k), restored.get(k)) for k in expected if expected[k] != restored.get(k)
    }
    print(json.dumps({"restored": restored, "expected": expected}, indent=1))
    if mismatch:
        raise BackupError(f"restored counts do not match the manifest: {mismatch}")
    log.info("backup_restored", archive=str(archive), **restored)
    print("\nRestored. Now run: uv run python scripts/bootstrap.py")


async def main() -> int:
    parser = argparse.ArgumentParser(description="Back up or restore Nucleus data.")
    sub = parser.add_subparsers(dest="command", required=True)
    make = sub.add_parser("create", help="write an archive")
    make.add_argument("--to", default=str(Path.home() / "Backups"), help="destination directory")
    put = sub.add_parser("restore", help="restore an archive into this machine")
    put.add_argument("--from", dest="archive", required=True)
    put.add_argument(
        "--force",
        action="store_true",
        help="overwrite a database that already holds data",
    )
    args = parser.parse_args()

    configure_logging(get_settings().log_level, "console")
    try:
        if args.command == "create":
            await create(Path(args.to).expanduser())
        else:
            await restore(Path(args.archive).expanduser(), force=args.force)
    except BackupError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1
    finally:
        await dispose_engine()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
