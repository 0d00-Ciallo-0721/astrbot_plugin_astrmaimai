# Round 04 Review：回复、Sys3 与 Gateway 边界

**Review Date**: 2026-07-14
**Source**: `.agent/bug-fix-rounds-2026-07-13/ROUND_04_REPLY_SYS3_GATEWAY.md`
**Verdict**: 9/9 IMPLEMENTED — all 9 fixes are implemented and effective in the codebase.

---

## R04-01 / P2: Planner follow-up 复用 final send key

**Verdict**: ✅ IMPLEMENTED

**Evidence**:
- `turn_identity.py:26-28` — `build_turn_send_key()` now accepts `response_kind` parameter, producing keys like `mode:chat_id:thread_id:generation:final` vs `mode:chat_id:thread_id:generation:follow_up`.
- `reply_artifact_builder.py:374-376` — reads `astrmai_response_kind` from event extras, normalizes to `final` or `follow_up`, passes it to `build_turn_send_key` at line 384.
- `planner.py:1493-1503` — before follow-up execution, saves previous `astrmai_response_kind`, sets it to `follow_up`, restores in finally.
- Exactly-once via `runtime_coordinator.claim_send()` / `commit_send()` — duplicate follow-up sends block with `reply.duplicate_follow_up_blocked`.

**Regression target met**: First and follow-up send independently; repeat follow-up rejected.

---

## R04-02 / P2: 分段中途 stale 后把未发送尾段持久化为完整回复

**Verdict**: ✅ IMPLEMENTED

**Evidence**:
- `reply_artifact_builder.py:432-437` — `_send_segments()` checks `_check_reply_freshness()` before each segment; if EXPIRED, breaks loop.
- `reply_artifact_builder.py:490-495` — partial send updates `artifact.persistable_text` to only sent segments via `\n.join(artifact.segments[:sent_segment_count]).strip()`; metadata marked `partial_sent`.
- `reply_service.py:133` — `_sync_native_history_mirror` uses `artifact.persistable_text` — only delivered text enters history.
- Exception path (lines 457-467) also marks `partial_sent` when segments were sent before error.
- `reply_freshness.py:54-122` — `_check_reply_freshness` checks generation currency, event timestamp age, and superseding activity.

**Regression target met**: When stale before second segment, history contains only first segment; send claim/trace marked partial.

---

## R04-03 / P1: 普通聊天 Sys3 light tool 用 AstrMessageEvent 调用 ContextWrapper contract

**Verdict**: ✅ IMPLEMENTED

**Evidence**:
- `base_agent.py:19` — `AstrMaiBaseSubAgent` extends `FunctionTool[AstrAgentContext]`, the correct AstrBot FunctionTool contract.
- `base_agent.py:27-38` — `parameters` uses proper JSON Schema with `query` string property and `required: [query]`.
- `base_agent.py:55-64` — `call()` receives `context: ContextWrapper[AstrAgentContext]` and properly unwraps: `context.context` -> `AstrAgentContext` -> `AstrBot Context` and `AstrMessageEvent`.
- `router.py:39-40` — `get_light_tools_for_planner()` returns `ToolSet()` of `FunctionTool` subagents. No decorator handler stuffing.
- `planner_side_inputs.py:392` — Sys3 light tools obtained via `self.sys3_router.get_light_tools_for_planner().tools`.

**Regression target met**: TOOL_CALL passes query to static/dynamic subagent via proper FunctionTool contract; `/work` unchanged.

---

## R04-04 / P2: 静态 SubAgent 忽略配置 agent pool

**Verdict**: ✅ IMPLEMENTED

**Evidence**:
- `router.py:27` — `Sys3Router.__init__` injects `_gateway` on each static agent: `agent._gateway = gateway`.
- `base_agent.py:87-101` — `call()` prefers gateway path: calls `gateway.get_agent_models()` (defined in `gateway_tasks.py:434`) and `gateway.tool_chat_in_lane_result(models=models, ...)`.
- `base_agent.py:107-118` — fallback to raw AstrBot provider only when gateway unavailable.
- `gateway_tasks.py:434-435` — `get_agent_models()` routes through `self.router.get_ranked_models("agent", self._agent_models())` — AstrMai agent pool.
- `plugin_facade.py:668` — `/work` entry also uses `self.runtime.gateway.get_agent_models()`.

**Regression target met**: Outer and inner agents use configured agent pool; raw provider missing doesn't block `/work`.

---

## R04-05 / P1: 正常技术回答含通用错误词就被 output guard 拒绝

**Verdict**: ✅ IMPLEMENTED

**Evidence**:
- `output_guard.py:162-208` — `looks_like_provider_failure_text()` requires **structural envelope evidence**: provider failure prefix match, 2+ structural envelope fields (request ID, status code, JSON response), or JSON envelope with provider-specific keys (`candidates`, `usageMetadata`).
- Simple substring matches limited to high-confidence provider error messages only (lines 164-176): `you have reached your usage limit`, `被安全过滤器拦截`.
- `output_guard.py:287-305` — `validate_visible_output_text()` delegates to same `looks_like_provider_failure_text()` — unified strict check.
- `gateway_call.py:317-324` and `gateway_lane.py:696-703` — both use `validate_visible_output_text()`.
- Normal reply discussing quotas or status codes won't match structural envelope (needs 2+ lines of structured format).

**Regression target met**: Quota/status-code discussion passes; real provider error envelope still blocked.

---

## R04-06 / P1: 15 秒 API timeout 覆盖 120 秒 tool timeout

**Verdict**: ✅ IMPLEMENTED

