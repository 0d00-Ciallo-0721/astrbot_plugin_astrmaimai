# AstrMai Round-1 Full Audit Report

> Generated: 2026-06-30  
> Repository: project root
> Total source files under `astrmai/`: 299 (excluding `__pycache__` and `venv`)  
> 7-dimension audit across 14 subdirectories

---

## 🟢 COMPLETED Modules (agents returned)

### ✅ astrmai/memory/ — 16 findings
### ✅ astrmai/learning/ — 14 findings  
### ✅ astrmai/infrastructure/ — 17 findings
### ✅ astrmai/conversation/ — 17 findings
### ⏳ astrmai/app/ — agent running
### ⏳ astrmai/multimodal+presentation+proactive+shared+state+workmode+webui — agent running

---

## 🔴 P0 — Production Blockers (Data Loss / Crash)

### P0.1 — `self` reference in `@staticmethod` → NameError crash
```
FILE: astrmai/memory/retrieval/react_retriever.py:304
DIMENSION: 2 (Async Safety)
DESCRIPTION: `_safe_parse_json` is `@staticmethod` but references `self._extract_braced_json(raw)`.
When the LLM returns a string response, this raises `NameError`, crashing the ReAct retrieval loop.
CODE:
    @staticmethod
    def _safe_parse_json(raw) -> Dict:
        if isinstance(raw, str):
            chunk = self._extract_braced_json(raw)  # ← NameError
```

### P0.2 — Missing `import json` → NameError in dream generation
```
FILE: astrmai/memory/dream/dream_generator.py:159
DIMENSION: 3 (State Machine)
DESCRIPTION: `build_maintenance_result()` calls `json.loads(payload)` but `import json` is missing.
Any dream log containing `[fact]` lines causes `NameError`. Caught by bare except → silent data loss.
CODE:
    elif normalized.startswith("[fact]"):
        payload = normalized.removeprefix("[fact]").strip()
        item = json.loads(payload)  # ← NameError, swallowed by except Exception on line 162
```

### P0.3 — Clock source mismatch: `time.time()` vs `monotonic()` → timeout miscalibration
```
FILE: astrmai/memory/dream/dream_agent.py:74,90
DIMENSION: 2 (Async Safety)
DESCRIPTION: `start_time` set with `monotonic()` at line 74, but timeout check at line 90 uses `time.time()`.
NTP adjustment can cause early/late exit. System suspend causes miscalibrated timeout.
CODE:
    start_time = monotonic()          # line 74
    if time.time() - start_time > self.TIMEOUT_SEC:  # line 90 — MIXED clocks
```

### P0.4 — Batch removed BEFORE LLM enrichment → data loss on failure
```
FILE: astrmai/learning/review/reflector.py:79-82
DIMENSION: 4 (Data Consistency)
DESCRIPTION: Batch of pending reflections is sliced OUT of the queue BEFORE the LLM enrichment call.
If LLM fails, those reflections are permanently lost — no retry, no rollback.
CODE:
    batch = self._pending_reflections[:8]
    self._pending_reflections = self._pending_reflections[8:]  # ← REMOVED before LLM
    # line 108: result = await self.gateway.call_data_process_task(...)  # LLM may fail
```

### P0.5 — Dead branch `elif False:` + `return` makes fallback weight adjustment unreachable
```
FILE: astrmai/learning/review/reflector.py:249-261
DIMENSION: 3 (State Machine)
DESCRIPTION: `_adjust_canonical_pattern_weight` has `elif False:` and `else: return` that make all per-pattern
inline weight adjustment code (lines 254-261) dead. When `pattern_id` is empty, adjustments silently fail.
CODE:
    elif False:           # ← dead branch
        return
    else:
        return             # ← makes lines 254-261 unreachable
        for p in patterns: # DEAD CODE
```

### P0.6 — Raw sqlite3 bypasses lock → concurrent write conflicts
```
FILE: astrmai/infrastructure/persistence/database_jargon.py:69
DIMENSION: 4 (Data Consistency)
DESCRIPTION: _save_jargon_to_canonical_sync opens raw sqlite3.Connection (own lock), bypassing both the
asyncio.Lock and SQLModel Session. Concurrent writes can hit SQLITE_BUSY or interleave.
CODE:
    with sqlite3.connect(self.db_path) as conn:  # ← no lock, independent connection
        self._ensure_canonical_jargon_schema_sync(conn)
        cursor = conn.execute(...)
```

### P0.7 — Async DB init fires-and-forgets → "no such table" on first read
```
FILE: astrmai/infrastructure/persistence/persistence_schema.py:138-144
DIMENSION: 4 (Data Consistency) / 7 (Lifecycle)
DESCRIPTION: In async context, _schedule_init_db fires `safe_create_task(self._init_db())` and returns immediately.
Any DB read before _init_db creates tables will get "no such table" errors. No readiness signal.
CODE:
    self._init_task = safe_create_task(self._init_db())  # fire-and-forget!
```

