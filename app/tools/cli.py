"""Command-line entry point for the Neucleus tools (PRD s19, D-013).

Invoked by the `neucleus-tools` OpenClaw skill from inside a specialist turn.
Every call states its context and is authorised against the database before any
work happens, so the implementation stays in Python exactly as s19 requires while
OpenClaw sees only a skill.

    uv run python -m app.tools.cli save_evidence --context RUN-2026-0042:T1 ...

Prints one JSON object on stdout. Exits non-zero on refusal or failure so the
caller cannot mistake an error for a result.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any

from app.audit.logger import configure_logging
from app.config import get_settings
from app.storage.db import dispose_engine
from app.tools import store
from app.tools.calculator import CalculatorError, evaluate
from app.tools.context import ToolAuthorisationError, resolve_context


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="neucleus-tools", description=__doc__)
    sub = parser.add_subparsers(dest="tool", required=True)

    def with_context(p: argparse.ArgumentParser) -> argparse.ArgumentParser:
        p.add_argument(
            "--context",
            required=True,
            help="RUN-REF:TASK-ID, exactly as given in your task briefing",
        )
        return p

    calc = with_context(sub.add_parser("calculator", help="evaluate arithmetic exactly"))
    calc.add_argument("--expression", required=True)

    ev = with_context(sub.add_parser("save_evidence", help="record an evidence item"))
    ev.add_argument(
        "--source-type",
        required=True,
        choices=["web", "document", "database", "api", "regulatory", "literature"],
    )
    ev.add_argument("--source-title", required=True)
    ev.add_argument("--excerpt", required=True, help="the excerpt or locator relied on")
    ev.add_argument(
        "--reliability",
        required=True,
        type=int,
        choices=[1, 2, 3, 4],
        help="1 authoritative, 2 high-quality secondary, 3 secondary, 4 unverified",
    )
    ev.add_argument("--source-url")
    ev.add_argument("--notes")

    sa = with_context(sub.add_parser("save_artifact", help="store a long work product"))
    sa.add_argument("--title", required=True)
    sa.add_argument("--content", help="content inline")
    sa.add_argument("--content-file", help="read content from a file instead")

    ra = with_context(sub.add_parser("read_artifact", help="read a work product"))
    ra.add_argument("--artifact-ref", required=True)

    ps = with_context(sub.add_parser("project_search", help="search project documents"))
    ps.add_argument("--query", required=True)
    ps.add_argument("--max-results", type=int, default=5)

    return parser


async def dispatch(args: argparse.Namespace) -> dict[str, Any]:
    ctx = await resolve_context(args.context)

    if args.tool == "calculator":
        ctx.require_tool("calculator")
        try:
            return {"expression": args.expression, "result": evaluate(args.expression)}
        except CalculatorError as exc:
            return {"expression": args.expression, "error": str(exc)}

    if args.tool == "save_evidence":
        return await store.save_evidence(
            ctx,
            source_type=args.source_type,
            source_title=args.source_title,
            excerpt_or_locator=args.excerpt,
            reliability_level=args.reliability,
            source_url_or_document_id=args.source_url,
            notes=args.notes,
        )

    if args.tool == "save_artifact":
        content = args.content
        if args.content_file:
            content = Path(args.content_file).read_text(encoding="utf-8")
        if not content:
            return {"error": "provide --content or --content-file"}
        return await store.save_artifact(ctx, title=args.title, content=content)

    if args.tool == "read_artifact":
        return await store.read_artifact(ctx, artifact_ref=args.artifact_ref)

    if args.tool == "project_search":
        return await store.project_search(ctx, query=args.query, max_results=args.max_results)

    return {"error": f"unknown tool: {args.tool}"}


async def main() -> int:
    args = build_parser().parse_args()
    configure_logging(get_settings().log_level, "json")
    try:
        result = await dispatch(args)
    except ToolAuthorisationError as exc:
        print(json.dumps({"error": f"not authorised: {exc}"}), file=sys.stderr)
        return 2
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1
    finally:
        await dispose_engine()

    print(json.dumps(result, default=str))
    return 1 if "error" in result else 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
