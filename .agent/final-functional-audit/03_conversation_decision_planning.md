# AstrMai Final Functional Audit: Conversation Decision & Planning

## Scope

- Audited the current working tree for `astrmai/conversation/decision/` and `astrmai/conversation/planning/`.
- Followed reachable production paths through AttentionGate, System2Runner, ConcurrentExecutor, proactive dispatch, configuration hot-apply, and reply completion only where needed to prove module cooperation.
- `astrmai/infrastructure/security` was treated as opaque and was not inspected.
- Result: **7 confirmed functional defects** (`P0: 0`, `P1: 4`, `P2: 3`, `P3: 0`).

## Findings

### FFA-03-001 / P1 - Agency TTL and cooldowns never expire because timestamps use incompatible clocks

- **File:line:** `astrmai/conversation/planning/agency_runtime.py:33`, `astrmai/conversation/planning/agency_runtime.py:44`, `astrmai/conversation/planning/agency_runtime.py:78`
- **Trigger:** Any completed Planner turn records an `AgencyReflection`; the same chat is processed again after the intended 10-minute cooldown or 30-minute reflection TTL.
- **Real call chain:** `Planner._finalize_plan_result()` (`planner.py:1401`) -> `Planner._record_agency_reflection()` (`planner.py:495`) -> `AgencyRuntimeStore.record()` (`agency_runtime.py:65`) stores `time.time()` -> next turn `Planner._prepare_plan_context()` (`planner.py:937`) -> `PlanningInputLoader.load_pre_budget()` (`planning_input_loader.py:99`) -> `_agency_snapshot()` (`planning_input_loader.py:232`) -> `AgencyRuntimeStore.summary()/cooldown_tags()` -> `recent()` compares with `monotonic()`.
- **Actual behavior:** `record()` stores an epoch timestamp (about 1.7 billion seconds), while `recent()` and `cooldown_tags()` subtract it from the much smaller monotonic process clock. The delta remains negative, so every stored item satisfies both `<= 30 minutes` and `<= 10 minutes` forever. Old cooldown tags and reflections survive until process restart or replacement by the 12-item cap.
- **Expected behavior:** Reflections should leave the short-term summary after 30 minutes, and action cooldown tags should stop filtering behavior after 10 minutes.
- **Production impact:** Old `meme`, `poke`, `at`, `like`, `sharp_reply`, and `long_reply` decisions can permanently affect tool filtering, CognitiveLoop gating, behavior tuning, and prompt guidance for a chat. The store also retains a key for every touched chat for the lifetime of the plugin.
- **Why existing guards fail:** Both TTL checks are mathematically present, but they compare timestamps from different clock domains; a negative age always passes. The 12-item cap limits entries per chat but does not restore time-based behavior or remove chat keys.
- **Classification:** confirmed
- **Confidence:** 1.00

### FFA-03-002 / P1 - Available tools are recorded as executed actions, corrupting cooldown and long-term agency feedback

- **File:line:** `astrmai/conversation/planning/planner.py:478`, `astrmai/conversation/planning/planner.py:495`, `astrmai/conversation/planning/planner.py:1401`
- **Trigger:** A normal reply runs with chat/full tools available but the model does not call those tools. Chat tier normally exposes `proactive_meme` and `proactive_like_action` (`planner_side_inputs.py:305-311`); full tier exposes additional poke/@ tools (`planner_side_inputs.py:273-303`).
- **Real call chain:** `Planner._invoke_planning_llm()` builds the available tool list (`planner.py:1036`) -> `ConcurrentExecutor.execute(..., tools=tools)` (`planner.py:1139`) -> a text reply completes -> `_finalize_plan_result()` passes the same availability list to `_record_agency_reflection()` (`planner.py:1401`) -> `_cooldown_tags_from_tools()` derives action tags solely from tool names (`planner.py:478`) -> next turn `PlanningInputLoader._agency_snapshot()` supplies those tags -> `ActionModifier.modify_tools()` removes tools for cooldown (`expression_policy.py:258-274`) -> `AgencyReflectionBridge` may persist the false pattern (`agency_feedback_bridge.py:43-78`).
- **Actual behavior:** Merely making `proactive_meme`, `proactive_like_action`, `proactive_poke`, or `construct_at_event` available is recorded as if that action happened. A plain reply with no tool call therefore creates cooldowns such as `meme` and `like`; repeated false tags can be promoted into long-term cognitive feedback saying those actions were recently repeated.
- **Expected behavior:** Cooldowns and feedback must be derived from actual tool calls/side effects or an explicit execution trace, not the candidate tool set supplied to the model.
- **Production impact:** After ordinary replies, social tools are removed from later turns despite never being used. Combined with FFA-03-001, the first false cooldown can suppress those actions for the rest of the process lifetime and contaminate persistent behavior feedback.
- **Why existing guards fail:** `turn_context.tools` distinguishes initial/available/filtered tools, but `_record_agency_reflection()` does not consume an executed-tool result. Its `tools` argument is the Planner candidate list, and `_cooldown_tags_from_tools()` treats every name in that list as an action taken.
- **Classification:** confirmed
- **Confidence:** 1.00

