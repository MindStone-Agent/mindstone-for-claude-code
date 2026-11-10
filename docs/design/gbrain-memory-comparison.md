# gbrain's memory approach, compared with MindStone-Agent and MS4CC

Research input for a memory design discussion. It proposes nothing for merge and changes no code.

- **Question:** does gbrain's approach have ideas that would improve memory in MindStone-Agent (MSA) and in MS4CC?
- **Short answer:** yes, in how it handles failure and measurement, and no, in how it treats original material.
  - **Worth taking:** its write path never loses text to an embedding failure, its search says in the response when a stage failed, and it measures retrieval with a replayable benchmark. Those three would close several defects we have already recorded.
  - **Not worth taking:** its summary layer is rewritten in place, and its session transcripts are stored with tool calls and thinking replaced by placeholders, then deleted after 30 days by default. Both conflict with "compression is the enemy of true continuity".
- **A difference in purpose that explains most of the rest:** gbrain is a knowledge base about the world (people, companies, meetings) that an agent queries. MSA and MS4CC keep an agent's own continuity. gbrain can afford to rewrite a page about a company. We cannot afford to rewrite what the agent lived through.

## Sources and limits

| Codebase | Commit read | How |
| --- | --- | --- |
| gbrain (`garrytan/gbrain`) | `ec20eee` (VERSION 0.60.651.0) | Code and docs, read only. Nothing was run. |
| MindStone-Agent | `8c0242f` (main) | Code read fresh. `core/` below means `packages/mindstone-core/src/`, `gw/` means `packages/mindstone-gateway/src/`. |
| MS4CC | `f84ecaf` (main) | Code, docs and open issues. Paths are under `orchestrator/hooks/` unless given in full. |

- **Coverage:** gbrain has about 3,600 files under `src/`. This is a targeted read of storage, the write path, search, the consolidation cycle, forgetting, context loading and the health checks. The database engines, the AI gateway, most cycle phase bodies and the connectors were not read.
- **Numbers:** every gbrain benchmark figure below is the project's own claim from its docs. None was reproduced.
- **Checking:** three independent readers produced the citations, one per codebase. The citations the recommendations rest on were then re-read against the code. Many gbrain citations point at a comment that describes the code beside it. Where that is the only support, the behaviour was not traced. Two are marked "header" as examples; treat any citation to a file's first 30 lines the same way.
- **Privacy:** mechanisms only. No memory contents or operator data are quoted.
- **Terms:** FTS is full-text (keyword) search. RRF is reciprocal rank fusion, which merges result lists by position. MRR and nDCG are standard ranking-quality metrics. SCRI is the older name for MindStone's recall architecture. "Pi" is the agent runtime MSA runs on. The "sidecar" is MS4CC's small state file beside its vector store. The "doctor" is a product's health-check command.

## 1. What gbrain does

### Storage

- **Units.** A page (one Markdown file, one `pages` row), a chunk (`content_chunks`), a fact (a typed, dated claim about an entity) and a take (a held position with a weight). `src/schema.sql:99-179`, `:354-404`; `src/core/schema-migrations/v045-facts-hot-memory-v0-31.ts:113-147`.
- **Source of truth.** "Canonical Markdown and frontmatter are the system of record for file-backed knowledge. Their database indexes can be rebuilt from those files." `docs/architecture/system-of-record.md:3-5`. The store is Markdown in git plus Postgres with pgvector.
- **A page has two halves.** `compiled_truth` is the current summary, and `timeline` is dated evidence below a `<!-- timeline -->` marker. `src/core/markdown.ts:868-890`.
- **History.** The importer snapshots a page into `page_versions` before overwriting it (`src/core/import-file.ts:765`). The docs call that table "rebuildable in principle but not in practice" (`docs/architecture/system-of-record.md:77`).

### Raw material

Session transcripts captured by its Claude Code hook are **not verbatim**:
- Text is kept. A tool call becomes `[tool: name]`, a tool result `[tool result]`, thinking `[thinking]`, an image `[image]`. `src/core/transcripts/claude-code-jsonl.ts:529-540`.
- Files over 10 MiB are read from the tail only (`:64`, `:314-321`).
- The corpus is deleted after 30 days by default, once a file has been through extraction: `CORPUS_RETENTION_DAYS_DEFAULT = 30`, `src/commands/hook.ts:150`, `:1916-1919`. The period is configurable, and unfinished files are kept longer (`src/core/context/corpus-segments.ts:811-818`).
- This describes gbrain's own corpus. Claude Code's JSONL on disk is outside it.

