# Round 01 Review

**Status summary below. Full report via structured sections.**

## Overall Assessment

**8/9 VERIFIED. 1 PARTIAL (R01-09).**

## Per-Fix Status

**R01-01**: VERIFIED. gate.py:937-950 checks signal_new_message() return; private_chat_manager.py:87-100 returns False when no waiter, True when waiter exists. First DM -> no waiter -> System2. Strong wakeup -> skips wait check entirely.

**R01-02**: VERIFIED. perception.py:15-16, gate.py:701, chat_runtime_coordinator.py:55-58 all key by unified_msg_origin. GroupReplyWaitManager uses UMO. Two adapters with same raw group ID get separate UMO-based keys.

**R01-03**: VERIFIED. gate.py:892 _claim_message atomically deduplicates. gate.py:912-914 force_engage returns ENGAGED before pool append. gate.py:927-929 fast_wakeup same pattern. Event never reaches accumulation_pool.append.

**R01-04**: VERIFIED. window_buffer.py:60-78 merge() marks non-batch events as astrmai_attention_historical=True. focus_selector.py:15-22,66 gate wakeup bonuses on not is_historical. Consumed events keep context, lose focus weight.

**R01-05**: VERIFIED. chat_runtime_coordinator.py:112-131 advance_generation() cancels stale active_turn_tasks. chat_runtime_coordinator.py:133-153 register_turn_task() rejects stale generations. gate.py:360-380 _run_managed_system2_task closes rejected coroutines.

**R01-06**: VERIFIED. ProactiveDispatcher -> inject_external_event -> process_event -> _ensure_turn_identity (gate.py:510-542) binds full TurnIdentity before System2 dispatch at gate.py:1089. SyntheticExternalEvent supports get/set_extra.

**R01-07**: VERIFIED. gate.py:360-380 _run_managed_system2_task catches exceptions -> _handle_system2_failure (gate.py:544-574) sends fallback text, releases proactive callback, checks is_current_turn for staleness. plugin_facade.py:764-771 adds outer safety net.

**R01-08**: VERIFIED. _thread_id_from_event (group_reply_wait_manager.py:53-62) uses consistent precedence. Register and incoming both call same method. Reply ID recovery scans outbound_message_ids (lines 321-327). Unique target disambiguation (lines 329-337).

**R01-09**: PARTIAL. GroupReplyWaitManager is per-thread (Dict[chat_id, Dict[thread_id, wait]]). ChatLoopKernel has single-valued wait per chat in ChatLoopState. _sync_wait_from_adapters only syncs once. _expire_wait_if_needed independently expires kernel-local wait. When thread A and B both have waits, kernel tracks only one. Functional correctness preserved (manager is authority) but heartbeat scheduling may be based on stale info.

## Related Tests

Found: test_turn_identity.py, test_group_thread_resolver.py, test_private_chat_manager_migrated.py, test_group_reply_wait_manager_concurrency_migrated.py, test_group_reply_wait_manager_ported.py, test_group_wait_thread_signature_ported.py, test_attention_private_chat_ported.py, test_attention_focus_thread_selection_migrated.py.

Missing: dedicated tests for R01-03 (concurrency), R01-05 (stale cancellation), R01-06 (proactive TurnIdentity), R01-07 (fallback).

## Recommendations

1. R01-09: Make kernel observational for group waits (read-only from manager) or expand ChatLoopState to multi-thread wait tracking.
2. R01-03: Add asyncio.gather concurrency test for force-engage + debounce.
