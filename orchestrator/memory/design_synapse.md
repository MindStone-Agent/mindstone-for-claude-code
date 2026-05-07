---
name: design_synapse
description: Pointer to the canonical Synapse design doc. Project lives at R1ngZer0/synapse; full design at docs/DESIGN.md there.
type: design
tags: [synapse, comms, pointer]
projects: [synapse]
hits: 0
prevented: 0
last_applied: null
created: 2026-05-06
half_life_days: 365
critical: true
evergreen: true
---

# Synapse — pointer

The canonical Synapse design lives in its own repo:

- **Repo:** https://github.com/R1ngZer0/synapse
- **Design:** https://github.com/R1ngZer0/synapse/blob/main/docs/DESIGN.md
- **PRD:** https://github.com/R1ngZer0/synapse/blob/main/docs/PRD.md
- **Origin ticket:** https://github.com/R1ngZer0/MindStone/issues/18

## What Synapse is (one paragraph for SessionStart context)

A self-hostable HTTP service that gives a "family" of AI agents and humans a shared, async messaging space. Structurally a private Slack/Discord — channels, threads, mentions, reactions — but built around agent-native primitives: pull-not-push delivery to agents, per-agent bearer-token auth, channel-scoped permissions, chain-limit governance, single-command Docker deployment. Substrate-neutral by design — MindStone, MS4CC, and plain HTTP-capable agents are all peer clients.

## Naming history

Briefly named "Agora" during initial scaffolding on 2026-05-06. Renamed to **Synapse** on 2026-05-07 when efforts merged with Charlene Watson's earlier `synapse/` work in MS4CC — same conceptual frame, better-fitting name (signals jumping the gap between agents). The standalone-HTTP-service architecture I built stayed; Charlene's unshipped conceptual contributions (chain-limit, governance boundary, debate protocol) merged into the Synapse design. Charlene's earlier `claude --print` + JSONL transport is *not* what we're building — that path is Claude-Code-only and won't scale to Brian's thousands-of-agents case.

## Authorship

- **Co-Architects:** Charlene Watson, Clint Bodungen
- **Phase 1 Lead:** me (Hearth)

## My role on this project

Phase 1 Lead. Took the work on 2026-05-06 when Clint offered it (Cairn focused on SCRI engineering, Mira on the SCRI paper). Drafted design + PRD; built the Phase 1 backend, frontend, and ops surface end-to-end on 2026-05-06. Continuing through Phase 1 MVP completion (WebSocket + reference clients + Postgres-readiness verification).

## Phase

Phase 1 — UI surface complete; reference clients + WebSocket + Postgres verification remaining for MVP.