### P0.8 — Migration tracking drift: sync init tracks via PRAGMA user_version, async init doesn't
```
FILE: astrmai/infrastructure/persistence/persistence_schema.py:55-76 vs 287-333
DIMENSION: 4 (Data Consistency)
DESCRIPTION: _init_db_sync() calls _run_migrations() (uses PRAGMA user_version). _init_db() (async) does NOT —
unconditionally applies ALTER TABLE patches. On reload, both paths can run, causing drift and duplicate columns.
```

### P0.9 — EventBus fire-and-forget dispatch tasks never tracked → unbounded task leak
```
FILE: astrmai/infrastructure/runtime/event_bus.py:149-157
DIMENSION: 7 (Resource/Lifecycle)
DESCRIPTION: Worker loops dispatch subscribers via `safe_create_task()`. Tasks NOT added to _background_tasks.
If subscribers are slow (LLM/DB), tasks accumulate without bound. Shutdown cancels worker loops but leaves
dispatched callback tasks orphaned.
CODE:
    task = safe_create_task(callback(data))  # ← NOT tracked
```

---

## 🟠 P1 — High Priority (Behavior Error / Race / Leak)

### P1.1 — asyncio.create_task without exception callback → zombie exceptions lost
```
FILE: astrmai/memory/persona/persona_summarizer.py:196,271
DIMENSION: 7 (Resource/Lifecycle)
DESCRIPTION: Two `asyncio.create_task()` calls without `add_done_callback()`. If coroutine fails to start
or exception occurs before try/except, error is silently lost. Contrast with lifecycle.py:25.
```

### P1.2 — LLM call missing lane_key → rate-limit bypass
```
FILE: astrmai/memory/services/memory_retrieval_service.py:383
DIMENSION: 5 (LLM Call Chain)
DESCRIPTION: `_rewrite_queries()` calls `gateway.call_data_process_task()` without `lane_key`, `base_origin`,
or `template_envelope`. Bypasses lane-based concurrency control — potential provider flood under load.
```

### P1.3 — Unbounded `_session_locks` dict → memory leak
```
FILE: astrmai/memory/services/v2_store.py:60-61
DIMENSION: 7 (Resource/Lifecycle)
DESCRIPTION: Every new session_id creates permanent `asyncio.Lock`. Never cleaned up.
CODE: self._session_locks: dict[str, asyncio.Lock] = {}
```

### P1.4 — Unbounded `_cognitive_feedback_cache` dict → memory leak
```
FILE: astrmai/memory/services/memory_engine.py:87-88
DIMENSION: 7 (Resource/Lifecycle)
DESCRIPTION: Per-chat cache list is capped at 32 items, but the outer dict grows with every new chat_id forever.
CODE: self._cognitive_feedback_cache: dict[str, list[CognitiveFeedbackSignal]] = {}
```

### P1.5 — Compound memory leak: 4 unbounded dicts in MemoryTurnPipeline
```
FILE: astrmai/memory/services/memory_turn_pipeline.py:38-44
DIMENSION: 7 (Resource/Lifecycle)
DESCRIPTION: `_session_history_buffer`, `_memory_locks`, `_worker_tasks`, `_worker_queues` all grow per-chat
without cleanup for inactive sessions.
```

### P1.6 — HybridRetriever.add_memory returns None silently → write failure
```
FILE: astrmai/memory/retrieval/hybrid_retriever.py:27-31
DIMENSION: 3 (State Machine)
DESCRIPTION: When `self.vector` is None/offline, `add_memory` returns `None`. Caller at
`memory_index_projector.py:55` may treat None as valid doc_id → projection silently fails.
```

### P1.7 — TOCTOU race in reflect_tracker.try_consume_feedback → double-processing
```
FILE: astrmai/learning/review/reflect_tracker.py:70-134
DIMENSION: 4 (Data Consistency)
DESCRIPTION: `_pending` dict read under `_lock`, then lock released during LLM call (~1-5s) and DB update.
Lock re-acquired only at pop. Two concurrent calls read same candidates → both process → double-review.
```

### P1.8 — get_unsent_requests marks ALL as sent, not just returned → orphaned entries
```
FILE: astrmai/learning/review/reflect_tracker.py:55-58
DIMENSION: 3 (State Machine)
DESCRIPTION: Every `get_unsent_requests` call sets `item["sent"] = True` on ALL _pending entries, not just
those returned. Undelivered items become permanent orphans — never returned again.
```

### P1.9 — _pending_reflections list grows unbounded
```
FILE: astrmai/learning/review/reflector.py:33
DIMENSION: 7 (Resource/Lifecycle)
DESCRIPTION: `_pending_reflections: List[Dict] = []` appends on every bot reply, removes max 8 at a time
only when governance loop runs. If governance stopped, grows indefinitely.
```