### Linking

- **Typed edges, no weights.** `links` carries `link_type`, `link_kind`, and tense. `src/schema.sql:549-584`.
- **Made by rules, not by a model.** Wikilinks, Markdown links and frontmatter are parsed, and verb patterns assign a type with a fixed precedence. `src/core/link-extraction.ts:1354-1362`, `:1447`.
- **Entity resolution** runs exact page, alias, prefix, fuzzy, then a slug fallback (`src/core/entities/resolve-on-save.ts:23-29`). Near-duplicates are flagged and never merged (`src/core/entities/near-name.ts:2`).
- **Missing targets are recorded** in `wanted_links` (`src/schema.sql:592-611`).

### Write path

- **Chunking** is recursive over five delimiter levels, about 300 words with a 50-word overlap, with no model call. `src/core/chunkers/recursive.ts:50-56`, `:135-137`.
- **A failed embedding never loses the text.** "An embedding outage never blocks the text write: the page and its chunks commit with NULL vectors ... and the stale sweep ... embeds them once the provider recovers." `src/core/import-file.ts:711-714`; the catch block at `:723-730` clears the vectors and reports "text saved, chunks queued".
- **It knows what needs re-embedding**, by several independent signals:
  - the page's content hash (`src/core/import-file.ts:521-528`, `:577-590`);
  - a per-chunk record of exactly what was embedded: column, model, dimensions and input (`:669-675`);
  - `embedded_text_hash` against the current chunk text (`src/schema.sql:376-378`);
  - a chunker version that forces a re-embed when chunking changes (`src/core/chunkers/recursive.ts:31-48`).
- **The backlog has an age.** `embedding_pending_since` records when a vector went missing (`src/schema.sql:364-366`), and the health check warns when a large backlog's oldest chunk has waited too long (`src/commands/doctor/checks/schema-health.ts:486-489`).

### Retrieval

- **Hybrid.** Keyword search (Postgres full-text) and vector search run as separate arms and are fused by rank with reciprocal rank fusion, `RRF_K = 60`. `src/core/search/hybrid.ts:58`; the stage order is in a comment at `:1095-1100`; `src/core/search/keyword-statement.ts:67-70`.
- **No model rewrites the query by default.** Expansion is off in all three modes (`src/core/search/mode.ts:492`, `:557`, `:629`).
- **The reranker fails open and says so.** It is on in the default mode and off in the lowest-cost one (`src/core/search/mode.ts:495-497`, `:560`). On an error it returns the fused order and reports a reason (`src/core/search/rerank.ts:160-180`). A separate signal covers a reranker that answers with nothing, so that callers can tell a reranker that is off from one that "died silently" (`:24-31`).
- **Deleted pages are filtered in the query itself.** Soft-deleted pages and archived sources are excluded in one shared clause (`src/core/search/sql-ranking.ts:136-150`). Chunks cascade when a page is deleted (`src/schema.sql:356`).
- **Recency is opt-in**, set per page-path prefix, and "only consulted when a caller passes" it. `src/schema.sql:125-127`; `src/core/search/recency-decay.ts:21-25`, `:43-63`.
- **Results are bounded** by a result limit and a token budget per mode: 10 and 4,000, 25 and 12,000, or 50 and none. `src/core/search/mode.ts:491-494`, `:556-559`, `:628-631`.

### What reaches the agent's context

- **Search is a tool call.** The agent writes the query.
- **The automatic per-turn injection is mostly not a search.** It assembles three things (`src/core/context/turn-context.ts:5-16`):
  - pointers, from resolving names in the recent turns to existing pages by alias and title: a name, a page id and a one-line synopsis. "Detect + point, NEVER auto-dump the body." "Deterministic, zero-LLM." `src/core/context/retrieval-reflex.ts:5-18`;
  - up to three volunteered pages, chosen the same way;
  - recent "hot" facts.
