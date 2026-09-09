# Nucleus

A multi-agent AI ecosystem for generic oral soft gelatin capsule development.

Sixteen pharmaceutical specialist profiles work under a deterministic orchestrator.
A question arrives, an executive planner decides which specialists it needs and how
their work depends on each other, you approve the plan, specialists run in parallel
groups, an adversarial reviewer attacks the result, conflicts are detected rather
than averaged, and the surviving position is synthesised, gated and rendered into
four documents.

The system is built to fail closed. Missing access, unresolved evidence, a material
contradiction or a malformed model response produce **REVIEW REQUIRED** — never a
quietly confident answer.

---

## Setting up on a new MacBook

Around thirty minutes, most of it downloads. Two steps need a browser and cannot be
automated: subscription auth belongs to a person on a device, which is why this runs
on a Mac rather than a server.

### Offline, on a USB stick

No network on the new machine? One command writes everything needed:

```bash
uv run python scripts/usb_kit.py create --to /Volumes/<STICK>
```

That is the repository with its full history as a git bundle, the data as a verified
archive, and a `SETUP.txt` with the steps. Add `--include-env` to carry the `.env`
too — the stick then holds your database password and gateway token, so keep track
of it.

**Copying `~/Nucleus` to a stick does not move this system.** The database lives in
PostgreSQL's own data directory, not in the project; the gateway's agents and
workspaces live under `~/.openclaw`; and `.venv` holds absolute paths that mean
nothing on another machine. A folder copy preserves the source — which git already
preserves, better — and silently leaves the database behind.

### Before you start, on the old machine

```bash
cd ~/Nucleus
git push                                          # the remote is only as current as your last push
uv run python scripts/backup.py create --to ~/Backups
```

The archive holds the database, every artifact and ingested document, and a manifest
with the git commit, row counts and a checksum. Copy that one file to the new
machine. It contains no secrets.

### Step 1 — Install OpenClaw

From <https://docs.openclaw.ai>. This is the only dependency the installer cannot
handle: no non-interactive install command could be verified from the artefacts it
leaves behind, and guessing one is worse than saying so.

Verify:

```bash
openclaw --version        # or ~/.openclaw/bin/openclaw --version
```

If it is not on PATH, add this to `~/.zshrc`:

```bash
export PATH="$HOME/.openclaw/bin:$PATH"
```

### Step 2 — Clone the repository

```bash
git clone https://github.com/AhmedSherifRashwan/nucleus-pharma-ecosystem.git ~/Nucleus
cd ~/Nucleus
```

**The location matters.** Do not clone into `~/Documents`, `~/Desktop` or
`~/Downloads`. macOS denies a launchd-spawned process access to those folders
whatever the file permissions say, so the worker cannot run as a service from them —
it fails with `EX_CONFIG` and runs queue silently with nothing executing. `~/Nucleus`
or `~/Developer/Nucleus` are both fine.

### Step 3 — System dependencies

```bash
./scripts/install-dependencies.sh --check      # see what it would do
./scripts/install-dependencies.sh              # do it
```

Installs Homebrew, Python 3.12, uv, PostgreSQL 17 with its service started, Node,
and offers LibreOffice (optional, only for PDF export). It is idempotent: anything
already present is reported and left alone.

It will tell you if `psql` is not on PATH. PostgreSQL is keg-only under Homebrew:

```bash
export PATH="/opt/homebrew/opt/postgresql@17/bin:$PATH"
```

### Step 4 — Sign in to both subscriptions

```bash
claude                    # Claude — runs the sixteen specialists
openclaw onboard          # ChatGPT / Codex — runs planning, challenge and synthesis
```

Each opens a browser once. The two vendors are deliberate: the executive route
plans and challenges, the specialist route does the domain work, so one model does
not mark its own homework.

### Step 5 — Configuration

```bash
cp .env.example .env
```

Then edit `.env`:

| Setting | Value | Why |
|---|---|---|
| `OPENCLAW_GATEWAY_TOKEN` | from `~/.openclaw/openclaw.json` after step 1 | the gateway is privileged local access |
| `DEFAULT_MAX_SPECIALISTS` | `8` | the example ships the PRD's 6 |
| `REPORT_COMPOSER_ROUTE` | `specialist` | keeps report writing off the planner's quota |

`OPENCLAW_TIMEOUT_SECONDS=900` and `DEFAULT_MAX_RUNTIME_SECONDS=3600` are already
correct in the example — report-length specialist work overran the shipped 300s and
1200s.

You do not need to carry the old `.env`. A fresh one works: the database role is
created locally and the gateway token is new on each machine.

### Step 6 — Restore your data

```bash
uv run python scripts/backup.py restore --from ~/Backups/nucleus-<stamp>.tar.gz
```

**Before bootstrap, not after** — migrations should meet the real schema.

