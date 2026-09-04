# Progress

**PRD:** v3.0 (3 September 2026) · **Method:** one phase per session (§41.1)

| Phase | Milestone | Status |
|-------|-----------|--------|
| 0 | Foundation: repo, settings, logging, models/migrations, schemas, gateway skeleton, diagnostics | **Complete** |
| 1 | Agent Registry: A01–A16 YAML, validation, prompt versions, seed | **Complete** |
| 2 | PostgreSQL Run Model: repositories and idempotency | **Complete** |
| 3 | OpenClaw Client: gateway transport, routing, error normalisation | **Complete** |
| 4 | Executive Planner: prompt, structured parsing, deterministic validator | **Complete** |
| 5 | Generic Specialist Runtime: envelope, prompt composition, evidence enforcement | **Mostly complete** — tool skills outstanding |
| 6 | LangGraph Core: all §8 states, PostgresSaver, human gates | **Complete** |
| 7 | Parallel Specialists | **Complete** — covered by the Phase 6 dispatcher |
| 8 | Challenge and Conflict | **Complete** — conflict detection, challenge, bounded follow-ups |
| 9 | OpenClaw Pharma Skill | Not started |
| 10 | Worker Service | Not started |
| 11 | Slack Delivery | Not started |
| 12 | Reports (DOCX/PDF) | Not started |
| 13 | Human Decisions | **Mostly complete** — both gates in the graph; Slack UX pending |
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

## Phase 1 — delivered

**Gates:** ruff clean · mypy clean (23 source files) · 85 tests passing.

- `app/schemas/agent.py`: versioned `AgentProfile` carrying `status`, `source_sds`,
  the §12.1 `common_follow_on` routing, tool allowlist, knowledge scopes, forbidden
  actions and escalation policy.
- `app/agents/definitions/`: all 16 profiles. A01–A14 derived from their named
  client SDS; A15/A16 provisional with no source document.
- `app/prompts/workers/`: shared prompt (naming the ecosystem **Neucleus**) plus 16
  domain prompts.
- `app/agents/registry.py`: loader that fails closed on an unknown tool, an AMBER/RED
  tool, an unknown knowledge scope, a missing prompt, an active profile with no
  `source_sds`, or an unknown follow-on agent.
- `scripts/seed_agents.py`: idempotent seeding of profiles and prompt versions;
  refuses to proceed if a released prompt changed without a version bump.

**Verified by test:**
- All 16 load; only A15/A16 are provisional and they are disabled by default.
- Every active profile names its SDS, and that file exists in the repo.
- No profile holds an AMBER/RED tool, and no tool falls outside the §19 table.
- The §12.1 routing matrix matches the PRD and drops disabled agents.
- Every prompt composes with no placeholders, names Neucleus, states the
  untrusted-data boundary and carries the evidence-basis rule.
- Seeding is idempotent, snapshots the whole profile, and refuses released-prompt drift.

## Phase 3 — delivered

**Gates:** ruff clean · mypy clean (23 source files) · 119 tests passing, 3 live tests gated.

- `app/providers/openclaw_client.py`: the verified `agent` RPC contract behind one
  adapter. Executive/specialist/local route selection, structured output with one
  correction attempt, normalised errors, usage capture, CLI discovery.
- Errors are classified into `GatewayRateLimited`, `GatewayAuthError`,
  `GatewayTransientError`, `GatewayNotConfigured` and `StructuredOutputError` so
  orchestration branches on cause rather than on wire detail.
- `scripts/health_check.py` now performs a real gateway probe (no tokens spent).
- `tests/integration/test_openclaw_live.py`: gated live tests
  (`RUN_LIVE_GATEWAY=1`).

**Verified live against OpenClaw 2026.9.1:**
- Health probe: reachable, 15 plugins, subscription auth.
- Structured call on the specialist route returned schema-valid JSON, with
  `provider=claude-cli` and `model=claude-sonnet-5` captured for the run record (§20).
- A domain question (PEG 400 / polysorbate 80 dissolution slow-down) returned the
  correct mechanism — aldehyde cross-linking of the gelatin shell.

**Verified by test:**
- Requests name a logical agent route, never a provider model (§2).
- Every turn carries an idempotency key; correction turns get distinct keys, so a
  retry is never replayed as the original.
- A rate limit raises `GatewayRateLimited` and does **not** retry against another
  route (§22: never silently switch models).
- Invalid structured output gets exactly one correction, then fails explicitly.
- JSON is recovered from fenced blocks, prose and nested/brace-containing strings.
- CLI discovery honours `OPENCLAW_CLI_PATH`, falls back to the default install
  location, and names the setting to fix when it cannot find the binary.

## Phase 2 — delivered

Repository layer over the system of record. Orchestration never issues raw SQL.

- `projects` (Slack thread → one project/session, §17.2), `runs` (`RUN-YYYY-NNNN`
  refs, idempotency lookup), `jobs` (claim with `FOR UPDATE SKIP LOCKED`, backoff,
  stale reclaim), `evidence` + `artifacts` (reliability levels, report versioning),
  `governance` (assumptions, conflicts, risks, approvals, decisions), `audit`.

**Verified by test (21 cases):**
- A Slack thread maps to exactly one project; a follow-up reuses it.
- The same idempotency key returns the same run, and the same job.
- Two concurrent workers never claim one job; a crashed worker's job is reclaimed.
- Failure backs off, then fails explicitly once attempts are exhausted.
- A decided approval gate cannot be re-decided, and an invalid decision class is refused.
- Open **material** conflicts are surfaced separately from cosmetic ones.
- Superseded decisions drop out of project memory (FR-020).
- Specialist results record route, model and agent version (§20).
- Reports version rather than overwrite (§18.3).

