# Round 07 Review: Memory Governance, Persona & Dream

**Review Date**: 2026-07-14
**Reviewer**: codex (automated review agent)
**Scope**: 9 fixes (R07-01 through R07-09)
**Conclusion**: ALL 9 PASS - every fix is correctly implemented in the codebase.

---

## Summary Table

| Fix ID | Status | Evidence |
|--------|--------|----------|
| R07-01 | PASS | decay_rate passed explicitly; _last_memory_decay only advanced on success (else block) |
| R07-02 | PASS | All 3 legacy import loops use offset-based pagination, exhaustive until rows < page_size |
| R07-03 | PASS | _minimum_confidence() reads live config; gate at write() skips low-confidence; 0 disables |
| R07-04 | PASS | replace_dedup_identity() + resolve_dedup_key() + conflict merging + alias tombstone |
| R07-05 | PASS | _invalidate_if_prompt_changed() hashes raw prompt, invalidates on mismatch, cancels stale tasks |
| R07-06 | PASS | persona_summarizer.stop() called from lifecycle.terminate(); _closed check + generation guard |
| R07-07 | PASS | fact_contract.py defines unified schema; dream_agent + promotion_engine + dream_generator all use it |
| R07-08 | PASS | DREAM_STYLES are clean Chinese strings; _normalize_style() validates and fallbacks |
| R07-09 | PASS | _build_split_write_request writes raw extracted_fact content; no garbled prefix anywhere |

---

## Detailed Findings

### R07-01 - Decay parameter & failure-safe 24h throttle

**Files**: astrmai/proactive/decay_service.py, astrmai/memory/services/memory_engine.py

**What was verified**:
1. decay_service.py lines 69-76: decay_rate is read from self.config.memory.time_decay_rate (line 70) and passed as keyword argument apply_daily_decay(decay_rate=decay_rate) (line 72).
2. The try/except/else pattern (lines 71-76) ensures self._last_memory_decay = now only executes on success. If the call raises, the mark is NOT advanced, making it retryable in the next maintenance cycle.
3. memory_engine.py line 689: apply_daily_decay(self, decay_rate: float, days: int = 1) accepts the parameter and delegates to maintenance_service.apply_daily_decay(decay_rate=decay_rate, ...).

Result: No TypeError possible - the parameter is always provided.

---

### R07-02 - Legacy import exhausts all rows (not just first 1000)

**File**: astrmai/memory/services/memory_engine.py

**What was verified**:
1. import_legacy_memory_events (line 697): Uses page_size = max(int(limit or 1000), 1) (line 706) as page size, NOT as total limit. The while True loop (line 722) increments offset += len(events) and breaks only when len(events) < page_size (line 764). Migration version check at line 699 prevents re-running.
2. import_legacy_jargons (line 772): Same pagination pattern, line 828-829.
3. import_legacy_expression_patterns (line 837): Same pagination pattern, line 897-898.

Result: All 1001+ records will be imported across multiple pages.

---

### R07-03 - min_memory_confidence unified gating

**Files**: config.py, _conf_schema.json, astrmai/memory/services/memory_write_service.py

**What was verified**:
1. _conf_schema.json line 573-580: min_memory_confidence field exists (type float, default 0.3, range 0-1).
2. config.py line 186: MemoryConfig.min_memory_confidence: float = Field(default=0.3, ge=0.0, le=1.0).
3. memory_write_service.py lines 33-38: _minimum_confidence() reads from self.config.memory.min_memory_confidence with safe fallback to 0.0.
4. Lines 85-92: Gate applied in write() - when minimum_confidence > 0.0 and confidence < minimum_confidence, the write is skipped with a log entry. When minimum_confidence == 0, the gate is disabled and all writes pass through.
5. All write paths go through MemoryWriteService.write() - unified gating.

Result: All write sources are consistently gated.

---

### R07-04 - Expression replacement atomically changes dedup key

**Files**: astrmai/memory/services/expression_pattern_service.py, astrmai/memory/services/v2_store.py

