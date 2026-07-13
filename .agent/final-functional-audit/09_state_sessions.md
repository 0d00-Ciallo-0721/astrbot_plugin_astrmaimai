# AstrMai Final Functional Audit: State and Sessions

## Audit result

- Scope: the current working tree under `astrmai/state/`, with adjacent production callers read only where required to prove reachability through conversation ingress/concurrency, persistence, lifecycle, group departure, and proactive services.
- Exclusions honored: no tests or coverage were inspected; security policy, authentication, authorization, style, duplication, dead code, and refactoring opportunities were not reviewed; `astrmai/infrastructure/security/` was treated as opaque.
- Result: **9 confirmed functional defects** (`P0: 0`, `P1: 2`, `P2: 5`, `P3: 2`).

| Severity | Count |
| --- | ---: |
| P0 | 0 |
| P1 | 2 |
| P2 | 5 |
| P3 | 2 |

## Findings

### FFA-09-001 / P1: Group mood and energy are split across raw group ID and UMO state records

- **Severity:** P1
- **File:line:** `astrmai/conversation/attention/perception.py:15-16`; `astrmai/conversation/attention/gate.py:734-747`; `astrmai/conversation/decision/judge.py:308,357`; `astrmai/conversation/execution/system2_runner.py:27,42`; state ownership at `astrmai/state/chat_state_service.py:30,130-132,442-501`.
- **Trigger:** Any normal group message whose raw `group_id` differs from its AstrBot `unified_msg_origin`, as it does for the normal `platform:GroupMessage:<id>` UMO shape.
- **Real call chain:** `main.AstrMaiPlugin.on_global_message()` -> `message_entry.handle_global_message()` creates a `MessageScope` whose `chat_id` is the UMO (`presentation/dto/message_scope.py:37-38,53-69`) -> `PluginFacade.record_and_dispatch_attention()` -> `ChatLoopKernel.tick(chat_id=UMO)` -> `AttentionGate.process_event()` -> `PerceptionBuilder.build()` replaces the group chat key with raw `group_id` -> `StateEngine.update_mood(raw_group_id)` and `Judge.evaluate(raw_group_id)` -> `StateEngine.should_drop_by_energy(raw_group_id)`. If the turn engages, `System2Runner.run()` independently derives `chat_id=event.unified_msg_origin` -> `StateEngine.consume_energy(UMO)`; planning also reads state using the UMO.
- **Actual behavior:** One physical group gets two independent `ChatState` objects and persistence rows. The Judge checks the raw-ID record, while reply energy is charged to the UMO record, so consumed energy does not lower the energy used by subsequent reply decisions. Ingress mood is written to the raw-ID record, while planning/post-send paths use the UMO record. Group-departure cleanup receives the UMO and therefore does not clear the raw-ID cache entry.
- **Expected behavior:** Every group state read, update, settlement, cleanup, and proactive lookup must use one canonical conversation key, preferably the UMO already used by ingress, runtime coordination, sending, and persistence.
- **Production impact:** Group energy throttling is effectively bypassed, mood-dependent planning can see a different value from the Judge, duplicate state rows accumulate, and stale raw-ID state survives group departure cleanup.
- **Why existing guards fail:** Per-chat locks and persistence uniqueness are scoped to the supplied string and cannot detect aliases. No normalization layer maps raw `group_id` back to the UMO before `StateEngine` access.
- **Classification:** confirmed functional defect (state identity / group isolation).
- **Confidence:** 0.99.

### FFA-09-002 / P1: Ordinary private messages are buffered without an active waiter and never dispatched

