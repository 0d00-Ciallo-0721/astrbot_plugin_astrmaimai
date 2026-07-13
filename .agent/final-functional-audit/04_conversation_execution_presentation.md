# AstrMai Final Functional Audit: Conversation Execution / Loop / Presentation

## Audit result

- Scope: current working tree production paths under `astrmai/conversation/execution/`, `astrmai/conversation/loop/`, and `astrmai/presentation/`, plus adjacent production call sites needed to prove reachability.
- Result: **9 confirmed functional defects** (`P0: 0`, `P1: 4`, `P2: 5`, `P3: 0`).
- The dominant contract break is that Planner receives only `reply_text`, while `ReplyService` does not return the `VisibleReplyArtifact` or a send outcome. Stale, duplicate, failed, and partial sends therefore become indistinguishable from successful final sends above the presentation boundary.

## Findings

### FF-01 / P1: Private-chat continuation is never armed, so normal private messages are buffered without reaching System2

- **File:line:** `astrmai/conversation/execution/followup_manager.py:80`; adjacent proof at `astrmai/presentation/events/message_entry.py:90`, `astrmai/presentation/events/message_entry.py:162`, and `astrmai/conversation/attention/gate.py:774`.
- **Trigger:** A private-chat reply is sent, followed by a normal private message that is not an `@`, reply-to-bot, nickname-only wakeup, or forced engagement. A first ordinary private message can hit the same failure directly.
- **Real call chain:** `main.AstrMaiPlugin.on_global_message()` -> `presentation.events.message_entry.handle_global_message()` -> `PluginFacade.record_and_dispatch_attention()` -> `ChatLoopKernel.tick(trigger="message")` -> `AttentionGate.process_event()`. After a successful reply, `System2Runner._finalize_followups()` -> `FollowupManager.finalize_after_reply()` reads `main_event.get_extra("is_private_chat", False)`. No production ingress path writes that extra; private mode exists only in `MessageScope`/`TurnIdentity`. The branch that calls `PrivateChatManager.wait_for_new_message()` is therefore skipped. On the next normal private message, `AttentionGate.process_event()` calls `signal_new_message()` and returns `PRIVATE_WAIT` without scheduling System2.
- **Actual behavior:** The message is appended to a private-session pending buffer, but there is no active waiter to consume the signal and no planner execution is started. AstrMai private conversation processing stalls.
- **Expected behavior:** A successful private reply must arm the private continuation waiter, or a private message with no active waiter must enter the normal attention/System2 path.
- **Production impact:** Ordinary DMs can receive no AstrMai response, and a conversation that began through a strong wakeup stops on the next natural follow-up message.
- **Why existing guards fail:** `is_direct_call_event()` is only used later for host-result suppression. It is not propagated to follow-up ownership. `PrivateChatManager.signal_new_message()` creates/buffers a session even when `is_bot_waiting` is false, so the missing waiter is not surfaced as an error.
- **Classification:** confirmed.
- **Confidence:** 99%.

### FF-02 / P1: A failed outbound send permanently poisons the turn claim and suppresses the fallback-model reply

- **File:line:** `astrmai/conversation/execution/reply_artifact_builder.py:381`, `astrmai/conversation/execution/reply_artifact_builder.py:450`, and `astrmai/infrastructure/runtime/chat_runtime_coordinator.py:153`.
- **Trigger:** `context.send_message()` raises after the final send key has been claimed, and another configured agent model is available.
- **Real call chain:** `Planner._invoke_planning_llm()` -> `ConcurrentExecutor.execute()` -> `_run_text_mode()`/`_run_tool_mode()` -> `_finalize_reply()` -> `ReplyService.handle_reply()` -> `_send_segments()` -> `claim_send()` -> `context.send_message()` raises -> `mark_send_failed()`. The exception returns to the executor's broad per-model `except`, which tries the next model. That model reaches `_send_segments()` with the same turn/final key, but `claim_send()` rejects every existing key regardless of its `failed` status.
- **Actual behavior:** The retry model may generate a valid answer, but its send is rejected as a duplicate. `_finalize_reply()` still returns the text as if execution succeeded, so the model loop stops and the user receives no reply.
- **Expected behavior:** A failed outbound attempt must remain retryable for the same turn, while only a committed/in-flight successful owner should block a duplicate final send.
- **Production impact:** A transient platform send failure defeats the configured model fallback chain and converts a recoverable send into a silent missing reply; the rejected retry can also be recorded as an unsent bot reply.
- **Why existing guards fail:** `mark_send_failed()` preserves the key, `claim_send()` checks key presence rather than claim status, and the executor treats outbound-send exceptions as model failures inside the same retry loop.
- **Classification:** confirmed.
- **Confidence:** 99%.

