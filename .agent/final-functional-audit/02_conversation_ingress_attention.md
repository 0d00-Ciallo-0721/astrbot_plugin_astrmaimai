# AstrMai Final Functional Audit: Conversation Ingress / Attention

## Audit scope

- Audited the current working tree, including uncommitted production changes.
- Owned modules: `astrmai/conversation/ingress/`, `attention/`, `contracts/`, `concurrency/`, and `threading/`.
- Adjacent production call sites were read only where needed to prove an actual call chain: `main.py`, `PluginFacade`, `ChatLoopKernel`, `ChatRuntimeCoordinator`, `System2Runner`, reply freshness/sending, private/group wait managers, proactive dispatch, and lifecycle cleanup.
- `astrmai/infrastructure/security/` was not inspected and is treated as opaque.
- No production file was modified.

## Finding summary

| Severity | Count |
| --- | ---: |
| P0 | 0 |
| P1 | 7 |
| P2 | 4 |
| P3 | 0 |
| **Confirmed defects** | **11** |

## Findings

### FFA-02-001 / P1: Ordinary private messages are buffered forever instead of entering a reply turn

- **File:line:** `astrmai/conversation/attention/gate.py:774-777`; corroborating call sites `astrmai/state/private_chat/private_chat_manager.py:86-96`, `astrmai/presentation/events/message_entry.py:195-224`.
- **Trigger:** Private chat is enabled and a user sends a normal private text that is not represented as an `At`, reply-to-bot, forced engage, or another pre-marked strong wakeup.
- **Real call chain:** `main.py:on_global_message` -> `message_entry.handle_global_message()` -> `PluginFacade.prepare_conversation_turn()` -> `ChatLoopKernel.tick(trigger="message")` -> `AttentionGate.process_event()` -> `PrivateChatManager.signal_new_message()` -> return `PRIVATE_WAIT` -> `is_direct_call_event()` is true for every private event -> `suppress_default_llm_if_engaged()` suppresses AstrBot's default reply.
- **Actual behavior:** The message is appended to `PrivateChatSession.pending_messages` and the gate exits. No production consumer calls `get_pending_messages()`, and the follow-up waiter only observes an event/timeout; it does not redispatch the buffered event. Initial private messages and later ordinary follow-ups therefore produce no AstrMai reply.
- **Expected behavior:** An initial ordinary private message should enter attention/System2. A message should be converted to a wait-resume signal only when an existing private follow-up wait is actually active, and the resumed message must be dispatched.
- **Production impact:** The enabled private-chat feature is functionally silent for the normal message shape used by private adapters; users see missing replies and accumulated, never-consumed pending text.
- **Why existing guards fail:** `is_private` is not itself included in `is_strong_wakeup`; `signal_new_message()` does not report whether a waiter existed; `PRIVATE_WAIT` is treated as a successful non-error status; and private events always suppress the host LLM.
- **Classification:** confirmed.
- **Confidence:** 0.99.

### FFA-02-002 / P1: Group attention sessions use raw `group_id`, allowing different AstrBot origins to share one focus pool

- **File:line:** `astrmai/conversation/attention/perception.py:15-16`; downstream use at `astrmai/conversation/attention/gate.py:745-817`.
- **Trigger:** Two configured adapters/bot accounts have group events with the same raw group identifier, and their messages arrive within the attention-window/debounce lifetime.
- **Real call chain:** each event has a distinct `unified_msg_origin` in `MessageScope` -> `PluginFacade.record_and_dispatch_attention()` ticks each UMO -> `PerceptionBuilder.build()` replaces the UMO with raw `group_id` -> `_get_or_create_session(group_id)` returns the same `SessionContext` -> both events enter the same `accumulation_pool`/`attention_window` -> one focus event is selected and its System2 call receives the mixed event list.
- **Actual behavior:** Messages from distinct production conversations can be normalized, scored, and threaded together. The eventual reply is sent to the selected focus event's UMO while its prompt context may contain the other adapter/account's messages.
- **Expected behavior:** Every attention/session/runtime key must use the collision-resistant AstrBot conversation origin (or an equivalently namespaced key) consistently.
- **Production impact:** Cross-conversation context contamination, wrong focus selection, replies based on another group's message, and state updates attributed to the wrong conversation.
- **Why existing guards fail:** Ingress dedupe and System2 locks are keyed by UMO, but the contamination occurs later in the raw-`group_id` focus pool before those UMO-scoped mechanisms can isolate work. Private chats use UMO, so this bug is specifically in the group branch.
- **Classification:** confirmed.
- **Confidence:** 0.98.

