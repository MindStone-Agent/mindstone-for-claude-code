---
name: design_agora
description: Pointer to the canonical Agora design doc. Project lives at R1ngZer0/agora; full design at docs/DESIGN.md there.
type: design
tags: [agora, comms, pointer]
projects: [agora]
hits: 0
prevented: 0
last_applied: null
created: 2026-05-06
half_life_days: 365
critical: true
evergreen: true
---

# Agora — pointer

The canonical Agora design lives in its own repo:

- **Repo:** https://github.com/R1ngZer0/agora
- **Design:** https://github.com/R1ngZer0/agora/blob/main/docs/DESIGN.md
- **Origin ticket:** https://github.com/R1ngZer0/MindStone/issues/18

## What Agora is (one paragraph for SessionStart context)

A self-hostable HTTP service that gives a "family" of AI agents and humans a shared, async messaging space. Structurally a private Slack/Discord — channels, threads, mentions, reactions — but built around agent-native primitives: pull-not-push delivery to agents, per-agent bearer-token auth, channel-scoped permissions, chain-limit governance, single-command Docker deployment. Substrate-neutral by design — MindStone, MS4CC, and plain HTTP-capable agents are all peer clients.

## My role on this project

Lead. Took the work on 2026-05-06 when Clint offered it (Cairn focused on SCRI engineering, Mira on the SCRI paper). Drafting design then PRD; will scaffold MVP after sign-off.

## Phase

Phase 0 — design. Pre-implementation. Open questions live at the bottom of the canonical design doc.

## Local working files (during Phase 0 only)

When I'm actively iterating on the design and the canonical version drifts, I may keep a working copy here in MS4CC orchestrator memory for SessionStart loading. That working copy is named `design_agora_working.md` to make the pointer-vs-working distinction obvious. After Phase 0 sign-off, the working copy gets removed and only this pointer remains.
