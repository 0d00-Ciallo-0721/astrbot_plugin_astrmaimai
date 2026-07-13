# Assignment 11 - Multimodal and Workmode Functional Audit

## Audit result

- Scope: current working tree, focused on `astrmai/multimodal/` and `astrmai/workmode/` plus production callers and the installed AstrBot contracts needed to prove reachability.
- Exclusions honored: no tests, test coverage, security policy, authentication, authorization, permission policy, style, duplication, dead-code, or refactoring review; `astrmai/infrastructure/security/` was not inspected.
- Production code was not modified.
- Confirmed findings: **5** (`P0: 0`, `P1: 1`, `P2: 4`, `P3: 0`).

## Findings

### 1. P1 - Normal-chat Sys3 light tools are dispatched with an `AstrMessageEvent` where every subagent expects a `ContextWrapper`

- **File:line:** `astrmai/workmode/router.py:36-47`; consuming contract at `astrmai/workmode/subagents/base_agent.py:55-64`; production exposure at `astrmai/conversation/planning/planner_side_inputs.py:391-426`.
- **Trigger:** Work mode is enabled, the normal conversation judge selects `TOOL_CALL`, and the outer model invokes any static or dynamic Sys3 tool exposed by `get_light_tools_for_planner()`.
- **Reachable production call chain:** `main.py:202-207` `on_global_message()` -> `AstrMaiPluginFacade.on_global_message()` -> `AttentionGate.process_event()` schedules Sys2 at `astrmai/conversation/attention/gate.py:920-921` -> `System2Runner.run()` -> `Planner.plan_and_execute()` -> `Planner._build_execution_tools()` -> `Sys3Router.get_light_tools_for_planner()` -> `ToolSet.get_light_tool_set()` creates a plain `FunctionTool` -> `router.py:46` assigns `light_tool.handler = raw_agent.call` -> `ConcurrentExecutor._run_tool_mode()` at `astrmai/conversation/execution/executor.py:663-676` -> `GlobalModelGateway.tool_chat_in_lane_result()` -> AstrBot local tool executor sees a non-null `handler` and invokes it as a decorator handler with `event` as its first argument -> `AstrMaiBaseSubAgent.call()` reaches `base_agent.py:58` and evaluates `context.context` on the `AstrMessageEvent`.
- **Actual:** The delegated tool raises an attribute/handler-parameter error before the subagent can acquire its context, provider, or business tools. The same adapter is used for dynamic handoffs, so any handoff whose `call()` follows the `FunctionTool.call(ContextWrapper, **kwargs)` contract is affected. In addition, the light tool schema has empty properties, so even after correcting dispatch it would not carry the required `query` argument used at `base_agent.py:56`.
- **Expected:** Planner-visible lightweight tools must preserve a callable path that AstrBot dispatches using `FunctionTool.call(ContextWrapper, **kwargs)` and must retain the minimum routing payload (at least `query`) needed by the subagent.
- **Production impact:** Sys3 delegation from ordinary chat is nonfunctional whenever the model selects one of these tools. Direct `/work` uses the full tools and does not hit this particular adapter, so the failure is mode-dependent and can appear as normal chat being unable to perform tasks that `/work` can perform.
- **Why existing guards fail:** The handler injection at `router.py:39-46` was added to avoid the framework's missing-handler guard, but a populated `handler` has priority over an overridden `call()` and is invoked with decorator-handler semantics. The `hasattr(raw_agent, "call")` check verifies only attribute presence, not the incompatible calling convention; there is no invocation-time adapter or schema preservation.
- **Classification:** Framework contract mismatch / cross-module tool dispatch.
- **Confidence:** High. The current installed AstrBot `FunctionTool`/local executor contract was read directly: handler dispatch receives `event`, while overridden `call()` dispatch receives `ContextWrapper`.

### 2. P2 - Static subagents ignore the configured agent model pool and reacquire the session's ordinary chat provider

