"""Re-generate a run's report from its stored work (PRD s18.3).

The specialists are not re-run: the report is rebuilt from the artifacts,
evidence and review record already in the database. Used to reissue a report
after the generator improves, and as the regression harness for report changes.

The new report is a new version. The earlier one is never overwritten.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from app.agents.registry import get_registry
from app.audit.logger import configure_logging, get_logger
from app.config import get_settings
from app.reports.service import ReportService
from app.schemas.executive import ExecutiveDecisionBrief
from app.schemas.planning import Plan
from app.schemas.specialist import SpecialistResult
from app.storage.db import dispose_engine, session_scope
from app.storage.repositories import RunRepository

log = get_logger(__name__)


async def regenerate(run_ref: str, *, output_dir: str | None = None) -> dict:
    async with session_scope() as session:
        runs = RunRepository(session)
        run = await runs.get_by_ref(run_ref)
        if run is None:
            raise ValueError(f"no run {run_ref}")
        if not run.executive_brief:
            raise ValueError(f"{run_ref} has no executive brief to build a report from")
        brief = ExecutiveDecisionBrief.model_validate(run.executive_brief)
        plan = Plan.model_validate(run.plan) if run.plan else None
        records = await runs.list_results(run.id)
        results = [
            SpecialistResult.model_validate(r.structured_result)
            for r in records
            if r.structured_result
        ]
        run_id, project_id, request = run.id, run.project_id, run.user_request

    service = ReportService(output_dir=output_dir) if output_dir else ReportService()
    outcome = await service.generate_technical_report(
        run_id=run_id,
        run_ref=run_ref,
        user_request=request,
        brief=brief,
        results=results,
        plan=plan,
        agent_names={p.id: p.name for p in get_registry().all_profiles()},
        project_id=project_id,
    )
    return {
        "run_ref": run_ref,
        "version": outcome.version,
        "title": outcome.document_title,
        "sections": outcome.section_count,
        "words": outcome.word_count,
        "review_required": outcome.review_required,
        "files": [str(p) for p in outcome.paths],
        "warnings": outcome.warnings,
        "gate_failures": outcome.gate.reasons if outcome.gate else [],
    }


async def main() -> int:
    parser = argparse.ArgumentParser(description="Re-generate a Nucleus report.")
    parser.add_argument("--run-ref", required=True)
    parser.add_argument("--output-dir", help="write elsewhere than the configured path")
    args = parser.parse_args()

    configure_logging(get_settings().log_level, "console")
    try:
        result = await regenerate(args.run_ref, output_dir=args.output_dir)
    except Exception as exc:
        print(json.dumps({"error": f"{type(exc).__name__}: {exc}"}), file=sys.stderr)
        return 1
    finally:
        await dispose_engine()
    print(json.dumps(result, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
