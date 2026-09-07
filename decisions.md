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

### D-005 · A15 / A16 remain provisional — *superseded by D-027*
No dedicated client SDS exists for GMP Compliance or Regulatory Submission (§11).
They stayed disabled until the client instructed otherwise on 6 September 2026.

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


### D-015 · The report is assembled deterministically, and never overwritten
§18 makes the DOCX the deliverable a client actually keeps. Two ways to produce it
were available: ask the executive model to write the document, or assemble it from
the records the run already persisted.

*Decision:* `app/reports/builder.py` assembles it from `ExecutiveDecisionBrief`,
`SpecialistResult`, `EvidenceItem` and `Risk` rows. No model call is involved. Every
§18.1 section is rendered every time; a section with nothing in it prints
"None identified." rather than disappearing, because an absent section reads as an
oversight while an explicit statement can be relied on.

Versioning follows §18.3 literally: `ReportBuilder.build()` raises rather than
overwrite an existing file, and each run gets its own folder
(`<reports>/<RUN-REF>_<uuid8>/`) so two runs can never contend for a filename.
The artifact row records the sha256, template version and generator version, so any
delivered file can be traced back to the layout and code that produced it.

*Report QA (§32.3):* every `E-n` cited in the brief is checked against the evidence
table before the file is written. A dangling ID is surfaced as a warning rather than
printed as provenance the reader cannot check.

*Consequences:* the report cannot be paraphrased, reordered or quietly abridged
between runs, and it costs no tokens. It also means the wording is only as good as
the brief — the model's judgement lives in the brief, and the document is a faithful
container for it.

*PDF:* conversion goes through LibreOffice when it is installed
(`REPORT_PDF_ENABLED=true`), and returns nothing when it is not. The DOCX remains
authoritative; a missing converter must not fail a pharmaceutical run.

*Delivery:* the file is uploaded into the Slack thread through Slack's external
upload flow (`files:write`, already granted). A path in a message is useless to a
client who is not sitting at this machine.


### D-016 · A gate is raised once per pause, not once per node execution
LangGraph resumes an interrupted node by re-running its body from the top. The
first live run showed the consequence: resuming the plan gate created a *second*
approval row, posted a *second* plan message to Slack, and left the approval the
user had actually clicked pending forever.

*Decision:* `GovernanceRepository.request_approval` returns `(approval, is_new)`
and reuses an open approval for the same run and gate. The node posts the Slack
gate and writes the audit event only when the approval is new. A gate raised again
after the first was decided is a genuinely new question, so it does create a new row.

*Consequences:* the button the user is looking at stays the one that decides the run,
and `scripts/repost_gate.py` re-posts the oldest pending gate rather than a duplicate.


### D-017 · The gateway timeout is a deployment number, not a PRD number
The first live report run failed two of five specialists with
`gateway timeout after 300000ms`. The request asked each specialist for a table of
contents and full report content with checked references — 3-10 minutes of work
against a 5-minute ceiling.

*Decision:* the shipped default rises to 900s (`openclaw_timeout_seconds`), and this
deployment sets `DEFAULT_MAX_RUNTIME_SECONDS=3600` in `.env`. The §22 budget default
in code stays at 1200s, because that number is the PRD's; a deployment overriding it
is a local decision and the test now reads the code default rather than the `.env`.

The Slack ETA constants were recalibrated at the same time (260s per specialist
group, up from 110s). An estimate that keeps expiring reads as a stall, which is
worse than a generous one.


### D-018 · The report is composed from the work, not from summaries of it
The first live report exposed the defect plainly. Synthesis received each
specialist's `executive_summary` and a list of artifact references; nothing ever
opened the artifacts. A 44 KB QTPP revision and a 28 KB CQA/CMA/CPP register sat in
PostgreSQL while the delivered document rendered one heading per `SpecialistResult`
in execution order — the same agent twice, the superseded draft printed beside its
own correction. The document's structure *was* the execution, which is why it read
as a transcript rather than a report.

*Decision:* a composition stage between synthesis and rendering.

`context.py` decides what may be seen: current work products in full, superseded
ones held back for the crosswalk and the audit appendix, evidence with provenance,
conflicts, risks and the review record. `composer.py` writes in stages — a dynamic
outline from the request and the available material, then each section against its
own sources, then closing matter, then references. `gate.py` checks the result in
code before anything renders. `technical_builder.py` draws it.

*Why staged rather than one call:* a single call asked for a 30-page report
truncates; a section given only its own sources stays anchored to real work; and a
staged composition can be inspected between stages, which one opaque call cannot.

*What did not change:* the specialists, their prompts, the challenge stage, conflict
detection, follow-ups and the quality gate are untouched. The problem was never the
reasoning. Independent specialists disagreeing, and the system detecting and
resolving it, is the feature — it now reaches the reader as a critical review and a
revision crosswalk instead of as two contradictory sections.

*Fallback:* composition can fail. When it does, the deterministic brief-only
document still ships and says why. A run that produced real specialist work must
never end with nothing in the client's hands.


