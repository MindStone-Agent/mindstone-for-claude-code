"""Shared authority math for MindStone / Cairn recall ranking — MS4CC #63.

Portable, dependency-free scoring primitives so every substrate (MS4CC / MS4PI /
MindStone-Agent) ranks recall the SAME way. This module holds ONLY pure functions
over an already-parsed frontmatter dict (`fm`); each caller parses with its own
real frontmatter parser (e.g. session_start.parse_frontmatter) and passes the dict
in. No IO, no third-party imports — safe to import from any hook.

Two deliberately-separated entry points, per Clint's 2026-06-10 ruling that
experiential weighting is auto-recall-only (manual/CLI stays raw vector —
[[design_recall_scoring_split]]):

  salience(fm, now)          full session-start injection ranking. +inf for
                             critical/evergreen (always inject up to budget);
                             else authority-forward base * recency decay.
  authority_factor(fm, now)  bounded [1.0, AUTH_CAP] multiplier for RE-RANKING an
                             auto per-turn recall hit. Similarity stays the
                             relevance gate; this only reorders among already-
                             relevant hits, so it can never surface an irrelevant
                             memory — only break modest gaps in favor of authority.

Key correction (2026-07-07): `hits` is an age-odometer (it accumulates with a
memory's presence over time, NOT its usefulness — oldest/always-injected files
reach ~2400 while a memory created today sits at ~6), so it enters ranking only as
log1p(hits): dampened, so the human-confirmed `prevented` signal is no longer
numerically swamped. `prevented` is the highest-quality signal we have (confirmed
at checkpoint that the memory stopped a real mistake). Full rationale:
design_recall_ranking_ontorank_2026-07-07.md.
"""

from __future__ import annotations

import math
from datetime import datetime

# --- Tunables (documented so QA + cross-substrate propagation can reason about them) ---
PREVENTED_WEIGHT = 3.0        # weight on the human-confirmed authority signal
DEFAULT_HALF_LIFE_DAYS = 30   # matches session_start.weight()'s default
AUTH_CAP = 1.8                # max per-turn re-rank multiplier (bounded, never surfaces noise)
SAT_K = 12.0                  # saturation constant: squashes raw authority into [1, CAP]


def _num(v) -> float:
    """Coerce a frontmatter value to a FINITE float; missing/garbage/inf/nan -> 0.0.

    OverflowError guards `float(huge_int)`; the isfinite check rejects inf/nan (a
    frontmatter `hits: 1e309` parses to the string "1e309" -> float -> inf, which
    would otherwise make raw/(raw+K) evaluate to nan and escape the [1.0, CAP] bound).
    """
    try:
        f = float(v)
    except (TypeError, ValueError, OverflowError):
        return 0.0
    return f if math.isfinite(f) else 0.0


def is_always_inject(fm: dict) -> bool:
    """critical or evergreen -> always injected at session start (pointer/full)."""
    return bool(fm.get("critical") in (True, "true", "True", 1, "1")
                or fm.get("evergreen") in (True, "true", "True", 1, "1"))


def base(fm: dict) -> float:
    """Authority-forward base score: dampened frequency + human-confirmed authority.

    log1p(hits) declaws the age-odometer; PREVENTED_WEIGHT * prevented carries the
    signal we actually trust. This is the canonical base formula — session_start.py
    applies it inline (log1p(hits) + 3*prevented) to avoid a new import on its
    critical path; keep the two in sync if either changes.
    """
    # Clamp to >=0: hits/prevented are non-negative counters. A corrupt negative value
    # must not crash log1p (ValueError) or flip authority negative; _num already maps
    # garbage/inf/overflow to 0.0. Result is always a finite, non-negative base.
    hits = max(0.0, _num(fm.get("hits")))
    prevented = max(0.0, _num(fm.get("prevented")))
    return math.log1p(hits) + PREVENTED_WEIGHT * prevented


def decay(fm: dict, now: datetime) -> float:
    """exp(-age/half_life) recency factor, anchored on last_applied or created."""
    anchor = fm.get("last_applied") or fm.get("created")
    age_days = 0.0
    if anchor:
        try:
            dt = datetime.fromisoformat(str(anchor))
            if dt.tzinfo is None:
                from datetime import timezone
                dt = dt.replace(tzinfo=timezone.utc)
            age_days = max(0.0, (now - dt).total_seconds() / 86400.0)
        except Exception:
            age_days = 0.0
    half_life = _num(fm.get("half_life_days")) or DEFAULT_HALF_LIFE_DAYS
    if half_life <= 0:  # a non-positive half-life would flip decay into unbounded growth
        half_life = DEFAULT_HALF_LIFE_DAYS
    return math.exp(-age_days / half_life)


def salience(fm: dict, now: datetime) -> float:
    """Session-start injection ranking. +inf for critical/evergreen; else (base+1)*decay.

    The +1 floor keeps zero-signal memories ordered by recency instead of all
    collapsing to 0. Project-match boost stays in the session_start.py caller — this
    is the corpus-independent core so every substrate shares the same authority math.
    """
    if is_always_inject(fm):
        return float("inf")
    return (base(fm) + 1.0) * decay(fm, now)


def authority_factor(fm: dict, now: datetime) -> float:
    """Bounded [1.0, AUTH_CAP] multiplier for re-ranking an auto per-turn recall hit.

    Similarity is the relevance gate; this reorders among comparably-relevant hits so
    a same-similarity, high-authority memory outranks a stale/ubiquitous one.

    Always-inject (critical/evergreen) memories return NEUTRAL 1.0 here — NOT AUTH_CAP
    — deliberately: they are already guaranteed in context via the session-start block,
    so the scarce per-turn slots should go to surfacing memories that ISN'T already
    injected. Boosting them would re-crowd context with what you already have. (This is
    the intentional divergence from salience(), where always-inject == +inf because
    session-start IS the place to include them.)
    """
    if is_always_inject(fm):
        return 1.0
    # base>=0 and decay in (0,1] after the clamps above, so raw>=0; the explicit clamp
    # guards the division and keeps the multiplier provably within [1.0, AUTH_CAP).
    raw = max(0.0, base(fm) * decay(fm, now))
    return 1.0 + (AUTH_CAP - 1.0) * (raw / (raw + SAT_K))