**Evidence**:
- `gateway_lane.py:181-184` — `_tool_loop_total_timeout(tool_timeout, max_steps)` returns `max(float(self._api_timeout()), max(0.1, float(tool_timeout)))`. api_timeout=15 + tool_timeout=120 -> result=120.
- `gateway_lane.py:591-606` — `asyncio.wait_for(..., timeout=self._tool_loop_total_timeout(timeout, max_steps))` uses bounded timeout.
- `base_agent.py:49-50,100` — subagent passes `timeout=self.get_timeout()` (60s default).
- `plugin_facade.py:692` — `/work` passes `timeout=config.sys3.tool_timeout` (default 120s).
- `defaults.py:20` — api_timeout default 15.0; `_tool_loop_total_timeout` guard prevents override.

**Regression target met**: 120s tool completes within budget; total budget exceeded triggers single cancellation.

---

## R04-07 / P1: 模型成功后的 lane/trace 失败被反向判为模型失败

**Verdict**: ✅ IMPLEMENTED

**Evidence**:
- `gateway_call.py:112-159` — `_record_success_artifacts()` wraps each step (usage logging, economy, benchmark) in individual try-except. No exception propagates.
- `gateway_lane.py:67-160` — `_finalize_success_artifacts()` wraps artifact recording individually (lines 96-99, 119-120, 137-138, 154-156).
- `gateway_lane.py:162-166` — `_safe_finalize_success_artifacts()` wraps finalization; exceptions logged but do NOT affect result.
- `gateway_lane.py:425,785` — `return result` always executes after finalization, regardless of outcome.
- Success result (LLM text, model_id, usage) already constructed before finalization and returned unchanged.

**Regression target met**: Finalizer exception returns original success result; only records degradation; no tool re-execution.

---

## R04-08 / P2: tool-loop 绕过全局 LLM semaphore

**Verdict**: ✅ IMPLEMENTED

**Evidence**:
- `model_gateway.py:38` — `self._global_semaphore = asyncio.Semaphore(max(1, max_concurrent_llm_calls))`.
- `gateway_call.py:179` — `_elastic_call_result()`: `async with self._global_semaphore:` — plain LLM consumes slot.
- `gateway_lane.py:480-481` — `tool_chat_in_lane_result()`: `async with self._global_semaphore:` — tool-loop ALSO consumes slot.
- `gateway_lane.py:498-499` — ponytail comment confirms `_tool_chat_in_lane_result_unlimited` does NOT re-acquire (no deadlock).
- `gateway_lane.py:280-426` — `chat_in_lane_result()` calls `_elastic_call_result()` which acquires internally. Both chat and tool share global budget.
- `model_gateway.py:43-52` — `refresh_config` rebuilds semaphore on config change.

**Regression target met**: Concurrent tool-loop peaks <= configured limit; normal chat gets fair scheduling.

---

## R04-09 / P2: tool-loop 不使用模型重试/退避，TimeoutError 被记为 unknown

**Verdict**: ✅ IMPLEMENTED

**Evidence**:
- `gateway_lane.py:572-573` — tool-loop uses `max_retries = max(0, int(self._max_retries()))` and `backoff_factor = max(0.0, float(self._backoff_factor()))` — same retry policy as plain calls.
- `gateway_lane.py:574-578` — `attempt_plan` iterates all models × retry attempts, unified pattern.
- `gateway_lane.py:788` — exception calls `self._classify_failure_kind(last_error, error=exc)` — unified classifier.
- `gateway_policy.py:132-133` — `_classify_failure_kind()`: `isinstance(error, asyncio.TimeoutError)` -> `FailureKind.TIMEOUT`. Also `timeout in lowered` at line 147. TimeoutError NEVER UNKNOWN.
- `gateway_lane.py:790-791` — `side_effect_recorded` guard. `retry_same_model = not is_fatal and not side_effect_recorded and attempt < max_retries`.
- `gateway_lane.py:817-822` — side effects recorded -> `abort_after_side_effect = True`, loop breaks, no re-execution.
- `gateway_lane.py:831` — `await asyncio.sleep(backoff_factor ** attempt)` for retry backoff.
- `gateway_policy.py:153-175` — `_is_fatal_failure()`: timeouts are non-fatal, allowing retry.

**Regression target met**: TimeoutError -> FailureKind.TIMEOUT; single-model transient errors retry with configured backoff.

---

## Summary

| Fix ID | Status | Confidence |
|--------|--------|-----------|
| R04-01 | ✅ IMPLEMENTED | High — send key differentiation confirmed in `build_turn_send_key` and `_send_segments` |
| R04-02 | ✅ IMPLEMENTED | High — partial send `persistable_text` truncation confirmed, mid-segment freshness check present |
| R04-03 | ✅ IMPLEMENTED | High — `FunctionTool[AstrAgentContext]` inheritance, ContextWrapper unwrapping confirmed |
| R04-04 | ✅ IMPLEMENTED | High — `_gateway` injection + `get_agent_models()` confirmed in `gateway_tasks.py:434` |
| R04-05 | ✅ IMPLEMENTED | Medium-high — structural envelope evidence required; substring matches limited |
| R04-06 | ✅ IMPLEMENTED | High — `_tool_loop_total_timeout` uses `max(api_timeout, tool_timeout)` |
| R04-07 | ✅ IMPLEMENTED | High — all post-success artifacts wrapped in individual try-except, never propagate |
| R04-08 | ✅ IMPLEMENTED | High — `tool_chat_in_lane_result` acquires semaphore, internal method does not re-acquire |
| R04-09 | ✅ IMPLEMENTED | High — unified classifier, side-effect-aware retry, exponential backoff confirmed |

**Overall verdict**: All 9 fixes from Round 04 are implemented and effective. No gaps found.
