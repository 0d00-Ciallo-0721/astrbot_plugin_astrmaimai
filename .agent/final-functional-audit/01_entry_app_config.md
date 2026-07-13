# AstrMai Final Functional Audit: Entry, App, and Configuration

## Audit result

- Findings: **5** (`P0: 0`, `P1: 3`, `P2: 2`, `P3: 0`)
- Classification: **4 confirmed**, **1 partial**
- Audited working tree: current production files, including uncommitted changes in `config.py`, `_conf_schema.json`, `astrmai/app/bootstrap.py`, `astrmai/app/plugin_facade.py`, and `astrmai/app/runtime_facade_protocol.py`.

## Findings

### FFA-ENTRY-001 / P1: Threaded group waits are collapsed back into one chat-wide ChatLoop wait

- **File:line:** `astrmai/app/plugin_facade.py:355`, `astrmai/app/plugin_facade.py:357`, `astrmai/app/plugin_facade.py:367`; originating wiring at `astrmai/app/bootstrap.py:336`.
- **Trigger:** Enable `conversation.group_thread_wait_enabled`, arm waits for two different threads in the same group, then receive a message that resumes or interrupts either thread.
- **Real call chain:** `main.py:203 AstrMaiPlugin.on_global_message` -> `PluginFacade.on_global_message` -> `message_entry.handle_global_message` -> `PluginFacade.prepare_conversation_turn` -> `PluginFacade.handle_group_reply_wait` -> `GroupReplyWaitManager.handle_incoming_message` -> `ChatLoopKernel.resume_wait/expire_wait/arm_group_wait` -> `PluginFacade.record_and_dispatch_attention` -> `ChatLoopKernel.tick`.
- **Actual behavior:** `GroupReplyWaitManager` can retain multiple states per chat keyed by `thread_id`, but every Facade call into `ChatLoopKernel` supplies only `chat_id`. `ChatLoopKernel.arm_group_wait()` stores one wait in the chat-wide `ChatLoopState`, so the second thread overwrites the first. Resuming thread A then clears whichever chat-wide wait is present, including thread B's wait. During the subsequent tick, adapter resynchronization can arm B again, but the A event's `astrmai_group_wait_resume` marker immediately makes the chat-wide state look resumed and clears it again. The manager may still contain B while the kernel reports no active wait.
- **Expected behavior:** With threaded waiting enabled, manager and ChatLoop state must address the same `(chat_id, thread_id)` wait, and activity in one thread must not clear or interrupt another thread's wait.
- **Production impact:** Unrelated group threads interfere with each other's wait lifecycle. Heartbeat/proactive scheduling can run while another thread is still waiting, wait diagnostics diverge from the authoritative manager, and continuation behavior becomes order-dependent under normal concurrent group traffic.
- **Why existing guards fail:** Thread isolation exists only inside `GroupReplyWaitManager`. The Facade drops `thread_id` when calling the kernel, and the kernel's wait APIs and state store have no thread parameter.
- **Classification:** confirmed.
- **Confidence:** high.

### FFA-ENTRY-002 / P1: Hot-enabling Sys3 exposes `/work` before the Sys3 router exists

