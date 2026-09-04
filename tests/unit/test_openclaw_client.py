"""OpenClaw gateway adapter (PRD s7, s20.3, Phase 3).

Mocked transport throughout: these assert the adapter contract, error
normalisation and structured-output discipline without touching a gateway.
"""

from __future__ import annotations

import json

import pytest
from pydantic import BaseModel

from app.config import Settings
from app.providers.openclaw_client import (
    AgentRoute,
    GatewayAuthError,
    GatewayError,
    GatewayRateLimited,
    GatewayTransientError,
    OpenClawClient,
    StructuredOutputError,
    Usage,
    build_structured_prompt,
    classify_error,
    extract_json_object,
)
from app.schemas import Plan, SpecialistResult


class Tiny(BaseModel):
    value: int
    label: str


def _settings(**over) -> Settings:
    base = {"openclaw_gateway_token": "t", "_env_file": None}
    return Settings(**{**base, **over})


def _reply(text: str, provider="claude-cli", model="claude-sonnet-5", duration=1234):
    """Shape of a real `agent` RPC reply, verified against OpenClaw 2026.9.1."""
    return {
        "runId": "r-1",
        "status": "ok",
        "summary": "completed",
        "result": {
            "payloads": [{"text": text, "mediaUrl": None}],
            "meta": {
                "durationMs": duration,
                "finalAssistantVisibleText": text,
                "systemPromptReport": {
                    "provider": provider,
                    "model": model,
                    "sessionKey": "agent:pharma-specialist:main",
                },
            },
        },
    }


class Runner:
    """Stub transport. Returns queued replies and records the calls made."""

    def __init__(self, *replies):
        self._replies = list(replies)
        self.calls: list[tuple[str, dict, int]] = []

    async def __call__(self, method, params, timeout):
        self.calls.append((method, params, timeout))
        if not self._replies:
            raise AssertionError("stub ran out of replies")
        nxt = self._replies.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return nxt


def _client(runner, **over) -> OpenClawClient:
    return OpenClawClient(_settings(**over), runner=runner)


# -- routing (s7.3, s2 model abstraction) ------------------------------------


def test_routes_resolve_to_configured_agent_ids():
    c = _client(Runner(), openclaw_executive_agent="x", openclaw_specialist_agent="y")
    assert c.agent_id_for(AgentRoute.EXECUTIVE) == "x"
    assert c.agent_id_for(AgentRoute.SPECIALIST) == "y"


def test_params_name_a_logical_agent_not_a_provider_model():
    params = _client(Runner()).build_params(AgentRoute.EXECUTIVE, "hi")
    assert params["agentId"] == "pharma-executive"
    assert "gpt" not in json.dumps(params) and "claude" not in json.dumps(params)


def test_idempotency_key_is_always_present():
    """The gateway rejects a turn without one, and s31.2 depends on it."""
    p = _client(Runner()).build_params(AgentRoute.SPECIALIST, "hi")
    assert p["idempotencyKey"]


def test_supplied_idempotency_key_is_used_verbatim():
    p = _client(Runner()).build_params(AgentRoute.SPECIALIST, "hi", "run-7:task-3")
    assert p["idempotencyKey"] == "run-7:task-3"


# -- reply parsing (verified contract) ---------------------------------------


async def test_agent_reply_is_parsed():
    runner = Runner(_reply("hello"))
    reply = await _client(runner).call_agent(AgentRoute.SPECIALIST, "hi")
    assert reply.text == "hello"
    assert reply.run_id == "r-1"
    assert reply.usage.duration_ms == 1234


async def test_reply_records_provider_and_model_for_the_run():
    """s20: a run must record which route produced it."""
    runner = Runner(_reply("hello", provider="claude-cli", model="claude-sonnet-5"))
    reply = await _client(runner).call_agent(AgentRoute.SPECIALIST, "hi")
    assert reply.route.provider == "claude-cli"
    assert reply.route.model == "claude-sonnet-5"
    assert reply.route.agent_id == "pharma-specialist"
    assert reply.route.auth_mode == "subscription"


async def test_auth_mode_is_recorded_when_using_api_keys():
    runner = Runner(_reply("hi"))
    reply = await _client(runner, openclaw_auth_mode="api_key").call_agent(
        AgentRoute.EXECUTIVE, "hi"
    )
    assert reply.route.auth_mode == "api_key"


async def test_empty_payload_is_an_error_not_an_empty_string():
    bad = _reply("x")
    bad["result"]["payloads"] = []
    with pytest.raises(GatewayError, match="no text payload"):
        await _client(Runner(bad)).call_agent(AgentRoute.SPECIALIST, "hi")