- **File:line:** `astrmai/workmode/subagents/base_agent.py:66-69` and `base_agent.py:90-119`; direct-entry caller at `astrmai/app/plugin_facade.py:642-669`.
- **Trigger:** `/work` is enabled and successfully starts on a configured `provider.agent_models` entry, then the outer agent delegates to `CronAgent` or `ComputerAgent`; the UMO's current chat provider is absent, differs from the configured agent model, or lacks the required tool behavior.
- **Reachable production call chain:** `main.py:216-222` `/work` -> `AstrMaiPluginFacade.enter_sys3_direct()` checks `gateway.get_agent_models()` at `plugin_facade.py:643-646` -> outer `gateway.tool_chat_in_lane_result(models=agent_models)` -> model invokes a full static subagent -> AstrBot correctly dispatches the overridden `AstrMaiBaseSubAgent.call(ContextWrapper, ...)` -> `base_agent.py:67` calls `ctx.get_current_chat_provider_id(event.unified_msg_origin)` -> the current bootstrap provides the host AstrBot `Context` as `ctx` and does not attach AstrMai's gateway to it, so `base_agent.py:92-109` cannot take the gateway branch -> `base_agent.py:111-119` runs the nested agent using the ordinary session provider.
- **Actual:** The nested static agent runs on a provider unrelated to the configured deep-thinking/tool pool, or raises `ProviderNotFoundError` at `base_agent.py:67` even though `/work` already proved that a configured agent model is available.
- **Expected:** A Sys3 subagent entered from an AstrMai gateway-selected agent should inherit that routed provider/model policy, or explicitly reacquire from `provider.agent_models`; the ordinary chat provider should be only an intentional fallback.
- **Production impact:** Delegated `/work` tasks can fail after the outer work model has already accepted them, or execute on a weaker/different provider with different tool-call support, cost, and timeout behavior than configured.
- **Why existing guards fail:** `enter_sys3_direct()` validates only the gateway agent pool. The subagent provider lookup occurs before tool availability checks and before the nominal gateway branch. `getattr(ctx, "gateway", None)` does not find AstrMai's gateway on the host `Context` in the current bootstrap, so the intended routed path is unreachable for these calls.
- **Classification:** Provider-routing contract mismatch.
- **Confidence:** High.

### 3. P2 - A best-effort meme failure escapes after the primary reply has already been sent

- **File:line:** `astrmai/conversation/execution/reply_post_send.py:247-256` and `astrmai/multimodal/meme/meme_sender.py:17-38`; send ordering at `astrmai/conversation/execution/reply_service.py:129-151`.
- **Trigger:** The `proactive_meme` tool records a non-neutral tag (`astrmai/conversation/planning/tools/pfc_tools.py:486-498`), the primary text reply succeeds, and meme directory enumeration, component construction, or `context.send_message()` raises (for example, a file disappears between enumeration and send, the directory becomes unreadable, or the platform adapter rejects the image).
- **Reachable production call chain:** normal message -> Planner exposes/invokes `ProactiveMemeTool` -> event receives `astrmai_bypass_mood_analysis` -> `ConcurrentExecutor` obtains the final text -> `ReplyService.handle_reply()` sends and marks the primary reply at `reply_service.py:129-143` -> `_settle_post_send()` -> `send_meme()` -> exception from `Path.iterdir()`, `Image.fromFileSystem()`, or the adapter send -> exception bubbles through `handle_reply()` -> executor core handler at `astrmai/conversation/execution/executor.py:874-877` treats the already-completed reply as a fatal execution failure and enters fallback handling.
- **Actual:** An optional attachment changes the whole execution outcome after the final text is visible. With the current send-claim path, a second final reply is usually suppressed, but the executor still reports failure, returns `None`, can skip later normal completion/follow-up behavior, and may emit operational error handling. If reply send claims are disabled or the event lacks a turn identity, the fatal fallback can also produce a second, misleading error reply.
- **Expected:** Meme selection/sending should be isolated as post-send best effort. Its failure should be logged and should not invalidate, retry, or replace an already committed primary reply.
- **Production impact:** Users can see misleading fallback/error output after a successful answer in configurations without final-send claims; otherwise the turn is still recorded as an executor failure and downstream completion behavior is lost.
- **Why existing guards fail:** `_settle_post_send()` catches mood/profile settlement errors only through `reply_post_send.py:216-245`; the meme call is outside that `try`. `send_meme()` has no exception boundary. The final-send claim is conditional and protects duplicate text sends, not the executor's success state.
- **Classification:** Post-send failure containment / partial-success handling.
- **Confidence:** High.