**What was verified**:
1. expression_pattern_service.py update_review() (lines 245-342):
   - Lines 269-273: When replacement_expression and apply_replacement, replaces expression with new value.
   - Lines 304-306: Computes new_dedup_key from the NEW expression.
   - Lines 307-319: On conflict (new key already exists for a different record), merges content samples, counts, and weights.
   - Lines 321-330: Calls store.replace_dedup_identity(old_dedup_key=..., new_dedup_key=...) for atomic migration.
2. v2_store.py replace_dedup_identity() (lines 785-860):
   - Lines 806-826: If new_dedup_key conflicts with an existing record, soft-deletes self (SUPERSEDED_STATUS) and removes this record FTS entry.
   - Lines 827-858: Updates content/summary/metadata/dedup_key; writes old key as alias tombstone in memory_dedup_aliases; removes any previous alias for the new key.
3. v2_store.py resolve_dedup_key() (lines 766-783): Resolves alias keys back to canonical records via memory_dedup_aliases JOIN.

Result: Old expression appearing again will NOT overwrite the manually replaced result (resolved via alias). New expressions with the same content accumulate to the same record.

---

### R07-05 - Persona cache invalidation on prompt change

**File**: astrmai/memory/persona/persona_summarizer.py

**What was verified**:
1. _invalidate_if_prompt_changed() (lines 63-88):
   - Computes expected_hash = _compute_hash(original_prompt) (line 64).
   - Retrieves cached raw_hash (line 71); falls back to hashing the cached raw text if raw_hash is missing.
   - If cached_hash == expected_hash, returns False (no invalidation needed) - correct cache-hit behavior.
   - If mismatch, increments _cache_generations[cache_key], pops the cache entry, cancels any stale background task (lines 78-81), and clears persona lore (lines 82-87).
2. Called at top of get_summary() (line 268) before any cache lookup.
3. New cache entries include raw_hash (line 332) for future validation.

Result: Same persona ID with a changed prompt - cache invalidated, new summary generated. Unchanged prompt - cache hit preserved.

---

### R07-06 - Persona shard tasks lifecycle integration

**Files**: astrmai/memory/persona/persona_summarizer.py, astrmai/app/lifecycle.py, astrmai/shared/helpers/plugin_helpers.py

**What was verified**:
1. lifecycle.py lines 232-236: _terminate_impl() calls persona_summarizer.stop() during shutdown.
2. persona_summarizer.py stop() (lines 90-101):
   - Sets _closed = True - prevents new tasks from starting (checked at line 50, line 172).
   - Increments all generation counters to invalidate in-flight tasks.
   - Collects all _background_tasks and pending_tasks, cancels them, and awaits completion with return_exceptions=True.
3. _generate_all_shards_background() (lines 365-443): Checks _generation_is_current() at multiple checkpoints (lines 383, 393, 416, 421). If generation is stale (counter incremented by stop()), the task returns early without writing cache or calling LLM.
4. plugin_helpers.py safe_create_task() (lines 30-73): Tracks tasks in provided set, auto-logs exceptions.

Result: Reload during generation - old instance stops calling LLM, new instance starts fresh without concurrent cache overwrite.

---

### R07-07 - Dream fact output / promotion consumption contract

**Files**: astrmai/memory/dream/fact_contract.py, dream_agent.py, dream_generator.py, promotion_engine.py, astrmai/proactive/dream_scheduler.py

**What was verified**:
1. fact_contract.py - unified schema:
   - normalize_dream_fact() (lines 11-42): Normalizes any dict with keys subject_id/entity/attribute/value/confidence_score/confidence_signal/evidence.
   - format_dream_fact_log() (line 56-57): [FACT] {json} - the standard log format.
   - parse_dream_fact_log() (lines 60-72): Parses both new and legacy prefixes.
