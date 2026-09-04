"""OpenClaw gateway adapter (PRD s7, s20.3, s28.3).

The ONLY module that knows how OpenClaw speaks. Orchestration calls
`complete_structured` / `run_specialist` and never learns the wire format, the
auth mode, or which upstream provider answered (s41.1 rule 2).

TRANSPORT: the `openclaw gateway call` CLI, not raw HTTP.

PRD s28.3 specifies `POST /v1/responses` with an `x-openclaw-agent-id` header.
That route does not exist on OpenClaw 2026.9.1 - it returns 404. The gateway is
a WebSocket service that opens with a `connect.challenge` nonce handshake. The
CLI implements that handshake and is the supported entry point, so the adapter
shells out to it rather than reimplementing a private protocol that would break
on any gateway upgrade. See decisions.md D-001 and D-012.

Verified contract, RPC method `agent`:

    params: {agentId, message, idempotencyKey}
    result: {runId, status, summary,
             result: {payloads: [{text}],
                      meta: {durationMs, systemPromptReport: {provider, model, ...}}}}
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import time
import uuid
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from app.audit.logger import get_logger
from app.config import Settings, get_settings

T = TypeVar("T", bound=BaseModel)
log = get_logger(__name__)

OPENCLAW_BIN = "openclaw"
# Checked when the CLI is not on PATH - a supervised worker (launchd, systemd,
# container) frequently has a minimal environment.
FALLBACK_CLI_PATHS = ("~/.openclaw/bin/openclaw", "/usr/local/bin/openclaw")
AGENT_METHOD = "agent"
HEALTH_METHOD = "health"


class AgentRoute(StrEnum):
    """Logical OpenClaw agent routes (s7.3). Never a provider model ID."""

    EXECUTIVE = "executive"
    SPECIALIST = "specialist"
    LOCAL = "local"


# ---------------------------------------------------------------------------
# Errors - normalised so orchestration branches on cause, not on wire detail
# ---------------------------------------------------------------------------


class GatewayError(RuntimeError):
    """Base class for every OpenClaw gateway failure."""


class GatewayNotConfigured(GatewayError):
    """The gateway binary or endpoint is unavailable."""


class GatewayTransientError(GatewayError):
    """Retryable: timeout, connection reset, gateway restart."""


class GatewayAuthError(GatewayError):
    """Gateway rejected the token, or upstream provider auth failed."""


class GatewayRateLimited(GatewayError):
    """Subscription or API rate limit reached.

    s22 / s38: the run pauses and retries, or uses an explicitly configured
    fallback. It must NEVER silently switch model or provider.
    """

    def __init__(self, message: str, retry_after_seconds: int | None = None):
        super().__init__(message)
        self.retry_after_seconds = retry_after_seconds


class StructuredOutputError(GatewayError):
    """The route returned output that does not satisfy the required schema."""


# Substrings that classify a gateway or provider error. Ordered by specificity.
_ERROR_SIGNATURES: tuple[tuple[str, type[GatewayError]], ...] = (
    ("rate limit", GatewayRateLimited),
    ("rate_limit", GatewayRateLimited),
    ("too many requests", GatewayRateLimited),
    ("429", GatewayRateLimited),
    ("quota", GatewayRateLimited),
    ("credit balance", GatewayRateLimited),
    ("usage limit", GatewayRateLimited),
    ("unauthorized", GatewayAuthError),
    ("forbidden", GatewayAuthError),
    ("invalid token", GatewayAuthError),
    ("auth", GatewayAuthError),
    ("timeout", GatewayTransientError),
    ("timed out", GatewayTransientError),
    ("econnrefused", GatewayTransientError),
    ("connection", GatewayTransientError),
    ("unavailable", GatewayTransientError),
    ("503", GatewayTransientError),
    ("502", GatewayTransientError),
    # The gateway holds one turn per idempotency key. A killed client leaves its
    # turn open, and the next call for that key is refused until it settles -
    # transient, not a failure of the request.
    ("in_flight", GatewayTransientError),
    ("already running", GatewayTransientError),
)


def classify_error(message: str, *, retryable: bool | None = None) -> GatewayError:
    """Map a gateway error string onto a normalised exception."""
    lowered = (message or "").lower()
    for needle, exc_type in _ERROR_SIGNATURES:
        if needle in lowered:
            return exc_type(message)
    if retryable:
        return GatewayTransientError(message)
    return GatewayError(message)


# ---------------------------------------------------------------------------
# Value objects
# ---------------------------------------------------------------------------


@dataclass
class Usage:
    """Per-call telemetry. The gateway exposes duration; token counters are not
    surfaced, so s22 accounting is best-effort as the PRD allows."""

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
    """Which route, provider and model served a call - recorded against the run (s20)."""

    route: AgentRoute
    agent_id: str
    auth_mode: str
    provider: str | None = None
    model: str | None = None
    session_key: str | None = None


@dataclass
class GatewayReply:
    text: str
    usage: Usage
    route: RouteInfo
    run_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass
class GatewayHealth:
    reachable: bool
    detail: str
    routes: dict[str, bool] = field(default_factory=dict)
    auth_mode: str = "unknown"
    plugins_loaded: int = 0


# ---------------------------------------------------------------------------
# Structured-output helpers
# ---------------------------------------------------------------------------

_JSON_FENCE = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Recover a JSON object from model prose.

    Tries a fenced block first, then the first balanced brace span. Returns None
    rather than raising so the caller can run its correction attempt.
    """
    if not text:
        return None

    candidates: list[str] = []
    fenced = _JSON_FENCE.search(text)
    if fenced:
        candidates.append(fenced.group(1))

    start = text.find("{")
    if start != -1:
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : i + 1])
                    break

    for candidate in candidates:
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def build_structured_prompt(system: str, user: str, schema: type[BaseModel]) -> str:
    """Compose a single message asking for one schema-valid JSON object.

    OpenClaw agent turns take a single message, so the system policy travels with
    the task rather than in a separate role.
    """
    return (
        f"{system}\n\n"
        f"{user}\n\n"
        "---\n"
        f"Respond with ONE JSON object conforming to the {schema.__name__} schema "
        "below. Output the JSON object only: no preamble, no commentary, no code "
        "fence, no trailing text. Include every required field. Do not add fields "
        "that are not in the schema, and do not nest the result inside another "
        "object.\n\n"
        f"{schema.__name__} JSON schema:\n"
        f"{json.dumps(schema.model_json_schema(), indent=2)}"
    )


