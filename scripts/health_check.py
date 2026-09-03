"""Phase 0 diagnostic CLI (PRD s42).

Checks everything that costs nothing: configuration, database, schema contracts
and gateway configuration. Makes no live model calls.

    uv run python scripts/health_check.py
"""

from __future__ import annotations

import asyncio
import sys

from sqlalchemy import text

from app.audit.logger import configure_logging
from app.config import get_settings
from app.providers.openclaw_client import get_gateway_client
from app.storage.db import dispose_engine, session_scope
from app.storage.models import Base

OK = "  \033[32mok\033[0m   "
WARN = "  \033[33mwarn\033[0m "
FAIL = "  \033[31mFAIL\033[0m "


async def main() -> int:
    configure_logging("WARNING", "console")
    settings = get_settings()
    failures: list[str] = []
    warnings: list[str] = []

    print("Pharma AI Ecosystem - Phase 0 diagnostics\n")

    # 1. Configuration
    print(f"{OK} environment: {settings.app_env}")
    print(
        f"{OK} budgets: max {settings.default_max_specialists} specialists, "
        f"{settings.default_max_parallel_specialists} parallel, "
        f"{settings.default_max_followup_rounds} follow-up round(s)"
    )

    # 2. Database
    try:
        async with session_scope() as session:
            await session.execute(text("SELECT 1"))
            result = await session.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema='public' ORDER BY table_name"
                )
            )
            present = {r[0] for r in result}
        expected = set(Base.metadata.tables)
        missing = expected - present
        if missing:
            failures.append(f"missing tables: {', '.join(sorted(missing))}")
            print(f"{FAIL} database: {len(missing)} table(s) missing - run alembic upgrade head")
        else:
            print(f"{OK} database: reachable, all {len(expected)} application tables present")
        if "checkpoints" not in present:
            warnings.append(
                "LangGraph checkpoint tables absent - initialise in Phase 6 via "
                "PostgresSaver.setup()"
            )
    except Exception as exc:
        failures.append(f"database: {type(exc).__name__}: {exc}")
        print(f"{FAIL} database: {type(exc).__name__}")

    # 3. Schema contracts
    try:
        from app.schemas import ExecutiveDecisionBrief, Plan, SpecialistResult

        for schema in (Plan, SpecialistResult, ExecutiveDecisionBrief):
            schema.model_json_schema()
        print(f"{OK} schemas: Plan, SpecialistResult, ExecutiveDecisionBrief validate")
    except Exception as exc:
        failures.append(f"schemas: {exc}")
        print(f"{FAIL} schemas: {exc}")

    # 4. Gateway configuration (no live call in Phase 0)
    client = get_gateway_client(settings)
    health = await client.health_check()
    print(f"{OK} gateway target: {health.gateway_url}  (auth: {health.auth_mode})")
    print(
        f"{OK} routes configured: executive={settings.openclaw_executive_agent}, "
        f"specialist={settings.openclaw_specialist_agent}"
    )
    if not health.reachable:
        warnings.append(f"gateway not reachable: {health.detail}")
    if not settings.openclaw_gateway_token:
        warnings.append("OPENCLAW_GATEWAY_TOKEN is unset (required in production)")
    await client.aclose()

    # 5. Provisional profile gate
    if settings.enable_provisional_profiles:
        warnings.append("A15/A16 provisional profiles are ENABLED (PRD s11 keeps them off)")
    else:
        print(f"{OK} provisional profiles A15/A16 disabled")

    print()
    for w in warnings:
        print(f"{WARN} {w}")
    if failures:
        print(f"\n\033[31m{len(failures)} failure(s):\033[0m")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\n\033[32mPhase 0 checks passed.\033[0m")
    return 0


if __name__ == "__main__":
    try:
        code = asyncio.run(main())
    finally:
        asyncio.run(dispose_engine())
    sys.exit(code)
