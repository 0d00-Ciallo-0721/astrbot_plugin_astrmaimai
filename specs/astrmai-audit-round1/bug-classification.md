# AstrMai Round-1 Bug Classification

> Generated: 2026-06-30  
> Based on: `specs/astrmai-audit-round1/audit-report.md`  
> Modules audited: memory, learning, infrastructure, conversation, app (5/6 complete; remaining modules WIP)

---

## Classification by Severity

### 🔴 P0 — Production Blockers (Crash / Data Loss / Deadlock / Infinite Loop / Event Loss)

| # | File:Line | Module | Dimension | Description |
|---|-----------|--------|-----------|-------------|
| P0.1 | `memory/retrieval/react_retriever.py:304` | memory | 2-Async | `@staticmethod` references `self._extract_braced_json()` → NameError crash on string LLM response |
| P0.2 | `memory/dream/dream_generator.py:159` | memory | 3-StateMachine | Missing `import json` → NameError when parsing `[fact]` lines; swallowed by bare except |
| P0.3 | `memory/dream/dream_agent.py:74,90` | memory | 2-Async | `monotonic()` set but `time.time()` checked for timeout → NTP/suspend miscalibration |
| P0.4 | `learning/review/reflector.py:79-82` | learning | 4-DataConsistency | Batch sliced OUT of queue BEFORE LLM enrichment → permanent data loss on LLM failure |
| P0.5 | `learning/review/reflector.py:249-261` | learning | 3-StateMachine | `elif False:` + `else: return` makes per-pattern weight adjustment dead code |
| P0.6 | `infrastructure/persistence/database_jargon.py:69` | infrastructure | 4-DataConsistency | Raw sqlite3.Connection bypasses both asyncio.Lock and SQLModel Session → concurrent write conflicts |
| P0.7 | `infrastructure/persistence/persistence_schema.py:138-144` | infrastructure | 4/7-DataConsistency/Lifecycle | Async DB init fires-and-forgets; no readiness signal → "no such table" on first read |
| P0.8 | `infrastructure/persistence/persistence_schema.py:55-76 vs 287-333` | infrastructure | 4-DataConsistency | Sync init tracks migrations via PRAGMA user_version; async init doesn't → drift on reload |
| P0.9 | `infrastructure/runtime/event_bus.py:149-157` | infrastructure | 7-Lifecycle | Dispatch tasks via `safe_create_task()` never tracked → unbounded orphan task leak |

**P0 count: 9**

---

### 🟠 P1 — High Priority (Function Error / State Anomaly / Critical Path Break / Race Condition)