### D-019 · A superseded draft stays in the record and leaves the report
`Artifact.version` existed but only reports used it. Work products had no lifecycle,
so ART-7 (QTPP Rev 0) and ART-9 (QTPP Rev 1) were indistinguishable to every
consumer, and both reached the reader as current.

*Decision:* artifacts carry `status`, `supersedes_ref` and `superseded_by_ref`.
`save_artifact --supersedes ART-7` records the replacement; the row is never
deleted. The report context excludes superseded drafts from the composer's material
and offers them to the revision crosswalk and the audit appendix instead. The
pre-release gate fails the report if a superseded reference appears in the body.

*Consequence:* the database keeps the complete forensic history and the client-facing
report carries the current controlled interpretation. Those are different documents
serving different readers, and conflating them is what produced a report containing
both a wrong regulatory identification and its own correction.


### D-020 · A reference states what it is, or states that nobody checked
The evidence register recorded a title, a reliability level and sometimes a URL. The
report printed the title. "FDA guidance" cannot be checked by the person who has to
defend it.

*Decision:* `Reference` carries organisation, title, document identifier, version,
date, authority, locator, URL, retrieval date, evidence id and a verification status.
The composer structures these from what was actually recorded and is instructed to
leave a field null rather than guess. `primary_source_verified` requires an
authoritative source with evidence it was retrieved; anything less is
`partially_verified`, `not_retrieved` or `unverified`, and the document prints that
status beside the citation.

*Consequence:* a reader can tell a verified primary source from a plausible-looking
citation nobody opened. The gate reports imprecise references without blocking —
the honest label is the deliverable, not the absence of the reference.


### D-021 · Three run modes, read from the user's own words
A request now engages the ecosystem in one of three ways.

`standard` — the planner selects the specialists the question needs, within the
run budget (raised to 8 on this deployment). The default.

`full` — every enabled profile gets a workstream. Triggered only by an explicit
instruction ("use all 16 agents", "the whole ecosystem", "every specialist").
Inferring it from a broad question would spend an hour of someone's afternoon on
a guess, so detection is literal and the planner is told plainly not to narrow the
team.

`direct` — `/A07` or `/shell-formulation` followed by a task sends that one
specialist to work. The plan is built in code rather than asked of the planner:
there is nothing left to decide, and asking anyway risks the planner answering
with a different team than the user named. The plan gate is skipped for the same
reason — it would ask the user to approve what they just requested. The AMBER/RED
decision gate after synthesis is untouched.

*Consequences:* the routing prefix never reaches the specialist, and an unknown
command is treated as an ordinary request rather than guessed at. Slash commands
are registered in the Slack manifest, one per enabled specialist plus `/neucleus`;
the same words typed in a mention work without them.


### D-022 · The time estimate is measured, not assumed
The first estimator used fixed constants and told the user "8-10 minutes" on a run
that took an hour. Constants cannot know that this deployment's specialists take
four minutes on report-length work.

*Decision:* `app/orchestration/timing.py` reads `duration_ms` back from the
specialist results — the run's own turns first, this deployment's recent history
next, a generous default only if nothing has ever been measured. A parallel group
costs about one turn plus a margin for its slowest member, not one turn per
specialist. Stages already completed stop being counted.

The range widens when the estimate is a guess and narrows once it is measured, so
its width carries information. An estimate that keeps expiring reads as a stall and
destroys trust in the number, so the default errs long.


### D-023 · Specialist commands are skills, not Slack app commands
`/qtpp <task>` could be registered as a Slack slash command in the app manifest, or
served by OpenClaw's own command path. The manifest route needs 15 command entries,
the `commands` scope, and a reinstall of the Slack app — and the Slack app is where
this deployment has lost the most time to token and scope problems.

*Decision:* one generated skill per enabled specialist, installed on the
Slack-facing agent, with `channels.slack.commands.nativeSkills: true`. OpenClaw
exposes a user-invocable skill as a slash command over the connection it already
holds. `/skill qtpp <task>` works as the documented fallback whether or not native
registration is on.

The skills are generated from the registry by `scripts/generate_command_skills.py`
and drift-tested, so a command can never point at the wrong specialist or outlive
the profile behind it. A provisional profile gets no command, because A15 and A16
have no client SDS behind them.

*Consequences:* no Slack app change, nothing to reinstall, and one routing
mechanism rather than two to keep in step. The manifests keep no slash commands, so
nobody reinstalls the app expecting them to matter.


### D-024 · Specialists could not retrieve anything, because nothing was ingested
`source_documents: 0 | chunks: 0`. The `project_search` tool worked, the specialists
called it, and it returned nothing on every run — so they reasoned from general
principles and labelled the result honestly as assumptions. The report that came
back was dominated by "evidence unavailable", and the product-specific science that
made an earlier run useful was simply not available to them.