### P1.10 — asyncio.Event affection_changed never cleared → permanently stale
```
FILE: astrmai/infrastructure/runtime/event_bus.py:57-66
DIMENSION: 1 (Event Flow)
DESCRIPTION: `trigger_affection_change()` calls `self.affection_changed.set()` but NEVER clears it.
After first trigger, `await self.affection_changed.wait()` returns immediately forever — no-op.
```

### P1.11 — _lane_locks dict grows unboundedly
```
FILE: astrmai/infrastructure/runtime/lane_manager.py:89-93
DIMENSION: 7 (Resource/Lifecycle)
DESCRIPTION: New `asyncio.Lock` per unique `lane_umo`. Locks never cleaned up. Each prompt version change
creates new lane — unbounded growth over days.
```

### P1.12 — ChatRuntimeCoordinator._states dict never cleaned
```
FILE: astrmai/infrastructure/runtime/chat_runtime_coordinator.py:26-35
DIMENSION: 7 (Resource/Lifecycle)
DESCRIPTION: Every chat_id gets permanent `ChatRuntimeState` with locks and lists. Hundreds of groups →
hundreds of objects accumulated forever.
```

### P1.13 — PersistenceManager.dispose() never called
```
FILE: astrmai/infrastructure/persistence/persistence_manager.py:54-55
DIMENSION: 7 (Resource/Lifecycle)
DESCRIPTION: `dispose()` releases SQLAlchemy engine pool but is NEVER called. On plugin reload, old engine
pool leaks connections/file handles.
```

### P1.14 — get_chat_state reads without _db_lock while writes use it → consistency risk
```
FILE: astrmai/infrastructure/persistence/database_service.py:189
DIMENSION: 4 (Data Consistency)
DESCRIPTION: `get_chat_state` opens raw sqlite3 without `_db_lock`. Writes use `with_lock=True`.
In non-WAL mode, concurrent read+write → SQLITE_BUSY or stale data. WAL mode NOT explicitly enabled.
```

---

## 🟡 P2 — Medium Priority (Edge Cases / Performance / Robustness)

### P2.1 — recall() hardcodes exclude_kinds, ignores caller intent
```
FILE: astrmai/memory/services/memory_engine.py:537-558
DIMENSION: 4 (Data Consistency)
DESCRIPTION: `recall()` hardcodes `exclude_kinds=["feedback"]`. Caller cannot override.
Signatures should expose the parameter or document the constraint.
```

### P2.2 — Greedy regex in JSON parse may capture too much
```
FILE: astrmai/memory/services/memory_processor.py:195
DIMENSION: 5 (LLM Call Chain)
DESCRIPTION: `re.search(r'\{.*\}', text, re.DOTALL)` is greedy — matches from first `{` to LAST `}`.
If LLM returns `{"a":1} ... {"b":2}`, captures a malformed blob. Use brace-counting (as react_retriever does).
```

### P2.3 — ExpressionAutoCheckTask._last_run_at dict unbounded
```
FILE: astrmai/learning/review/expression_auto_check_task.py:34
DIMENSION: 7 (Resource/Lifecycle)
DESCRIPTION: Same issue in `jargon_auto_check_task.py:31`. Grows per unique scope forever.
```

### P2.4 — Cancelled tasks logged as errors in evolution_manager._handle_task_result
```
FILE: astrmai/learning/evolution_manager.py:69-76
DIMENSION: 2 (Async Safety)
DESCRIPTION: `task.exception()` returns CancelledError (doesn't raise it), so `exc` is truthy
and `logger.error()` fires for every cancelled task. `except asyncio.CancelledError` never catches this.
```

### P2.5 — MessageRecorder._windows dict unbounded
```
FILE: astrmai/learning/logging/message_recorder.py:19
DIMENSION: 7 (Resource/Lifecycle)
DESCRIPTION: Never pruned. `clear()` exists but never called from anywhere.
```

### P2.6 — save_pattern silently drops canonical write in async context
```
FILE: astrmai/infrastructure/persistence/database_review.py:72-77
DIMENSION: 2 (Async Safety)
DESCRIPTION: `save_pattern()` checks `asyncio.get_running_loop()` — if succeeds (async context), canonical
write is SKIPPED entirely. Patterns saved from async code silently lose canonical memory entries.
```

### P2.7 — ModelRouter._pools accumulates dead model entries forever
```
FILE: astrmai/infrastructure/gateway/model_router.py:97-101
DIMENSION: 7 (Resource/Lifecycle)
DESCRIPTION: ModelState entries for removed/renamed models persist in _pools forever.
```

