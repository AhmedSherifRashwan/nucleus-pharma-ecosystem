# Progress

**PRD:** v3.0 (3 September 2026) · **Method:** one phase per session (§41.1)

| Phase | Milestone | Status |
|-------|-----------|--------|
| 0 | Foundation: repo, settings, logging, models/migrations, schemas, gateway skeleton, diagnostics | **Complete** |
| 1 | Agent Registry: A01–A16 YAML, validation, prompt versions, seed | Not started |
| 2 | PostgreSQL Run Model: repositories and idempotency | Partial — tables and constraints exist; repositories pending |
| 3 | OpenClaw Client: live `/v1/responses` transport | **Blocked on D-001** |
| 4 | OpenAI Planner | Not started |
| 5 | Generic Claude Worker | Not started |
| 6 | LangGraph Core | Not started |
| 7 | Parallel Specialists | Not started |
| 8 | Challenge and Conflict | Not started |
| 9 | OpenClaw Pharma Skill | Not started |
| 10 | Worker Service | Not started |
| 11 | Slack Delivery | Not started |
| 12 | Reports (DOCX/PDF) | Not started |
| 13 | Human Decisions | Not started |
| 14 | Expand to 16 Profiles | Not started |
| 15 | Production Hardening | Not started |

## Phase 0 — delivered

**Gates:** ruff clean · mypy clean (20 source files) · 49 tests passing.

- Repository per §25, with `docs/source-specifications/` holding all 18 client documents.
- `pyproject.toml` per §26.2 — deliberately **no** FastAPI, Slack Bolt, or openai/anthropic SDKs.
- `app/config.py`: §26.3 environment, budget bounds, and production guardrails that refuse
  to start without a gateway token or with provisional profiles enabled.
- `app/audit/logger.py`: structured logging with secret redaction.
- `app/storage/models.py`: all 16 §14 tables plus `document_chunks`; Alembic migration applied
  to `pharma_ai` and `pharma_ai_test`.
- `app/schemas/`: typed contracts for §9.1 Plan, §10 TaskEnvelope/SpecialistResult,
  §9.2 ChallengeResult, §9.3 ExecutiveDecisionBrief, §8 QualityGateResult, §13 AgentMessage,
  §15.1 reliability levels 1–4, §16 GREEN/AMBER/RED.
- `app/providers/openclaw_client.py`: interface skeleton. Transport raises
  `GatewayNotConfigured` rather than guessing an unverified contract (D-001).
- `scripts/health_check.py`: Phase 0 diagnostic CLI; makes no model calls.

**Verified by test, not assertion:**
- A stated fact with no evidence reference is flagged unsupported; a labelled assumption is not (§15, FR-007).
- `escalation_required` without a reason is rejected.
- An unknown field on any contract is rejected, so gateway drift fails loudly.
- RED dominates AMBER in decision class (§16).
- Duplicate run refs, job idempotency keys and evidence refs are rejected by database constraint.
- Report artifacts version rather than overwrite (§18.3).
- A run may hold several jobs — orchestrate plus resume per decision (§31).
- The gateway transport fails closed and the health check does not claim reachability it lacks (§2).
- Requests name a logical OpenClaw agent, never a provider model (§2 model abstraction).

## Next

Phase 1 (Agent Registry) is unblocked and does not need the gateway.
Phase 3 needs D-001 closed first: OpenClaw onboarded, Slack plugin installed, and the
real `/v1/responses` contract confirmed.
