---
name: study-platform-docs
description: Use when starting any task on Apps Platform before asking the user questions or writing code — runs a swarm of subagents across the full doc corpus to surface capabilities and constraints relevant to the user's request
---

# Study Platform Docs

## Overview

Before responding to any user request, run a two-pass doc analysis. Pass 1 extracts findings per doc across all plausible approaches. Pass 2 cross-references findings. Synthesize before asking anything.

## Process

**Step 1 — Get all doc IDs:**
```bash
apps-platform docs list
```

**Step 2 (Pass 1) — Dispatch one subagent per doc (in parallel):**

Treat the output of step 1 as a list. For every line — iterate through all of them, no skipping — launch one Agent call. Your job here is mechanical iteration, not filtering. Even docs whose names seem unrelated must be processed; you cannot know in advance what constraints they contain. Relevance filtering happens inside each subagent's response, not here.

For each doc ID, launch a subagent with this prompt:
```
User request: <paste full user request>

Doc ID: <doc_id>
Content:
<apps-platform docs get <doc_id>>

Extract everything in this doc relevant to the user request:
- Supported features or APIs that apply
- Constraints, limitations, or TTLs
- Required config (project.toml fields, env vars, etc.)
- Anything that would change the design or make part of the request impossible

Consider all plausible implementation approaches, not just the most obvious one. A constraint that only applies under a specific architectural choice (e.g., polling vs webhooks, sync vs async, push vs pull) is still relevant — report it with a note about which approach it affects.

Be specific. Quote exact values. Err on the side of including findings. Only say "not relevant" if there is genuinely nothing.
```

**Step 3 (Pass 2) — Cross-reference:**

Launch one more subagent with ALL pass-1 outputs concatenated:
```
The user wants: <paste full user request>

Below are raw findings from every platform doc. Your job is CROSS-REFERENCING — find constraints that only emerge when combining information from multiple docs:

<paste all pass-1 outputs>

Specifically look for:
- Token/auth flows that span multiple services (e.g., async tokens + Data API + third-party scopes)
- Networking constraints that affect integration patterns (e.g., IAP + webhooks)
- Feature combinations that interact (e.g., scheduled jobs + per-user tokens + TTLs)
- Approval chains (e.g., need scope approval AND SA allowlist AND channel permissions)

For each cross-doc finding, cite which docs it comes from.
```

**Step 4 — Synthesize pass 1 + pass 2. Then respond to the user.**

Surface cross-doc constraints prominently. Only ask questions the docs don't answer.