### FF-03 / P1: Stale suppression blocks visibility but records the unsent answer as real conversation state

- **File:line:** `astrmai/conversation/execution/reply_service.py:89`, `astrmai/conversation/execution/reply_service.py:98`, `astrmai/conversation/execution/executor.py:521`, and `astrmai/conversation/planning/planner.py:1398`.
- **Trigger:** A newer turn/activity arrives after the model call starts but before `ReplyService.handle_reply()` performs its final freshness check.
- **Real call chain:** `Planner` -> `ConcurrentExecutor._run_*_mode()` obtains valid model text -> `_finalize_reply()` -> `ReplyService.handle_reply()` -> `_check_reply_freshness()` returns `EXPIRED` -> `handle_reply()` returns without sending. Control then resumes in `_finalize_reply()`, which unconditionally calls `evolution_manager.process_bot_reply()` and returns the text. `Planner._finalize_plan_result()` unconditionally appends the same text to the dialogue store and may mark a proactive dispatch sent because `bool(reply_text)` is true.
- **Actual behavior:** The stale answer is correctly hidden from the platform, but is persisted as if the bot said it. Subsequent prompts, learning logs, compaction, and proactive status can observe a phantom assistant turn.
- **Expected behavior:** A stale/blocked artifact must propagate a non-sent outcome to Executor and Planner, and no visible-reply history or sent status should be committed.
- **Production impact:** Conversation state diverges from what users actually saw, causing context jumps, false continuity, and incorrect proactive completion state.
- **Why existing guards fail:** `ReplyService.handle_reply()` has no return contract for sent/blocked/partial status. Executor equates validated model text with a sent reply, and Planner receives only `reply_text`, not the `VisibleReplyArtifact` or final-send result.
- **Classification:** confirmed.
- **Confidence:** 99%.

### FF-04 / P2: Planner-generated follow-up messages are always rejected as duplicate finals

- **File:line:** `astrmai/conversation/planning/planner.py:1430` and `astrmai/conversation/execution/reply_artifact_builder.py:380`.
- **Trigger:** The first reply is sent successfully and `_should_follow_up()` returns a reason under the configured follow-up probability/eligibility rules.
- **Real call chain:** `Planner._finalize_plan_result()` sends the first answer through `ConcurrentExecutor`/`ReplyService`, then sleeps and calls `self.executor.execute()` again with the same event and `TurnIdentity`. Both calls reach `_send_segments()`, which hard-codes `build_turn_send_key(turn, "final")`. The first call committed that key; the second call is rejected by `claim_send()`.
- **Actual behavior:** The follow-up model call runs and consumes latency/model capacity, but the intended second message is never visible. Because the executor cannot see that send failure, the unsent follow-up can still enter the bot-reply learning log.
- **Expected behavior:** The deliberate follow-up must have distinct response ownership from the turn's final reply while remaining idempotent within its own response kind.
- **Production impact:** The configured natural second-message feature is functionally disabled whenever send-claim protection is enabled (the default), with hidden model cost and phantom state side effects.
- **Why existing guards fail:** Send ownership models only one hard-coded `final` response kind; Planner does not label the second execution as `followup`, even though `build_turn_send_key()` supports a response-kind parameter.
- **Classification:** confirmed.
- **Confidence:** 99%.

### FF-05 / P2: Mid-segmentation stale suppression persists unsent tail segments as if the full reply was delivered