## Phase 4 — delivered

- `app/prompts/executive/plan_v1.md`: versioned planner prompt carrying the agent
  catalog, §12.1 routing matrix, project context and prior approved decisions.
- `app/orchestration/planner.py`: PLAN through the executive route, returning a
  validated outcome rather than a raw plan.
- `app/orchestration/plan_validator.py`: the deterministic gate — agent existence
  and eligibility, acyclicity, budgets, forbidden actions, step clamping, and
  execution groups recomputed from dependencies rather than trusted.
- `tests/fixtures/routing_cases.json`: 15 routing cases including the four §34.4
  worked examples.

**Verified live against the gateway:**
- *"Dissolution slowing at 6-month accelerated, PEG 400 / polysorbate 80 fill"* →
  Analytical, Fill and Shell dispatched in parallel, then Stability integrating all
  three. Correct shape, unprompted.
- *"Approve the batch release for lot 24-001"* → QC then independent QA review,
  approval gate on, no commercial agents selected.

**Verified by test:**
- A planner-supplied execution grouping is discarded and recomputed, so a dependent
  task can never run alongside its dependency.
- Regulated language forces the approval gate **by pattern, not literal substring** —
  "assign *a* shelf life" and "close the *major* CAPA" both gate, while ordinary
  questions about shelf life or regulatory pathway do not.

## Phase 5 — delivered

- `app/orchestration/specialist_runner.py`: one runtime for all 16 profiles. Builds
  the task envelope from the profile, composes the prompt, calls the specialist
  route, enforces the evidence policy and returns a typed outcome.
- `scripts/generate_skills.py` + `openclaw/workspace/skills/neucleus-*/SKILL.md`:
  16 OpenClaw skills **generated** from the registry, one per specialist. The body
  is exactly the prompt the runtime would send, so the two cannot diverge.

**Verified live (A07 Shell Formulation, no data supplied):**
The specialist's first result contained one unevidenced claim. The runner detected
it, asked for a correction, and the specialist **relabelled it as an assumption
rather than inventing a citation**. Final result: 4 findings, all basis
`assumption`, zero `fact` or `inference`, `status=needs_information`,
`confidence=low`, escalation raised naming the profile's own triggers, and 7
specific items in `information_required`. That is the evidence-first rule holding
under pressure rather than in principle.

**Verified by test (27 cases):**
- Committed skills match the registry; a drift fails the suite with the regenerate
  command in the message. Stale skill directories are removed.
- Every active skill cites its source SDS; provisional ones say they have none.
- A task message marks upstream findings as fallible colleagues' work, not instructions.
- A specialist with no tools is told to disclose the gap rather than answer from recollection.
- A profile's own tool list is offered and nothing else.
- Evidence violations trigger exactly one correction; persistent violations are
  reported, never silently relabelled by the runner.
- Task identity is owned by the platform: a model returning wrong ids cannot corrupt
  the run record.
- A disabled or provisional profile refuses to execute.
- The downstream summary carries conclusions and references, not working detail.

**Outstanding in Phase 5:** the six §19 tools are declared in the envelope but not
yet callable — the skill wrappers that let a specialist invoke `project_search`,
`save_evidence` and the rest are still to build (D-013).

## Phase 6 — delivered (with 7, 8 and most of 13)

Every §8 state is implemented and wired, with durable Postgres checkpointing.

`INTAKE → LOAD_PROJECT_CONTEXT → OPENAI_PLAN → VALIDATE_PLAN → [PLAN_APPROVAL] →
DISPATCH → COLLECT → CONFLICT_DETECTION → [CHALLENGE → FOLLOW_UP_TASKS] →
SYNTHESIS → QUALITY_GATE → [HUMAN_DECISION] → PERSIST_FINAL_STATE`

- `state.py`: §8.1 run state with reducers, so parallel branches merge safely.
- `conflict.py`: deterministic signals computed from the record — declared
  conflicts, escalations, incomplete tasks, unevidenced claims, low confidence.
- `challenge.py`: adversarial review; follow-ups pass the same policy gate as a plan.
- `synthesis.py`: the brief, plus `enforce_integrity` and the seven-check quality gate.
- `graph.py`: 17 nodes with both human gates as LangGraph interrupts.
- `engine.py`: `AsyncPostgresSaver`, and a resume that continues from the last
  checkpoint when the graph is not actually paused.

**Verified by test (14 graph cases, all first-run):**
- Four specialists: three in parallel, then the dependent governance review.
- A dependent specialist receives its dependencies' summaries, marked as fallible.
- A failed specialist forces `review_required` — the brief cannot read as clean.
- An invalid plan or a provisional agent fails **before anything is dispatched**.
- Plan approval pauses with zero specialist calls made; approve resumes, decline cancels.
- A RED recommendation blocks completion until a human answers, and the decision is recorded.
- A material contradiction omitted from the brief is reinstated by the platform.
- The follow-up round is bounded: exactly one challenge, never a loop.
- **A run resumes on a fresh graph instance sharing the checkpoint store** (FR-016).

## Next

OpenClaw is fully operational: both model routes verified, Slack connected to
`#main-comms` under an allowlist, messaging tool granted.

Phase 2 (repositories over the run model) and Phase 4 (planner) are both unblocked.

**Phase 5 is unblocked**: D-013 is decided — tools become OpenClaw skills whose
scripts call into this codebase, so implementations stay in Python.

Outstanding OpenClaw item: `commands.ownerAllowFrom` is unset, which blocks §16
approval authorisation. It needs the operator's Slack member ID.