### FFA-03-003 / P1 - Hot-applied configuration does not reach Planner-owned runtime components

- **File:line:** `astrmai/app/plugin_facade.py:193`, `astrmai/conversation/planning/planner.py:109`, `astrmai/conversation/planning/context_engine.py:34`, `astrmai/conversation/execution/executor.py:55`, `astrmai/conversation/planning/expression_policy.py:55`
- **Trigger:** An operator saves configuration through the production plugin API, which calls `PluginFacade.apply_hot_config()` (`astrmai/webui/backend/adapters/plugin_api.py:451`), and then starts another conversation turn without restarting the plugin.
- **Real call chain:** plugin API -> `PluginFacade.apply_hot_config()` -> `_apply_hot_config_locked()` replaces `runtime.config` and refreshes only the component list at `plugin_facade.py:193-207` -> Planner continues using objects created at bootstrap: `ContextEngine.config`, `PromptRefiner.config`, `ConcurrentExecutor.config`, `CognitiveLoop.config`, `ActionModifier` thresholds, and `ExpressionSelector.config`.
- **Actual behavior:** The hot-apply operation returns success, and `GlobalModelGateway.config` is replaced, but the Planner and its owned components are not in the refresh list and expose no coordinated refresh. Values captured from the old config continue controlling runtime behavior. Confirmed examples include executor `agent.max_steps/timeout`, `reply.stale_reply_max_age_sec/fallback_text`, native-vision switches, ContextEngine persona/memory/prefix-caching settings, PromptRefiner settings, and ActionModifier life thresholds.
- **Expected behavior:** A successful hot apply must update every live component that reads configuration, or explicitly rebuild the Planner stack, so the next turn uses the saved values.
- **Production impact:** WebUI changes appear accepted but silently do nothing in major decision, prompt, tool, fallback, and execution paths until restart. Different layers can simultaneously use old and new configuration objects, producing internally inconsistent turns.
- **Why existing guards fail:** The component refresh loop includes the gateway, judge, attention gate, reply engine, and several services, but omits `system2_planner` and all Planner-owned children. Updating `gateway.config` cannot replace the separate config references and numeric thresholds captured during their constructors.
- **Classification:** confirmed
- **Confidence:** 1.00

### FFA-03-004 / P1 - Executor fallback replies bypass proactive completion, cooldown, and energy settlement