- **File:line:** `astrmai/conversation/execution/reply_artifact_builder.py:424`, `astrmai/conversation/execution/reply_artifact_builder.py:430`, and `astrmai/conversation/execution/reply_service.py:138`.
- **Trigger:** A reply is split into two or three segments and newer activity arrives during the inter-segment `asyncio.sleep()`.
- **Real call chain:** `ReplyService.handle_reply()` builds one artifact containing all segments -> `_send_segments()` sends segment 0 -> sleeps -> the next per-segment freshness check returns `EXPIRED` -> the loop breaks. `artifact.sent` remains true, the send claim is committed, and `_send_segments()` returns true. `handle_reply()` then ingests `artifact.persistable_text` (the full original reply), followed by Executor bot-reply recording and Planner dialogue recording of the full text.
- **Actual behavior:** The user sees only the prefix segment(s), while memory/dialogue/learning state contains the unsent tail and the turn is reported as fully sent.
- **Expected behavior:** A partial send must expose which segments were delivered and persist only the delivered visible text (or explicitly represent a partial outcome).
- **Production impact:** Later conversation context can refer to statements the user never received; follow-up wait and send-claim state also treat the truncated delivery as complete.
- **Why existing guards fail:** The freshness guard only breaks the send loop. It never rewrites `segments`/`persistable_text`, never returns a partial status, and `artifact.sent` is a single boolean set by the first successful segment.
- **Classification:** confirmed.
- **Confidence:** 98%.

### FF-06 / P2: External plugin results lose message scope and are routed into an empty private-session buffer

- **File:line:** `astrmai/presentation/events/result_sniffer.py:11` and `astrmai/conversation/ingress/external_result_bridge.py:52`; adjacent proof at `astrmai/conversation/attention/gate.py:685` and `astrmai/conversation/attention/gate.py:774`.
- **Trigger:** An allowed external/built-in plugin produces a non-empty result on a group or private event.
- **Real call chain:** `on_decorating_result` -> `sniff_external_plugin_results()` -> `bridge_external_plugin_result()` -> `build_external_reply_event(reply_text)` -> `AttentionGate.inject_external_event(chat_id, data)` -> `ChatLoopKernel.tick(trigger="external")` -> `AttentionGate.process_event(synthetic_event)`. The synthetic payload contains content and flags but no `group_id`, sender ID, or bot ID; only `unified_msg_origin` is added. `PerceptionBuilder` therefore classifies it as private, and the private branch calls `signal_new_message(sender_id="", ...)` and returns `PRIVATE_WAIT`.
- **Actual behavior:** The external result does not enter the normal attention accumulation/planning context. It is buffered under an empty private-session identity, even when the origin is a group conversation.
- **Expected behavior:** External bot results must retain/derive the original scope and bot identity, or bypass user-private-message routing through an explicit external-result path.
- **Production impact:** AstrMai cannot reliably reason over external plugin outputs in the next turn, and meaningless private-session state is accumulated under an empty sender.
- **Why existing guards fail:** The source whitelist proves provenance only. `is_external_bot_reply` is not consulted before the generic private-message branch, and the synthetic event shape omits the fields used by `PerceptionBuilder` to determine scope.
- **Classification:** confirmed.
- **Confidence:** 99%.

### FF-07 / P2: Result sniffing runs before ghost/error interception, allowing blocked output to mutate state

- **File:line:** `main.py:184`, `main.py:209`, `astrmai/conversation/ingress/external_result_bridge.py:46`, and `astrmai/conversation/execution/outbound_error_policy.py:20`.
- **Trigger:** A non-streaming decorated result is either an unmarked AstrMai ghost sentinel or an allowed external result matching an interception phrase such as `处理失败`.
- **Real call chain:** AstrBot runs the default-priority result-sniffer hook before the priority-90 error interceptor -> `bridge_external_plugin_result()` extracts the text, injects it through the external-event path, and calls `record_bot_reply()` -> only afterward `intercept_outbound_error()` recognizes the ghost/error and clears the visible result (and may stop propagation).
- **Actual behavior:** Output that is intentionally hidden can already modify attention/private-session state and bot-reply history. For example, `处理失败` is intercepted by `HostBridge.ERROR_KEYWORDS` but is not rejected by `BotReplyRecorder.ERROR_KEYWORDS`, so it is persisted before being blocked.
- **Expected behavior:** Ghost/error classification must occur before any external-result injection or history commit, so blocked output has no conversational side effects.
- **Production impact:** Internal sentinel/error text can contaminate future context and learning state even though users never saw it.
- **Why existing guards fail:** The bridge only checks `astrmai_is_self_reply`, loop source, and source whitelist. It has no ghost/error guard, and the later interceptor cannot roll back injection or recording already performed.
- **Classification:** confirmed.
- **Confidence:** 97%.