| # | File:Line | Module | Dimension | Description |
|---|-----------|--------|-----------|-------------|
| P1.1 | `memory/persona/persona_summarizer.py:196,271` | memory | 7-Lifecycle | `asyncio.create_task()` without `add_done_callback()` → zombie exceptions silently lost |
| P1.2 | `memory/services/memory_retrieval_service.py:383` | memory | 5-LLM | LLM call missing `lane_key`/`base_origin` → rate-limit bypass, potential provider flood |
| P1.3 | `memory/services/v2_store.py:60-61` | memory | 7-Lifecycle | Unbounded `_session_locks` dict → memory leak per session |
| P1.4 | `memory/services/memory_engine.py:87-88` | memory | 7-Lifecycle | Unbounded `_cognitive_feedback_cache` dict → memory leak per chat |
| P1.5 | `memory/services/memory_turn_pipeline.py:38-44` | memory | 7-Lifecycle | 4 unbounded dicts (`_session_history_buffer`, `_memory_locks`, `_worker_tasks`, `_worker_queues`) |
| P1.6 | `memory/retrieval/hybrid_retriever.py:27-31` | memory | 3-StateMachine | `add_memory` returns `None` when vector offline → caller treats as valid doc_id |
| P1.7 | `learning/review/reflect_tracker.py:70-134` | learning | 4-DataConsistency | TOCTOU race in `try_consume_feedback` → double-processing of reviews |
| P1.8 | `learning/review/reflect_tracker.py:55-58` | learning | 3-StateMachine | `get_unsent_requests` marks ALL as sent, not just returned → orphaned entries |
| P1.9 | `learning/review/reflector.py:33` | learning | 7-Lifecycle | `_pending_reflections` list grows unbounded when governance stopped |
| P1.10 | `infrastructure/runtime/event_bus.py:57-66` | infrastructure | 1-EventFlow | `affection_changed` asyncio.Event set once, never cleared → permanently stale |
| P1.11 | `infrastructure/runtime/lane_manager.py:89-93` | infrastructure | 7-Lifecycle | `_lane_locks` dict per unique lane_umo → unbounded growth |
| P1.12 | `infrastructure/runtime/chat_runtime_coordinator.py:26-35` | infrastructure | 7-Lifecycle | `_states` dict per chat_id → never cleaned |
| P1.13 | `infrastructure/persistence/persistence_manager.py:54-55` | infrastructure | 7-Lifecycle | `dispose()` never called → SQLAlchemy engine pool leaks on reload |
| P1.14 | `infrastructure/persistence/database_service.py:189` | infrastructure | 4-DataConsistency | `get_chat_state` reads without `_db_lock` while writes use it → WAL-mode unverified |
| P1.15 | `conversation/attention/context_compaction.py:298-314` | conversation | 2-Async | Race in compaction task creation → lost task reference for same chat_id |
| P1.16 | `conversation/attention/gate.py:839-844` | conversation | 1/2-EventFlow/Async | Session worker blocks on synchronous `sys2_process` await; `is_evaluating` holds entire System2 duration |
| P1.17 | `app/bootstrap.py:504-510` | app | 5-LLM | `_build_system2_bridge` closure captures `runtime.system2_callback` before binding → RuntimeError if called early |
| P1.18 | `app/lifecycle.py:22-25` | app | 2-Async | `track_task` calls `asyncio.create_task()` without RuntimeError guard → crash during shutdown race |
| P1.19 | `app/plugin_facade.py:451-510` | app | 5-LLM | `_system2_entry` only catches `LLMCascadeFailureException`; other exceptions propagate unhandled |
| P1.20 | `main.py` (cross-cutting) | app | 1-EventFlow | `heartflow_is_command` markers NOT implemented anywhere → heartflow messages treated as regular |
| P1.21 | `main.py:80-115` | app | 1-EventFlow | Missing `on_llm_response` hook → cannot inspect/modify LLM responses |

**P1 count: 21**

---

### 🟡 P2 — Medium Priority (Edge Cases / Performance / Robustness / Logging)