### FFA-02-003 / P1: Immediate engage both launches System2 and leaves the same event in the accumulation pool

- **File:line:** `astrmai/conversation/attention/gate.py:552-566`.
- **Trigger:** `astrmai_force_engage` is set (group-wait resume/proactive injection), or a strong wakeup has a simple payload, while the session is idle or already has a debounce worker/pending messages.
- **Real call chain:** `AttentionGate.process_event()` -> `_handle_force_engage()` or `_handle_fast_wakeup()` -> `_engage_immediately()` -> replace `session.accumulation_pool` with `[event]` -> independently `_fire_priority_task(sys2_process(event, [event]))` -> return `ENGAGED`. A current worker, or the next ordinary message that starts a worker, later drains the still-present event and schedules System2 again.
- **Actual behavior:** Existing pending messages are overwritten. The immediate event is processed once directly and can be selected a second time from the pool; when no worker exists it remains stale until a later message arrives.
- **Expected behavior:** Immediate engage should atomically detach the event from buffered work, preserve unrelated pending messages, and establish exactly one owner for System2 execution.
- **Production impact:** Lost inputs, duplicate planning/tool side effects, repeated model cost, and delayed later replies. Final-send claims may hide the second visible text but do not undo duplicate upstream execution.
- **Why existing guards fail:** `_engage_immediately()` neither clears after taking ownership nor updates `session.is_evaluating`; the send claim is reached only at final output, after planning and possible actions have already run.
- **Classification:** confirmed.
- **Confidence:** 0.99.

### FFA-02-004 / P1: A historical direct wakeup can monopolize focus selection for the 180-second attention-window TTL

- **File:line:** `astrmai/conversation/attention/focus_selector.py:14-28`; merge/retention call sites `astrmai/conversation/attention/gate.py:854-921` and `window_buffer.py:60-69`.
- **Trigger:** A direct/reply/`At` message is retained after one evaluation, then one or more ordinary group messages arrive before `ATTENTION_WINDOW_TTL_SECONDS` expires.
- **Real call chain:** prior batch is appended to `attention_window` -> next debounce merges old window events with the new batch -> `select_focus_event()` scores the old direct event with +700/+800/+1000 while a new ordinary event receives only base/recency/follow-up points -> old event wins -> judge is skipped because it is direct -> System2 is launched again for the old event.
- **Actual behavior:** The new message is not the focus. The already-processed strong event is repeatedly replanned; generation or send-claim checks usually block its final text, leaving the new batch with no corresponding reply decision.
- **Expected behavior:** Historical events may provide context, but only unconsumed/current-batch events should be eligible to own the next focus, or already handled events must be marked ineligible.
- **Production impact:** Repeated stale LLM work and a multi-minute period where normal group traffic is ignored or evaluated against the wrong anchor after someone directly called the bot.
- **Why existing guards fail:** Recency can remove at most 90 points and cannot offset direct-wakeup bonuses; neither the attention window nor normalized event carries an answered/consumed marker; message-cache dedupe is applied only at `process_event()` ingress, not when old window events are reconsidered.
- **Classification:** confirmed.
- **Confidence:** 0.99.

### FFA-02-005 / P1: Advancing generation does not cancel stale System2 work, so obsolete turns block the current turn

