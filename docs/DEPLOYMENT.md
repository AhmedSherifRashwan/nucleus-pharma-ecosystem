# Moving Nucleus to another Mac

Four things make up a working system. Three of them are already portable; the
fourth cannot be, and that shapes the whole procedure.

| Part | How it travels |
|---|---|
| Code, agent definitions, prompts, skills | git |
| Gateway configuration | `openclaw/setup/openclaw.template.json`, rendered by `bootstrap.py` |
| Data — runs, evidence, artifacts, documents | `scripts/backup.py` |
| Model subscriptions | **cannot be copied.** Sign in on the new machine |

Subscription auth belongs to a person on a device. That is the reason this runs on
a Mac rather than a server, and the reason two steps below need a browser.

## On the machine you are leaving

```bash
cd ~/Nucleus
uv run python scripts/backup.py create --to ~/Backups
```

One archive holds the database dump, every artifact and ingested document, and a
manifest recording the git commit, row counts and a checksum. Copy it and the
repository to the new machine.

The archive contains no secrets. `.env` does — carry it separately and
deliberately, or rebuild it from `.env.example`.

## On the new machine

```bash
# 1. Install OpenClaw first: https://docs.openclaw.ai
#    It is the one dependency the installer cannot do for you.

# 2. Clone OUTSIDE ~/Documents, ~/Desktop and ~/Downloads. macOS denies a
#    launchd-spawned process access to those, so the worker cannot run as a
#    service from them (D-028).
git clone <repo> ~/Nucleus && cd ~/Nucleus

# 3. System dependencies: Homebrew, Python 3.12, uv, PostgreSQL 17, Node.
./scripts/install-dependencies.sh

# 4. Sign in to both subscriptions. A browser each, once.
claude                                    # Claude, for the specialists
openclaw onboard                          # ChatGPT / Codex, for planning

# 5. Secrets and identifiers.
cp .env.example .env                      # set OPENCLAW_GATEWAY_TOKEN at minimum

# 6. Restore the data BEFORE bootstrap, so migrations see the real schema.
uv run python scripts/backup.py restore --from ~/Backups/nucleus-<stamp>.tar.gz

# 7. Everything else: databases, migrations, agent registry, gateway config,
#    skills, and the worker as a LaunchAgent.
uv run python scripts/bootstrap.py
uv run python scripts/health_check.py
```

Then `openclaw dashboard`, select the **nucleus** agent, and ask it something.

## Check the model before the first run

A new ChatGPT account may not support the model the executive agent is pinned to.
This cost an afternoon once: OpenClaw reported it as a confusing session-lock error
rather than "unsupported model".

```bash
openclaw models list                      # what this account can actually use
openclaw agent --agent pharma-executive --message "Reply with: ok"
```

If that fails, set a supported model on `pharma-executive` in
`~/.openclaw/openclaw.json` and restart the gateway.

## What is not carried, and why

**The virtualenv.** `uv`'s console scripts embed the absolute path they were built
with, so a copied `.venv` points at the old machine. `bootstrap.py` rebuilds it.

**OpenClaw agent state** (`~/.openclaw/agents/*/codex-home`). Conversation history
and vendored runtimes belong to the machine that made them. Everything durable is
in PostgreSQL.

**Secrets.** Never in the archive. A backup that carries credentials is a
credential you have lost track of.

## Restoring onto a machine that already has data

`restore` refuses, because merging two histories into one audit trail would make
the record describe work that never happened. `--force` overwrites deliberately.

## Database ownership

The databases must be owned by the role in `DATABASE_URL`. PostgreSQL 15 removed
the public schema's default write permission, so a database created by one role and
used by another fails a restore with *permission denied for schema public*.
`bootstrap.py` creates them with `-O` and creates the role if it is missing.