- **File:line:** `astrmai/app/plugin_facade.py:209`, `astrmai/app/plugin_facade.py:630`, `astrmai/app/plugin_facade.py:648`; initial degradation path at `astrmai/app/bootstrap.py:268`.
- **Trigger:** Start with `sys3.enable_work_mode=false`, keep a valid agent model configured, change the setting to `true` through the admin configuration flow without restarting, then invoke `/work`. The same state is reachable if enabled Sys3 construction is caught as an optional-component failure.
- **Real call chain:** `SettingsUiService.update_section` -> `PluginApiAdapter.apply_config` -> `PluginFacade.apply_hot_config` -> `PluginRuntimeContext.rebuild_infrastructure_settings` -> `main.py:217 AstrMaiPlugin.enter_sys3_direct` -> `handle_work_mode` -> `PluginFacade.enter_sys3_direct`.
- **Actual behavior:** Hot apply immediately makes `runtime.feature_flags.work_mode_enabled` true, but it does not construct `runtime.sys3_router` or `runtime.cron_guard`. `/work` passes the feature-flag guard and dereferences `self.runtime.sys3_router` at line 648. The resulting `AttributeError` occurs before the method's `try` block, so the command does not return its normal failure reply.
- **Expected behavior:** Reload-required Sys3 changes should leave the live feature flag unchanged until restart, or hot apply should atomically create and start the complete Sys3 stack. A degraded/missing router should produce a deterministic unavailable/restart-required reply.
- **Production impact:** A supported admin action puts the live plugin into a command-crashing state until restart. The inverse toggle also leaves the already-running cron guard alive while the live feature flag says work mode is disabled.
- **Why existing guards fail:** `_requires_reload()` only reports advisory metadata; it does not prevent `_apply_hot_config()` from mutating live flags. `/work` checks only the config-derived feature flag, not router availability or `runtime.status.work_mode_enabled`.
- **Classification:** confirmed.
- **Confidence:** high.

### FFA-ENTRY-003 / P1: Hot config reports success while multiple runtime components keep stale operational values

- **File:line:** `astrmai/app/plugin_facade.py:193`, `astrmai/app/plugin_facade.py:215`; apply-result path at `astrmai/webui/backend/adapters/plugin_api.py:450`.
- **Trigger:** Change a nominally hot-applicable setting such as `reply.base_frequency`, `private_chat.wait_timeout_sec`, conversation compaction/prefix settings, dialogue-store TTLs, or evolution window settings through section update, replace, or reset.
- **Real call chain:** `SettingsUiService.update_section/replace_config/reset_section` -> `PluginApiAdapter.apply_config` -> `PluginFacade.apply_hot_config` -> `_apply_runtime` -> `_refresh_components` -> API returns `status=ok`, `runtime_bound=true`, generally `reload_required=false` for these sections.
- **Actual behavior:** The central dispatcher updates `runtime.config`, then treats calling any available `refresh_config()` as a complete apply. Several operational values remain stale:
  - `FrequencyController.refresh_config()` changes only `self.config`, while reply decisions continue using `BASE_FREQ` captured at construction.
  - `PrivateChatManager.refresh_config()` changes only `self.config`, while waits continue using the old `timeout_sec`.
  - `ContextCompactionEngine` implements `refresh_config()` but is absent from the Facade component list.
  - `ContextEngine`, `Planner`, `PersonaSummarizer`, `PromptRefiner`, and `ReActRetriever` retain initial config references and cached prefix/retrieval values.
  - `EvolutionManager.refresh_config()` does not refresh its recorder/miner objects, and `ExpressionGovernanceRunner.interval_seconds` is not refreshed.
  - `StateEngine.refresh_config()` does not update `ChatStateService.config`.
- **Expected behavior:** Every setting advertised as live-applied must update all derived/cached runtime state atomically; settings that require reconstruction must be marked reload-required and must not be reported as effective in the current runtime.
- **Production impact:** The admin page and runtime diagnostics show the new configuration while actual conversations use a mixture of old and new behavior. Operators cannot reliably tune reply frequency, private follow-up timeout, memory/compaction behavior, persona/prefix behavior, or learning cadence without a full restart.
- **Why existing guards fail:** The dispatcher checks only for method presence, has no per-key ownership/verification, and returns success after iteration. The reload-prefix list excludes these affected sections and fields.
- **Classification:** confirmed.
- **Confidence:** high.

### FFA-ENTRY-004 / P2: The explicit failure fallback does not suppress AstrBot's default LLM on direct/wake messages

