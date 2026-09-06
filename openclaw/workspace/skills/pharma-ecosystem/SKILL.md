---
name: pharma-ecosystem
description: Launch a Neucleus pharmaceutical analysis run for generic oral softgel development — portfolio, regulatory, QTPP, QbD, formulation, analytical, stability, process, validation, QC or QA work. Use when the user asks for an assessment, investigation, report or recommendation in that domain.
metadata:
  short-description: Launch a Neucleus specialist analysis run
---

# Neucleus — Pharma Ecosystem Launcher

Turn a request into a durable, tracked run executed by the Neucleus orchestrator.

**Your only job is to launch the run and acknowledge it.** Do not attempt the
pharmaceutical analysis yourself. Sixteen specialist profiles, an executive
planner, a challenge stage and a quality gate run outside this conversation and
produce the answer. Anything you write here is a placeholder that will be
contradicted by the real result.

## When to use this

Any substantive request about generic oral soft gelatin capsule development:
portfolio and commercial feasibility, regulatory pathway, QTPP and CQAs, QbD and
control strategy, formulation (shell, fill, excipients), analytical methods,
stability and shelf life, process development and scale-up, validation, quality
control, or quality assurance.

Do **not** use it for small talk, questions about Neucleus itself, or requests
outside pharmaceutical development.

## Three ways a request arrives

**Standard** — the ordinary case. The planner picks the specialists the question
needs, within the run budget. Pass the request through and let it decide.

**Whole ecosystem** — the user says something like "use all 16 agents", "use the
whole ecosystem" or "every specialist". Pass the request through verbatim; the
enqueue script reads that instruction itself and sets the mode. Tell the user it
will take considerably longer than a normal run.

**One named specialist** — the message starts with a slash command naming an agent,
either by id (`/A07`) or by name (`/shell-formulation`), followed by the task:

> `/shell-formulation assess cross-linking risk for a PEG 400 fill`

Pass the whole message through verbatim, including the leading slash command. The
script parses it, and skips planning entirely — that specialist does the task alone.

The addressable specialists:

| Command | Specialist |
|---|---|
| `/portfolio-strategy` or `/A01` | Portfolio Strategy |
| `/regulatory-intelligence` or `/A02` | Regulatory Intelligence |
| `/qtpp` or `/A03` | QTPP |
| `/qbd` or `/A04` | Quality by Design |
| `/product-development` or `/A05` | Product Development |
| `/excipient-selection` or `/A06` | Excipient Selection |
| `/shell-formulation` or `/A07` | Softgel Shell Formulation |
| `/fill-formulation` or `/A08` | Fill Formulation |
| `/analytical-development` or `/A09` | Analytical Development |
| `/stability` or `/A10` | Stability Studies |
| `/process-development` or `/A11` | Process Development |
| `/validation` or `/A12` | Validation |
| `/quality-control` or `/A13` | Quality Control |
| `/quality-assurance` or `/A14` | Quality Assurance |

If the user names a specialist that is not in this table, say so and list the
nearest ones rather than picking one for them.

## How to launch

Run the enqueue script with the request and its Slack context:

```bash
cd "{{NEUCLEUS_ROOT}}" && uv run python scripts/enqueue_run.py \
  --request "<the user's request verbatim>" \
  --channel "<slack channel id>" \
  --thread "<slack thread ts>" \
  --user "<slack user id>" \
  --event-id "<slack event id>"
```

Pass the request **verbatim**. Do not summarise, expand, correct or interpret it —
the orchestrator needs the user's own words, and rewriting them changes what gets
planned.

`--event-id` is the deduplication key. Slack retries events; without it the same
message can start two runs.

**Working from a document set.** Specialists can only search documents that were
ingested against a project. A new Slack thread starts a new project with nothing in
it, so when the user names one — "use the Midostaurin project", "against the
Candidate X documents" — pass it through:

```bash
  --project "Midostaurin Generic Programme"
```

If the script says there is no project by that name, tell the user exactly that and
list nothing you have not verified. Do not silently drop the flag: a run against the
wrong project searches an empty document set and the specialists will report that
they found nothing.

The script prints a JSON object with `run_ref`, `status`, `mode` and `agent`. Report
the `run_ref` back to the user and stop.

`mode` tells you what was understood — `standard`, `full` or `direct`. Say which one
in your acknowledgement, so a user who typed "use all 16 agents" can see that it was
read, and a user who did not can see that it was not.

## When there is no Slack thread

In the Control UI, a terminal, or anywhere without a Slack conversation, omit
`--channel`, `--thread`, `--user` and `--event-id`. The run works exactly the same
— but nothing posts itself back to you, because there is no thread to post to. You
report it instead.

After launching, poll:

```bash
cd "{{NEUCLEUS_ROOT}}" && uv run python scripts/run_status.py \
  --run-ref RUN-2026-0042 --wait 600
```

`--wait` returns as soon as the run finishes **or** needs a decision from the user,
whichever comes first, and at the latest after the seconds you give it. Report what
comes back — the stage, which specialists have returned, and anything blocking —
then poll again. A run takes tens of minutes, so tell the user that and keep going
rather than waiting in silence.

When the status shows `awaiting_decision`, put the question to the user in your own
words, wait for their answer, and only then run the command in its `how_to_answer`
field. Never answer a gate on the user's behalf.

When it shows `finished`, report the outcome and give the paths under `reports`.
Those are files on this machine; say where they are rather than trying to attach
them.

## What to tell the user

Acknowledge immediately and briefly. A run takes minutes, not seconds, and
progress arrives in this thread on its own.

> Run RUN-2026-0042 started. I am planning the specialist workstreams and will
> post progress here.

For a whole-ecosystem run, say so and warn about the time:

> Run RUN-2026-0042 started across the full ecosystem — every specialist gets a
> workstream. This takes substantially longer than a normal run; progress will
> appear here.

For a direct request, name the specialist:

> Run RUN-2026-0042 started. Sent to the Softgel Shell Formulation Expert.

If the script reports `deduplicated: true`, the request was already running —
give the existing `run_ref` rather than starting another.

## What not to do

- Do not answer the pharmaceutical question yourself, even partially.
- Do not invent a run reference. Use what the script returns.
- Do not read or echo database credentials, gateway tokens or any environment value.
- Do not retry a failed launch more than once; report the error text instead.