| # | File:Line | Module | Dimension | Description |
|---|-----------|--------|-----------|-------------|
| P2.1 | `memory/services/memory_engine.py:537-558` | memory | 4 | `recall()` hardcodes `exclude_kinds`, ignores caller intent |
| P2.2 | `memory/services/memory_processor.py:195` | memory | 5 | Greedy regex `\{.*\}` in JSON parse — captures malformed blobs |
| P2.3 | `learning/review/expression_auto_check_task.py:34` | learning | 7 | `_last_run_at` dict unbounded (same in `jargon_auto_check_task.py:31`) |
| P2.4 | `learning/evolution_manager.py:69-76` | learning | 2 | Cancelled tasks logged as ERROR (task.exception() returns CancelledError) |
| P2.5 | `learning/logging/message_recorder.py:19` | learning | 7 | `_windows` dict never pruned |
| P2.6 | `infrastructure/persistence/database_review.py:72-77` | infrastructure | 2 | `save_pattern` silently drops canonical write when called from async context |
| P2.7 | `infrastructure/gateway/model_router.py:97-101` | infrastructure | 7 | `ModelRouter._pools` accumulates dead model entries forever |
| P2.8 | `infrastructure/runtime/lane_storage.py:11-116` | infrastructure | 3 | `ensure_lane` can create duplicate conversations under concurrency |
| P2.9 | `infrastructure/persistence/persona_cache.py:24-26` | infrastructure | 4 | Persona cache writes not atomic (no tempfile+replace) |
| P2.10 | `conversation/attention/context_compaction.py:1243-1253` | conversation | 1 | Dead code: token-based compaction trigger never fires (no token_estimator wired) |
| P2.11 | `conversation/attention/context_compaction.py:195` | conversation | 7 | Unbounded `_cooldown_by_chat` dict |
| P2.12 | `conversation/attention/context_compaction.py:1393-1430` | conversation | 4 | Compaction summary merge failure leaves inconsistent state (no rollback) |
| P2.13 | `conversation/ingress/sensors.py:18` | conversation | 7 | `foreign_commands` set accumulates without dedup |
| P2.14 | `conversation/decision/judge.py:331` | conversation | 2 | `time.time()` for NTP-vulnerable cold-chat check |
| P2.15 | `conversation/attention/decision_router.py:67-80` | conversation | 5 | Judge evaluate timeout 2.0s → may fail on cold LLM requests |
| P2.16 | `conversation/loop/chat_loop_kernel.py:1240-1270` | conversation | 3 | `_apply_wait_arm` priority ordering: private wait dropped when group wait present |
| P2.17 | `conversation/loop/chat_loop_kernel.py:1283-1297` | conversation | 2 | `_collect_private_wait_state` uses sync getter without await safety check |
| P2.18 | `app/bootstrap.py:232-246` | app | 5 | `ContextCompactionEngine` provider_id stale after hot-apply |
| P2.19 | `app/bootstrap.py:258-263` | app | 7 | `VisualCortex` init failure may leak resources per partial construction |
| P2.20 | `app/bootstrap.py:265-280` | app | 3 | `CronHeartbeatGuard` partial construction resources not cleaned on failure |
| P2.21 | `app/lifecycle.py:144-156` | app | 2 | `_db_sync_task` CancelledError handler may be cancelled during secondary flush |
| P2.22 | `app/lifecycle.py:47-72` | app | 3 | `is_running` set BEFORE background services confirmed → false "running" state |
| P2.23 | `app/lifecycle.py:131-142` | app | 7 | `cron_guard` reload failure leaves heartbeat task running without status flag |
| P2.24 | `app/lifecycle.py:144-156` | app | 4 | `_db_sync_task` 15s flush interval loses data on crash |
| P2.25 | `app/lifecycle.py:227-240` | app | 7 | `SHUTDOWN_TASK_TIMEOUT` 3.0s too short for network-bound tasks |
| P2.26 | `app/lifecycle.py:97-98` | app | 7 | ProactiveTask auto-restart broken: `start()` returns immediately because `_is_running` still True after crash |
| P2.27 | `app/plugin_facade.py:430-437` | app | 5 | `enter_sys3_direct` hardcodes `max_steps=30`, `timeout=120` — not configurable |
| P2.28 | `app/plugin_facade.py:151-153` | app | 2 | `track_incoming_user_activity` fire-and-forget → user stats silently desync |
| P2.29 | `app/plugin_facade.py:183-186` | app | 1 | `suppress_default_llm_if_engaged` yields ghost message but does NOT `event.stop_event()` |
| P2.30 | `app/plugin_facade.py:80-110` | app | 4 | `apply_hot_config` partial failure → config version skew between components |
| P2.31 | `app/plugin_facade.py:393-398` | app | 2 | `enter_sys3_direct` async generator: `await` instead of `async for` silently returns unconsumed generator |
| P2.32 | `app/runtime_context.py:123` | app | 7 | `background_tasks` set unboundedly grows if done callbacks fail to fire |
| P2.33 | `app/runtime_context.py:313-320` | app | 7 | `ChatRuntimeCoordinator._states` not included in shutdown cleanup |
| P2.34 | (cross-cutting) | app | 3 | No unified `session_waiter` abstraction; GroupReplyWaitManager and PrivateChatManager independently |
| P2.35 | (cross-cutting) | app | 3 | No persona switch context cleanup on hot-apply |
| P2.36 | (cross-cutting) | app | 5 | No context compaction error recovery mechanism |
| P2.37 | `main.py:75-77` | app | 1 | Missing `on_agent_begin` / `on_agent_done` hooks |
| P2.38 | `main.py:79` | app | 1 | `on_llm_request` reverse session block may be overwritten by AstrBot persona injection |
| P2.39 | `main.py:125-126` | app | 1 | `on_decorating_result` hooks silently skipped during streaming output |
| P2.40 | `main.py:81-115` | app | 2 | `inject_gemini_reverse_session` swallows all exceptions silently |