### 4. P2 - One transient startup reload failure permanently disables cron heartbeat recovery for that process

- **File:line:** `astrmai/app/lifecycle.py:141-157`; heartbeat implementation at `astrmai/workmode/cron_guard/heartbeat.py:55-63`.
- **Trigger:** Work mode is enabled, but `reload_all_lost_jobs()` raises once during startup because the snapshot database or AstrBot cron manager temporarily fails to list/read jobs.
- **Reachable production call chain:** AstrBot loaded hook -> `AstrMaiPluginFacade.on_program_start()` -> `LifecycleManager.on_program_start()` -> `start_workmode_guard()` -> `CronHeartbeatGuard.reload_all_lost_jobs()` -> transient exception -> `lifecycle.py:147-150` records degradation and returns before `track_task(self.runtime.cron_guard.run_heartbeat())` at lines 151-153.
- **Actual:** No heartbeat task is started and there is no later retry. The guard object remains present and work mode remains enabled, which can make diagnostics look partially available while recovery is inactive.
- **Expected:** A failed eager reload should be reported, but the periodic heartbeat should still start (or startup should schedule an explicit retry) so transient failures can self-heal.
- **Production impact:** Lost recurring jobs remain lost for the entire plugin process lifetime, and expired one-shot snapshots are not periodically cleaned, until the plugin/AstrBot is restarted successfully.
- **Why existing guards fail:** The startup exception guard returns immediately. `CronHeartbeatGuard.run_heartbeat()` has its own per-tick exception containment, but it is never scheduled after the initial reload error.
- **Classification:** Lifecycle recovery gap / missing retry scheduling.
- **Confidence:** High.

### 5. P2 - Cron revival commits the host job before a non-atomic snapshot identity swap

- **File:line:** `astrmai/workmode/cron_guard/heartbeat.py:109-126` and `heartbeat.py:186-212`; repeat detection at `heartbeat.py:91-107`.
- **Trigger:** A snapshot is missing from AstrBot's active jobs, `cron_mgr.add_active_job()` succeeds, and AstrMai persistence fails while deactivating the old snapshot or saving the replacement snapshot.
- **Reachable production call chain:** startup reload or heartbeat tick -> missing snapshot ID detected -> `_revive_job()` -> `_call_add_job()` creates and schedules a real AstrBot active job -> `_extract_job_id()` obtains its new ID -> `_sync_revived_snapshot()` separately calls `deactivate_cron_snapshot(old_job_id)` and then `save_cron_snapshot(new_snapshot)` -> persistence exception -> outer per-snapshot catch logs and continues -> next heartbeat reloads whatever snapshot state survived.
- **Actual:** If deactivation fails, the stale snapshot remains active while the newly created host job already exists under a different ID; the next tick again sees the stale ID as missing and creates another scheduled job, potentially repeating every 60 seconds. If deactivation succeeds but saving the replacement fails, the revived host job runs but no active AstrMai snapshot protects it from the next host-side loss.
- **Expected:** Host-job creation and snapshot replacement need an idempotent recovery identity or compensation/transaction semantics so a partial persistence failure cannot duplicate the scheduled action or silently discard recovery coverage.
- **Production impact:** Users can receive duplicate reminders or duplicate proactive agent actions after a transient persistence failure; the alternative partial-failure order silently removes future recovery for the revived job.
- **Why existing guards fail:** The outer `try/except` only logs. There is no compensation that deletes the just-created host job, no pending/revival marker, and no atomic database transaction spanning old-snapshot deactivation plus replacement save. The current AstrBot `add_active_job()` contract does not accept the stored job ID, so a successful revival normally receives a new identity and cannot be deduplicated against the still-active old snapshot.
- **Classification:** Non-atomic recovery / idempotency failure.
- **Confidence:** High.