- **Severity:** P1
- **File:line:** `astrmai/state/private_chat/private_chat_manager.py:86-97,109-133`; caller at `astrmai/conversation/attention/gate.py:774-777`; continuation ownership at `astrmai/conversation/execution/followup_manager.py:80-95`; host suppression at `astrmai/presentation/events/message_entry.py:195-224`.
- **Trigger:** A user sends an ordinary private message that is not represented as an `At`, reply-to-bot, nickname-only wakeup, or forced-engage event. This includes a first DM and a natural follow-up after a strong-wakeup turn.
- **Real call chain:** `main.py:on_global_message` -> `message_entry.handle_global_message()` -> `PluginFacade.record_and_dispatch_attention()` -> `ChatLoopKernel.tick(trigger="message")` -> `AttentionGate.process_event()` -> `PrivateChatManager.signal_new_message()` -> `_get_or_create_session()` creates a session even when `is_bot_waiting=False` -> text is appended to `pending_messages` -> gate returns `PRIVATE_WAIT`. `is_direct_call_event()` is true for private events, so the host LLM is suppressed. After a successful strong-wakeup reply, `FollowupManager.finalize_after_reply()` checks `event.get_extra("is_private_chat", False)`, but no production ingress path writes that extra, so it does not arm `wait_for_new_message()` either.
- **Actual behavior:** The private message is stored as a string and the turn exits without starting System2. No production caller invokes `get_pending_messages()`, and the waiter only observes an `asyncio.Event`; it never redispatches buffered text. The message therefore receives no AstrMai reply and remains in memory.
- **Expected behavior:** A private message should become a wait-resume only when an existing waiter owns that session; otherwise it must enter normal attention/System2. A resumed message must preserve and dispatch the original event/content.
- **Production impact:** Normal private chat is functionally silent. Conversations that begin through a strong wakeup stop on the next natural message, while pending text accumulates.
- **Why existing guards fail:** `signal_new_message()` does not return whether a waiter existed, creates sessions unconditionally, and buffers before checking `is_bot_waiting`. `PRIVATE_WAIT` is treated as a successful status, and private events always suppress the host fallback.
- **Classification:** confirmed functional defect (private-chat state machine).
- **Confidence:** 1.00.

### FFA-09-003 / P2: Threaded group waits are registered and looked up with incompatible thread identities

- **Severity:** P2
- **File:line:** `astrmai/state/group_wait/group_reply_wait_manager.py:53-62,241-249,301-336`; resolver fallback at `astrmai/conversation/threading/group_thread_resolver.py:35-55`; cancellation caller at `astrmai/app/plugin_facade.py:397-408`.
- **Trigger:** `conversation.group_thread_wait_enabled=true`; a bot reply arms a wait after focus processing has set `astrmai_thread_signature`, then the target follows up with plain text or an `At` mention rather than a Reply component carrying a matching outbound message ID.
- **Real call chain:** focus/prompt processing writes `astrmai_thread_signature` on the outgoing event (`infrastructure/compat/legacy_compat.py:61-76,137-171`) -> `System2Runner` -> `FollowupManager.finalize_after_reply()` -> `GroupReplyWaitManager.register_from_reply_event()` -> `_thread_id_from_event()` stores the wait under that late focus signature. On the next event, `message_entry` binds the turn before wait handling -> `resolve_group_thread()` has no inherited signature and resolves to `sender:<id>` for plain/At messages -> `handle_incoming_message()` performs exact bucket lookup by that different key -> no Reply ID fallback applies -> returns `NONE`. Interruption cancellation also passes the preliminary turn thread ID and misses the registered key.
- **Actual behavior:** The intended target response neither resumes nor consumes the existing wait. It is processed as a new turn and can arm another wait while the old wait remains active until timeout, budget expiry, or a later explicit Reply.
- **Expected behavior:** Turn generation, wait registration, incoming lookup, cancellation, and loop mirroring must share one stable thread identity; an unambiguous active target response should resolve to its wait.
- **Production impact:** Threaded mode produces stale and duplicate waits, loses conversational continuation metadata, and can later resume obsolete context.
- **Why existing guards fail:** Outbound-message-ID scanning repairs only Reply components. `_is_strong_resume()` runs only after a state has already been found, so its At/plain heuristics cannot repair the key mismatch.
- **Classification:** confirmed functional defect (group wait / conversation concurrency integration).
- **Confidence:** 0.98.

### FFA-09-004 / P2: State hot reload reports success while operational state components retain old settings