- **File:line:** `astrmai/presentation/events/message_entry.py:199`, delegated from `main.py:203` through `astrmai/app/plugin_facade.py:75`.
- **Trigger:** A direct message, mention, or wake-command event reaches AstrMai and `record_and_dispatch_attention()` raises (for example, a runtime coordinator, dialogue, state, or gateway-side failure).
- **Real call chain:** `AstrMaiPlugin.on_global_message` -> `PluginFacade.on_global_message` -> `handle_global_message` -> `PluginFacade.record_and_dispatch_attention` -> exception caught at `message_entry.py:201` -> fallback yielded at line 209 -> AstrBot `StarRequestSubStage` sends the yielded result and clears it -> AstrBot `ProcessStage` sees a wake event with `event.call_llm == false` and invokes the default agent request.
- **Actual behavior:** The user receives AstrMai's fallback, then the host default LLM is still eligible to produce a second response. This also incurs an unintended extra model call during the failure path.
- **Expected behavior:** Once the explicit failure fallback is emitted, the event should suppress the host default LLM (and normally stop further propagation) exactly as the successful `ENGAGED`/direct-call path does.
- **Production impact:** Recoverable AstrMai failures can produce duplicate or contradictory replies, replacing a controlled fallback with an unrelated host answer and increasing latency/cost.
- **Why existing guards fail:** `suppress_default_llm_if_engaged()` is reached only after a non-error status. The error branch returns at line 210 without setting `event.call_llm` or calling `event.stop_event()`.
- **Classification:** confirmed.
- **Confidence:** high.

### FFA-ENTRY-005 / P2: EventBus shutdown leaves queued events available to the next plugin runtime

- **File:line:** `astrmai/app/bootstrap.py:175`, `astrmai/app/lifecycle.py:266`; adjacent shutdown implementation at `astrmai/infrastructure/runtime/event_bus.py:225`.
- **Trigger:** Unload/reload the plugin while the singleton EventBus queue still contains a learning or memory event that workers have not consumed.
- **Real call chain:** old runtime publishes to `EventBus._event_queue` -> `AstrMaiPlugin.terminate` -> `PluginLifecycleManager._terminate_impl` -> `EventBus.stop` cancels workers -> new `PluginBootstrap._build_memory_observability_services` calls `EventBus()` and receives the same singleton -> new subscriptions are attached -> first new publish restarts workers -> pending old queue entries are dispatched against the new subscriber set.
- **Actual behavior:** `EventBus.stop()` cancels and clears task sets but does not drain/recreate `_event_queue`, clear subscribers, or reset the singleton instance. A queued event from the old runtime can therefore execute after reload, potentially against both still-live weak subscribers and newly registered services.
- **Expected behavior:** Runtime shutdown must leave no deliverable event from the terminated instance; reload should start with a fresh queue/subscriber generation or explicitly discard pending entries.
- **Production impact:** A reload at the wrong point can replay stale learning/memory work, causing duplicate processing or writes attributed to the new runtime generation.
- **Why existing guards fail:** Task cancellation handles running workers only. There is no queue generation marker, drain step, or singleton reset, and bootstrap unconditionally reuses `EventBus()`.
- **Classification:** partial (the state transition is confirmed from code; occurrence depends on a pending queue item at unload time).
- **Confidence:** medium-high.

## Production paths reviewed without additional confirmed defects

- AstrBot entry registration and hook delegation: loaded, LLM request/response, decorating-result, global-message, command, group-membership, and terminate paths.
- Bootstrap construction order: persistence/database, gateway/lane, EventBus/memory/observability, state/dialogue/compaction, judge/sensors/vision, cognition, interaction, ChatLoop, learning, proactive, and lifecycle manager binding.
- Lifecycle ordering: memory initialization, command discovery, private-chat host binding, governance/proactive/visual startup, tracked background jobs, cron restoration, shutdown cancellation, EventBus stop, and persistence disposal.
- `RuntimeFacadeProtocol` versus `PluginFacade`: all protocol methods are implemented with aligned sync/async method shapes and parameters.
- `_conf_schema.json` versus Pydantic configuration models: field paths, declared scalar/container types, and defaults agree, including current uncommitted concurrency, judge-timeout, memory-retrieval, relationship-threshold, and Sys3 fields.
- Root metadata and dependency manifest: plugin identity/version metadata and imports used by the audited entry/app path are internally aligned.
- Legacy compatibility: runtime host binding, hot-config compatibility synchronization, centralized legacy attribute export, and public review bridge methods were traced to current production consumers.