# ---------------------------------------------------------------------------
# Adapter
# ---------------------------------------------------------------------------


class OpenClawClient:
    """Concrete gateway adapter."""

    def __init__(self, settings: Settings | None = None, runner: Any = None):
        self._settings = settings or get_settings()
        # Injectable for tests; defaults to the real subprocess transport.
        self._runner = runner or self._run_cli
        self._cli: str | None = None

    def resolve_cli(self) -> str:
        """Locate the OpenClaw CLI, honouring an explicit setting first."""
        if self._cli:
            return self._cli

        configured = self._settings.openclaw_cli_path
        if configured:
            path = Path(configured).expanduser()
            if not path.is_file():
                raise GatewayNotConfigured(f"OPENCLAW_CLI_PATH points at a missing file: {path}")
            self._cli = str(path)
            return self._cli

        found = shutil.which(OPENCLAW_BIN)
        if found:
            self._cli = found
            return self._cli

        for candidate in FALLBACK_CLI_PATHS:
            path = Path(candidate).expanduser()
            if path.is_file():
                self._cli = str(path)
                return self._cli

        raise GatewayNotConfigured(
            f"'{OPENCLAW_BIN}' was not found on PATH or at "
            f"{', '.join(FALLBACK_CLI_PATHS)}. Set OPENCLAW_CLI_PATH."
        )

    # -- routing ------------------------------------------------------------

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

    def build_params(
        self,
        route: AgentRoute,
        message: str,
        idempotency_key: str | None = None,
        session_key: str | None = None,
    ) -> dict[str, Any]:
        """Params for the verified `agent` RPC method.

        The gateway REQUIRES idempotencyKey. Carrying run/task identity in it
        rather than a random value gives s31.2 deduplication for free.

        `sessionKey` isolates the turn. Without it every specialist shares one
        session on the pharma-specialist agent, so parallel specialists would see
        each other's conversation history - a direct breach of the s10.3 context
        isolation rule.
        """
        params: dict[str, Any] = {
            "agentId": self.agent_id_for(route),
            "message": message,
            "idempotencyKey": idempotency_key or f"auto-{uuid.uuid4().hex[:16]}",
        }
        if session_key:
            params["sessionKey"] = session_key
        return params

    # -- transport ----------------------------------------------------------

    async def _run_cli(
        self, method: str, params: dict[str, Any], timeout_seconds: int
    ) -> dict[str, Any]:
        """Invoke `openclaw gateway call` and return the parsed JSON reply."""
        argv = [
            self.resolve_cli(),
            "gateway",
            "call",
            method,
            "--params",
            json.dumps(params),
            "--timeout",
            str(timeout_seconds * 1000),
            "--json",
        ]
        if method == AGENT_METHOD:
            argv.append("--expect-final")

        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        try:
            # Allow the CLI a grace period beyond its own timeout before we kill it.
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_seconds + 30
            )
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            raise GatewayTransientError(
                f"gateway call '{method}' exceeded {timeout_seconds}s"
            ) from exc

        text = stdout.decode("utf-8", "replace").strip()
        err = stderr.decode("utf-8", "replace").strip()

        if not text:
            raise classify_error(err or f"gateway call '{method}' returned no output")

        payload = extract_json_object(text)
        if payload is None:
            raise GatewayError(f"gateway call '{method}' returned unparseable output: {text[:300]}")
        return payload

    # -- gateway calls ------------------------------------------------------

    async def call_agent(
        self,
        route: AgentRoute,
        message: str,
        *,
        idempotency_key: str | None = None,
        session_key: str | None = None,
        timeout_seconds: int | None = None,
    ) -> GatewayReply:
        """One agent turn through the gateway."""
        timeout = timeout_seconds or self._settings.openclaw_timeout_seconds
        params = self.build_params(route, message, idempotency_key, session_key)

        started = time.monotonic()
        payload = await self._runner(AGENT_METHOD, params, timeout)
        elapsed_ms = int((time.monotonic() - started) * 1000)

        # A gateway-level failure is reported as ok:false with an error object.
        if payload.get("ok") is False:
            error = payload.get("error") or {}
            raise classify_error(
                error.get("message") or "gateway call failed",
                retryable=error.get("retryable"),
            )

        status = payload.get("status")
        if status and status != "ok":
            raise classify_error(f"agent turn status={status}: {payload.get('summary') or ''}")

        result = payload.get("result") or {}
        payloads = result.get("payloads") or []
        text = ""
        for item in payloads:
            if isinstance(item, dict) and item.get("text"):
                text = item["text"]
                break

        meta = result.get("meta") or {}
        report = meta.get("systemPromptReport") or {}

        info = self.route_info(route)
        info.provider = report.get("provider")
        info.model = report.get("model")
        info.session_key = report.get("sessionKey")

        usage = Usage(
            duration_ms=int(meta.get("durationMs") or elapsed_ms),
            steps=1,
        )

        if not text:
            raise GatewayError(f"agent turn on route '{route}' returned no text payload")

        return GatewayReply(
            text=text,
            usage=usage,
            route=info,
            run_id=payload.get("runId"),
            raw=payload,
        )

    # -- structured output --------------------------------------------------

    async def complete_structured(
        self,
        *,
        route: AgentRoute,
        system: str,
        user: str,
        schema: type[T],
        idempotency_key: str | None = None,
        session_key: str | None = None,
        timeout_seconds: int | None = None,
    ) -> tuple[T, Usage, RouteInfo]:
        """Structured call used for PLAN, CHALLENGE and SYNTHESIZE.

        One correction attempt on invalid output, then fail explicitly - the
        s2 fail-closed rule. A malformed plan must never reach the validator as
        a partially-populated object.
        """
        message = build_structured_prompt(system, user, schema)
        total = Usage()
        last_error: str | None = None
        info: RouteInfo | None = None
        base_key = idempotency_key or f"structured-{uuid.uuid4().hex[:12]}"

        for attempt in range(2):
            reply = await self.call_agent(
                route,
                message,
                # A correction is a distinct turn and needs its own key.
                idempotency_key=f"{base_key}-a{attempt}",
                session_key=session_key,
                timeout_seconds=timeout_seconds,
            )
            total = total + reply.usage
            info = reply.route

            data = extract_json_object(reply.text)
            if data is None:
                last_error = "no JSON object found in the response"
            else:
                try:
                    parsed = schema.model_validate(data)
                    return parsed, total, info
                except ValidationError as exc:
                    last_error = str(exc)

            if attempt == 0:
                log.warning(
                    "structured_output_correction",
                    schema=schema.__name__,
                    route=str(route),
                    error=(last_error or "")[:400],
                )
                message = (
                    f"{message}\n\n---\nYour previous response was rejected:\n"
                    f"{last_error}\n\nReturn the COMPLETE top-level "
                    f"{schema.__name__} object as raw JSON with every required "
                    "field present and no extra fields. Output nothing else."
                )

        raise StructuredOutputError(
            f"{schema.__name__} validation failed after one correction attempt "
            f"on route '{route}': {last_error}"
        )

    async def run_specialist(
        self,
        *,
        system: str,
        user: str,
        result_schema: type[T],
        idempotency_key: str | None = None,
        session_key: str | None = None,
        timeout_seconds: int | None = None,
    ) -> tuple[T, Usage, RouteInfo]:
        """Run one specialist profile through the Claude route."""
        return await self.complete_structured(
            route=AgentRoute.SPECIALIST,
            system=system,
            user=user,
            schema=result_schema,
            idempotency_key=idempotency_key,
            session_key=session_key,
            timeout_seconds=timeout_seconds,
        )

    # -- health -------------------------------------------------------------

    async def health_check(self) -> GatewayHealth:
        """Probe the gateway. Cheap: no model turn, no tokens spent."""
        try:
            payload = await self._runner(HEALTH_METHOD, {}, 15)
        except GatewayError as exc:
            return GatewayHealth(
                reachable=False,
                detail=str(exc)[:300],
                auth_mode=self._settings.openclaw_auth_mode,
            )

        loaded = payload.get("plugins", {}).get("loaded") or []
        return GatewayHealth(
            reachable=bool(payload.get("ok")),
            detail="gateway reachable" if payload.get("ok") else "gateway reported not ok",
            routes={r.value: True for r in AgentRoute},
            auth_mode=self._settings.openclaw_auth_mode,
            plugins_loaded=len(loaded),
        )

    async def aclose(self) -> None:
        return None


def get_gateway_client(settings: Settings | None = None) -> OpenClawClient:
    return OpenClawClient(settings)
