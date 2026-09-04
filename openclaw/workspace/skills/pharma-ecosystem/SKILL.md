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

The script prints a JSON object with `run_ref` and `status`. Report the `run_ref`
back to the user and stop.

## What to tell the user

Acknowledge immediately and briefly. A run takes minutes, not seconds, and
progress arrives in this thread on its own.

> Run RUN-2026-0042 started. I am planning the specialist workstreams and will
> post progress here.

If the script reports `deduplicated: true`, the request was already running —
give the existing `run_ref` rather than starting another.

## What not to do

- Do not answer the pharmaceutical question yourself, even partially.
- Do not invent a run reference. Use what the script returns.
- Do not read or echo database credentials, gateway tokens or any environment value.
- Do not retry a failed launch more than once; report the error text instead.