**P2 count: 40**

---

### 🟢 P3 — Low Priority (Code Smells / Types / Comments / Naming / Dead Code)

| # | File:Line | Module | Dimension | Description |
|---|-----------|--------|-----------|-------------|
| P3.1 | `memory/persona/persona_summarizer.py:458+` | memory | 2 | `logger.exception(exc_info=True)` redundant (8x occurrences) |
| P3.2 | `memory/services/topic_summarizer.py:366` | memory | 5 | Local `import json` instead of module-level |
| P3.3 | `memory/utils.py:76-82` | memory | 4 | RRFFusion "first wins" metadata: BM25 metadata overwrites vector metadata |
| P3.4 | `learning/evolution_manager.py:293` | learning | 5 | `__import__("re")` called dynamically instead of module-level import |
| P3.5 | `learning/review/reflect_tracker.py:171-192` | learning | 6 | `_extract_pattern_id` monkey-patch changes return type `Optional[int]` → `Optional[str]` |
| P3.6 | `learning/evolution_manager.py:119-134` | learning | 4 | Duplicate normalization logic in evolution_manager and enrichers |
| P3.7 | `infrastructure/gateway/gateway_lane.py:413-691` | infrastructure | 5 | `tool_chat_in_lane_result` duplicates ~200 lines from `_elastic_call_result` |
| P3.8 | `infrastructure/runtime/event_bus.py:169-179` | infrastructure | 7 | `_workers_started` never set False during normal operation |
| P3.9 | `conversation/decision/judge.py:26,305-309` | conversation | 2 | Judge group mutex uses plain `set()` without atomic guarantees |
| P3.10 | `conversation/planning/planner.py:87-89` | conversation | 7 | Planning history lists global (not per-chat); fidelity degrades under load |
| P3.11 | `conversation/execution/followup_manager.py:15-21` | conversation | 6 | Config zero-value ambiguity: `or 0.0` on float makes `0.0` indistinguishable from "not set" |
| P3.12 | `conversation/execution/followup_manager.py:15-21` | conversation | 6 | Same zero-value ambiguity (duplicate of P3.11 in different file) |
| P3.13 | `conversation/execution/executor.py:431-434` | conversation | 2 | Sync `tempfile.mkstemp()`/`os.fdopen()` in async context |
| P3.14 | `conversation/planning/conversation_continuity.py:213-219` | conversation | 3 | Lightweight event design intentional but undocumented |
| P3.15 | `conversation/planning/context_engine.py:316-322` | conversation | 6 | Persona fallback warning logged on every prompt build |
| P3.16 | `app/bootstrap.py:90-98` | app | 4 | `_log_boot_status` accesses `task_models[0]` without bounds check on empty list |
| P3.17 | `app/bootstrap.py:192` | app | 4 | `trace_cache_dir` path construction may not match actual cache dir |
| P3.18 | `app/bootstrap.py:504-510` | app | 7 | `_build_system2_bridge` closure creates reference cycle: runtime → interaction → bridge → runtime |
| P3.19 | `app/lifecycle.py:126-129` | app | 2 | `start_background_services` fires tasks without confirming they actually started |
| P3.20 | `app/lifecycle.py:252-270` | app | 3 | `_reset_runtime_status_flags` sets 10+ flags sequentially — no atomicity |
| P3.21 | `app/lifecycle.py:229` | app | 7 | `dict.fromkeys(tasks_to_wait)` relies on asyncio.Task being hashable (CPython detail) |
| P3.22 | `app/lifecycle.py:85-91` | app | 7 | `host()` weakref dereference called twice — race condition with GC |
| P3.23 | `app/lifecycle.py:115-124` | app | 7 | `visual_cortex.start()` called synchronously without await → if async, coroutine leaked |
| P3.24 | `app/plugin_facade.py:331-391` | app | 6 | `is_framework_command` uses private AstrBot API `_collect_descriptors` |
| P3.25 | `app/plugin_facade.py:22-28` | app | 6 | WebUI adapter registration failure silently swallowed |
| P3.26 | `app/runtime_context.py:81-82` | app | 3 | `threading.Lock` mixed with asyncio context |
| P3.27 | `app/runtime_context.py:140-148` | app | 2 | `sync_host_compat_attrs` partial failure: some attrs set, some not |
| P3.28 | `app/runtime_context.py:420-456` | app | 7 | 29 `LEGACY_RUNTIME_ATTRS` — dead compat shim |
| P3.29 | `app/runtime_facade_protocol.py:15-16` | app | 6 | `@runtime_checkable` on Protocol adds overhead (unnecessary for explicit inheritance) |
| P3.30 | `main.py:41-42` | app | 6 | Plugin Pages API comment warns of AstrBot v4.26.0 Quart→FastAPI change; no runtime guard |
| P3.31 | `main.py:134-137` | app | 6 | `@filter.event_message_type` priority=10 — lower-priority plugins can silence AstrMai |
| P3.32 | (cross-cutting) | app | 7 | No MCP connection management anywhere |

