# Slack app setup for Nucleus

Create the Slack app from a manifest rather than clicking through the UI. Every
scope and setting here is derived from what `@openclaw/slack` v2026.9.1 actually
uses, filtered to what PRD §17 needs, under the §2 least-privilege rule.

## Which manifest

| File | Use when |
|------|----------|
| `slack-app-manifest.json` | Nucleus lives in **public** channels. Start here. |
| `slack-app-manifest-private-channels.json` | You need it in **private** channels too. Adds `groups:history`, `groups:read`. |

## Create the app

1. Go to <https://api.slack.com/apps> → **Create New App** → **From an app manifest**.
2. Pick the workspace.
3. Choose **JSON**, paste the manifest contents, **Next** → **Create**.

The manifest already sets Socket Mode, interactivity, the `app_mention`
subscription and the bot user, so there is nothing further to toggle.

## Collect three credentials

| Credential | Where | Looks like |
|---|---|---|
| App-Level Token | **Basic Information → App-Level Tokens → Generate**, scope `connections:write` | `xapp-…` |
| Bot User OAuth Token | **Install App → Install to Workspace**, then **OAuth & Permissions** | `xoxb-…` |
| Signing Secret | **Basic Information → App Credentials** | 32 hex chars |

The app-level token is shown once. Copy it immediately.

## Invite and configure

In Slack, in the channel Nucleus should work in:

```
/invite @nucleus
```

Then:

```bash
openclaw configure --section channels
```

Prefer the SecretRef option (`source: env`) over pasting raw tokens if OpenClaw
offers it — §21 requires secrets to stay out of config files.

## Why each scope

| Scope | Needed for |
|---|---|
| `app_mentions:read` | Receiving `@nucleus` requests (§17.1) |
| `chat:write` | Plan previews, progress updates, the executive brief |
| `channels:history` | Thread context for the §17.2 project/session mapping |
| `channels:read` | Resolving channel metadata |
| `files:write` | Returning the DOCX/PDF report (§18, FR-014) |
| `users:read` | Attributing approvals to a named human (§16) |
| `reactions:write` | Acknowledgement reaction on receipt |

Deliberately **not** requested: `im:*`, `mpim:*` (no DM support in v3),
`commands` (no slash commands yet), `assistant:write`, `files:read`,
`chat:write.customize`, `users:read.email`. Add them only when a requirement
needs them, and update the manifest rather than editing the app by hand.

## After Slack connects

Two items remain, both needing the workspace to exist first:

1. Add the `message` tool to the Slack-routed agent, or explicit channel actions
   (`sendAttachment`, `thread-reply`) fail — `openclaw doctor` warns about this.
2. Set the command owner so §16 approvals can be authorised:
   ```bash
   openclaw config set commands.ownerAllowFrom '["slack:<YOUR_SLACK_USER_ID>"]'
   ```
   Find your ID in Slack: profile → **⋮** → *Copy member ID*. Restart the gateway
   afterwards.