## Reviewed without a confirmed reachable defect

- `ImagePipeline` decode, temporary-file cleanup, GIF/WebP transform, and vision-call cleanup were reviewed. The current production tree constructs and starts `VisualCortex`, but no production caller was found that invokes `submit_task()` or `process_image_async()`; therefore transform-only concerns were not promoted to confirmed findings under the reachable-call-chain rule.
- `VisualCortex` cancellation and cleanup propagate `CancelledError` through the worker and execute `ImagePipeline.cleanup()` in `finally`; no separate reachable shutdown defect was confirmed.
- Meme probability is bounded by configuration, neutral tags are skipped, and the storage bootstrap degrades without aborting startup. The confirmed issue is specifically exception containment after a successful primary send.
- Direct `/work` is event-scoped rather than a persistent chat mode. After the command finishes, later messages use the normal global-message path; no failure to return to normal chat was confirmed.
- Missing ComputerAgent imports and a disabled computer sandbox return a structured decline. Cron tool import absence also declines. No additional confirmed optional-tool availability defect was found beyond the provider and router contracts above.
- The installed AstrBot `add_active_job()` argument shape used by `_call_add_job()` is compatible with the current call; the confirmed cron issues concern lifecycle retry and partial recovery state.

## Paths reviewed

### Assigned production paths

- `astrmai/multimodal/__init__.py`
- `astrmai/multimodal/image_pipeline.py`
- `astrmai/multimodal/visual_cortex.py`
- `astrmai/multimodal/meme/meme_config.py`
- `astrmai/multimodal/meme/meme_init.py`
- `astrmai/multimodal/meme/meme_sender.py`
- `astrmai/workmode/__init__.py`
- `astrmai/workmode/router.py`
- `astrmai/workmode/tools/handoff_registry.py`
- `astrmai/workmode/subagents/base_agent.py`
- `astrmai/workmode/subagents/computer_agent.py`
- `astrmai/workmode/subagents/cron_agent.py`
- `astrmai/workmode/cron_guard/heartbeat.py`

### Production call-chain and contract dependencies

- `main.py`, `config.py`, `_conf_schema.json`
- `astrmai/app/bootstrap.py`, `astrmai/app/lifecycle.py`, `astrmai/app/plugin_facade.py`, `astrmai/app/runtime_context.py`
- `astrmai/conversation/attention/gate.py`
- `astrmai/conversation/planning/planner.py`, `astrmai/conversation/planning/planner_side_inputs.py`, `astrmai/conversation/planning/tools/pfc_tools.py`
- `astrmai/conversation/execution/executor.py`, `astrmai/conversation/execution/system2_runner.py`, `astrmai/conversation/execution/reply_service.py`, `astrmai/conversation/execution/reply_post_send.py`, `astrmai/conversation/execution/reply_artifact_builder.py`
- `astrmai/infrastructure/gateway/model_gateway.py`, `astrmai/infrastructure/gateway/gateway_lane.py`
- `astrmai/infrastructure/persistence/orm_models.py`, `astrmai/infrastructure/persistence/database_cron.py`
- `astrmai/shared/helpers/plugin_helpers.py`
- Installed AstrBot runtime contracts: `astrbot/core/agent/tool.py`, `astrbot/core/astr_agent_tool_exec.py`, `astrbot/core/star/context.py`, `astrbot/core/cron/manager.py`, `astrbot/core/tools/cron_tools.py`, `astrbot/core/db/po.py`, and `astrbot/core/pipeline/process_stage/stage.py`.

## Completion summary

Assignment 11 is complete. The report contains five confirmed, reachable production findings and no speculative finding without a closed production call chain.