- **File:line:** `astrmai/conversation/attention/gate.py:920-921`; ownership is checked only at `astrmai/conversation/execution/reply_freshness.py:54-82`, after `astrmai/conversation/execution/system2_runner.py:43-51` has run the planner under a chat-wide lock.
- **Trigger:** Turn A has started System2; a rapid valid message creates turn B and advances the same generation key before A finishes.
- **Real call chain:** turn A attention schedules detached `sys2_process` -> `System2Runner.run()` acquires the UMO lock and executes the full planner -> message B binds a newer `TurnIdentity` and schedules another System2 task -> B waits behind A -> A's generation is checked only when building/sending its visible reply.
- **Actual behavior:** A continues model calls and state/action work to completion despite already being obsolete. Its text is finally blocked as stale, then B can begin.
- **Expected behavior:** New generation ownership should cancel stale queued work and provide cancellation/current-turn checkpoints before expensive planning and before side effects, so the current turn is not held behind known-obsolete work.
- **Production impact:** User-visible latency spikes, head-of-line blocking, wasted model/tool work, and stale non-send side effects even though the final reply guard appears to work.
- **Why existing guards fail:** `is_current_turn()` is only called by reply freshness; the chat-wide System2 lock serializes but does not supersede; no task registry maps generation owners to cancellable System2 tasks.
- **Classification:** confirmed.
- **Confidence:** 0.98.

### FFA-02-006 / P1: Poke and proactive force-engage paths have no `TurnIdentity`, so newer messages cannot revoke their replies

- **File:line:** `astrmai/conversation/ingress/sensors.py:517-531`; proactive equivalent `astrmai/proactive/dispatcher.py:288-322`; turn binding exists only in `astrmai/presentation/events/message_entry.py:162`.
- **Trigger:** A poke or proactive synthetic event starts System2, then a normal user message arrives before that work sends its reply.
- **Real call chain:** poke: `handle_poke_if_needed()` -> `PreFilters.process_poke_event()` mutates the event -> direct `attention_gate.process_event()`; proactive: dispatcher -> `inject_external_event()` -> force engage. Both bypass `PluginFacade.prepare_conversation_turn()`. Reply freshness sees no `astrmai_turn_identity`, and send-claim setup also skips events without a turn.
- **Actual behavior:** The older poke/proactive work remains eligible to send after a newer user turn. In the proactive path, `_proactive_dispatching` is cleared as soon as `process_event()` has merely scheduled detached System2, not when generation/sending finishes.
- **Expected behavior:** Every reply-capable ingress should receive a turn owner before attention dispatch, and proactive serialization should cover the actual reply task or yield to newer user activity.
- **Production impact:** Out-of-order or duplicate-looking replies, proactive text emitted after the user has already changed context, and no idempotent final-send protection for these paths.
- **Why existing guards fail:** `ChatRuntimeCoordinator.is_current_turn(None)` intentionally preserves legacy behavior; send claim requires a non-null turn; activity freshness cannot replace generation ownership reliably, especially because group activity and reply checks currently use different chat keys.
- **Classification:** confirmed.
- **Confidence:** 0.98.

### FFA-02-007 / P1: Detached System2 exceptions are logged but never reach the ingress fallback

- **File:line:** `astrmai/conversation/attention/gate.py:324-343` and `gate.py:920-921`; ingress fallback at `astrmai/presentation/events/message_entry.py:200-210`.
- **Trigger:** The System2 callback raises after attention has returned `BUFFERED`/`ENGAGED`, including a planner/provider cascade failure.
- **Real call chain:** message entry awaits `ChatLoopKernel.tick()` -> gate stores the event and returns success immediately -> message entry suppresses the host LLM -> detached System2 task later fails -> `_handle_task_result()` logs the exception and discards the task.
- **Actual behavior:** No fallback reply is sent. The fallback at message entry only handles a synchronous `record_and_dispatch_attention` exception/status `error`, which can no longer observe the detached failure.
- **Expected behavior:** The detached owner must translate terminal System2 failure into the configured fallback (subject to current-turn ownership) or propagate a completion result to an owner that can do so.
- **Production impact:** Provider/planner failures become silent missing replies despite a configured fallback and despite ingress reporting successful engagement.
- **Why existing guards fail:** Task callbacks only log; `System2Runner.run()` has no fallback catch; the host LLM has already been suppressed by the successful attention status.
- **Classification:** confirmed.
- **Confidence:** 0.99.

