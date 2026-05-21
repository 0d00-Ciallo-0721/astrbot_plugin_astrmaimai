# P2.99 Test Migration Matrix

## Purpose

This matrix is the source of truth for the refactor-era test layout and batch ordering.
It keeps the directory contracts, regression suites, and pre-release checks aligned.

## Batch Order

1. **Architecture contracts**
   - `tests.regression.architecture.*`
   - Goal: lock import boundaries, directory contracts, and shared test-support rules.

2. **State and reply chain**
   - `tests.test_state_services_refactor`
   - `tests.test_attention_gate_refactor`
   - `tests.test_reply_service_refactor`
   - `tests.test_planner_side_inputs_refactor`
   - `tests.test_planner_cognitive_loop_refactor`
   - `tests.test_think_level_policy_refactor`
   - `tests.test_state_bar_audit_refactor`
   - `tests.test_host_mood_chain_audit_refactor`
   - Goal: keep the state bars, ingress flow, reply flow, and host mood audits stable.

3. **Scheduler and proactive**
   - `tests.test_chat_loop_kernel_refactor`
   - `tests.test_proactive_scheduler_refactor`
   - `tests.test_scheduler_benchmark_refactor`
   - `tests.test_chat_runtime_coordinator_refactor`
   - `tests.test_external_result_bridge_refactor`
   - Goal: preserve scheduler selection, batch planning, and proactive runtime behavior.

4. **Memory / review / learning / persona**
   - `tests.test_memory_refactor`
   - `tests.test_learning_refactor`
   - `tests.test_learning_event_collaboration_refactor`
   - `tests.test_persona_context_refactor`
   - `tests.regression.review.test_review_service_migrated`
   - `tests.unit.learning.*`
   - `tests.unit.memory.*`
   - Goal: keep learning, memory, review, and persona services aligned with the refactor baseline.

5. **WebUI / plugin page / fixture**
   - `tests.test_webui_backend_refactor`
   - `tests.test_plugin_pages_admin_refactor`
   - `tests.test_scheduler_fixture_refactor`
   - `tests.regression.suites.test_phase_p2_minimal_suite`
   - Goal: verify plugin routes, admin fixture data, and browser-facing P2 contracts.

## Release Use

- Pair this file with `plan/P2_99_ACCEPTANCE_CHECKLIST.md` during pre-release validation.
- New migrated suites should join the smallest matching batch instead of creating a parallel ad-hoc flow.