# -- error normalisation (s22, s38) ------------------------------------------


@pytest.mark.parametrize(
    ("message", "expected"),
    [
        ("Your credit balance is too low", GatewayRateLimited),
        ("429 Too Many Requests", GatewayRateLimited),
        ("usage limit reached", GatewayRateLimited),
        ("401 unauthorized", GatewayAuthError),
        ("invalid token supplied", GatewayAuthError),
        ("request timed out", GatewayTransientError),
        ("ECONNREFUSED", GatewayTransientError),
        ("503 service unavailable", GatewayTransientError),
    ],
)
def test_provider_errors_are_classified(message, expected):
    assert isinstance(classify_error(message), expected)


def test_unknown_error_stays_generic_unless_marked_retryable():
    assert type(classify_error("something odd")) is GatewayError
    assert isinstance(classify_error("something odd", retryable=True), GatewayTransientError)


async def test_gateway_error_object_is_raised_as_typed_error():
    runner = Runner({"ok": False, "error": {"message": "rate limit hit", "retryable": True}})
    with pytest.raises(GatewayRateLimited):
        await _client(runner).call_agent(AgentRoute.SPECIALIST, "hi")


async def test_non_ok_status_raises():
    runner = Runner({"runId": "r", "status": "failed", "summary": "provider down"})
    with pytest.raises(GatewayError):
        await _client(runner).call_agent(AgentRoute.SPECIALIST, "hi")


async def test_rate_limit_never_silently_switches_route():
    """s22: pause or use a configured fallback, never swap models quietly."""
    runner = Runner({"ok": False, "error": {"message": "usage limit reached"}})
    with pytest.raises(GatewayRateLimited):
        await _client(runner).call_agent(AgentRoute.SPECIALIST, "hi")
    assert len(runner.calls) == 1  # no retry against a different route


# -- JSON extraction ---------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ('{"a": 1}', {"a": 1}),
        ('```json\n{"a": 1}\n```', {"a": 1}),
        ('Sure thing! {"a": 1} hope that helps', {"a": 1}),
        ('{"a": {"b": [1, 2]}}', {"a": {"b": [1, 2]}}),
        ('{"a": "brace } inside string"}', {"a": "brace } inside string"}),
    ],
)
def test_json_is_recovered_from_prose(text, expected):
    assert extract_json_object(text) == expected


@pytest.mark.parametrize("text", ["", "no json", "{unbalanced", "[1,2,3]"])
def test_unrecoverable_json_returns_none(text):
    assert extract_json_object(text) is None


def test_structured_prompt_embeds_the_schema():
    prompt = build_structured_prompt("policy", "task", Tiny)
    assert "Tiny" in prompt and "policy" in prompt and "task" in prompt
    assert '"value"' in prompt and '"label"' in prompt


# -- structured output + correction (s2 fail closed) -------------------------


async def test_valid_first_response_makes_one_call():
    runner = Runner(_reply('{"value": 1, "label": "ok"}'))
    result, usage, info = await _client(runner).complete_structured(
        route=AgentRoute.EXECUTIVE, system="s", user="u", schema=Tiny
    )
    assert result.value == 1
    assert len(runner.calls) == 1
    assert usage.steps == 1


async def test_invalid_output_gets_one_correction_then_succeeds():
    runner = Runner(
        _reply('{"value": "not-an-int"}'),
        _reply('{"value": 2, "label": "fixed"}'),
    )
    result, usage, _ = await _client(runner).complete_structured(
        route=AgentRoute.EXECUTIVE, system="s", user="u", schema=Tiny
    )
    assert result.value == 2
    assert len(runner.calls) == 2
    assert usage.steps == 2
    # The correction must tell the model what was wrong.
    assert "was rejected" in runner.calls[1][1]["message"]


async def test_twice_invalid_fails_explicitly():
    runner = Runner(_reply('{"nope": 1}'), _reply('{"still": "wrong"}'))
    with pytest.raises(StructuredOutputError, match="after one correction attempt"):
        await _client(runner).complete_structured(
            route=AgentRoute.EXECUTIVE, system="s", user="u", schema=Tiny
        )
    assert len(runner.calls) == 2  # bounded: no infinite correction loop


async def test_prose_without_json_is_corrected():
    runner = Runner(_reply("I think the answer is 42."), _reply('{"value": 42, "label": "a"}'))
    result, _, _ = await _client(runner).complete_structured(
        route=AgentRoute.EXECUTIVE, system="s", user="u", schema=Tiny
    )
    assert result.value == 42