Restore verifies the dump against its checksum before touching anything, and checks
the restored row counts against the manifest afterwards. It refuses to restore over
a database that already holds data, because merging two histories into one audit
trail would make the record describe work that never happened.

Skip this step entirely if you are starting fresh.

### Step 7 — Bootstrap

```bash
uv run python scripts/bootstrap.py --check     # what it would change
uv run python scripts/bootstrap.py             # do it
```

Creates the databases owned by the right role, runs migrations, seeds the sixteen
agent profiles, writes the gateway configuration from
`openclaw/setup/openclaw.template.json`, installs the skills into each agent's
workspace, and installs the worker as a LaunchAgent that starts on boot and restarts
on crash.

Idempotent — rerun it any time, particularly after `git pull`.

### Step 8 — Verify

```bash
uv run python scripts/health_check.py
```

Expect environment, budgets, database, schemas, gateway, routes and profiles all
`ok`, and `16 enabled, 2 framework-scoped, 0 provisional`.

Then check the worker is actually running:

```bash
launchctl print gui/$UID/ai.nucleus.worker | grep -E "state|pid"
```

`state = running` with a pid. If it shows `last exit code = 78`, the checkout is in
a folder macOS denies to background services — see step 2.

### Step 9 — Use it

```bash
openclaw dashboard
```

Select the **nucleus** agent and ask a question. You never need to say "use the
pharma ecosystem"; that is all this agent does.

---

## Check the model before your first run

A new ChatGPT account may not support the model `pharma-executive` is pinned to, and
OpenClaw reports it as a session-lock error rather than "unsupported model". This is
the single most confusing failure in the stack.

```bash
openclaw models list
openclaw agent --agent pharma-executive --message "Reply with: ok"
```

If that fails, set a supported model on `pharma-executive` in
`~/.openclaw/openclaw.json` and restart the gateway with `openclaw gateway restart`.

---

## What you get from a run

Four documents, every time:

| Document | For |
|---|---|
| **Technical Report** | the full analysis, evidence register and precise references |
| **Executive Decision Brief** | the decision-maker who will not open the report |
| **Technical Summary and Action List** | the lead who has to act this week |
| **Agent Trail and Audit Record** | what each specialist reported, what the review challenged, what was corrected |

The agent trail is rendered from PostgreSQL with no model call, so it cannot be
paraphrased and it still ships when composition fails — which is exactly the run you
need it for.

---

## Day to day

```bash
# Ingest the client's documents — the biggest lever on report quality
uv run python scripts/ingest_documents.py --project "Midostaurin Generic Programme" ~/sources/

# Bind a run to that project so its documents are searchable
uv run python scripts/enqueue_run.py --request "..." --project "Midostaurin Generic Programme"

# What is a run doing?
uv run python scripts/run_status.py --latest --wait 600

# Re-issue a report without re-running the analysis
uv run python scripts/regenerate_report.py --run-ref RUN-2026-0011

# Back up
uv run python scripts/backup.py create --to ~/Backups
```

Until documents are ingested, `project_search` returns nothing and specialists
reason from general principles — which they then label honestly as assumptions.

---

## Addressing one specialist

`/qtpp`, `/shell_formulation`, `/stability` and eleven others send a single task to
that specialist with no planning stage. Ask the ecosystem for the full list, or see
`openclaw/workspace/skills/commands/`.

Say "use all 16 agents" to engage the whole ecosystem. Detection is literal — a
broad question is never silently escalated into an hour of work.

---

## When something breaks

| Symptom | Cause |
|---|---|
| Runs queue and nothing executes | worker not running; `launchctl print gui/$UID/ai.nucleus.worker` |
| Worker exits with `EX_CONFIG` | checkout is in `~/Documents`, `~/Desktop` or `~/Downloads` |
| "Codex session became active in another runner" | usually an unsupported model, not a lock. Check `openclaw models list` |
| `psql: command not found` | PostgreSQL is keg-only; add its bin directory to PATH |
| `permission denied for schema public` | the database is not owned by the role in `DATABASE_URL` |
| alembic points at an old path after moving | `rm -rf .venv && uv sync` — uv embeds absolute paths in console scripts |
| Report has one heading per agent | the composer failed and fell back; check `data/worker.log` |

Logs: `data/worker.log` for the worker, `~/Library/Logs/openclaw/gateway.log` for
the gateway.

---

## Development

```bash
uv run ruff check . && uv run mypy app && uv run pytest
```

All three must pass before a phase is complete. Read `CLAUDE.md` for the
non-negotiable rules, `decisions.md` for why the architecture is the way it is —
every deviation from the PRD is recorded there — and `progress.md` for what each
phase delivered.

`docs/DEPLOYMENT.md` covers migration in more depth, including what deliberately
does not travel between machines.