- An optional step asks a model whether recall is needed, and may fire one keyword-only search (`src/core/context/recall-needed.ts:6-15`).
- The block is capped at 8 KB (`src/core/context/turn-context.ts:19-21`).
- **Session start** loads an always-on core tier whose size is enforced when a page is written, not cut when it is loaded: a write over the budget is refused with the overage stated. `src/core/persistence/core-guard.ts:1-13`, `:90-99`; `src/core/core-memory.ts:12-15`.

### Summary layer

- **Compiled truth is rewritten, and the timeline is append-only.** The guide's rule: "REWRITE means rewrite, not append" for compiled truth, "always APPEND (never edit existing entries)" for the timeline. `docs/guides/compiled-truth.md:71`, `:79`, `:103`.
- **This is mostly a convention the agent follows.** The one mechanical check found covers typed relationships only: a health check counts pages that still assert a relationship their own timeline ended (`src/commands/doctor/checks/edge-validity.ts:5-11`). Separately, a cycle phase has a model flag relationships of one subject that cannot both hold, and date arithmetic then proposes or applies closing one (`src/core/cycle/edge-contradictions.ts:1-11`). No code was found that checks free-text compiled-truth claims against the timeline.
- **Search prefers the summary strongly only when low detail is asked for**: compiled-truth chunks then get a 2.0 boost. On a default search, a question framed around an entity is inferred as low detail and gets a soft 1.2 tilt. `src/core/search/hybrid.ts:59`, `:92-114`; `src/core/search/hybrid/request.ts:227`.

### Consolidation

- **One cycle** (`runCycle`) defines 31 phases, several off by default or gated. `src/core/cycle.ts:128-230`. It is started by a command or a background job; how often was not traced.
- **`synthesize`** has a cheap model score each transcript from a sample of at most 24,000 characters, and writes new reflection pages for those that pass. `src/core/cycle/synthesize.ts:147-149`, `:2903-2907`.
- **A mechanical verifier checks the result**, because of what the project measured: "fewer than half of quoted spans in produced pages were substrings of the source transcript." `src/core/cycle/synthesize-verify.ts:5-7` (header).
- **`consolidate`** clusters an entity's facts and promotes the highest-confidence fact's own text. It does not ask a model to rewrite, and it marks the source facts `consolidated_into` and never deletes them. `src/core/cycle/phases/consolidate.ts:6-21`, `:284-287`.

### Forgetting

- **Facts decay by kind:** an event's confidence has a 7-day time constant, a commitment or preference 90, a belief or fact 365. It is computed when read, so nothing is destroyed. `src/core/facts/decay.ts:28-35`, `:48-60`.
- **A trust floor:** facts the owner confirmed do not lose weight to age (default off). `src/core/facts/decay.ts:63-74`.
- **Supersession:** a fact carries `superseded_by` (`src/core/schema-migrations/v045-facts-hot-memory-v0-31.ts:128`). A superseded fact stays in the table but is left out of default recall (`src/core/search/fact-relevance.ts:153`). A superseded page stays searchable with a score penalty (`src/core/search/hybrid.ts:829-837`).
- **Deletion is soft first,** then hard after 72 hours. `src/schema.sql:120-124`; `src/core/cycle.ts:1711-1717`.
- **No rule prunes pages by age or disuse** in the code read.

### Measurement

- **Retrieval metrics** (precision, recall, MRR, nDCG at k) against judged queries. `src/core/search/eval.ts:1-9`.
- **Capture and replay:** real queries are recorded and replayed against a changed build. `docs/eval-bench.md:213-227`.
- **A health command with over 150 check files**, including embedding coverage and backlog age, stalled backfill, reranker health, and full-text index completeness. `src/commands/doctor/checks/`.
- **Negative results are kept and acted on.** By the project's own figures (`docs/eval-bench.md:432-438`, `:462-466`), on a benchmark with 470 scored questions:
  - 95.53% with the reranker and 93.40% without;
  - 54.26% with model-written query expansion, which is why expansion is off;
  - 80.64% with an automatic result cut-off, which is why that is off (`src/core/search/mode.ts:597-599`).

## 2. Side by side