2. Producer: dream_agent.py lines 133-134: On finish_dream tool call, normalizes detected_facts with normalize_dream_facts() and logs with format_dream_fact_log().
3. Parser: dream_generator.py build_maintenance_result() lines 162-165: Parses dream log lines with parse_dream_fact_log() and accumulates detected_facts.
4. Consumer: promotion_engine.py _iter_detected_facts() lines 56-76: Iterates over maintenance_result detected_facts using normalize_dream_facts(), extracts (subject_id, entity, attribute, value) tuples with evidence.
5. Orchestrator: dream_scheduler.py lines 129-133: Passes maintenance result to promotion_engine.run_audit().

Result: End-to-end contract: Agent output -> Generator parsing -> Promotion consumption - all using the same schema.

---

### R07-08 - Dream style defaults free of mojibake

**File**: astrmai/memory/dream/dream_generator.py

**What was verified**:
1. DREAM_STYLES (lines 17-22): All 21 style strings are clean, readable Chinese/English - no mangled Unicode, no mojibake whatsoever.
2. _normalize_style() (lines 42-45): Validates style against DREAM_STYLES. If the value is not in the list, falls back to random.choice(DREAM_STYLES). This guards against any persisted invalid style values.

Result: Default prompts/fallback text will never contain corrupted characters. Explicit valid styles remain selectable.

---

### R07-09 - InstantMemoryGate no longer writes garbled prefix

**File**: astrmai/memory/services/instant_memory_gate.py

**What was verified**:
1. _build_split_write_request() - all three output paths:
   - authority_override (lines 127-154): content=str(extracted_fact or raw_text or ") - clean user content, no prefix.
   - volatile_state_write (lines 163-184): content=str(raw_text or ") - raw user text, no prefix.
   - fallback (lines 195-215): content=str(extracted_fact or raw_text or ") - clean, no prefix.
2. Full-code search: No garbled prefix or corrupted string markers anywhere in the file.

Result: New canonical content, index, and prompt context will NOT contain garbled prefix artifacts. Historical polluted data cleanup is out of scope (as specified in the fix boundary).

---

## Risk Observations

1. R07-04 edge case: If a direct caller to replace_dedup_identity() (not through update_review) encounters a key collision, the original content is superseded without merging. The expression_pattern_service caller pre-merges, but other call paths could silently lose data. Not currently a problem since there is only one caller.

2. R07-01: The 24-hour throttle uses time.time() (wall clock). System time changes (DST, NTP, manual adjustment) could bypass or extend the throttle. Same level of robustness as any time-based throttle - acceptable.

3. R07-06: stop() collects tasks from _background_tasks and pending_tasks at a specific point in time. If a new task is added between collection and cancellation, that task runs until its next generation check before bailing (mitigated by the _closed flag). Minor timing window - non-critical.

---

## Verification Method

All findings are based on source code inspection only (no test execution, no state modification). Files examined:

| # | File | Lines |
|---|------|-------|
| 1 | astrmai/proactive/decay_service.py | 1-79 |
| 2 | astrmai/memory/services/memory_engine.py | 1-1015 |
| 3 | astrmai/memory/services/memory_write_service.py | 1-135 |
| 4 | astrmai/memory/services/instant_memory_gate.py | 1-363 |
| 5 | astrmai/memory/dream/fact_contract.py | 1-81 |
| 6 | astrmai/memory/persona/persona_summarizer.py | 1-805 |
| 7 | astrmai/memory/dream/dream_agent.py | 1-525 |
| 8 | astrmai/memory/dream/dream_generator.py | 1-183 |
| 9 | astrmai/memory/dream/promotion_engine.py | 1-170 |
| 10 | astrmai/proactive/dream_scheduler.py | 1-289 |
| 11 | astrmai/memory/services/expression_pattern_service.py | 1-454 |
| 12 | astrmai/memory/services/v2_store.py | 760-879 (relevant) |
| 13 | astrmai/app/lifecycle.py | 1-341 |
| 14 | astrmai/shared/helpers/plugin_helpers.py | 1-173 |
| 15 | astrmai/conversation/planning/context_engine.py | 1-774 |
| 16 | config.py | 1-304 |
| 17 | _conf_schema.json | 565-584 (relevant) |
