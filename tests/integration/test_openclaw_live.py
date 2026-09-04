"""Live gateway tests (PRD Phase 3).

Gated: these call the real OpenClaw gateway and spend subscription usage. Run with

    RUN_LIVE_GATEWAY=1 uv run pytest tests/integration/test_openclaw_live.py
"""

from __future__ import annotations

import os

import pytest
from pydantic import BaseModel

from app.providers.openclaw_client import AgentRoute, get_gateway_client

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(
        not os.environ.get("RUN_LIVE_GATEWAY"),
        reason="live gateway test; set RUN_LIVE_GATEWAY=1",
    ),
]


class Answer(BaseModel):
    answer: str


async def test_gateway_is_reachable():
    health = await get_gateway_client().health_check()
    assert health.reachable
    assert health.plugins_loaded > 0


async def test_specialist_route_returns_structured_output():
    result, usage, info = await get_gateway_client().complete_structured(
        route=AgentRoute.SPECIALIST,
        system="Answer concisely.",
        user="What is 2 + 2?",
        schema=Answer,
        idempotency_key="pytest-live-specialist",
        timeout_seconds=180,
    )
    assert "4" in result.answer
    # s20: the run records which provider and model served it.
    assert info.provider
    assert info.model
    assert info.agent_id == "pharma-specialist"
    assert usage.duration_ms > 0


async def test_executive_route_returns_structured_output():
    result, _, info = await get_gateway_client().complete_structured(
        route=AgentRoute.EXECUTIVE,
        system="Answer concisely.",
        user="What is the capital of France?",
        schema=Answer,
        idempotency_key="pytest-live-executive",
        timeout_seconds=180,
    )
    assert "paris" in result.answer.lower()
    assert info.agent_id == "pharma-executive"