- **Severity:** P2
- **File:line:** `astrmai/state/chat_state_service.py:134-142,153-166,216-223`; `astrmai/state/private_chat/private_chat_manager.py:32-34,80-84`; `astrmai/state/mood/mood_manager.py:35-58`; dispatcher at `astrmai/app/plugin_facade.py:185-223`.
- **Trigger:** Apply a valid hot configuration changing `energy.daily_recovery`, `mood.decay_interval`/`decay_rate`, `private_chat.wait_timeout_sec`, or `reply.emotion_mapping`.
- **Real call chain:** plugin-page/config update -> `PluginFacade.apply_hot_config()` -> `_apply_hot_config_locked()` replaces `runtime.config` -> `_refresh_components()` calls `StateEngine.refresh_config()`, `PrivateChatManager.refresh_config()`, and other component refresh methods -> the call returns `True`. Later, `ChatStateService._check_daily_reset()`/`atomic_update_mood()`, `PrivateChatManager.wait_for_new_message()`, and `MoodManager.analyze_mood()` execute.
- **Actual behavior:** `StateEngine.refresh_config()` updates itself plus three child `.config` attributes but never updates `chat_state_service.config`, so daily recovery and ChatStateService mood decay continue using the old object. `PrivateChatManager.refresh_config()` never recomputes `timeout_sec`. `MoodManager` receives the new config object but retains the `emotion_mapping` dictionary built only in `__init__`.
- **Expected behavior:** A successful hot apply must update every derived/cached state setting atomically, or reject the apply as restart-required.
- **Production impact:** State behavior depends on which method handles a turn: some paths use new mood/energy settings while others use old ones; private wait duration and mood-analysis descriptors remain stale until restart.
- **Why existing guards fail:** The hot-apply transaction checks only whether `refresh_config()` raises. It has no postcondition for derived fields or nested component ownership, so these no-op/partial refreshes appear successful.
- **Classification:** confirmed functional defect (configuration/state invariant).
- **Confidence:** 0.99.

### FFA-09-005 / P2: Group departure removes a per-chat lock while old waiters still reference it

- **Severity:** P2
- **File:line:** `astrmai/state/chat_state_service.py:36-54,147-151`; production cleanup caller at `astrmai/app/plugin_facade.py:115-151`.
- **Trigger:** The bot receives a self `group_decrease` notice while a state operation for that chat is running or queued, and another event/state operation reaches the same chat before the old queue drains.
- **Real call chain:** `main.py:on_group_membership_notice` -> `PluginFacade.handle_group_membership_notice()` -> `clear_group_runtime_state()` -> `StateEngine.clear_chat_state()` -> `ChatStateService.clear_chat_state()` acquires lock L1, removes the cached state, then pops L1 from `_chat_locks` before leaving `async with`. A queued caller still holds a reference to L1, while a new caller enters `_get_chat_lock()` and creates L2 for the same key; both can then execute `_get_state_inner()` and persistence writes concurrently.
- **Actual behavior:** The per-chat mutual-exclusion invariant is broken into two lock generations. An old queued operation may recreate state after departure cleanup, while a new operation mutates or persists another instance concurrently.
- **Expected behavior:** Lock identity must remain stable until the current holder and all queued users have drained; cleanup must prevent pre-cleanup work from resurrecting the chat state.
- **Production impact:** Mood/energy updates can be lost or reordered, duplicate cached objects can exist for one chat, and state can reappear immediately after the bot-left-group cleanup path reports success.
- **Why existing guards fail:** The lock dictionary is the identity registry, but `clear_chat_state()` deletes from it while holding the lock. The lock itself has no generation/cancellation check, and persistence writes are not conditioned on current conversation generation.
- **Classification:** confirmed functional defect (locking / lifecycle cleanup).
- **Confidence:** 0.96.

### FFA-09-006 / P2: Daily relationship maintenance applies two decays and persists only the first result

