# ROUND 06 REVIEW: 记忆检索、RAG 与升级迁移

**Review Date**: 2026-07-14
**Reviewer**: codex (automated)
**Round File**: `ROUND_06_MEMORY_RETRIEVAL_MIGRATION.md`
**Source Files Inspected**: 10 files, all fixes independently verified against actual code.

## Summary

| Fix ID  | Status   | Evidence |
|---------|----------|----------|
| R06-01  | VERIFIED | `_normalize_fts_scores` correctly inverts BM25 (lower-is-better → [0,1]), primary sort key is `relevance_score` |
| R06-02  | VERIFIED | `_retrieve_once` passes `track_access=False`; `_finalize_retrieval` only calls `finalize_access` on selected |
| R06-03  | VERIFIED | `_ensure_fts_projection` detects inconsistency and rebuilds entire FTS from canonical_memories at init |
| R06-04  | VERIFIED | `_resolve_search_limit` uses candidate_limit directly (no *8); return_limit respects it |
| R06-05  | VERIFIED | RRF scores normalized to [0,1] in `_apply_weighting`; fusion weights (0.70 query, 0.20 static) |
| R06-06  | VERIFIED | `refresh_config` updates all services; only resets vector state on embedding model change |
| R06-07  | PARTIAL  | Main path uses `sender_id` in dedup key; legacy fallback still uses `chat_id` |
| R06-08  | VERIFIED | `stop()` calls `flush_pending_sessions(force=True)` for ALL non-empty buffers before worker cancel |
| R06-09  | VERIFIED | `import_legacy_documents` reads `legacy_db_path`, records `"failed"` on missing source/table |

**Verdict**: 7 VERIFIED, 1 PARTIAL (R06-07 falls short in legacy fallback path), 0 MISSING.

---

## Per-Fix Details

### R06-01 / P1: Canonical FTS Negative BM25 → 1.0

**Fix**: Keep FTS5 lower-is-better monotonic order, normalize to higher-is-better; weighted sort must not flatten lexical rank.

**Evidence (VERIFIED)**:

- `v2_store.py:70-80` — `_normalize_fts_scores()`:
  ```python
  best = min(raw_scores)           # lowest BM25 = best
  worst = max(raw_scores)
  scale = worst - best
  return [min(max((worst - score) / scale, 0.0), 1.0) for score in raw_scores]
  ```
  The lowest (best) BM25 score maps to 1.0; the highest (worst) maps to 0.0. Different BM25 scores produce proportional normalized scores.

- `v2_store.py:1095-1105` — compound sort key:
  ```python
  key=lambda item: (
      item.relevance_score,       # primary: normalized FTS score
      item.relevance_score * search_weight + ...  # tiebreaker
  ),
  reverse=True,
  ```
  `relevance_score` is the primary sort key, preserving lexical rank. The composite is only a tiebreaker.

---

### R06-02 / P1: Deep Candidate Pool — Access Marking on Final Only

**Fix**: Candidate collection and rerank must be read-only; only final returned/injected items get access tracking and stale restoration.

**Evidence (VERIFIED)**:

- `memory_retrieval_service.py:484` — `_retrieve_once()`:
  ```python
  canonical_task = self.store.search(
      ...
      track_access=False,   # <--- read-only during collection
  )
  ```

- `memory_retrieval_service.py:366-378` — `_finalize_retrieval()`:
  ```python
  selected = self._finalize_candidates(query, candidates)  # selects top-k
  await finalize_access(selected, allow_stale=query.allow_stale)  # only on selected
  ```

- `v2_store.py:1108-1109` — `search()` only calls `finalize_access` on returned slice:
  ```python
  selected = candidates[:return_limit]
  if selected and track_access:
      await self.finalize_access(selected, allow_stale=allow_stale)
  ```

- `memory_scoring.py:76-94` — `rerank_candidates()` only modifies `relevance_score`, never access_count/status.

---

### R06-03 / P1: Legacy Canonical Migration — Empty FTS Projection

**Fix**: Project all canonical rows within migration transaction; detect projection incompleteness and rebuild.

**Evidence (VERIFIED)**:

- `v2_store.py:306-368` — `_ensure_fts_projection()`:
  - Counts `canonical_memories` active rows (line 307)
  - Counts `canonical_fts` joined rows (line 309-317)
  - Counts invalid (line 318-326), missing (line 327-335), duplicate (line 336-347) entries
  - If ANY inconsistency: `DELETE FROM canonical_fts` then full rebuild from `canonical_memories WHERE status='active'` (lines 360-368)

- `v2_store.py:207-304` — `initialize()` order:
  1. `_backup_legacy_once()`
  2. `_migrate_from_legacy_db()` — imports old data
  3. Creates `canonical_fts` virtual table
  4. Calls `_ensure_fts_projection(db)` — **after** migration, guarantees FTS complete

---

### R06-04 / P2: Adaptive candidate_limit Not Reaching Fusion

**Fix**: `candidate_limit` controls coarse recall; `top_k` only for dedup/rerank injection; no `*8` amplification when candidate_limit set.

**Evidence (VERIFIED)**:

- `v2_store.py:63-68` — `_resolve_search_limit()`:
  ```python
  if candidate_limit is None:
      return max(result_limit * 8, 20)      # *8 only when no explicit limit
  return max(int(candidate_limit), result_limit, 1)   # direct use
  ```

- `v2_store.py:1006-1008` — `search()` uses `search_limit` from above:
  ```python
  search_limit = self._resolve_search_limit(result_limit, candidate_limit)
  # FTS query: LIMIT search_limit  (line 1045)
  # Fallback query: LIMIT search_limit  (line 1069)
  ```

- `v2_store.py:1106` — return respects candidate_limit:
  ```python
  return_limit = search_limit if candidate_limit is not None else result_limit
  ```

- `memory_retrieval_service.py:480` — passes explicit limit to store:
  ```python
  candidate_limit=self._explicit_candidate_limit(query)
  ```

- Deep path (`retrieve_deep`, line 336) uses `_explicit_candidate_limit(query)` as primary; falls back to `top_k * pool_factor` only when unset.

---

### R06-05 / P2: Hybrid RRF Scores Not Normalized Before Fusion

**Fix**: Unify score scale; prevent static importance/confidence from drowning query relevance.

**Evidence (VERIFIED)**:

- `hybrid_retriever.py:86-93` — `_apply_weighting()` normalizes RRF to [0,1]:
  ```python
  raw_scores = [max(float(result.score or 0.0), 0.0) for result in results]
  high = max(raw_scores, default=0.0)
  if high > 0.0:
      normalized_scores = [score / high for score in raw_scores]
  # Then: r.score = relevance * importance_factor * decay
  ```
  RRF scores (tiny, like 1/61) are divided by max → [0,1] before fusion.

- `v2_store.py:70-80` — `_normalize_fts_scores()` also produces [0,1].

- `memory_retrieval_service.py:608-622` — fusion weights:
  ```python
  canon_weighted = canon * 0.25              # FTS normalized [0,1]
  hybrid_weighted = hybrid_score * 0.45       # RRF normalized [0,1]
  importance_weighted = importance * 0.15     # static [0,1]
  confidence_weighted = confidence * 0.05     # static [0,1]
  ```
  Query relevance total weight: **0.70** vs. static components: **0.20**. Strong lexical/vector matches reliably outrank high-importance weak matches.

---

### R06-06 / P2: Temporal/Hybrid Hot Settings Bound to Old Runtime Object

**Fix**: Refresh scoring and retriever config independently; embedding rebuild must stay separate.

**Evidence (VERIFIED)**:

- `memory_engine.py:103-138` — `refresh_config()`:
  ```python
  self.retrieval_service.refresh_config(config)    # → updates scoring
  self.retriever.refresh_config(config)             # → updates time_decay_rate source
  # ... pipeline, instant_gate, summarizer refreshed ...
  self.embedding_models = self._configured_embedding_models(config)
  if self.embedding_models != old_embedding_models:  # only on MODEL change:
      self.faiss_db = None                            # reset vectors
  ```

- `memory_retrieval_service.py:29-30` — `refresh_config()`:
  ```python
  self.scoring = scoring_from_config(config)
  ```

- `hybrid_retriever.py:85` — `_apply_weighting()` reads from config at runtime:
  ```python
  time_decay_rate = getattr(self.config.memory, 'time_decay_rate', 0.01) if self.config else 0.01
  ```

---

### R06-07 / P1: Group Chat Memory Loses sender_id