- **File:line:** `astrmai/conversation/execution/executor.py:621`, `astrmai/conversation/execution/executor.py:741`, `astrmai/conversation/execution/executor.py:883`, `astrmai/conversation/planning/planner.py:1383`
- **Trigger:** A proactive wakeup reaches Planner and every configured text/tool model fails or returns unusable output, causing executor pool exhaustion.
- **Real call chain:** `ProactiveDispatcher.dispatch()` attaches `astrmai_proactive_completion_callback` (`proactive/dispatcher.py:277-310`) -> AttentionGate -> System2Runner -> Planner -> `ConcurrentExecutor._run_text_mode()` or `_run_tool_mode()` exhausts models -> `_handle_fatal_fallback()` sends `reply.fallback_text` through `ReplyService` (`executor.py:883-895`) and returns `None` -> `Planner._finalize_plan_result()` enters the `reply_text is None` branch (`planner.py:1383-1396`) and returns without `_finalize_proactive_event()` -> dispatcher completion is never called.
- **Actual behavior:** The fallback is visibly sent and `astrmai_reply_sent` becomes true, but the proactive decision remains queued, its completion callback remains stored, dispatcher cooldown is not armed, and `WakeupService._on_complete()` does not consume energy or persist `next_wakeup_timestamp` (`proactive/wakeup_service.py:178-188`). Planner also records the turn as `stale_drop` with no reply.
- **Expected behavior:** Once the fallback is successfully sent, the proactive path must complete with `reply_sent=True`, invoke its callback, charge energy, persist wakeup cooldown, and record a sent fallback outcome.
- **Production impact:** Model outages can produce repeated proactive fallback messages without the configured wakeup cost/cooldown, leave dispatch history in a false queued state, and leak one callback entry per affected intent.
- **Why existing guards fail:** Executor communicates fallback delivery only via event extras while returning `None`. Planner treats every `None` uniformly as stale/no-send and does not check `astrmai_reply_sent` or execution status before skipping proactive finalization.
- **Classification:** confirmed
- **Confidence:** 1.00

### FFA-03-005 / P2 - Judge still advertises removed actions and then converts them to IGNORE

- **File:line:** `astrmai/conversation/decision/judge.py:424`, `astrmai/conversation/decision/judge.py:445`, `astrmai/conversation/decision/judge.py:501`
- **Trigger:** In a non-direct group turn, Judge follows its output schema and returns `FETCH_KNOWLEDGE` or `RETHINK_GOAL`, both explicitly listed as valid choices in the generated prompt.
- **Real call chain:** `AttentionGate._debounce_and_judge()` -> `AttentionDecisionRouter.evaluate()` -> `Judge.evaluate()` builds a dynamic list that omits those actions (`judge.py:46-59`) but later instructs the model that the JSON action enum includes them (`judge.py:424-445`) -> parser accepts the string -> `valid_actions` excludes it and rewrites the action to `IGNORE` (`judge.py:501-505`) -> DecisionRouter returns `IGNORE` -> AttentionGate does not invoke System2 (`gate.py:911-921`).
- **Actual behavior:** A model-compliant declared action is silently transformed into `IGNORE`; the group receives no reply and no knowledge/goal path runs.
- **Expected behavior:** The prompt and parser must share one action contract. Removed actions should not appear anywhere in the schema/instructions, or they should be mapped to a reachable Planner action rather than silence.
- **Production impact:** Knowledge and goal-oriented group messages can be dropped even though Judge produced an action the prompt explicitly authorized.
- **Why existing guards fail:** The validation guard detects the contract mismatch but chooses the most destructive fallback (`IGNORE`). The private-chat override later changes ignore to reply, but group traffic has no equivalent recovery.
- **Classification:** confirmed
- **Confidence:** 0.98

### FFA-03-006 / P2 - WaitTool's executed result is reduced to `None` and never reaches Planner state

