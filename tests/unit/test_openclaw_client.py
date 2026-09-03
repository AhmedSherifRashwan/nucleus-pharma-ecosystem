"""OpenClaw adapter interface (PRD s7, s20.3, s28.3).

Phase 0 asserts the seam, not the wire format: the contract is unverified until
the gateway is running (decisions.md D-001).
"""

from __future__ import annotations

import pytest

from app.config import Settings
from app.providers.openclaw_client import (
    AgentRoute,
    GatewayNotConfigured,
    OpenClawClient,
    Usage,
    get_gateway_client,
)


def _client(**overrides) -> OpenClawClient:
    base = {"openclaw_gateway_token": "test-token", "_env_file": None}
    return OpenClawClient(Settings(**{**base, **overrides}))


def test_routes_map_to_configured_agent_ids():
    c = _client(
        openclaw_executive_agent="exec-a",
        openclaw_specialist_agent="spec-b",
        openclaw_local_agent="local-c",
    )
    assert c.agent_id_for(AgentRoute.EXECUTIVE) == "exec-a"
    assert c.agent_id_for(AgentRoute.SPECIALIST) == "spec-b"
    assert c.agent_id_for(AgentRoute.LOCAL) == "local-c"


def test_request_selects_a_logical_agent_not_a_provider_model():
    """s2 model abstraction: business logic never names a provider model."""
    body = _client().build_request(AgentRoute.EXECUTIVE, "plan this")
    assert body["model"] == "openclaw/pharma-executive"
    assert body["stream"] is False
    assert "gpt" not in body["model"] and "claude" not in body["model"]


def test_headers_carry_the_agent_route_and_bearer_token():
    headers = _client().build_headers(AgentRoute.SPECIALIST)
    assert headers["x-openclaw-agent-id"] == "pharma-specialist"
    assert headers["Authorization"] == "Bearer test-token"


def test_headers_omit_authorization_when_no_token():
    headers = _client(openclaw_gateway_token=None).build_headers(AgentRoute.EXECUTIVE)
    assert "Authorization" not in headers


def test_route_info_records_the_auth_mode():
    """s20: runs must be attributable to subscription or api_key."""
    info = _client(openclaw_auth_mode="api_key").route_info(AgentRoute.EXECUTIVE)
    assert info.auth_mode == "api_key"
    assert info.agent_id == "pharma-executive"


async def test_transport_fails_closed_in_phase_0():
    """s2 fail closed: an unimplemented transport must raise, never fake output."""
    from app.schemas import Plan

    with pytest.raises(GatewayNotConfigured, match="Phase 3"):
        await _client().complete_structured(
            route=AgentRoute.EXECUTIVE, system="s", user="u", schema=Plan
        )


async def test_specialist_transport_fails_closed_in_phase_0():
    from app.schemas import SpecialistResult

    with pytest.raises(GatewayNotConfigured):
        await _client().run_specialist(
            system="s", user="u", tools=[], result_schema=SpecialistResult
        )


async def test_health_check_reports_unreachable_without_pretending():
    health = await _client().health_check()
    assert health.reachable is False
    assert "not implemented" in health.detail
    assert set(health.routes) == {"executive", "specialist", "local"}


def test_usage_accumulates():
    total = Usage(100, 50, 10, 1) + Usage(200, 25, 5, 1)
    assert (total.input_tokens, total.output_tokens, total.steps) == (300, 75, 2)


def test_factory_returns_a_client():
    assert isinstance(get_gateway_client(), OpenClawClient)
