# Decisions Register

Open questions and resolved decisions. Per CLAUDE.md, missing pharmaceutical
requirements are flagged here rather than invented.

## Open — need client/user input

### D-001 · OpenClaw gateway contract — **RESOLVED, and the PRD is wrong**
Verified live against OpenClaw 2026.9.1 (ad6fe23) on 4 Sep 2026.

**PRD §28.3 is incorrect.** There is no `POST /v1/responses`:

| PRD §28.3 says | Reality |
|---|---|
| `POST http://127.0.0.1:18789/v1/responses` | **404.** No such route. |
| `/openai/v1/responses` | 404 on POST; GET returns the Control UI SPA fallback. |
| HTTP + `x-openclaw-agent-id` header | The gateway is a **WebSocket JSON-RPC** service. |

**The verified contract** is RPC method `agent` over the WebSocket gateway:

```
params: {
  agentId:        "pharma-specialist",   # the logical route (s7.3)
  message:        "<prompt>",
  idempotencyKey: "<unique>"             # REQUIRED by the gateway
}
```

```
result: {
  runId, status: "ok", summary: "completed",
  result: {
    payloads: [{ text, mediaUrl }],
    meta: {
      durationMs,
      finalAssistantVisibleText,
      systemPromptReport: { provider, model, sessionKey, sessionId, workspaceDir, ... }
    }
  }
}
```

*Consequences for the build:*
- `openclaw_client.py` must speak WebSocket JSON-RPC, not HTTP POST. `build_request`
  and `build_headers` encode the wrong contract and must be replaced in Phase 3.
- `OPENCLAW_GATEWAY_URL` should be a `ws://` URL, not `http://`.
- The gateway **requires** an idempotency key on every turn — this aligns with §31.2
  and should carry the run/task identity rather than a random value.
- `meta.systemPromptReport` returns `provider` and `model`, which satisfies §20's
  requirement that a run record which route produced it.
- No token counters were observed in the response. §22 telemetry is therefore
  best-effort, as the PRD itself allows ("where provider counters are available").

*Working invocation for reference:*
`openclaw gateway call agent --params '{...}' --expect-final --timeout 120000 --json`

### D-011 · Route configuration — resolved and verified
Both routes are live end-to-end as of 4 Sep 2026:

| Route | OpenClaw agent | Model | Runtime | Verified |
|---|---|---|---|---|
| Executive | `pharma-executive` | `openai/gpt-5.4` | codex harness | `ROUTE OK` |
| Specialist | `pharma-specialist` | `anthropic/claude-sonnet-5` | `claude-cli` | `RPC OK` |

Three fixes were needed beyond the documented setup:
1. The `codex` harness plugin is **not installed by onboarding**. Without it every
   OpenAI turn fails with `owner-plugin-not-activatable`. Installed from
   `clawhub:@openclaw/codex` v2026.9.1.
2. Anthropic auth must be selected interactively; `--method anthropic-cli` is
   rejected as an unknown method id. The interactive picker's "Claude CLI" option
   sets `agentRuntime: claude-cli` on every Anthropic model.
3. `capability model run --local` fails for Claude with "no API key found" because
   the CLI runtime is an *agent* harness. Turns must go through the gateway.

*Still outstanding:* Slack is `enabled, not configured, stopped`; no command owner
is set (blocks §16 approvals); the Slack-routed agent lacks the `message` tool.

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


### D-012 · Gateway transport is the OpenClaw CLI, not raw HTTP or WebSocket
The gateway opens with a `connect.challenge` nonce handshake over WebSocket. The
response format is undocumented, and reimplementing it would break on any gateway
upgrade.

*Decision:* `app/providers/openclaw_client.py` shells out to
`openclaw gateway call <method> --params <json> --json`. This is the supported
entry point, handles the handshake, and keeps every OpenClaw specific behind the
one adapter as §41.1 requires.

*Trade-offs accepted:* a process spawn per turn (negligible against multi-minute
agent turns in a single-user system); no token streaming; the CLI must be
locatable. The last is mitigated by `OPENCLAW_CLI_PATH` plus fallback discovery,
because a worker started by launchd or systemd does not inherit the user's PATH.

*Revisit if:* OpenClaw documents the WebSocket protocol, or streaming progress
becomes a requirement for §17 Slack updates.


### D-013 · Specialist tools reach OpenClaw as skills that call Python
*Decision (user, 4 Sep 2026):* implement the §19 tools as **OpenClaw skills**
rather than an MCP server.

*Refinement:* a skill is a `SKILL.md` plus a script. The script will be a thin
wrapper that shells into this codebase (`uv run python -m app.tools.<name>`), so
the implementation stays in Python and §19's "tool implementations belong in
Python" holds. What OpenClaw sees is the skill; what executes is our code. This
avoids the objection to the pure-skill route, where tool logic would have been
duplicated outside Python.

*Consequences:*
- Per-profile binding (§19 "explicitly bound to profiles") must be enforced by the
  Python wrapper checking the calling agent against the profile allowlist. OpenClaw
  scopes skills per agent, but the authoritative check stays in our code so the
  registry remains the single source of truth.
- Evidence written by a tool lands in PostgreSQL directly, so §15 provenance is
  unaffected.
- No MCP server to run or supervise, which suits the single-host topology in §36.

*Progress:* the 16 **specialist** skills are generated and committed. The **tool**
skill wrappers are not yet built.

*Still open:* how a tool-skill invocation identifies the run and task it belongs to,
so evidence and artifacts attach to the right records. The specialist runner already
threads `run_id:task_id` as the gateway idempotency key, so the same identity is
available to pass through — likely as an environment variable set on the skill
script, since the specialist itself must not be able to choose which run it writes to.


### D-014 · Slack delivery posts directly, rather than through an agent turn
OpenClaw's only outbound path is an `agent` RPC with `deliver: true`, which means a
language model generates the delivered text.

*Problem:* for a regulated deliverable that is the wrong shape. §18 requires
deterministic assembly precisely so a model cannot reformat or paraphrase the
output, and asking one to reproduce a long Executive Decision Brief verbatim is
both unreliable and an extra model call per post.

*Decision:* `app/slack/notifier.py` posts the exact rendered Block Kit through the
Slack Web API, using the bot token OpenClaw already holds (env var first, then
OpenClaw's config, including SecretRef indirection). OpenClaw remains the inbound
gateway per §7.1; only outbound fidelity is ours.

*Consequences:* delivery is exact, cheap and fast. Delivery failure is logged and
returns `None` rather than raising — a Slack outage must not fail an otherwise
complete pharmaceutical run, and the result is in PostgreSQL either way.

*Revisit if:* OpenClaw exposes a non-agent send method on the gateway.