### FFA-02-008 / P2: Threaded group waits are registered and looked up with incompatible thread identifiers

- **File:line:** `astrmai/state/group_wait/group_reply_wait_manager.py:53-62`, `241-249`, and `301-336`; resolver fallback at `astrmai/conversation/threading/group_thread_resolver.py:42-55`.
- **Trigger:** `group_thread_wait_enabled=true`; a bot reply arms a wait, then the target follows up by `At`-mentioning the bot without a Reply component.
- **Real call chain:** outgoing focus processing creates `astrmai_thread_signature` -> `register_from_reply_event()` chooses that signature as the wait bucket key -> incoming turn is bound before wait handling and `resolve_group_thread()` falls back to `sender:<id>` -> `handle_incoming_message()` performs exact lookup by that sender key -> outbound-message-id recovery is unavailable because the incoming event is `At`, not Reply -> returns `NONE`.
- **Actual behavior:** The intended follow-up does not resume or consume the existing wait. It is processed as a new direct turn and can arm a second wait while the old one remains live until timeout/budget; a later reply to the old message can resume stale context.
- **Expected behavior:** Resolver, generation, and wait manager must share one stable thread identity, with `At`/target fallback able to associate the active wait when unambiguous.
- **Production impact:** Stale waits, duplicate active waits for one conversational thread, incorrect late resumes, and ineffective thread-level cancellation under the gray switch.
- **Why existing guards fail:** Reply-ID scanning only repairs Reply components; `astrmai_thread_signature` is produced after turn binding; cancellation/info lookups continue to use the preliminary turn thread ID rather than the registered wait key.
- **Classification:** confirmed.
- **Confidence:** 0.97.

### FFA-02-009 / P2: Ingress dedupe drops legitimate repeated messages because it ignores AstrBot message identity

- **File:line:** `astrmai/conversation/ingress/dedupe.py:14-37`.
- **Trigger:** The same sender intentionally sends identical text twice in the same conversation within 1.5 seconds, or sends two non-text payloads whose string representations have the same length.
- **Real call chain:** `handle_global_message()` -> `check_message_dedup()` -> fingerprint is only `chat_id + sender_id + message text` (or `obj_len_N`) -> second distinct event matches the cache -> event is stopped before poke/command/scope/turn handling.
- **Actual behavior:** A distinct AstrBot event with its own `message_id` is discarded as a framework duplicate. Non-text events are especially collision-prone because only serialized length is retained.
- **Expected behavior:** Framework redelivery should be deduped by stable message/event identity; content fallback should not collapse distinct user actions when an ID is present.
- **Production impact:** Missing repeated user inputs, dropped image/notice/poke actions, and no downstream state or reply for the second event.
- **Why existing guards fail:** The fingerprint never reads `message_obj.message_id`; UMO and sender isolation prevent cross-chat collisions but cannot distinguish two valid events from the same sender.
- **Classification:** confirmed.
- **Confidence:** 0.99.

### FFA-02-010 / P2: External plugin results are declared injected but are filtered as self messages before entering attention context