*Decision:* `app/ingest/` plus `scripts/ingest_documents.py`. PDF, DOCX, Markdown,
text, CSV, JSON and HTML are extracted and chunked with a locator on every chunk —
a page number, a heading, a table number — because an excerpt a reader cannot find
in the original is not provenance.

Re-ingesting a file supersedes the old version rather than adding a second copy:
two versions of one guidance is how a specialist ends up citing the withdrawn one.
`--keep-old-versions` makes retaining both a deliberate choice.

*One defect found by its own tests:* short blocks were being dropped as too small
to be useful, which discarded exactly the lines that matter most — "Reference NDA:
207997", an assay result, a revision date. A short chunk is kept; the size floor
now governs only where a long block is split.


### D-025 · Uncertainty is stated once, structurally, not in every sentence
The composed report was accurate and hard to read: "provisional", "not
independently verified", "screening-level" and "unresolved" recurred so often that
the science between them was lost. Repeating a caveat does not make a document more
honest — a reader who skims past the fortieth has been warned less effectively than
one who read a single clear line.

*Decision:* a section carries its standing as structured fields — `status`,
`evidence_confidence`, `open_gap_refs` — and the renderer prints them once under
the heading:

    Section status: PROVISIONAL · Evidence confidence: Medium · Unresolved gaps: G-02, G-07

Gaps get stable ids (G-01, G-02...) assigned in code, so a section points at one
instead of restating it, and a reference to a gap that does not exist is dropped
rather than sending the reader looking for it.

The composer is told to state the standing there and then write plainly, qualifying
inline only where a specific number is weaker than the section around it. The
safeguards are unchanged — the gate still fails a report whose evidence does not
resolve, and a provisional target is still labelled provisional. What changed is
that it is said where a reader will read it.


### D-026 · A dedicated front-door agent, so nobody has to ask for the ecosystem
Talking to the general-purpose `main` agent meant saying "use the pharma ecosystem"
every time. A client should not have to know the name of the thing they are using.

*Decision:* a `neucleus` agent whose only purpose is the ecosystem. Its standing
instructions live in the repository (`openclaw/workspace/agent-neucleus/`) and are
installed with the skills, so what it will and will not do is reviewed with the code
rather than edited in place on the machine.

The instruction that matters most is what it must **not** do: never answer the
pharmaceutical question itself, not even as a summary while the run proceeds.
Anything it writes from its own knowledge will be contradicted by specialists who
cite sources, and a plausible answer with no evidence behind it is the specific
failure the ecosystem exists to prevent.

Its generated `BOOTSTRAP.md` was removed: a front door's identity is defined, not
discovered.

*Runs outside Slack:* `scripts/run_status.py` reports a run's state to any surface,
and a run with no external owner is decided by an approver or by the local
`operator` identity — which is refused on a run that does belong to someone.


### D-027 · A15 and A16 enabled from the client's own role definitions
*Decision (user, 6 September 2026):* add the two remaining specialists using what the
client supplied.

They supplied more than the earlier note recorded. `AI_Ecosystem_PRD_v2_Client_Agent_Roles`
defines both roles — operating persona, mission, responsibilities, inputs, outputs,
quality controls, escalation behaviour and hand-offs — derived from
`AI_Framework_Generic_Softgel_Agents`. What was missing was a *dedicated* specification
for each role, which is a difference in depth, not in authorship. Both profiles are now
built from the client's own wording, and both are active.

*The weaker provenance travels with them.* `source_basis` distinguishes
`dedicated_sds` from `framework_definition`, and it reaches every place the
distinction matters: the planner's catalog sees `scope_basis` before it assigns work,
each generated skill states that its role is defined at framework level and must say so
rather than infer beyond it, and a report naming either agent prints a
**Framework-level scope** line on its document control table.

*Their client's own limits are the profiles' limits.* The role table forbids autonomous
compliance sign-off, batch release and quality approval for A15, and filing, health-
authority communication and treating a dossier as approved for A16. Those are
`forbidden_actions`, and `scope_beyond_framework_definition` escalates rather than
guessing.

*The safeguard was not removed, only emptied.* Provisional profiles are still disabled by
default, the plan validator still refuses a disabled agent, and both behaviours are still
tested — against a constructed profile now that no real one is provisional.


### D-028 · The checkout lives outside ~/Documents
The worker was never a service — a `nohup` process started by hand, so a reboot left
runs enqueued with nothing executing, silently. Making it a LaunchAgent failed with
`EX_CONFIG`, and a probe agent running plain `ls` in the project directory returned
`Operation not permitted`: macOS TCC denies a launchd-spawned process access to
`~/Documents`, `~/Desktop` and `~/Downloads` whatever the file permissions say.

*Decision:* the checkout is `~/Neucleus`. `bootstrap.py` detects a TCC-protected
location and refuses to install a service that cannot start, saying what to do
instead — a service that fails silently on every boot is worse than none.

*Also fixed on the way:* recreating the virtualenv after the move, because uv's
console scripts embed the absolute path they were created with, and `uv sync` alone
does not rewrite them.