- **Severity:** P2
- **File:line:** `astrmai/proactive/decay_service.py:29-57`; `astrmai/state/chat_state_service.py:334-342`; `astrmai/state/relationship/relationship_engine.py:421-429,457-495`; `astrmai/state/user_profile_service.py:128-133,177-202`.
- **Trigger:** A profile remains cached but has not been accessed for more than 24 hours when the proactive maintenance cycle runs (the cycle is scheduled every 60 seconds in `proactive/proactive_task.py:771-796`).
- **Real call chain:** `ProactiveTask._run_maintenance_cycle()` -> `DecayService.run_once()` -> inactive profile branch computes a +/-1 legacy social-score decay -> `StateEngine.update_social_score_from_fact()` -> `RelationshipEngine.align_social_score()` mutates the vector -> `UserProfileService.update_social_score()` persists that score/vector. In the same `run_once()`, `relationship_engine.apply_global_decay()` then calls `_apply_decay()` on the same vector and applies an additional exponential dimensional decay without updating the profile or persistence.
- **Actual behavior:** Runtime relationship dimensions/social score immediately diverge from the just-persisted `UserProfile` and database row. Restarting before another relationship event discards the second decay; staying online uses it. The persistence call also routes through `_touch_profile()`, which changes `last_seen` to maintenance time despite no user interaction.
- **Expected behavior:** One canonical relationship decay should be applied per interval and its resulting vector, social score, and activity timestamps should be persisted consistently; maintenance must not fabricate user-seen activity.
- **Production impact:** Relationship-dependent prompts and proactive decisions vary based on restart timing, users receive more decay than configured by either algorithm alone, and persisted/profile/runtime views disagree.
- **Why existing guards fail:** The legacy scalar decay and multidimensional decay run independently with no ownership flag. `apply_global_decay()` returns no changed vectors to persist, while generic profile writes conflate state mutation with real user activity.
- **Classification:** confirmed functional defect (relationship clock / persistence invariant).
- **Confidence:** 0.97.

### FFA-09-007 / P2: Non-finite mood output is converted to maximum positive mood and persisted

- **Severity:** P2
- **File:line:** `astrmai/state/mood/mood_manager.py:75-147`; persistence path at `astrmai/state/chat_state_service.py:306-332`.
- **Trigger:** The mood model returns structurally valid JSON containing a non-finite numeric token such as `{"mood_tag":"neutral","mood_value":NaN}`. Python's default JSON decoder accepts `NaN`.
- **Real call chain:** group/private ingress -> `AttentionGate._apply_primary_mood_update()` -> `StateEngine.update_mood()` -> `MoodManager.analyze_mood()` -> `_parse_result_payload()` uses `json.loads()` -> `_normalize_result()` converts the value with `float()` -> `max(-1.0, min(1.0, mood_value))` -> `StateEngine.update_mood()` CAS writes and persists the result.
- **Actual behavior:** IEEE `NaN` passes conversion, and the current clamp expression evaluates it to `1.0`. A nominally neutral invalid model result therefore becomes maximum positive mood and is saved as valid state.
- **Expected behavior:** Non-finite values must be rejected with `math.isfinite()` and routed to the local fallback/current mood before any state mutation.
- **Production impact:** Judge prompts and mood-dependent expression policy can operate on a fabricated extreme mood until natural/daily reset, producing inconsistent tone and state history.
- **Why existing guards fail:** Validation catches only conversion exceptions and range-clamps finite assumptions; neither the parser nor `_clamp_mood()` checks finiteness.
- **Classification:** confirmed functional defect (invalid value handling).
- **Confidence:** 0.99.

### FFA-09-008 / P3: Per-message profile flush always calls a nonexistent API and falls back to delayed persistence

- **Severity:** P3
- **File:line:** `astrmai/state/user_profile_service.py:45-55,253-284`; model at `astrmai/infrastructure/persistence/orm_models.py:91-115`; actual persistence signature at `astrmai/infrastructure/persistence/state_profile_persistence.py:233-263`.
- **Trigger:** Any non-anonymous production message reaches profile activity observation.
- **Real call chain:** `message_entry.handle_global_message()` -> `EvolutionManager.record_user_message()` -> runtime event bus `TOPIC_LEARNING_MESSAGE_RECORDED` -> `StateEngine.on_learning_message_recorded()` -> `UserProfileService.observe_user_activity()` mutates name/footprint/recent messages -> `_flush_profile()` evaluates `profile.as_dict()` and calls `save_user_profile(user_id, ...)`. `UserProfile` is a plain dataclass with no `as_dict`, and production persistence accepts one profile argument. The broad `except` logs and leaves the profile dirty; lifecycle `_db_sync_task()` eventually retries dirty profiles every five seconds using `_save_profile()`.
- **Actual behavior:** The advertised immediate save fails on every message. Persistence occurs only on the later batch flush or shutdown flush.
- **Expected behavior:** The mutation should invoke the actual one-argument profile persistence contract immediately and clear dirty state only after success.
- **Production impact:** A crash/reload within the flush interval loses recent profile names, footprints, and interaction snippets; each message also emits a misleading persistence warning.
- **Why existing guards fail:** The broad exception handler converts a deterministic programming error into degradation, and the five-second background flush masks it during healthy shutdown rather than restoring immediate durability.
- **Classification:** confirmed functional defect (profile persistence).
- **Confidence:** 1.00.

