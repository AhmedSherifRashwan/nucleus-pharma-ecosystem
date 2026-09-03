# Decisions Register

Open questions and resolved decisions. Per CLAUDE.md, missing pharmaceutical
requirements are flagged here rather than invented.

## Open — need client/user input

### D-001 · OpenClaw gateway contract is UNVERIFIED · **blocking Phase 3**
PRD §28.3 sketches `POST /v1/responses` with `x-openclaw-agent-id` and a
`openclaw/<agent>` model string. This was not verifiable at build time: the
gateway was not installed, and the PRD's own Appendix C says interfaces "must be
verified against the pinned versions selected at build time."

*Status:* OpenClaw **2026.9.1 (ad6fe23)** is now installed at `~/.openclaw/bin`
(it was missing from `PATH`; the installer does not write to `~/.zshrc`).
Onboarding and the Slack plugin install are still outstanding.

*Consequence:* `app/providers/openclaw_client.py` ships as an interface skeleton.
`build_request` / `build_headers` encode the §28.3 sketch and are unit-tested for
*shape*, not correctness. The transport raises `GatewayNotConfigured`.

*To close:* run the gateway, confirm the real request/response envelope, usage
counters and error codes, then implement transport in Phase 3.

### D-004 · Subscription auth vs provider terms
§20.1 makes subscription auth (ChatGPT/Codex OAuth, Claude CLI reuse) the default
and §20.2 requires an API-key fallback. Consumer subscription terms generally
cover interactive personal use rather than programmatic backend serving, which is
what this system does.

*Decision (user, 3 Sep 2026):* subscription is the default route; API-key remains
the configurable fallback. `OPENCLAW_AUTH_MODE` records which route ran each run
so reports can state it.

*Open:* confirm both providers' terms before the client deployment. This is an
account-level risk, not a technical one.

### D-005 · A15 / A16 remain provisional
No dedicated client SDS exists for GMP Compliance or Regulatory Submission (§11).
They stay disabled; `Settings` refuses to start in production with
`ENABLE_PROVISIONAL_PROFILES=true`. Needs client confirmation or replacement roles.

## Resolved

### D-002 · Homebrew PostgreSQL instead of Docker
Docker is not installed on the build machine. `docker-compose.yml` (§29.1) ships
as documented, and Homebrew PostgreSQL 17 is used locally. Nothing in the
application depends on the choice. Databases: `pharma_ai`, `pharma_ai_test`.

### D-003 · `SpecialistResult` keeps per-finding `basis` tagging
Appendix A defines `findings: list[str]`. This build uses `list[Finding]` with a
`basis` field (fact / inference / assumption / estimate).

*Why:* §2 and FR-007 require material findings to cite evidence or be labelled
assumptions. With plain strings that rule cannot be checked in code. With `basis`
it is one method — `SpecialistResult.unsupported_findings()` — which the Phase 6
quality gate consumes.

*Decision (user, 3 Sep 2026):* keep the richer schema. Appendix A field *names*
are otherwise followed (`needs_information`, `EvidenceRef`, `confidence`,
`escalation_required`).

### D-006 · Fresh repository rather than porting v2
A working v2.x build exists at `../ai-ecosystem` (FastAPI + Slack Bolt + direct
provider SDKs, 126 passing tests). *Decision (user, 3 Sep 2026):* build v3 fresh,
phase by phase, per §43. The v2 repo remains untouched as a reference.

### D-007 · No `users` or `organizations` table
§14's table list omits both and §3.2 rules out multi-tenancy. Identity is the
Slack user id recorded on `approvals` and `decisions`. Adding tenancy later is a
migration, not a redesign.