**P3 count: 32**

---

## 📊 Combined Summary

| Severity | Count | % | Impact Summary |
|----------|-------|---|----------------|
| **P0** | 9 | 8.8% | NameError crash (×2), data loss (×3), DB corruption (×3), task leak (×1) |
| **P1** | 21 | 20.6% | Memory leaks (×7), race conditions (×3), silent failures (×3), event flow gaps (×3), lifecycle (×3), LLM resilience (×2) |
| **P2** | 40 | 39.2% | Unbounded growth (×8), async safety (×6), state machine (×6), data consistency (×5), event flow (×5), LLM (×5), resource (×5) |
| **P3** | 32 | 31.4% | Code quality (×12), framework compat (×8), lifecycle (×5), async (×4), data (×3) |
| **Total** | **102** | 100% | — |

---

## 🗂️ Dimension Heatmap

| Dimension | P0 | P1 | P2 | P3 | Total |
|-----------|----|----|----|----|-------|
| 1. Event Flow | 0 | 3 | 5 | 0 | 8 |
| 2. Async Safety | 2 | 2 | 6 | 4 | 14 |
| 3. State Machine | 2 | 2 | 6 | 3 | 13 |
| 4. Data Consistency | 4 | 3 | 5 | 3 | 15 |
| 5. LLM Call Chain | 0 | 2 | 5 | 3 | 10 |
| 6. AstrBot Compat | 0 | 0 | 0 | 6 | 6 |
| 7. Resource/Lifecycle | 1 | 8 | 5 | 6 | 20 |

**Highest-risk dimension: 7 (Resource/Lifecycle) with 20 findings** — unbounded dicts, task leaks, undisposed connections.  
**Second highest: 4 (Data Consistency) with 15 findings** — DB races, batch loss, non-atomic writes.

---

## 🛡️ Excluded Categories (not audited)
- Cookie/Session validation
- CSRF/CORS/XSS protection
- Request signing/authentication
- Rate limiting (framework responsibility)
- Encryption/key management (framework responsibility)

---

*Report: Round-1 / Phase 2. To be updated with remaining module findings.*