### FFA-09-009 / P3: Private-session eviction leaks chat-to-user mappings

- **Severity:** P3
- **File:line:** `astrmai/state/private_chat/private_chat_manager.py:61-67,153-186,190-225`.
- **Trigger:** More than 100 distinct private `FriendMessage` session keys are created during a long-running process, causing `_get_or_create_session()` to evict the oldest non-waiting session.
- **Real call chain:** ordinary private ingress -> `PrivateChatManager.signal_new_message(user_id, chat_id)` -> `_session_key()` creates `user_id::chat_id` and `_bind_chat_session()` stores `_chat_to_user[chat_id]=user_id`. At capacity, `_get_or_create_session()` selects `(composite_key, session)` and calls `close_session(oldest[0])`. `close_session()` removes the composite `_sessions` key but removes chat mappings only when their value equals its argument; mapping values are plain user IDs, not composite keys.
- **Actual behavior:** `_sessions` remains capped, but every composite-key eviction can leave an orphan `_chat_to_user` entry. The same mismatch exists in the stale cleanup path's orphan sweep.
- **Expected behavior:** Eviction/cleanup by session key must remove the exact associated chat mapping, and auxiliary indexes must remain bounded with the primary session store.
- **Production impact:** `_chat_to_user` grows without bound across private origins and can keep stale chat identities alive; chat-based diagnostics/lookup may resolve a user for which no session exists.
- **Why existing guards fail:** `close_session()` ambiguously accepts either a user ID or a session key, while the reverse index stores only user IDs. The orphan sweep compares those incompatible representations, and no production lifecycle task invokes `cleanup_stale_sessions()`.
- **Classification:** confirmed functional defect (session cleanup/index invariant).
- **Confidence:** 0.99.

## Production paths reviewed without an additional confirmed defect

- `astrmai/state/__init__.py` lazy exports and state contract exports.
- `astrmai/state/contracts/wait_state.py` and `profile_summary.py` snapshot conversion paths.
- Group wait timeout task replacement/cancellation, message budget handling, reply-ID matching, max-active-wait eviction, and bot-left-group cancellation outside the identity defect above.
- Private session event signaling, timeout `finally` handling, shutdown KV marker cleanup, and group/private entry checks outside the confirmed state-machine and index defects.
- Chat-state load/coercion, daily reset persistence, mood CAS update, energy settlement, natural decay anchors, and dirty-state retry behavior outside the canonical-key and hot-config defects above.
- Relationship event classification, intensity finiteness/clamping, vector saturation, affection publishing, and persisted vector migration outside the double-decay defect above.
- User-profile manual locks, nickname/name safeguards, tag/memory-point merging, structured prompt bundles, periodic dirty flush, and per-user locking outside the immediate-flush defect above.
- State integration with `System2Runner`, reply post-send affection/mood settlement, `ChatRuntimeCoordinator`, `ChatLoopKernel`, `DecayService`, wakeup/Heartflow signal collection, profiling maintenance, and group-departure cleanup.

## Overall assessment

No P0 defect was confirmed. The most serious state failures are identity/ownership breaks at integration boundaries: group state is keyed differently before and after attention, and private messages are converted into wait signals without an owning waiter or redispatch path. The remaining confirmed defects can produce stale waits, partially applied configuration, concurrent state mutation after cleanup, restart-dependent relationship values, invalid extreme mood, short profile durability gaps, and long-run private-session index growth.