async def test_each_correction_turn_gets_a_distinct_idempotency_key():
    """Reusing the key would make the gateway replay the first turn."""
    runner = Runner(_reply("prose"), _reply('{"value": 1, "label": "a"}'))
    await _client(runner).complete_structured(
        route=AgentRoute.EXECUTIVE, system="s", user="u", schema=Tiny, idempotency_key="run-1"
    )
    keys = [c[1]["idempotencyKey"] for c in runner.calls]
    assert keys == ["run-1-a0", "run-1-a1"]
    assert len(set(keys)) == 2


# -- real schemas ------------------------------------------------------------


async def test_a_real_plan_round_trips():
    plan = {
        "objective": "Assess the dissolution slow-down.",
        "output_type": "report",
        "tasks": [
            {
                "task_id": "T1",
                "agent_id": "A10",
                "objective": "Characterise the stability trend.",
                "required_output": "Trend assessment.",
                "depends_on": [],
                "context_refs": [],
                "max_steps": 12,
            }
        ],
        "information_gaps": [],
        "assumptions": [],
        "estimated_complexity": "medium",
        "plan_approval_required": True,
        "approval_rationale": "Regulated stability signal.",
        "execution_groups": [],
    }
    runner = Runner(_reply(json.dumps(plan)))
    result, _, _ = await _client(runner).complete_structured(
        route=AgentRoute.EXECUTIVE, system="s", user="u", schema=Plan
    )
    assert result.selected_agents == ["A10"]
    assert result.plan_approval_required


async def test_run_specialist_uses_the_specialist_route():
    result_json = {
        "task_id": "T1",
        "agent_id": "A07",
        "status": "complete",
        "executive_summary": "Shell assessment done.",
        "findings": [],
        "recommendations": [],
        "risks": [],
        "assumptions": [],
        "evidence_refs": [],
        "conflicts_or_questions": [],
        "confidence": "medium",
        "artifacts": [],
        "escalation_required": False,
        "escalation_reason": None,
        "information_required": [],
    }
    runner = Runner(_reply(json.dumps(result_json)))
    result, _, info = await _client(runner).run_specialist(
        system="s", user="u", result_schema=SpecialistResult
    )
    assert result.agent_id == "A07"
    assert runner.calls[0][1]["agentId"] == "pharma-specialist"
    assert info.route is AgentRoute.SPECIALIST


# -- health ------------------------------------------------------------------


async def test_health_reports_reachable():
    runner = Runner({"ok": True, "plugins": {"loaded": ["slack", "anthropic"]}})
    health = await _client(runner).health_check()
    assert health.reachable
    assert health.plugins_loaded == 2


async def test_health_reports_unreachable_without_raising():
    runner = Runner(GatewayTransientError("connection refused"))
    health = await _client(runner).health_check()
    assert not health.reachable
    assert "connection refused" in health.detail


def test_usage_accumulates():
    assert (Usage(1, 2, 3, 1) + Usage(4, 5, 6, 1)).steps == 2


# -- CLI discovery (supervised workers often have a minimal PATH) -------------


def test_explicit_cli_path_is_used(tmp_path):
    fake = tmp_path / "openclaw"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    c = _client(Runner(), openclaw_cli_path=str(fake))
    assert c.resolve_cli() == str(fake)


def test_missing_explicit_cli_path_fails_closed(tmp_path):
    from app.providers.openclaw_client import GatewayNotConfigured

    c = _client(Runner(), openclaw_cli_path=str(tmp_path / "nope"))
    with pytest.raises(GatewayNotConfigured, match="missing file"):
        c.resolve_cli()


def test_discovery_falls_back_when_not_on_path(monkeypatch, tmp_path):
    """A worker started by launchd/systemd may not inherit the user's PATH."""
    import app.providers.openclaw_client as mod

    fake = tmp_path / "openclaw"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setattr(mod.shutil, "which", lambda _: None)
    monkeypatch.setattr(mod, "FALLBACK_CLI_PATHS", (str(fake),))

    assert _client(Runner()).resolve_cli() == str(fake)


def test_discovery_failure_names_what_to_set(monkeypatch):
    import app.providers.openclaw_client as mod
    from app.providers.openclaw_client import GatewayNotConfigured

    monkeypatch.setattr(mod.shutil, "which", lambda _: None)
    monkeypatch.setattr(mod, "FALLBACK_CLI_PATHS", ())
    with pytest.raises(GatewayNotConfigured, match="OPENCLAW_CLI_PATH"):
        _client(Runner()).resolve_cli()
