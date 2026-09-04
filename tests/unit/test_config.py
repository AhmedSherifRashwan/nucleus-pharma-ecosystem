"""Settings behaviour and production guardrails (PRD s21, s26.3)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.config import Settings


def _settings(**overrides) -> Settings:
    base = {
        "app_env": "development",
        "openclaw_gateway_token": "token",
        "_env_file": None,
    }
    return Settings(**{**base, **overrides})


def test_defaults_match_prd_budgets(settings):
    assert settings.default_max_specialists == 6
    assert settings.default_max_parallel_specialists == 4
    assert settings.default_max_followup_rounds == 1
    assert settings.default_max_runtime_seconds == 1200


def test_gateway_url_is_informational_only():
    """The adapter reaches the gateway through the CLI, which resolves the
    endpoint itself. PRD s28.3's /v1/responses route does not exist (D-001)."""
    s = _settings(openclaw_gateway_url="http://127.0.0.1:18789")
    assert s.openclaw_gateway_url == "http://127.0.0.1:18789"
    assert not hasattr(s, "gateway_responses_url")


def test_subscription_is_the_default_auth_mode():
    """PRD s20.1 default; s20.2 requires api_key to remain selectable."""
    assert _settings().openclaw_auth_mode == "subscription"
    assert _settings(openclaw_auth_mode="api_key").openclaw_auth_mode == "api_key"


def test_production_requires_a_gateway_token():
    with pytest.raises(ValidationError, match="OPENCLAW_GATEWAY_TOKEN"):
        _settings(app_env="production", openclaw_gateway_token=None)


def test_production_refuses_provisional_profiles():
    """A15/A16 must stay disabled in production until client SDS approval."""
    with pytest.raises(ValidationError, match="provisional"):
        _settings(app_env="production", enable_provisional_profiles=True)


def test_parallelism_cannot_exceed_total_specialists():
    with pytest.raises(ValidationError, match="cannot exceed"):
        _settings(default_max_specialists=2, default_max_parallel_specialists=4)


def test_specialist_budget_is_bounded_to_the_registry():
    with pytest.raises(ValidationError):
        _settings(default_max_specialists=99)


def test_no_secret_has_a_default_value(monkeypatch):
    """A credential must never have a usable built-in default (s21)."""
    monkeypatch.delenv("OPENCLAW_GATEWAY_TOKEN", raising=False)
    s = Settings(_env_file=None)
    assert s.openclaw_gateway_token is None