- **File:line:** `astrmai/conversation/ingress/external_result_bridge.py:46-61`; synthetic identity at `astrmai/conversation/attention/gate.py:26-47`; filter at `astrmai/conversation/ingress/sensors.py:67-69`.
- **Trigger:** `on_decorating_result` observes an allowed external plugin result with visible text.
- **Real call chain:** `bridge_external_plugin_result()` builds data containing content/timestamp only -> `inject_external_event()` creates `_SyntheticExternalEvent` -> both `get_sender_id()` and `get_self_id()` return empty strings -> `AttentionGate.process_event()` applies the primary mood update, then `_passes_sensor_filters()` -> `should_process_message()` treats `"" == ""` as a self message and returns false.
- **Actual behavior:** The bridge logs that the result was injected, but it never reaches accumulation/dialogue context. It may still update mood as though the external bot output were incoming user text before being filtered.
- **Expected behavior:** External bot output should be recorded as an assistant/external context segment without triggering a new reply and without being interpreted as user mood input.
- **Production impact:** Later replies lack parser/built-in-plugin output context, while mood/state can be skewed by text that was never accepted into the conversation window.
- **Why existing guards fail:** The synthetic event has no explicit assistant identity/role; the loop-source checks only prevent recursion; sensor filtering occurs after mood mutation and has no external-result branch.
- **Classification:** confirmed.
- **Confidence:** 0.99.

### FFA-02-011 / P2: Debounce session workers are not cancelled by chat clearing or plugin shutdown

- **File:line:** `astrmai/conversation/attention/gate.py:345-380` and `gate.py:138-151`; adjacent shutdown collector `astrmai/shared/helpers/plugin_helpers.py:165-171`.
- **Trigger:** A debounce worker is sleeping or judging when the bot leaves a group, chat state is cleared, or the plugin terminates/reloads.
- **Real call chain:** `_spawn_session_worker()` stores the task only in `_session_tasks` -> `clear_chat_state()` removes maps but does not cancel matching tasks -> lifecycle `collect_background_tasks()` collects only owners' `_background_tasks` -> the session task retains its detached `SessionContext`, wakes, and may launch a new `_background_tasks` System2 task after cleanup/task collection.
- **Actual behavior:** Cleared sessions can continue processing and dispatching; shutdown can finish while untracked session workers are alive, and those workers can create new work against disposing runtime components.
- **Expected behavior:** Session workers must be tracked by chat and cancelled/awaited during `clear_chat_state()` and shutdown before dependent services are disposed.
- **Production impact:** Replies attempted after group removal/reload, use-after-shutdown failures, duplicate work after a new runtime starts, and nondeterministic teardown.
- **Why existing guards fail:** `_session_tasks` is separate from `_background_tasks`; done callbacks only remove completed tasks/recover failures; clearing focus maps does not affect task-held session objects.
- **Classification:** confirmed.
- **Confidence:** 0.98.

## Production paths reviewed without an additional confirmed defect

- Framework command short-circuit and HeartCore command bypass into dedicated command handlers.
- Message-scope construction and UMO-based group/private decision at presentation ingress.
- `TurnIdentity` construction, generation advancement, final send-key construction, send claims, and generation freshness checks.
- Reply/At/direct-wakeup normalization, focus-thread construction, direct and extracted vision references, judge timeout/config wiring, and judge fallback actions.
- Group-wait timeout/budget bookkeeping, reply-ID matching, wait resume extras, and hot-config refresh.
- Conversation concurrency configuration wiring: generation, final-send claim, threaded group wait, non-conversational guard, and debug trace all enter production runtime logic; the defects above concern their placement/identity semantics rather than unused values.
- Dialogue-store locking and compaction scheduling were followed far enough to verify their ingress/focus call cooperation; no separate reachable corruption defect was confirmed in those paths.

## Overall assessment

No P0 defect was confirmed. The current tree has multiple P1 conversation-control failures: private chat is not dispatching ordinary messages, attention identity is inconsistent between UMO and raw group IDs, immediate/retained focus paths can duplicate stale work, and generation ownership does not govern all reply-capable ingress or cancel obsolete System2 execution. These issues are reachable through normal production event flows and can manifest as missing, delayed, out-of-order, or contextually incorrect replies.
