"""OpenClaw gateway adapter (PRD s7, s20.3, s28.3).

This module is the ONLY place that may know how OpenClaw speaks. Orchestration
code calls `complete_structured` / `run_specialist` and never learns the wire
format, the auth mode, or which upstream provider answered (s41.1 rule 2).

PHASE 0 STATUS: interface skeleton only. The transport raises
`GatewayNotConfigured` rather than guessing at an unverified contract. The live
HTTP implementation lands in Phase 3, built against a running gateway.

The contract sketched in PRD s28.3 is:

    POST {OPENCLAW_GATEWAY_URL}/v1/responses
    Authorization: Bearer <gateway token>
    x-openclaw-agent-id: pharma-executive
    {"model": "openclaw/pharma-executive", "input": "...", "stream": false}

That sketch is UNVERIFIED - see decisions.md D-001. Phase 3 must confirm the
request shape, the response envelope, the usage counters and the error codes
against the pinned gateway version before this adapter is trusted.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, Protocol, TypeVar

from pydantic import BaseModel

from app.config import Settings, get_settings

T = TypeVar("T", bound=BaseModel)


class AgentRoute(StrEnum):
    """Logical OpenClaw agent routes (s7.3). Never a provider model ID."""

    EXECUTIVE = "executive"
    SPECIALIST = "specialist"
    LOCAL = "local"


# ---------------------------------------------------------------------------
# Errors. Normalised so orchestration can branch on cause, not on wire detail.
# ---------------------------------------------------------------------------


class GatewayError(RuntimeError):
    """Base class for every OpenClaw gateway failure."""


class GatewayNotConfigured(GatewayError):
    """The gateway is unreachable or not yet implemented for this phase."""


class GatewayTransientError(GatewayError):
    """Retryable: timeout, connection reset, 5xx."""


class GatewayAuthError(GatewayError):
    """Gateway rejected the token, or the upstream provider auth failed."""


class GatewayRateLimited(GatewayError):
    """Subscription or API rate limit reached.

    s22 / s38: the run pauses and retries, or uses an explicitly configured
    fallback. It must NEVER silently switch to a different model or provider.
    """

    def __init__(self, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class StructuredOutputError(GatewayError):
    """The route returned output that does not satisfy the required schema."""


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    """Per-call usage telemetry, where the gateway exposes counters (s22)."""

    input_tokens: int = 0
    output_tokens: int = 0
    duration_ms: int = 0
    steps: int = 0

    def __add__(self, other: Usage) -> Usage:
        return Usage(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.duration_ms + other.duration_ms,
            self.steps + other.steps,
        )


@dataclass
class RouteInfo:
    """Which route and auth mode served a call, recorded against the run (s20)."""

    route: AgentRoute
    agent_id: str
    auth_mode: str
    model_id: str | None = None


@dataclass
class GatewayResponse:
    """Normalised gateway reply."""

    output: Any
    usage: Usage = field(default_factory=Usage)
    route: RouteInfo | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class ToolSpec:
    """A tool as presented to a route, decoupled from its implementation."""

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Any
    max_calls: int = 10


# ---------------------------------------------------------------------------
# Interface
# ---------------------------------------------------------------------------


class GatewayClient(Protocol):
    """What orchestration is allowed to depend on."""

    async def complete_structured(
        self,
        *,
        route: AgentRoute,
        system: str,
        user: str,
        schema: type[T],
        max_output_tokens: int = ...,
    ) -> tuple[T, Usage, RouteInfo]:
        """One-shot structured call. Used for PLAN, CHALLENGE and SYNTHESIZE."""
        ...

    async def run_specialist(
        self,
        *,
        system: str,
        user: str,
        tools: list[ToolSpec],
        result_schema: type[T],
        max_steps: int = ...,
        max_output_tokens: int = ...,
    ) -> tuple[T, Usage, RouteInfo]:
        """Tool-using specialist loop returning a validated result."""
        ...

    async def health_check(self) -> GatewayHealth:
        """Probe the gateway and its configured routes."""
        ...

    async def aclose(self) -> None: ...


@dataclass
class GatewayHealth:
    reachable: bool
    gateway_url: str
    detail: str
    routes: dict[str, bool] = field(default_factory=dict)
    auth_mode: str = "unknown"


class OpenClawClient:
    """Concrete adapter. Transport is implemented in Phase 3."""

    def __init__(self, settings: Settings | None = None):
        self._settings = settings or get_settings()

    def agent_id_for(self, route: AgentRoute) -> str:
        s = self._settings
        return {
            AgentRoute.EXECUTIVE: s.openclaw_executive_agent,
            AgentRoute.SPECIALIST: s.openclaw_specialist_agent,
            AgentRoute.LOCAL: s.openclaw_local_agent,
        }[route]

    def route_info(self, route: AgentRoute) -> RouteInfo:
        return RouteInfo(
            route=route,
            agent_id=self.agent_id_for(route),
            auth_mode=self._settings.openclaw_auth_mode,
        )

    def build_request(self, route: AgentRoute, payload: str) -> dict[str, Any]:
        """Assemble the request body per the s28.3 sketch.

        Kept separate from transport so Phase 3 can correct the shape against the
        real gateway without touching orchestration.
        """
        agent_id = self.agent_id_for(route)
        return {
            "model": f"openclaw/{agent_id}",
            "input": payload,
            "stream": False,
        }

    def build_headers(self, route: AgentRoute) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "x-openclaw-agent-id": self.agent_id_for(route),
        }
        if self._settings.openclaw_gateway_token:
            headers["Authorization"] = f"Bearer {self._settings.openclaw_gateway_token}"
        return headers

    # -- transport (Phase 3) ------------------------------------------------

    async def complete_structured(
        self,
        *,
        route: AgentRoute,
        system: str,
        user: str,
        schema: type[T],
        max_output_tokens: int = 8000,
    ) -> tuple[T, Usage, RouteInfo]:
        raise GatewayNotConfigured(
            "OpenClaw transport is not implemented yet (Phase 3). Install and "
            "onboard the gateway, then build this against the verified "
            "/v1/responses contract."
        )

    async def run_specialist(
        self,
        *,
        system: str,
        user: str,
        tools: list[ToolSpec],
        result_schema: type[T],
        max_steps: int = 12,
        max_output_tokens: int = 8000,
    ) -> tuple[T, Usage, RouteInfo]:
        raise GatewayNotConfigured("OpenClaw transport is not implemented yet (Phase 3).")

    async def health_check(self) -> GatewayHealth:
        """Reachability probe only. Does not spend tokens.

        Phase 3 replaces this with a real gateway call; Phase 0 reports the
        configured target so the diagnostic CLI is honest about what it knows.
        """
        return GatewayHealth(
            reachable=False,
            gateway_url=self._settings.gateway_responses_url,
            detail="transport not implemented (Phase 0 skeleton)",
            routes={r.value: False for r in AgentRoute},
            auth_mode=self._settings.openclaw_auth_mode,
        )

    async def aclose(self) -> None:
        return None


def get_gateway_client(settings: Settings | None = None) -> OpenClawClient:
    return OpenClawClient(settings)