- **File:line:** `astrmai/conversation/planning/tools/pfc_tools.py:87`, `astrmai/conversation/execution/executor.py:681`, `astrmai/conversation/planning/planner.py:1383`
- **Trigger:** In full or Sys3 tool mode, the model calls `wait_and_listen` and follows its contract by ending with `[SYSTEM_WAIT_SIGNAL]`.
- **Real call chain:** Planner exposes `WaitTool` (`planner_side_inputs.py:273-303` or `planner_side_inputs.py:411-426`) -> `WaitTool.call()` returns `[SYSTEM_WAIT_SIGNAL]` (`pfc_tools.py:95-96`) -> `ConcurrentExecutor._run_tool_mode()` detects it, writes `astrmai_execution_signal="wait"`, and returns `None` (`executor.py:681-685`) -> `_invoke_planning_llm()` returns `reply_text=None` -> `_finalize_plan_result()` takes the generic stale-drop branch (`planner.py:1383-1396`). No production reader consumes `astrmai_execution_signal`.
- **Actual behavior:** Visible silence occurs, but the executed wait decision is not propagated as a wait outcome. Agency/continuity record no wait, no-send relationship settlement is skipped, proactive completion is skipped, and the turn trace is labeled `stale_drop` rather than wait.
- **Expected behavior:** Tool execution should return a typed wait outcome or Planner should consume `astrmai_execution_signal`, run the same no-send settlement used by cognitive wait, finalize proactive state when relevant, and record `skipped_wait`.
- **Production impact:** Planner state and lifecycle diverge from the model's executed action. For proactive events it reproduces an incomplete dispatch; for normal turns it corrupts decision history and downstream behavior feedback.
- **Why existing guards fail:** The executor preserves the signal only in an event extra, while Planner branches solely on the nullable reply text. The explicit wait handling at `planner.py:1282-1323` applies only to CognitiveLoop decisions and is never reached for tool results.
- **Classification:** confirmed
- **Confidence:** 1.00

### FFA-03-007 / P2 - A valid empty goal list cannot clear existing goals

- **File:line:** `astrmai/conversation/planning/goal_service.py:96`, `astrmai/conversation/planning/goal_service.py:120`, `astrmai/conversation/planning/goal_service.py:122`
- **Trigger:** Goal analysis decides that no short-term goal remains and returns the valid JSON array `[]`, including after the prompt's “delete no longer relevant goals” instruction.
- **Real call chain:** `Planner._invoke_planning_llm()` -> `PlanningInputLoader.load_prompt_inputs()` -> `_load_goal_update()` (`planning_input_loader.py:351`) -> `GoalManager.analyze_and_update()` -> `_parse_goals([])` returns an empty list -> `if new_goals:` skips all mutation -> `load_prompt_inputs()` immediately calls `_load_goals_context()` (`planning_input_loader.py:218-225`) -> old goals are injected by `ContextEngine.build_prompt()` (`planner.py:1062-1075`).
- **Actual behavior:** Existing goals remain unchanged, are not aged, and continue entering the same turn's prompt and all later prompts. The API cannot distinguish a valid empty decision from parse/model failure.
- **Expected behavior:** A successfully parsed empty JSON array should clear the goal set (or at minimum age/remove all unreferenced goals), while malformed/model-failure fallback should preserve prior state separately.
- **Production impact:** Completed or abandoned objectives keep steering replies, encouraging the bot to drag old topics into new conversation and preventing the documented goal deletion behavior.
- **Why existing guards fail:** All state updates, aging, and expiry are nested under truthiness of `new_goals`; an empty but valid result follows the same path as failure and returns a generic planner reason without touching `_goals`.
- **Classification:** confirmed
- **Confidence:** 1.00

## Production Paths Reviewed

- Attention window focus selection -> `FocusThreadContext` -> Judge -> `BrainActionPlan` -> AttentionDecisionRouter -> System2 admission.
- Focus/current/direct/related/ambient/warm/recent context construction -> `PromptEnvelope` -> ContextEngine -> PromptRefiner -> executor prompt.
- ThinkLevelPolicy -> CognitiveLoop gate/readonly observation -> BehaviorTuningPolicy -> reply/wait/ignore/tool routing.
- Goal update/context injection and conversation continuity state.
- Chat/full/Sys3 tool construction -> ActionModifier -> ToolSet execution -> terminal/wait/fallback result handling -> ReplyService.
- Agency reflection/cooldown -> next-turn side inputs -> long-term cognitive feedback bridge.
- Proactive intent injection -> Planner completion -> dispatcher callback -> energy and cooldown persistence.
- WebUI config save -> PluginFacade hot apply -> live decision/planning/execution component configuration.

## Audit Conclusion

No P0 defect was confirmed. The highest-risk cluster is the interaction between false tool-use recording and the Agency clock mismatch: an ordinary reply can create cooldowns for actions that never ran, and those cooldowns then never expire. The proactive failure path is independently capable of sending visible fallback messages without completing cost/cooldown state.
