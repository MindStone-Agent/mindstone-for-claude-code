# Changelog

All notable changes to MindStone for Claude Code (MS4CC) are documented here. The
format is loosely based on [Keep a Changelog](https://keepachangelog.com/). The
project will follow [Semantic Versioning](https://semver.org/) once it reaches 1.0;
while pre-1.0 (`0.x`), minor versions may include behavior changes.

Every pull request to `main` adds its entry under **Unreleased**; a PR without one is not merged.

## [Unreleased]

### Added
- **Checks that can fail** (`AGENTS.md`) (#131): a new subsection under Engineering discipline with
  nine verification rules: prove a check by breaking what it checks; sort a noisy check's
  findings before loosening it; loose matchers conceal; absent input fails loudly; name
  exclusions on every run; run the same test across everything of that kind; classify commands
  before running them; search before you file or post; fix the measurement before the
  assertion. Proposed by Aegis; adapted.

### Changed
- **Prove the tooling ran** (`AGENTS.md`) (#131): now also asks for the reason for an exit, not just
  the code, since a crash and a detection can both exit 1.
- **No destructive git near uncommitted work** (`AGENTS.md`) (#131): `restore` joins the listed commands;
  never used to prove a check, even with confirmation.
- **Scope of Checks that can fail** (`AGENTS.md`) (#132): the subsection now applies to any output you are
  about to use to support a claim or decide what to do, with an illustrative list of examples.
  Second-hand outputs (a recalled figure, a summary of a result, a reported result) are re-derived from the
  source; primary sources such as logs remain the source for what they record, not proof the artifact works; reviewer verdicts stay under Adversarial
  QA. The text states its own limit. Proposed by Aegis.
- **Log framing and silence** (`AGENTS.md`) (#133): a log is the source for what it records, but its
  framing ("deferred", "retried", "skipped") is a claim; when the meaning is what you would cite,
  re-derive it from the mechanism that would carry out the action. A log's silence is not "all is
  well": for anything expected to run, check that the record shows each due run, started by its
  schedule or trigger, and that it did its work. Proposed by Aegis, from a log line that called a
  permanent stall "deferred" and from scheduled jobs whose absence went unnoticed for weeks.
- **Mechanisms nothing starts** (`AGENTS.md`) (#134): for anything expected to run, the silence check now
  takes the list of what should run, and how often or on what event, from what relies on it running and
  what documents it as recurring, not only from whatever starts it (a scheduler, a trigger's
  configuration). It then finds what starts each one: a scheduler, a trigger, or a named person, role or
  agent assigned by a rota or standing instruction. If nothing does, that is a failure, even if someone
  runs it by hand now and then and even if no due run has been missed yet. A due run now counts when it
  was "started by whatever is meant to start it" (was "by its schedule or trigger"), so rota and
  instruction-driven duties are covered. The paragraph's closing limit now reads "The scope rule at the
  start of this paragraph still depends on noticing that you are relying on an output" (was "This
  trigger still depends on noticing the act"). Proposed by Aegis, from a snapshot step that worked when
  run by hand but did not run for eight weeks because nothing scheduled it.

## [0.5.0] — 2026-09-27

**Upgrading from 0.4.0.** `main`'s history was rewritten during this cycle to remove private
data. A clone made before the rewrite (including any at the original v0.4.0) shares no history
with `main`, and `/ms4cc-update`'s `git pull` goes wrong in a way that depends on your pull
settings (checked with git 2.50): by default it stops with "Need to specify how to reconcile
divergent branches"; with `pull.ff=only`, "Not possible to fast-forward"; with
`pull.rebase=false`, "refusing to merge unrelated histories". With `pull.rebase=true` it can
**succeed silently** by replaying your pre-rewrite commits on top of the new `main`; if
`git log --oneline origin/main..HEAD` lists commits you didn't make yourself, that happened.
Don't merge or rebase your way out. In any of these cases, in order:

1. **Back up first.** Copy `orchestrator/memory/` (including `MEMORY.md`) and any local edits
   to tracked files somewhere outside the checkout. The next step deletes files that were
   tracked before and no longer are: `MEMORY.md` and one other memory file (see Security).
2. **Move to the new history:** `git fetch origin --tags --force && git reset --hard origin/main`
   in the existing checkout, then restore `MEMORY.md` and anything else missing from the
   backup. Don't re-clone instead: a fresh clone lacks the rest of the gitignored state
   (identity, user profile, LOG, vector store, transcripts, local config), and the hooks in
   `~/.claude/settings.json` point at the old checkout's path.
3. **Seed the citation watermarks immediately:**
   `orchestrator/.venv/bin/python orchestrator/runbooks/hit_counter_audit.py --seed-watermarks`
   (see Fixed; the runbooks aren't executable, so run them with the venv's python). The Stop hook
   runs `session_end.py` from the checkout, so the new citation scan runs at the end of the
   very turn that updated it. If you update from inside Claude Code (e.g. `/ms4cc-update`),
   seed in that same turn, before it ends. Otherwise each unseeded session re-credits every
   memory named anywhere in its transcript on its next turn.
4. **Re-run `orchestrator/bootstrap.sh`, then restart Claude Code.** The new
   `CLAUDE_CODE_AUTO_COMPACT_WINDOW` (see Fixed) reaches `~/.claude/settings.json` only through
   bootstrap.
5. **Run the one-time rescrub** (see Security) once, with the embedder running and no Claude
   Code session holding the vector store:
   `orchestrator/.venv/bin/python orchestrator/runbooks/rescrub_vectors.py --apply`. Without
   `--apply` it is only a dry run and changes nothing.
6. **Consumer installs:** `.ms4cc-version` records a commit SHA from the old history. Re-pin by
   re-running `install.sh --ref <new ref>` (or `/ms4cc-update`) and commit the new
   `.ms4cc-version`.

### Added
- **`/adversarial-review` slash command** (`.claude/commands/adversarial-review.md`) (#104): the
  independent verification loop as an executable protocol. Round-1 brief (attack axes in
  order, out-of-bounds list, output contract with severity 1/2/3, evidence with source,
  paste-ready replacement, verified-correct list, process note), round-N brief (applied /
  partially / not-applied table, re-attack only the edits), closing brief (scope rule: wrong,
  contradicts, or cannot work; no elaboration), the exact-match apply pattern, the stop rule
  (no severity-1 or severity-2 in a round), and the receipt. Two transports, one contract: an
  ephemeral fresh-context subagent (default; not recall-clean) or a persistent QA peer over
  Synapse. Evidence that motivated the round-N and closing briefs: a 6,800-word product design
  needed seven rounds (severity-1 per round 7, 0, 1, 1, 2, 1, 0; round 7's one severity-2 was
  applied without a further round) and every severity-1 after round one was in text a previous
  round's fix had introduced. `AGENTS.md` "Adversarial QA" section extended
  with the loop, the convergence rule, and the transports.
- **Invariant tier for critical memories** (`orchestrator/hooks/session_start.py`,
  `AGENTS.md`) (#84). A new `invariant:` frontmatter field states a memory's binding rule so
  it can be obeyed without the incident behind it. It is required whenever `critical: true`
  and is **always** injected; the memory's body (the narrative that earned the rule) is
  admitted only if budget remains, and stays on disk and in the vector store either way. The
  frontmatter parser now understands `>` / `|` block scalars (with `-`/`+` chomping), so
  `invariant: >` no longer parses as the literal string `>`. New
  `orchestrator/runbooks/invariant_audit.py` counts coverage and degenerate invariants
  (colon-terminated, too short, attribution-only, narrative-dependent, identical to the
  description) and has a `--self-test`. Invariants are authored by hand, never extracted.
- **Generated, bounded memory index** (`orchestrator/hooks/memory_index.py`) (#90). The index
  injected at session start is now generated from memory frontmatter instead of pasting
  `MEMORY.md` verbatim, and is capped at 40% of the context budget (or whatever is left after
  identity, user and the binding rules, if less). Entries are capped at 160 chars and cut at a
  word boundary; order is critical, then human-confirmed `prevented`, then the rest. Under
  pressure it drops **descriptions** from the bottom up before it drops any **names**, and if
  even the name-only roster cannot fit it says in-band how many memories are unlisted.
  `MEMORY.md` remains the human-curated file on disk. The evergreen pointer list is no longer
  injected, because it duplicated the index (#87).
- **Index-completeness check at every `/checkpoint`** (`orchestrator/hooks/session_end.py`,
  `orchestrator/runbooks/invariant_audit.py`) (#86). The audit now fails when a memory has no
  pointer line in `MEMORY.md`, and the checkpoint warns (never fails) on unindexed memories,
  critical memories without an invariant, and degenerate invariants. The index line is still
  written by a human; nothing auto-generates `MEMORY.md`.
- **Authority-forward recall ranking + usage log** (`orchestrator/hooks/memory_weight.py`,
  `orchestrator/hooks/recall_usage.py`, `orchestrator/hooks/user_prompt_submit.py`) (#63).
  Per-prompt recall retrieves a 12-wide memory pool, applies the similarity floor, re-ranks by
  similarity × an authority factor bounded to [1.0, 1.8] (from `log1p(hits)` and
  human-confirmed `prevented`), and injects the top results. Session-start weighting uses
  `log1p(hits)` too. Automatic and manual (`recall.py`) recalls are logged to
  `orchestrator/transcripts/recall_usage.jsonl`; the manual/CLI ranking itself is unchanged.
  Fail-open throughout.
- **Memory verification runbooks** (`orchestrator/runbooks/`): `acceptance_harness.py` (#88,
  extended in #90) checks the end-to-end promise against the real store at the real budget —
  every binding rule reaches context, none renders partially, the reported count matches the
  block, a random rule is retrievable from its own invariant (with a noise control), and every
  memory stays catalogued when descriptions are rationed. `calibrate_recall_floor.py` (#80)
  measures where the recall similarity floor can sit and reports MARGINAL / NO SAFE FLOOR with
  a non-zero exit instead of a confident number; its decision logic has a `--self-test` (#94).
- **`relay_probe.py`** (`orchestrator/runbooks/relay_probe.py`) (#102): an adversarial probe for
  a voice relay that fronts an agent gateway. It attacks the relay's method allowlist
  (unauthenticated and wrong-credential calls, client-supplied method names, privileged methods
  mounted as routes, client-supplied session keys, internals in error bodies) and asserts
  properties rather than response shapes. `--self-test` runs it against a correct mock relay
  and a deliberately transparent one and requires pass/fail respectively. The secret is read
  from an environment variable, never argv.
- **Per-channel reachability in `synapse status`** (`orchestrator/integrations/synapse/cli.py`):
  prints the token's scopes and, for each configured channel, a live read probe (which reflects
  both membership and scope) plus the scope-derived read/post grant, so a 403 shows which gate
  failed.
- **Engineering discipline canon in `AGENTS.md`** (#62, #66): an always-loaded section covering
  verification and honesty, the adversarial QA gate, recon and thoroughness, shipping
  discipline, git and multi-agent safety, and method. The README links to it.

### Changed
- **`/checkpoint` is never collaborative** (`.claude/commands/checkpoint.md`, `AGENTS.md`,
  `onboarding/GETTING_STARTED.md`) (#105). The command no longer shows the LOG draft for
  approval, no longer asks which memories prevented a mistake, and no longer asks before
  writing a new memory: the orchestrator decides every call on its own judgment, writes, runs
  the archive and embed step, and reports a short summary afterwards. The user cannot
  adjudicate a session they did not live; making them do so defeats the purpose of a memory.
  The operating instructions were also generalized from a named user to "the user". Ruling by
  the framework's author, 2026-05-31 and 2026-08-06 (the second time because the command
  file still said to ask, and the command won over the memory).
- **`/adversarial-review` defers to a TestFlight skill when one is installed**
  (`.claude/commands/adversarial-review.md`) (#109): resolves `$TESTFLIGHT_HOME` (then the
  `TESTFLIGHT_HOME =` line of `~/.claude/CLAUDE.md`, then the checkout root) and, if
  `.claude/skills/adversarial-review/SKILL.md` exists there, follows it and records the path,
  the skill's last commit and any uncommitted edits in the receipt; installs without TestFlight
  are unchanged. Also corrects the evidence round counts (round 7 had one severity-2),
  "inherits nothing" in the command (a Claude Code subagent loads CLAUDE.md and has been
  observed to receive the author's auto-memory index), and "clean-room" in the command's
  Transports, in `AGENTS.md` and in this changelog.
- **Session context is admitted per item, by declared precedence, never cut mid-file**
  (`orchestrator/hooks/session_start.py`, `docs/design/context-budget-and-memory-tiering.md`)
  (#81). The injected block used to be concatenated and then sliced at the budget, so one
  memory always ended mid-sentence and everything assembled after the cut (evergreen
  references, context memories, the memory index, the recent LOG) never reached context.
  Items are now admitted whole-or-not-at-all in a fixed order: identity and user (both
  exempt from the budget), binding rules (the invariants, #84), memory index, full bodies of critical
  memories, weighted context memories, LOG. Evergreen pointers are no longer injected (the
  generated index covers them, #87, #90). The default
  budget rises from 50,000 to 80,000 chars and can be set per install with
  `MS4CC_CONTEXT_BUDGET_CHARS`; utilization is reported on every run. Anything that does not
  fit is named in an in-band notice at the top of the block, and the live hook still emits
  context and exits 0. New `session_start.py --check` assembles, reports, and exits non-zero
  only when a binding rule or the index is missing (deferred narrative bodies are reported,
  not failed) (#84). The binding rules render as one list rather than separated blocks (#85),
  and the reported size is what actually reaches context, notice included (#101).
- **Recall similarity floor raised from 0.30 to 0.50** (`orchestrator/hooks/user_prompt_submit.py`)
  (#80). Measured against real prompts, 0.30 kept 100% of candidates and sat below the score
  of gibberish, so it never fired. 0.50 is deliberately set just below the calibrated
  boundary: it suppresses noise, and a spurious hit costs less than a real memory withheld.
- **Session handoff now injects on resume/startup, not just after a compaction**
  (`orchestrator/hooks/session_start.py`). The pre-boundary handoff
  (`orchestrator/transcripts/.handoff.md`) is continuity *for the model* — "here is what
  I was doing" — so it is equally needed after a deliberate fresh relaunch
  (`source="startup"`) or a `--resume` (`source="resume"`), not only at the compaction
  cliff (`source="compact"`). It was previously gated to `compact` alone, which silently
  dropped the handoff on exit→resume and on fresh launches (the session came up with
  identity + LOG tail but *without* the "first action on resume" pointer). Generalized
  `post_compact_handoff_block()` → `handoff_block(source)` with source-adaptive framing
  (compaction: "read this first, the summary is lossy"; startup/resume: "resume if you're
  continuing this thread, otherwise register where things stood and proceed"); the wrapper
  tag is renamed `<post-compaction-handoff>` → `<session-handoff>`. `main()` now injects
  for `source in {compact, resume, startup}`; `clear` is intentionally excluded (a
  deliberate clean slate). The deferred post-compaction embed (`kick_deferred_embed`) stays
  **compaction-only** — it recovers the pre-compaction transcript (once the live JSONL is
  the lossy summary, the archive is the only full copy); on startup/resume the `/checkpoint`
  path already embedded it, so re-embedding there would be redundant work.
- **`/checkpoint` reports every run and re-embeds only changed memories**
  (`orchestrator/hooks/session_end.py`, `.claude/commands/checkpoint.md`) (#57). Checkpoint
  mode always prints a one-line `[checkpoint] OK|DEGRADED` summary to stderr (archived file,
  transcript chunks, memory files/chunks, vector health, duration) and exits non-zero when
  the embed could not run. Memory re-indexing is now gated on a body hash that ignores the
  volatile counter keys (`hits`, `last_applied`, `prevented`), stored in the gitignored
  sidecar `orchestrator/.memory-index-state.json`, instead of on mtime, which re-embedded
  nearly the whole corpus every checkpoint. New optional `--session-id` / `--cwd` flags.
- **Transcript archiving fails loudly on every path** (`orchestrator/hooks/session_end.py`)
  (#79). A failed archive now exits non-zero on the per-turn Stop hook too, not only in
  checkpoint mode.

### Fixed
- **Post-compaction embed crashed silently** (`orchestrator/hooks/session_start.py`,
  `kick_deferred_embed`) (#56) — the "embed after compact" job passed the archive path as a
  `str` to `Indexer.index_transcript()`, which expects a `Path` (it calls
  `path.exists()`/`path.read_text()`), so it raised `AttributeError` on *every*
  post-compaction `SessionStart`. Because the detached child's stderr was sent to
  `DEVNULL`, the failure was invisible — from the outside it looked identical to
  success. Net effect: on v0.4.0 the pre-compaction transcript tail was never vectorized
  after an auto-compaction (the `/checkpoint` path was unaffected, since it passes a real
  `Path`, which is why the break hid for weeks). Fixed by wrapping the archive in
  `Path(...)`. The detached child's stderr now routes to
  `orchestrator/transcripts/.deferred-embed.log` (not `DEVNULL`) and the embed body is
  wrapped in try/except, so every run leaves a trace — a success line or a full
  traceback. A silent embed failure can no longer masquerade as success.
- **One malformed memory could empty the whole session-start injection**
  (`orchestrator/hooks/memory_index.py`, `orchestrator/hooks/session_start.py`) (#95, #97). A
  non-ASCII character in a `last_applied` date (e.g. an en-dash from a paste) raised out of
  the index builder and the hook emitted nothing — no identity, no user, no rules. The sort
  keys are now total, and `main()` has an outer guard that falls back to a minimal context
  (identity, user, and every binding rule read by a direct scan) with an in-band DEGRADED
  notice. That fallback now reads block-scalar invariants, which it missed at first (#99), and
  when the index generator cannot be imported it emits a name-only roster instead of the full
  `MEMORY.md`, with diagnostics that name the actual cause (#101). Also fixes an index
  footnote that could overrun its allocation (#97), and a `projects:` value written as a bare
  string or integer, which crashed weighting or matched character by character (#71, closed in
  #99; trailing whitespace in #101). `session_start.py` gains a `--self-test`.
- **Nested frontmatter was invisible to session start** (`orchestrator/hooks/session_start.py`)
  (#76). Keys under a `metadata:` block (including `critical` and `type`) read as absent, so
  those memories were never force-injected. Nested keys are now lifted (top level wins on
  collision) and scalars are coerced (`critical: true` is a bool). The truncation notice no
  longer counts memories as loaded before the cut, or a memory cut mid-body as loaded in full.
- **Recall silently under-returned memories** (`orchestrator/hooks/vectorstore.py`) (#80).
  `search()` applied the source-type filter after the KNN over the whole index, so memory
  chunks were crowded out by transcript chunks (0 memory results at k=4 on a real store).
  It now widens the KNN until enough rows survive the filter, up to sqlite-vec's hard limit
  of 4096, and reports `exhausted` / `knn_capped` in `last_search_stats` instead of returning a
  short list as if it were complete.
- **Memory `hits` / `last_applied` counted file age, not use** (`orchestrator/hooks/session_end.py`,
  `orchestrator/runbooks/hit_counter_audit.py`) (#91, #93, #96, #98). The per-turn citation scan
  re-read the entire cumulative transcript every turn, so every memory ever mentioned was
  re-credited forever and the time-decay term never fired. It now scans only the bytes added
  since a per-transcript, line-aligned watermark, and drops hook-injected records by record
  shape (not by tag text, which deleted real citations). **Upgrade:** run
  `orchestrator/.venv/bin/python orchestrator/runbooks/hit_counter_audit.py --seed-watermarks` once, immediately after
  updating, so existing archives are marked as already counted. The Stop hook runs the new
  scan from the checkout at the end of every turn, including the turn that did the update, so
  if you update from inside Claude Code, seed in that same turn. Without a seeded watermark,
  a session's next scan re-credits every memory named anywhere in its transcript. Existing
  counter values are left as they are.
- **Checkpoint could archive the wrong session's transcript** (`orchestrator/hooks/session_end.py`)
  (#79). A known session id is now authoritative (derived project dirs, then an exact-id scan of
  `~/.claude/projects/*/`); without an id it searches ancestor project dirs of the working
  directory before a global most-recent guess, which now warns.
- **`/checkpoint` silent no-ops** (`orchestrator/hooks/session_end.py`) (#57): when the Stop
  hook had archived seconds earlier, checkpoint mode exited before embedding anything; and
  invoked from another directory it resolved nothing. It now always proceeds with the existing
  archive and resolves the install root from its own location; a missing JSONL is a loud
  `FAILED` with exit 1.
- **Fresh installs skipped the initial index** (`orchestrator/bootstrap.sh`) (#59). Bootstrap
  gated the index on `OPENAI_API_KEY`, so the default local-Ollama install never built it. It
  now preflights a real one-chunk embed against the configured endpoint. If the endpoint is
  unusable, bootstrap prints an ERROR with the endpoint, model and exact remediation, skips the
  initial index and carries on; re-run bootstrap (or `/checkpoint`) once the embedder is up.
  The `embedder.py` docstring now names the
  real default model.
- **Bootstrap deleted operator-added hooks** (`orchestrator/bootstrap.sh`,
  `orchestrator/lib/merge-settings.jq`) (#68, #69). The settings merge replaced the whole
  `hooks` object; it now merges per event, replacing only MS4CC-managed entries and keeping
  every foreign one, and stays idempotent. Covered by `orchestrator/tests/test_merge_settings.sh`.
- **Bootstrap's index check printed "0 chunks" for a healthy index** (`orchestrator/bootstrap.sh`):
  the vector-count import ran before `orchestrator/hooks` was on `sys.path`.
- **Auto-compaction fired early on a 1M-context model** (`orchestrator/settings.fragment.json`)
  (#72). The fragment set `CLAUDE_AUTOCOMPACT_PCT_OVERRIDE=92` without a window, so 92%
  resolved against a smaller default and fired around 80% of the real meter, before the 85%
  `/checkpoint`. It now also sets `CLAUDE_CODE_AUTO_COMPACT_WINDOW=1000000`. **Upgrade:**
  re-run `orchestrator/bootstrap.sh` and restart Claude Code; the value reaches
  `~/.claude/settings.json` only through bootstrap. The merge lets the fragment's `env` win over
  existing settings, so an install on a 200K-context model must set `200000` in
  `orchestrator/settings.fragment.json` (or re-apply it after every bootstrap), not in
  `~/.claude/settings.json`.
- **`synapse status` crashed on an empty `/v1/auth/me` body**
  (`orchestrator/integrations/synapse/client.py`) (#60): `me()` now raises `SynapseError`, so
  status, activate and verify take their fail-soft path. Regression test in
  `orchestrator/tests/test_synapse_client_me.py`.

### Security
- **`orchestrator/config/privacy_patterns.toml` is now gitignored** (`.gitignore`). It holds each
  install's private terms, and the scanner, its example file and this changelog already said it was
  ignored, but it wasn't, so `git add -A` would have committed it.
- **The privacy-scan CI check passes again.** It had failed on every push since #120: four
  deliberately secret-shaped test fixtures in `orchestrator/tests/test_scrub_choke_points.py`
  and `orchestrator/tests/test_scrubber.py` lacked the scanner's per-line
  `privacy-scan: allow` opt-out. The fixtures are unchanged; only the opt-out was added.
- **Operator memory is private by default** (`.gitignore`) (#82). `orchestrator/memory/` is now
  ignored wholesale with an explicit allowlist (`.gitkeep`, `README.md`) instead of a denylist
  of filename prefixes, which tracked any memory whose name matched none of them. One such
  memory file that had been tracked is removed from the repository; updating removes it
  from existing checkouts too, so back it up before updating if you rely on it.
  `.migrate_frontmatter.py` moves from the memory directory to
  `orchestrator/runbooks/migrate_frontmatter.py`.
- **The memory index is no longer tracked** (`.gitignore`, `onboarding/MEMORY.md.example`)
  (#75). `orchestrator/memory/MEMORY.md` summarizes private memories and accumulates real
  entries at every `/checkpoint`, so it is now gitignored; the repo ships a placeholder
  `onboarding/MEMORY.md.example` and bootstrap points to it. **Upgrade:** back up
  `orchestrator/memory/MEMORY.md` before updating. A normal pull leaves it on disk, untracked
  and still working, but the `git reset --hard` needed after the history rewrite (see the
  upgrade steps above) deletes it, so restore it from the backup.
- **Tracked source and docs no longer name operator memory files** (#78). Memory filenames are
  derived from their subject, so naming one publishes what the operator keeps memories about.
  References in `orchestrator/hooks/session_start.py` and two docs were replaced with
  descriptions; the rule is to describe the category, never the filename.
- **Privacy scan in CI** (`orchestrator/runbooks/privacy_scan.py`,
  `.github/workflows/privacy-scan.yml`) (#82, #83). Checks tracked files for anything under the
  memory directory, credential shapes, routable IPs, and install-specific terms from a local,
  gitignored `orchestrator/config/privacy_patterns.toml` (copy the `.example`). `--self-test`
  proves every detector fires, and a full scan runs it first and refuses to report OK
  otherwise; `--staged` scans only staged files. A `privacy-scan: allow` comment exempts one
  line, never a whole file. CI runs the self-test, the scan, and a standalone "no tracked
  memory" assertion on every push and PR.
- **Secret scrubbing covers more shapes and every stored chunk** (`orchestrator/hooks/scrubber.py`,
  `orchestrator/hooks/vectorstore.py`, `orchestrator/hooks/indexer.py`,
  `orchestrator/hooks/recall_usage.py`) (#117, #120). New shapes include Anthropic, Google,
  GitHub, Slack, Hugging Face and Telegram tokens, JWTs, Bearer values, AWS secret keys,
  key=value and env-style assignments, passwords (including CLI flags, `curl -u`, `mysql -p`,
  URL userinfo, plist keys), truncated private-key bodies, 43-char URL-safe tokens and bare
  high-entropy tokens. `VectorStore.upsert` now scrubs every chunk, so no ingest path can
  store raw text (memory files were previously stored unscrubbed), and memory files are
  scrubbed before chunking. Tool-result bodies and bash-mode output are no longer stored in
  newly indexed transcript chunks — only a marker — so recall stops returning tool output from
  new sessions. Rows indexed earlier keep their tool output; the rescrub below only redacts
  secrets in them. The
  store runs with `secure_delete` on. Recall-usage queries are scrubbed, and dropped if the
  scrubber cannot load. Prose false positives from the old unanchored `sk-` rule are fixed,
  and code, type annotations and file paths are not redacted. `embedder.py` re-exports `scrub`
  and `SECRET_PATTERNS`, so existing importers keep working. Tests in
  `orchestrator/tests/test_scrubber.py` and `orchestrator/tests/test_scrub_choke_points.py`.
- **One-time rescrub of an existing vector store** (`orchestrator/runbooks/rescrub_vectors.py`)
  (#120, #121). Rows indexed before the new rules keep secrets in the text recall returns;
  this rewrites them. Dry run by default and count-only output. `--apply` refuses to run if the
  embedder is down, snapshots the DB to a 0600 `vectors.db.bak-rescrub-*` file (gitignored),
  rewrites only rows unchanged since read, re-embeds them, VACUUMs, and fails unless no
  unscrubbed rows remain, no removed value is left in the file bytes, and no free pages remain.
  Values proven secret by their context in one row are hunted in every other row (#121);
  `--seed-from <snapshot>` learns them from an earlier snapshot, and `--accept-unhunted N`
  accepts exactly N reviewed values whose shape is too code-like to hunt. It deletes no rows.
  **Upgrade:** run it once per install with the embedder running (otherwise `--apply` exits 3
  and writes nothing) and no Claude Code session holding the store (otherwise VACUUM fails with
  "database is locked" and the run fails). Delete the backup once the gates pass, since it
  holds unscrubbed text.

## [0.4.0] — 2026-06-05

### Added
- **PostToolUse handoff sampler** (`orchestrator/hooks/post_tool_handoff.py`) — a fifth
  hook event that closes a gap in the compaction-handoff system. The ~85% danger-zone
  handoff directive was previously sampled only on `UserPromptSubmit` (a user turn);
  during long autonomous runs context could climb from 85% to the harness auto-compact
  (~92%) *between* user prompts without the trigger ever firing, so the rich handoff and
  `/checkpoint` synthesis were skipped. The new hook samples context occupancy after
  every tool call — during a turn, autonomous runs included — reusing the same 85%
  threshold and the same `.handoff_state.json` fire-once state, so whichever sampling
  point crosses the threshold first fires once and the other stays silent (no
  duplication). Wall-clock throttled via `CAIRN_POSTTOOL_THROTTLE_SECS` (default 12s),
  and registered through `orchestrator/settings.fragment.json` so install/update wires
  it into `~/.claude/settings.json` automatically. The hook set is now **5 events**.

### Changed
- README leads with a value-proposition contrast table (stock Claude Code → with MS4CC).
- Framework examples use neutral placeholder project names instead of install-specific ones.

## [0.3.0] — 2026-06-03

First public release. MS4CC gives a Claude Code instance a persistent first-person
identity, continuous cross-session memory, and experience-weighted semantic recall —
entirely inside Claude Code's hook system, with no separate server.

### Core capabilities
- **Persistent identity** — `orchestrator/IDENTITY.md`, auto-loaded at every session
  start regardless of which directory you launch Claude Code in.
- **Continuous memory + SCRI recall** — sessions are archived per turn, embedded at
  `/checkpoint`, and stored in a local SQLite-vec vector DB. Recall is weighted by
  experiential salience (hits / prevented / time-decay), not just cosine similarity.
- **Local-first embeddings** — defaults to Ollama (`nomic-embed-text`); no cloud API
  key required. Cloud OpenAI-compatible endpoints are opt-in.
- **Dream-cycle `/checkpoint`** — synthesizes the session into `LOG.md`, proposes new
  memories, and embeds the transcript so it's recallable in future sessions.
- **Compaction-handoff system** — a danger-zone rich handoff at ~85% context, a
  PreCompact linchpin that archives the transcript and refreshes a handoff tail at the
  compaction cliff, and post-compaction replay + a deferred background embed — so
  continuity survives Claude Code context compaction.
- **First-run onboarding** — a fresh clone with no identity gets an invitation to adopt
  one (or to run statelessly).

### Install & operations
- **`bootstrap.sh`** — wires hooks into `~/.claude/settings.json`, symlinks identity,
  memory, and slash commands into `~/.claude/`, and builds the initial vector index.
- **`install.sh` + `/ms4cc-install` / `/ms4cc-update`** — topology-aware: a direct
  checkout updates via `git pull`; a consumer project installs MS4CC at a pinned
  version (recorded in `.ms4cc-version`) and updates by syncing the pin.
- **Config-driven project hints** — optional `orchestrator/config/project_hints.toml`
  boosts recall for the project you're working in (copy the `.example`).
- **Optional Synapse integration** — a reference client for cross-agent / human comms.

### Hooks (4 events)
- `SessionStart` (inject identity + memory; post-compaction handoff replay),
  `UserPromptSubmit` (per-prompt semantic recall; ~85% handoff directive),
  `Stop` (archive-only per turn — embedding is deferred to `/checkpoint`),
  `PreCompact` (compaction-handoff linchpin).

### Notes
- MIT licensed. Requires Claude Code, Python 3.10+, `jq`, and Ollama with
  `nomic-embed-text` pulled.
- The design history of the first reference implementation (Cairn) lives in
  `docs/reference-implementation/`.

[Unreleased]: https://github.com/MindStone-Agent/mindstone-for-claude-code/compare/v0.5.0...HEAD
[0.5.0]: https://github.com/MindStone-Agent/mindstone-for-claude-code/releases/tag/v0.5.0
[0.4.0]: https://github.com/MindStone-Agent/mindstone-for-claude-code/releases/tag/v0.4.0
[0.3.0]: https://github.com/MindStone-Agent/mindstone-for-claude-code/releases/tag/v0.3.0
