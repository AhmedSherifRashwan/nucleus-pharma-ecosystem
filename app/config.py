"""Application configuration (PRD v3.0 s26.3).

Model and provider identity live in the OpenClaw gateway, not here: the worker
selects a logical OpenClaw *agent route* rather than a provider model ID
(s2 "model abstraction", s20.3). Nothing in this file may contain a secret
value; secrets arrive from the environment.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

AuthMode = Literal["subscription", "api_key"]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # ---- Application -------------------------------------------------------
    app_env: Literal["development", "staging", "production"] = "development"
    log_level: str = "INFO"
    log_format: Literal["console", "json"] = "console"

    # ---- Database (s14) ----------------------------------------------------
    database_url: str = "postgresql+psycopg://ai:change-me@localhost:5432/pharma_ai"
    langgraph_database_url: str = "postgresql://ai:change-me@localhost:5432/pharma_ai"

    # ---- OpenClaw gateway (s7, s20, s28.3) ---------------------------------
    # Informational only. The adapter reaches the gateway through the OpenClaw
    # CLI, which resolves the endpoint itself (decisions.md D-012).
    openclaw_gateway_url: str = "http://127.0.0.1:18789"
    openclaw_gateway_token: str | None = None
    # Path to the OpenClaw CLI. Left unset it is discovered: PATH first, then
    # the default install location. A supervised worker often has a minimal PATH.
    openclaw_cli_path: str | None = None
    openclaw_executive_agent: str = "pharma-executive"
    openclaw_specialist_agent: str = "pharma-specialist"
    openclaw_local_agent: str = "pharma-local"

    # s20.1 default is subscription auth; s20.2 REQUIRES the api_key fallback to
    # remain available. Recorded per-run so a report can state which route ran it.
    openclaw_auth_mode: AuthMode = "subscription"

    # Observed: a specialist asked for report-length content with references
    # runs 3-10 minutes. 300s failed two of five tasks on the first live run.
    openclaw_timeout_seconds: int = 900
    openclaw_max_retries: int = 3

    # ---- Budgets (s12.2, s22) ----------------------------------------------
    default_max_specialists: int = Field(default=6, ge=1, le=16)
    default_max_parallel_specialists: int = Field(default=4, ge=1, le=16)
    default_max_followup_rounds: int = Field(default=1, ge=0, le=3)
    default_max_specialist_steps: int = Field(default=12, ge=1, le=50)
    default_max_runtime_seconds: int = Field(default=1200, ge=60)

    # ---- Artifacts and reports (s18, s32) ----------------------------------
    artifact_storage: Literal["local", "s3"] = "local"
    artifact_local_path: str = "./data/artifacts"
    report_template: str = "./templates/pharma_report_template.docx"
    report_pdf_enabled: bool = False

    # ---- Slack approvals (s16) ---------------------------------------------
    # Comma-separated Slack user ids permitted to decide any run's gates. Empty
    # means only the user who started a run may decide it.
    slack_approver_ids: str = ""

    # ---- Worker (s31) ------------------------------------------------------
    worker_poll_interval_seconds: float = 2.0
    worker_max_job_attempts: int = 3
    worker_id: str | None = None

    # ---- Registry (s11) ----------------------------------------------------
    agent_definitions_path: str = "app/agents/definitions"
    worker_prompts_path: str = "app/prompts/workers"
    executive_prompts_path: str = "app/prompts/executive"
    enable_provisional_profiles: bool = False

    @model_validator(mode="after")
    def _check_production_requirements(self) -> Settings:
        if self.app_env == "production":
            if not self.openclaw_gateway_token:
                raise ValueError(
                    "OPENCLAW_GATEWAY_TOKEN is required in production: the gateway "
                    "endpoint is privileged operator access (PRD s21)"
                )
            if self.enable_provisional_profiles:
                raise ValueError(
                    "A15/A16 are provisional and must stay disabled in production "
                    "until dedicated client SDS documents are approved (PRD s11)"
                )
        if self.default_max_parallel_specialists > self.default_max_specialists:
            raise ValueError(
                "DEFAULT_MAX_PARALLEL_SPECIALISTS cannot exceed DEFAULT_MAX_SPECIALISTS"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


def reset_settings_cache() -> None:
    """Test helper: drop the cached Settings so env changes take effect."""
    get_settings.cache_clear()