| Point | gbrain | MindStone-Agent | MS4CC |
| --- | --- | --- | --- |
| **Purpose** | Knowledge about the world, queried by an agent | An agent's own continuity | An agent's own continuity, inside Claude Code |
| **Source of truth** | Markdown pages in git | Identity and memory files, journals, append-only transcripts (`core/transcript/store.ts:65-74`) | Identity and memory files, LOG, transcript archives (`session_end.py:189-224`) |
| **Raw transcripts** | Placeholders for tools and thinking; deleted after 30 days by default | Verbatim, never rewritten | Verbatim archive; the *indexed* copy flattens tool calls to markers (`indexer.py:318-370`) |
| **Index** | Postgres, pgvector, full-text | SQLite tables; vectors as JSON, scanned in JS (`core/memory/sqlite-memory.ts:1346-1361`) | SQLite with sqlite-vec (`vectorstore.py:96-114`) |
| **When embedding happens** | On write, with a background sweep for the rest | After every completed turn (`gw/index.ts:739-756`) | Only at `/checkpoint` and after a compaction (`session_end.py:671-675`) |
| **Embedding failure** | Text saved, vector NULL, retried, backlog aged | Chunk left without a vector, found by its words, retried with a refusal ledger (`sqlite-memory.ts:1010-1052`) | **A zero vector is stored** and never retried (`embedder.py:152-160`) |
| **Keyword search** | Yes, a separate arm | A word-overlap fallback, not an index (`sqlite-memory.ts:409-428`) | None (issue #89) |
| **Fusion** | By rank (RRF) | Lexical and cosine scores sorted together on one scale (`sqlite-memory.ts:1266-1273`) | n/a |
| **Reranker** | A model in the default mode, fail-open, failure reported | No model. An additive score (`core/memory/scri-ranking.ts:203`) | Similarity times an authority factor, memory hits only (`user_prompt_submit.py:487-499`) |
| **Query text** | Written by the agent in a tool call | The last user message verbatim (`core/memory/recall.ts:61-63`) | The raw prompt (`user_prompt_submit.py:127-143`) |
| **Automatic injection** | Pointers and up to 3 pages by name match, plus recent facts; an optional model-gated keyword search | Up to 8 hits, 2,500 tokens, each turn (`recall.ts:31-33`) | 4 memory and 2 transcript chunks each turn (`user_prompt_submit.py:31-32`) |
| **Deleted sources** | Filtered in the query; chunks cascade | Chunks stay until manual maintenance (`sqlite-memory.ts:1130-1235`) | Chunks stay and remain recallable (`session_end.py:397-399`) |
| **Summary layer** | Compiled truth, rewritten in place | An authored `invariant:` per critical memory; an index of pointers (`core/memory/invariants.ts:100-112`) | The same, plus a generated index (`memory_index.py:243-287`) |
| **Consolidation** | A cycle, run by command or background job, that writes new pages | **Not implemented**; a scaffold that performs nothing (`core/lifecycle/post-compact.ts:47`) | `/checkpoint`: the model writes a LOG entry and new memory files |
| **Forgetting** | Decay by kind; soft delete; no age pruning | A recency boost of at most 0.04; nothing removed | Decay in ranking only; nothing removed |
| **Degradation visible?** | Stamped in the search response | A failed embed during search is caught with no log (`sqlite-memory.ts:1275-1277`) | A count in the LOG at checkpoint. A failed query embed injects unrelated chunks at a score of 0.500. A recall exception gives an empty block that looks like "nothing relevant" |
| **Retrieval benchmark** | Yes, with replay | None (issue #64) | No judged-query benchmark or replay (issue #18). Two narrower harnesses exist: a floor calibration with positive and negative query classes (`orchestrator/runbooks/calibrate_recall_floor.py`) and a retrievability check with a control (`orchestrator/runbooks/acceptance_harness.py`, A4) |
| **Tests** | Unit, end-to-end, model-checked specs | Smoke scripts only; no unit tests or CI in the repo | Six pytest files, none on retrieval; CI runs a privacy scan only |

Two facts about our own side that the discussion should start from:

- **MSA has no keyword index, no fusion, no reranker model and no dream cycle today.** A search of `packages/` for `rerank`, `bm25` and `fts5` finds nothing. Its docs describe more than the code does. The README says the current prompt, task, role, project and channel become retrieval cues (`README.md:51`), and `docs/refactor/MEMORY_STRATEGY.md:62` says the query is built "from the current user prompt and routing/session context". The code uses the last user text only (`core/memory/recall.ts:61-63`).
- **MS4CC's indexed transcript is already not verbatim.** The archive on disk is a byte copy, but what recall can find has tool calls and results reduced to markers (`indexer.py:318-370`). Thinking text is kept. The original is one file read away, so this is an index choice, not a loss.

## 3. Ideas worth taking, ranked

Ranked by what they fix against what they cost. Effort is S (days), M (a week or two), L (more).

### 1. Say in-band when recall is degraded, and always record that recall ran

- **What gbrain does:** every fail-open stage reports a reason, and the response carries a `degraded[]` list. `src/core/search/rerank.ts:24-31`, `:160-180`.
- **Where it lands:**
  - MSA: the empty `catch {}` at `core/memory/sqlite-memory.ts:1275-1277`; a `memory_recall_injected` event even when there are zero hits (`gw/index.ts:1650` writes none today).
  - MS4CC: a failed query embed returns a zero vector (`embedder.py:152-160`) and recall proceeds. Every stored chunk then scores exactly 0.500, which passes the `>= 0.50` floor (`user_prompt_submit.py:66`, `:478-479`; issue #111), so unrelated chunks are injected as if relevant. In that case the block should say "recall unavailable" and inject nothing.
- **Effort:** S in both.
- **Risk:** low. The one care is that a degraded notice in every turn becomes noise the agent learns to ignore, so it should appear only when something actually failed.

### 2. Treat embedding as a queue: NULL vectors, a record of what was embedded, and an aged backlog

- **What gbrain does:** see "Write path" above.
- **Where it lands:**
  - MS4CC: replace the zero vector in `embedder.py:152-160` with "no vector", skip such rows in search, and retry them. Record model and input hash per chunk in `vectorstore.py`, so a rebuilt store or a model change is detected (issue #123). Report the oldest pending chunk's age beside the existing `Vector health` line (`session_end.py:710-718`).
  - MSA: it already leaves a refused chunk without a vector. It lacks the age: add the oldest pending chunk's age to the doctor (`core/doctor/doctor.ts:194-211`), and count only vectors from the current model (issue #150).
- **Effort:** M for MS4CC, S for MSA.
- **Risk:** a schema change to `vectors.db` on every install. It needs a migration that is safe to re-run.

### 3. A retrieval benchmark with capture and replay, used as a gate

- **What gbrain does:** judged queries, standard metrics, and replay of captured queries against a changed build.
- **Why it ranks this high:** gbrain's own numbers show a plausible-sounding feature, model-written query expansion, cutting recall from 93% to 54%. We have changed ranking several times with no way to see such a drop. MS4CC already logs every candidate per query (`recall_usage.py:44-74`), and MSA does too (`core/memory/recall-usage.ts:54-91`), so most of the capture half exists. Neither log writes a row for a query that returned no candidates (`recall_usage.py:48-49`; `recall-usage.ts:61`), and both store only the first 300 characters of the query (`recall_usage.py:41`, `:53`; `recall-usage.ts:37`, `:63`), so both need a small change before replay is faithful.
- **Where it lands:** MS4CC issue #18; MSA issue #64. A judged set is the missing part: a few dozen real questions, each with the memory that should come back.
- **Effort:** M.
- **Risk:** a small judged set overfits. The earlier floor calibration is a warning here: its margin was +0.0029 (`user_prompt_submit.py:44-65`), which is not a separation. The set must include paraphrases (issue #100) and questions the store cannot answer.

### 4. Filter deleted and renamed sources at query time

- **What gbrain does:** soft delete plus one shared visibility clause, so a deleted page cannot be returned.
- **Where it lands:**
  - MS4CC: on reindex, delete the chunks of any memory file that no longer exists (`session_end.py:397-399` drops only the sidecar entry today), and store a path relative to the install (issue #125). PR #115 (open) already prunes the chunks of deleted memory files; what remains after it is the install-move case.
  - MSA: run the stale-source removal that `maintainSqliteMemoryIndex` does as part of the per-turn update, for file-backed sources (`sqlite-memory.ts:817-822` only upserts).
- **Effort:** S in both; for MS4CC it is mostly in review already.
- **Risk:** deleting index rows for a file that is briefly missing, for example mid-edit or on an unmounted volume. MSA already guards its transcript case against a missing directory (`sqlite-memory.ts:730-746`); the same guard applies.

### 5. A real keyword arm, fused by rank

- **What gbrain does:** keyword and vector results are fused by rank position, so their scores are never compared.
- **Where it lands:**
  - MSA: `sqlite-memory.ts:1266-1273` sorts word-overlap fractions and cosines together. A fresh chunk containing every query word scores 1.0 and outranks any cosine. Rank fusion removes that.
  - MS4CC: issue #89. SQLite's FTS5 is available without a new dependency.
- **Effort:** M in both.
- **Risk:** this is the change most likely to reintroduce a defect we have already had, a keyword arm that favours whatever was indexed most recently. The keyword index must be written in the same transaction as the chunk, and the benchmark from idea 3 should exist first.

### 6. Budget the always-loaded tier when it is written, not when it is loaded

- **What gbrain does:** a write that would push the core tier over its budget is refused. `src/core/persistence/core-guard.ts:1-13`.
- **Where it lands:** both products already degrade in the open at load. MS4CC admits each critical memory's invariant ahead of its body and names anything omitted in an in-band notice (`session_start.py:450-452`, `:515-516`, `:648-654`); bodies that do not fit are dropped whole, in filename order, by design. MSA's invariant tier degrades per item with an in-band note (`core/memory/invariants.ts:137-201`). Neither checks the budget when a memory is marked critical, so over-flagging (MS4CC issue #64) surfaces at load, or when someone runs the audit runbook, which prints the critical tier as a share of the budget but does not fail on it (`orchestrator/runbooks/invariant_audit.py:132-144`). The check belongs where the mark is made: in MS4CC's `/checkpoint`, with that projection turned into a warning.
- **Effort:** S.
- **Risk:** refusing a write at checkpoint could lose a memory. It should warn and name what to demote, not refuse.

### 7. Pointers by name, as a second automatic path beside vector recall

- **What gbrain does:** it matches names in recent turns to existing pages and injects pointers, with no embedding call.
- **Where it lands:** a memory's filename, `name:` and declared aliases are exact strings. When a prompt names one, the pointer can be injected with certainty, where vector recall is probabilistic. MS4CC: `user_prompt_submit.py`, beside the recall block. MSA: `core/routing/run.ts:199-234`.
- **Effort:** M.
- **Risk:** it only helps when the prompt names the thing. It must be a pointer to the full file and never a substitute for reading it, which is the same precondition `docs/design/context-budget-and-memory-tiering.md:174-176` sets for invariants.

### 8. Supersede, don't overwrite

- **What gbrain does:** see "Forgetting" above. A superseded fact is kept and left out of default recall; a superseded page is kept and ranked down.
- **Where it lands:** a `superseded_by:` frontmatter key in the memory schema. `/checkpoint` already tells the model to update an existing memory when a new one overlaps (`.claude/commands/checkpoint.md:85-101`); linking the old file to the new keeps both and stops the old one outranking its correction.
- **Effort:** S for the field and the ranking factor.
- **Risk:** chains of supersession that nobody reads. Ranking should follow the link once, not recursively.

### 9. Decay by kind, with a floor for what the owner confirmed

- **What gbrain does:** a decay time constant per fact kind, and no age decay for owner-confirmed facts.
- **Where it lands:**
  - MSA reads `half_life_days` in ranking but never loads it from the file, so every memory has a half-life of 30 (`core/memory/file-memory.ts:117-129`; `scri-ranking.ts:118`). Loading it is a small fix regardless of this idea.
  - MS4CC has per-file half-lives. Its `prevented` counter is described as human-confirmed (`memory_weight.py:25-27`) but is set by the model's own judgment at checkpoint (`.claude/commands/checkpoint.md:69-81`). A real floor needs a real owner signal.
- **Effort:** S for MSA's fix; M for an owner-confirmed signal.
- **Risk:** low for the fix. The owner signal is a design question, not a code one.

### 10. Check model-written memory against its source, mechanically

- **What gbrain does:** after a model writes a synthesis page, a deterministic pass checks that quoted spans and numbers appear in the source transcript.
- **Where it lands:** MS4CC's `/checkpoint` writes a LOG entry and memory files from the session. MSA's dream cycle, when it is built, will do the same. A quote check against the archived transcript is cheap and needs no model.
- **Effort:** M.
- **Risk:** it verifies quotes and numbers, not meaning. A memory can pass and still mislead.

### Smaller points

- **A parser-drift signal.** gbrain raises an alarm when a transcript has bytes but yields no turns (`src/core/transcripts/claude-code-jsonl.ts:10-12`, header). MS4CC reads the same JSONL format and has no such check.
- **A stated rebuild contract.** gbrain lists what can and cannot be rebuilt from files (`docs/architecture/system-of-record.md:57-99`). MS4CC's `vectors.db` and sidecar state have no such statement, and issue #123 is a consequence.

## 4. Ideas that conflict with "compression is the enemy of true continuity"

MS4CC's design doc states the rule's scope: it "is about compressing the **store** — replacing 500k tokens with a summary and discarding the original. That is lossy and irreversible." (`docs/design/context-budget-and-memory-tiering.md:167-169`). By that test:

| gbrain mechanism | Why it conflicts | Citation |
| --- | --- | --- |
| **Compiled truth is rewritten in place** | The summary replaces the previous summary. The earlier text survives only in `page_versions`, which the project says cannot be rebuilt in practice and which is pruned for large pages. | `docs/guides/compiled-truth.md:79-103`; `docs/architecture/system-of-record.md:77`; `src/core/page-state/versions.ts:69-97` |
| **Session transcripts lose tool calls, results and thinking** | gbrain's corpus never holds the original. For an agent's own history, what a tool returned and what the agent thought are the texture the rule protects. | `src/core/transcripts/claude-code-jsonl.ts:529-540` |
| **Transcripts are deleted after 30 days** (the default, once extracted) | The reflection page a model wrote, and the facts extracted from the session, become the only record in the store. That is the exact case the rule names. | `src/commands/hook.ts:150`, `:1916-1919` |
| **A cheap model decides which transcripts are worth synthesising, from a sample** | A session scored under the threshold produces no reflection page. Whether its transcript is then deleted on the same schedule was not traced. | `src/core/cycle/synthesize.ts:147-149` |
| **Search can boost the summary over the evidence** | When low detail is asked for, the rewritten half outranks the dated entries it was written from; on a default search an entity-framed question gets a soft 1.2 tilt the same way. This is a ranking preference, not a change to the store, so it is the weakest conflict here. | `src/core/search/hybrid.ts:59`, `:92-114` |
| **`purge_fact` rewrites history** | It removes a fact from every stored version. This is intentional erasure for privacy, and it is a policy question for us, not a defect in gbrain. | `src/core/facts/purge.ts:1-19` |

Three things that look like compression and are not:

- **gbrain's `consolidate` phase** promotes one fact's own text and keeps every source fact with a pointer to what it was folded into (`src/core/cycle/phases/consolidate.ts:6-21`, `:284-287`). That is selection, not rewriting, and it is a usable pattern for MSA's dream cycle. The same file notes a planned change to have a model rewrite the cluster; that version would conflict.
- **Pointers with a one-line synopsis** (idea 7) compress nothing as long as the full file is one read away and the agent does read it.
- **Decay by fact kind** is computed at read time (`src/core/facts/decay.ts:48-60`) and removes nothing from the store. It is a ranking choice, the same kind MS4CC already makes, and idea 9 proposes it. The conflict is in the transcript rows above: the facts outlive the transcript they were extracted from.

The `timeline` half of a gbrain page is the part that matches the rule: append-only, dated, sourced. If the summary idea is ever wanted, the compatible form is the one MS4CC's design doc already chose: an authored description that sits beside the full text and never replaces it.

## 5. Known problems on our side: what gbrain's approach would and would not fix

The eight classes below were measured on a production MindStone agent on 2026-09-25, on the legacy MindStone codebase. MSA issue #59 and MS4CC issue #118 port them; MSA #57 and MS4CC #111 audit 19 classes in all. All four issues are open.

| Class | MSA at `8c0242f` | MS4CC at `f84ecaf` | Would gbrain's approach fix it? |
| --- | --- | --- | --- |
| **Dead recall** | Ingestion fixed (per-turn indexing, `gw/index.ts:739-756`). Still silent: a failed embed in search (`sqlite-memory.ts:1275-1277`) and no event on zero hits. Whether recall reaches the model in `routing.mode: "pi"` is open (#55). | A failed query embed gives a zero vector, which scores exactly at the 0.50 floor (#111). An empty block looks like "nothing relevant". | **Detects, does not prevent.** Degradation stamps and health checks (idea 1) make it visible the same turn. |
| **A bigint that killed reranking** | No bigint. The same shape remains: a stored value read as a default. Recency is 0 for every transcript hit and half-life is always 30 (`sqlite-memory.ts:1378-1393`; `file-memory.ts:117-129`). | Not present in this form. | **No.** Nothing in gbrain targets this. A replayed benchmark (idea 3) would have shown the ranking change. |
| **A polluted query envelope** | Still present for email: headers and a thread digest are part of the query (`gw/connectors/email.ts:239-247`; `recall.ts:61-63`). | Still present: the raw prompt, including harness wrappers, is the query (`user_prompt_submit.py:127-143`; PR #114 open). | **Sidesteps it.** gbrain's agent writes the query, and its automatic path mostly matches names, not raw text. It has no stripping code we could borrow. |
| **Phantom hits** | A deleted or renamed memory file keeps its chunks until manual maintenance. | A deleted memory stays recallable (`session_end.py:397-399`); a rename leaves the old chunks (#125). | **Yes** (idea 4). |
| **FTS freshness bias** | No FTS. Two analogues: candidates are capped at 5,000 rows, memory files first and then the newest transcript chunks (`sqlite-memory.ts:1319-1321`, #74), and not-yet-embedded chunks are scored on a different scale. | No keyword arm. | **Partly.** Rank fusion (idea 5) removes the mixed scale. It does nothing for the 5,000 cap. A keyword arm added carelessly would bring the original defect back. |
| **Journals embedded only on restart** | Not present: journals are picked up on every turn's index update. A journal written with no later turn waits for a manual backfill. | Present by design: nothing is embedded until `/checkpoint` (`session_end.py:660-684`). | **Yes for visibility** (idea 2): the backlog has an age and a warning. It does not decide how often to embed. |
| **A miscalibrated estimator** | Still present. Tool-call arguments, thinking and images count as zero (`gw/pi-context-pruning-extension.ts:97-109`); the core estimate is characters divided by 4 (`core/context/window.ts:66-71`). | The handoff uses the provider's own usage figures, so the estimate is sound; the window size is a fixed default (#124). | **No.** gbrain also estimates at characters divided by 4 (`src/core/import-file.ts:708`) and does not manage a context window. |
| **Re-pruning on every call** | Still present: pruning runs on every context event with no stored cut point (`pi-context-pruning-extension.ts:244-247`). | Not applicable; Claude Code owns the window. | **No.** Out of gbrain's scope. |

Summary: gbrain's approach helps with four of the eight (dead recall, phantom hits, journals, and half of the freshness class), sidesteps one, and has no direct fix for three. Two of those three (the estimator and re-pruning) concern the live context window, which gbrain does not manage. The third (a stored value read back as a default) is a ranking defect that a replayed benchmark would detect but not prevent.

## 6. Questions for the discussion

1. **Which comes first, the benchmark or the fixes?** Ideas 1, 2 and 4 are safe without a benchmark. Idea 5 is not.
2. **Should MSA's docs be brought in line with its code before new memory work?** The dream cycle, the retrieval cues and the LOG tail are documented and not built.
3. **Is the indexed transcript's loss of tool content acceptable in MS4CC?** It is a narrower reduction than gbrain's (thinking text is kept), applied to the index and not to the archive.
4. **Does the dream cycle, when built in MSA, write new memory or only link and rank what exists?** gbrain's `consolidate` shows the second is workable.
5. **What is the owner-confirmed signal?** Both a decay floor and an honest `prevented` counter need one.
