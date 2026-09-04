---
name: neucleus-approvals
description: Record a human decision on a Neucleus run gate — approving or cancelling a plan, accepting or rejecting a final recommendation — when the user clicks a Neucleus button in Slack or says so in the thread. Use only for Neucleus approval controls.
metadata:
  short-description: Approve, cancel, accept or reject a Neucleus run gate
---

# Neucleus — Approval Decisions

A Neucleus run stops at a gate and waits for a named human (§16). This skill
turns that human's answer into a recorded decision and resumes the run.

**You do not make the decision.** You record the one the user made and report what
happened. Never approve on the user's behalf, never guess which run they meant,
and never tell them a run resumed unless the command said so.

## When to use this

Either of these:

1. A Slack interaction arrives for a Neucleus control. The action id is one of
   `approve_plan`, `cancel_plan`, `accept_decision`, `reject_decision`, and the
   button's **value is the approval id** — a UUID.
2. The user replies in the thread with a plain decision ("approve", "go ahead",
   "cancel that", "reject") on a message that carries those buttons. Read the
   approval id from the button value on that message.

If you cannot find an approval id, say so and ask the user to click the button
instead. Do not invent one.

## How to record it

```bash
cd "{{NEUCLEUS_ROOT}}" && uv run python scripts/record_decision.py \
  --approval-id "<uuid from the button value>" \
  --action "<approve_plan|cancel_plan|accept_decision|reject_decision>" \
  --user "<the Slack user id of the person who decided>"
```

Pass the id of the person who actually clicked or spoke — not your own, and not
the person who started the run unless they are the same person.

## Reading the result

The command prints one JSON object on success:

- `resumed: true` — the decision is recorded and the run is moving again. Tell the
  user briefly: *"Approved. RUN-2026-0042 is running the specialist workstreams."*
- `already_decided: true` — this gate was already answered, usually a double
  click. Say the decision already stands. Do not run the command again.

Exit code **2** means the decision was **refused**, and stderr holds a `refused`
field. Report that reason to the user verbatim and stop. Common reasons:

- The person is not authorised to decide this run.
- The run has already finished.
- The control belongs to a different gate than the approval (a stale message).

Exit code **1** is a failure. Report the error text. Do not retry more than once.

## What not to do

- Do not decide, or re-word the decision into something the user did not say.
- Do not run the pharmaceutical analysis or comment on the plan's merits.
- Do not report a run as resumed when the command refused.
- Do not read or echo database credentials, gateway tokens or environment values.