**Fix**: Group fact subjects must use real sender; chat ID only for session scope.

**Evidence (PARTIAL — fallback gap)**:

- **Main path (VERIFIED)**:
  - `reply_post_send.py:119` — passes `sender_id=str(event.get_sender_id())`
  - `memory_turn_pipeline.py:114` — stores `sender_id` in `CommittedMemoryTurn`
  - `instant_memory_gate.py:115` — `subject_id=str(turn.sender_id or turn.chat_id)`
  - `instant_memory_gate.py:152` — authority dedup key: `f"{subject_id}:{entity}:{attribute}"`
    → `senderA:food:preference` ≠ `senderB:food:preference` ✅

- **Legacy fallback (PARTIAL — the gap)**:
  - `instant_memory_gate.py:214`:
    ```python
    dedup_key=f"{fallback_prefix}:{turn.chat_id}:{category}:{extracted_fact[:60]}"
    ```
    Uses `chat_id` instead of `sender_id` — cross-member collision possible when claim extraction fails.
  - The fallback runs when `MemoryClaimExtractor.extract()` raises or returns no claims.
  - Mitigation: fallback path is secondary; primary authority_override path is correct. Impact depends on claim extractor reliability.

---

### R06-08 / P1: Shutdown Discards Below-Threshold Committed Turns

**Fix**: Drain/flush non-empty session buffers before stop; don't clear data before flushing.

**Evidence (VERIFIED)**:

- `memory_turn_pipeline.py:66-80` — `stop()`:
  ```python
  self.event_bus.unsubscribe(...)          # 1. block new events
  await self.flush_pending_sessions()       # 2. DRAIN all buffers FIRST
  self._running = False                     # 3. then stop
  # 4. cancel workers last
  ```

- `memory_turn_pipeline.py:82-95` — `flush_pending_sessions()`:
  ```python
  chat_ids = [
      chat_id for chat_id, session_data in list(self._session_history_buffer.items())
      if list((session_data or {}).get("buffer", []) or [])
  ]
  for chat_id in chat_ids:
      results[chat_id] = await self.run_maintenance_for_session(chat_id, force=True)
  ```
  Iterates ALL sessions with non-empty buffers, `force=True` bypasses threshold check.

- `lifecycle.py:208-211` — calls `pipeline.stop()` as FIRST shutdown step:
  ```python
  memory_pipeline = getattr(self.runtime.memory_engine, "memory_pipeline", None)
  if memory_pipeline:
      await memory_pipeline.stop()
  ```

---

### R06-09 / P1: Legacy Documents Import Opens V2 DB, Marks Missing Tables as Done

**Fix**: Always read from `legacy_db_path`; only write `"applied"` marker on real source traversal completion.

**Evidence (VERIFIED)**:

- `v2_store.py:1855` — reads from legacy source:
  ```python
  source_path = str(self.legacy_db_path or self.db_path or "").strip()
  ```

- `v2_store.py:1856-1858` — source unavailable → `"failed"`:
  ```python
  if not source_path or not os.path.isfile(source_path):
      await self.record_migration(version, status="failed", detail="legacy documents source unavailable")
      return 0
  ```

- `v2_store.py:1864-1866` — table missing → `"failed"`:
  ```python
  if text_col not in columns or "metadata" not in columns:
      await self.record_migration(version, status="failed", detail="documents table unavailable")
      return 0
  ```

- `v2_store.py:1903` — only after successful import → `"applied"`:
  ```python
  await self.record_migration(version, status="applied", detail=f"imported={imported}")
  ```

- `v2_store.py:1909-1955` — `import_persona_cache`: marks `"applied"` when cache file missing (correct — terminal state, nothing to retry).

---

## Action Items

1. **R06-07**: Fix legacy fallback dedup key in `instant_memory_gate.py:214` to use `turn.sender_id` instead of `turn.chat_id`.
   Suggested: `dedup_key=f"{fallback_prefix}:{turn.sender_id}:{category}:{str(extracted_fact or raw_text or '')[:60]}"`

## Optional Future Considerations

- R06-01: The compound sort key at `v2_store.py:1095-1105` has variable weight ranges — `relevance_score` is [0,1] but the composite spans [0, ~0.9]. Since `relevance_score` is the first key, lexicographic order is preserved; the composite only acts as tiebreaker.
