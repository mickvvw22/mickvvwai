---
name: agently-mail
description: "Agently CLI: Tencent Agent Mail from terminal. Send, read, search, reply, forward emails via agently-cli."
version: 1.0.0
author: mickvvw
license: MIT
platforms: [linux, macos, windows]
metadata:
  hermes:
    tags: [Email, CLI, Communication, Tencent]
    homepage: https://agent.qq.com
prerequisites:
  commands: [agently-cli]
---

# Agently Mail CLI

Agently CLI (`agently-cli`) is Tencent's Agent-native email tool. It lets the agent send, read, search, reply, forward, and manage emails from the terminal via OAuth-authorized QQ Agent Mail.

This skill is the connector for Agent Mail. Pair it with `email-inbox-triage` for inbox prioritization workflows.

## Prerequisites

1. Agently CLI installed (`agently-cli --version` to verify)
2. OAuth authorization completed (`agently-cli auth login`)
3. Verify with `agently-cli +me`

### Installation

```bash
npm install -g @tencent-qqmail/agently-cli
```

### Authorization

```bash
agently-cli auth login
# Open the printed URL in a browser to authorize
```

### Verify

```bash
agently-cli +me
```

## Commands

### Account

| Command | Purpose |
|---------|---------|
| `agently-cli auth login` | OAuth login and save credentials |
| `agently-cli auth logout` | Clear saved OAuth credentials |
| `agently-cli auth status` | Show current credentials and auth status |
| `agently-cli +me` | Get user info and alias list |

### Reading

| Command | Purpose |
|---------|---------|
| `agently-cli message +list` | List emails by folder with pagination |
| `agently-cli message +list --folder INBOX --limit 20` | List 20 emails from inbox |
| `agently-cli message +read --id msg_xxx` | Get full content (body, attachments) |
| `agently-cli message +search --q "keyword"` | Search by keyword with filters |
| `agently-cli message +watch` | Wait for and return new email details |

### Sending

| Command | Purpose |
|---------|---------|
| `agently-cli message +send --to user@example.com --subject "Hi" --body "Hello"` | Send new email |
| `agently-cli message +send --to a@x.com --cc b@x.com --subject "Hi" --body-md "# Hello"` | Send with Markdown body |
| `agently-cli message +reply --id msg_xxx --body "Thanks"` | Reply to email |
| `agently-cli message +reply --id msg_xxx --reply-all --body "Thanks all"` | Reply all |
| `agently-cli message +forward --id msg_xxx --to new@example.com` | Forward to new recipient |

### Managing

| Command | Purpose |
|---------|---------|
| `agently-cli message +trash --id msg_xxx` | Soft delete (30 days) |
| `agently-cli message +delete --id msg_xxx` | Permanently delete |
| `agently-cli attachment +download --msg msg_xxx --att att_xxx` | Download attachment to local |

## Email Body Conventions

When sending/replying/forwarding, the body should only contain what the user asked to convey. Do not add agent signatures or "sent by Agent" notices unless the user explicitly requests it.

## Safety: Two-Phase Confirmation

For write operations (send, reply, forward, delete):
1. First draft the email and show it to the user for confirmation
2. Only send after explicit user approval

Do not send emails without user confirmation. "Handle my inbox" does not imply permission to send.

## Rate Limits

- Daily send quota: 50 emails
- Requests per hour: 200
- Requests per minute: 10
- Max attachments: 50 files, 20MB total

## JSON Output

All commands support `--json` for structured output. Prefer JSON when parsing results programmatically.

```bash
agently-cli +me --json
agently-cli message +list --json --limit 10
```
