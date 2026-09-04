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

### D-009 · Ecosystem name is **Neucleus**
*Decision (user, 4 Sep 2026):* the assistant is `@neucleus`, not PharmaAI. Applied
to `app/prompts/workers/common_v1.md`; the Slack app display name follows in Phase 9.

*Open:* the standard English spelling is "nucleus". `@neucleus` is used exactly as
given. Confirm before the Slack app is created — a bot handle is user-visible and
awkward to change later.

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


### D-008 · Tool set narrowed to PRD §19
v2 profiles carried `read_evidence` and `web_research_general`. Neither appears in
the §19 tool table, so both were dropped rather than carried over silently.

*Consequence:* a specialist can create evidence but cannot read back an evidence
item another specialist saved; upstream evidence reaches it only through the
orchestrator's context assembly. Commercially-oriented profiles (A01) now have
`web_research_official` only.

*Open:* if the client wants either capability, add it to §19 first. Flagged rather
than assumed.

### D-010 · A15/A16 still have no source specification
The client re-supplied the source pack on 4 Sep 2026. It is byte-identical to what
was already in `docs/source-specifications/` and contains **14** specialist SDS
documents, not 16. The instruction was to use "these exact personalities".

A01–A14 are derived from their named SDS and each records `source_sds`. A15 and A16
have no source document, remain `status: provisional`, and stay disabled. The
registry refuses to load an `active` profile without a `source_sds`, so this cannot
be papered over by accident.

*To close:* the client supplies SDS documents for GMP Compliance and Regulatory
Submission, or nominates two replacement roles.