### P2.8 — ensure_lane can create duplicate conversations under concurrency
```
FILE: astrmai/infrastructure/runtime/lane_storage.py:11-116
DIMENSION: 3 (State Machine)
DESCRIPTION: Rotation check and new_conversation happen before lane_lock acquisition. Two concurrent
ensure_lane calls can both create new conversations for the same lane.
```

### P2.9 — persona_cache writes not atomic
```
FILE: astrmai/infrastructure/persistence/persona_cache.py:24-26
DIMENSION: 4 (Data Consistency)
DESCRIPTION: `json.dump` writes directly to file. Crash mid-write → corrupted cache.
Contrast with `raw_trace_store.py` which uses `tempfile + os.replace` for atomicity.
```

---

## 🟢 P3 — Low Priority (Code Quality / Defensive)

### P3.1 — logger.exception() with redundant exc_info=True
```
FILE: astrmai/memory/persona/persona_summarizer.py:458,496,529,562,596,626,657,691
DIMENSION: 2
DESCRIPTION: logger.exception() already includes exc_info=True by default.
```

### P3.2 — Local `import json` inside TopicSummarizer._parse_summaries
```
FILE: astrmai/memory/services/topic_summarizer.py:366
DIMENSION: 5
DESCRIPTION: Should be module-level import.
```

### P3.3 — RRFFusion "first wins" metadata loses vector metadata when BM25 matches
```
FILE: astrmai/memory/utils.py:76-82
DIMENSION: 4
DESCRIPTION: BM25 metadata wins when doc appears in both lists. Vector's richer metadata optionally lost.
```

### P3.4 — __import__("re") called dynamically instead of module-level import
```
FILE: astrmai/learning/evolution_manager.py:293
DIMENSION: 5
DESCRIPTION: `__import__("re").search(...)` on every invocation. Should be `import re` at module level.
```

### P3.5 — _extract_pattern_id type change monkey-patch: Optional[int] → Optional[str]
```
FILE: astrmai/learning/review/reflect_tracker.py:171-192
DIMENSION: 6
DESCRIPTION: `staticmethod` replacement changes return type from `int` to `str`. Annotation is misleading.
```

### P3.6 — Duplicate normalization logic in evolution_manager and enrichers
```
FILE: astrmai/learning/evolution_manager.py:119-134 and expression_pattern_enricher.py:21
DIMENSION: 4
DESCRIPTION: `_normalize_pattern_review_status` and `_normalize_jargon_review_status` duplicated across files.
```

### P3.7 — tool_chat_in_lane_result duplicates ~200 lines from _elastic_call_result
```
FILE: astrmai/infrastructure/gateway/gateway_lane.py:413-691
DIMENSION: 5
DESCRIPTION: ~200 lines duplicated. Bug fixes in one path won't propagate to the other.
```

### P3.8 — EventBus._worker_health_check spawns workers that may not be tracked yet on shutdown
```
FILE: astrmai/infrastructure/runtime/event_bus.py:169-179
DIMENSION: 7
DESCRIPTION: `_workers_started` never set False except in `stop()`. Newly spawned workers may miss shutdown.
```

---

## 📊 Preliminary Summary (3 of 6 modules analyzed)

| Severity | Count | Impact |
|----------|-------|--------|
| **P0** | 9 | Crash (NameError × 2), data loss (×3), db corruption (×3), task leak (×1) |
| **P1** | 14 | Memory leaks (×6), race conditions (×2), silent failures (×2), stale event (×1), rate bypass (×1), db consistency (×1), undisposed (×1) |
| **P2** | 9 | Unbounded growth (×2), stale data (×2), concurrent safety (×1), log noise (×1), robustness (×3) |
| **P3** | 8 | Code quality / hygiene only |

**Awaiting**: app/, conversation/, multimodal+presentation+proactive+shared+state+workmode+webui/ audit results

---

## 🗂️ Appendix: Dimension Coverage Map

| Dimension | P0 | P1 | P2 | P3 | Total |
|-----------|----|----|----|----|-------|
| 1. Event Flow | - | P1.10 | - | - | 1 |
| 2. Async Safety | P0.1, P0.3 | - | P2.4, P2.6 | P3.1 | 5 |
| 3. State Machine | P0.2, P0.5 | P1.6, P1.8 | P2.8 | - | 5 |
| 4. Data Consistency | P0.4, P0.6, P0.7, P0.8 | P1.7, P1.14 | P2.1, P2.9 | P3.3, P3.6 | 10 |
| 5. LLM Call Chain | - | P1.2 | P2.2 | P3.2, P3.4, P3.7 | 5 |
| 6. AstrBot Compat | - | - | - | P3.5 | 1 |
| 7. Resource/Lifecycle | P0.9 | P1.1, P1.3, P1.4, P1.5, P1.9, P1.11, P1.12, P1.13 | P2.3, P2.5, P2.7 | P3.8 | 9 |

---

*Report: Round-1 / Phase 1. To be updated when remaining 3 module audits complete.*