### FF-08 / P2: `error_interception_mode` values do not control the documented runtime behavior

- **File:line:** `astrmai/conversation/execution/outbound_error_policy.py:33` and `config.py:34`.
- **Trigger:** A decorated result matches an error keyword while `error_interception_mode` is `log_only`, `block_only`, or `block_and_stop`.
- **Real call chain:** `main.intercept_and_notify_errors()` -> `presentation.events.error_interceptor.intercept_and_notify_errors()` -> `intercept_outbound_error()` -> `event.set_result(None)` executes before the mode is examined -> only `log_only` returns early; every other value calls `event.stop_event()`.
- **Actual behavior:** `log_only` still blocks the result because it was already cleared. `block_only` also stops the entire event. `block_and_stop` is behaviorally identical to `block_only`.
- **Expected behavior:** `log_only` must leave the result intact, `block_only` must clear only the result, and `block_and_stop` must clear it and stop propagation, matching the production configuration contract.
- **Production impact:** Operators cannot select the advertised fallback behavior; changing the configuration can unexpectedly suppress user-visible output or terminate downstream processing.
- **Why existing guards fail:** The destructive clear happens before mode dispatch, and the branch distinguishes only `log_only` from "all other values" rather than the three configured modes.
- **Classification:** confirmed.
- **Confidence:** 100%.

### FF-09 / P1: The ingress error fallback is yielded without stopping the AstrBot event

- **File:line:** `astrmai/presentation/events/message_entry.py:206`.
- **Trigger:** `record_and_dispatch_attention()` raises, including a `ChatLoopKernel.tick()`/attention dispatch failure.
- **Real call chain:** `main.AstrMaiPlugin.on_global_message()` -> `PluginFacade.on_global_message()` -> `handle_global_message()` -> `record_and_dispatch_attention()` raises -> the exception is converted to `status = "error"` -> `event.plain_result(fallback_text)` is yielded -> the function returns without `event.stop_event()`.
- **Actual behavior:** The fallback is sent, but the event remains propagating, so later handlers and the framework LLM path are still eligible to process the same user message and can produce an additional reply.
- **Expected behavior:** Once AstrMai emits its terminal processing-error fallback, it must stop the event so the failed turn has exactly one user-visible terminal response.
- **Production impact:** Failure paths can produce duplicate/conflicting replies and invoke downstream work after AstrMai has already told the user the turn failed.
- **Why existing guards fail:** This branch returns before `suppress_default_llm_if_engaged()` and contains no equivalent stop call. Yielding a result does not itself terminate AstrBot event propagation.
- **Classification:** confirmed.
- **Confidence:** 99%.

## Production paths reviewed

- Ingress: `main.on_global_message` -> `PluginFacade` -> `message_entry` -> `ChatLoopKernel` -> `AttentionGate` -> background System2 dispatch.
- Planning/execution: Planner result creation -> `ConcurrentExecutor` text/tool paths -> `ReplyService` -> `VisibleReplyArtifact` construction -> claim/freshness checks -> segmented active sends -> post-send state updates.
- Follow-up ownership: Planner's deliberate second message, `System2Runner` finalization, private/group wait registration, loop wait synchronization/resume/expiry.
- State recording: memory-turn ingestion, evolution bot-reply recording, Planner dialogue segments, proactive completion flags, outbound message IDs.
- Presentation/external paths: ghost suppression, error fallback and interception modes, decorated-result sniffing, external synthetic-event injection.
