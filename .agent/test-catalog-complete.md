# Test Catalog - AstrMai
**Total: 1168 test functions | 193 files | 0 parse errors**


## tests/test_webui_backend_refactor.py (49 tests)

### test_dashboard_service_uses_adapter_and_db_factory
Summary: Tests dashboard service uses adapter and db factory
Asserts:
  - `self.assertEqual(snapshot["total_users"], 3)`
  - `self.assertEqual(snapshot["pending_reviews"], 1)`
  - `self.assertIn("capabilities", snapshot)`

### test_runtime_ui_service_reports_unbound_when_no_facade_resolved
Summary: Tests runtime ui service reports unbound when no facade resolved
Asserts:
  - `self.assertFalse(status["runtime_bound"])`
  - `self.assertEqual(status["data"], {})`

### test_runtime_ui_service_does_not_mask_bound_facade_failures_as_ok_data
Summary: Tests runtime ui service does not mask bound facade failures as ok data
Asserts:
  - `self.assertRaisesRegex(RuntimeError, "boom")`

### test_server_mounts_aggregated_api_router
Summary: Tests server mounts aggregated api router
Asserts:
  - `self.assertIn("from .routes import api_router", content)`
  - `self.assertIn("app.include_router(api_router, prefix=\"/api\")", content)`

### test_plugin_api_adapter_default_paths_follow_env_at_instantiation_time
Summary: Tests plugin api adapter default paths follow env at instantiation time
Asserts:
  - `self.assertEqual(adapter.config_path, config_path)`
  - `self.assertEqual(adapter.schema_path, schema_path)`
  - `self.assertEqual(adapter.persona_cache_path, persona_cache_path)`

### test_auth_secret_follows_env_at_runtime
Summary: Tests auth secret follows env at runtime
Asserts:
  - `self.assertEqual(asyncio.run(access_mod.get_current_user("codex")), "codex")`
  - `self.assertEqual(asyncio.run(access_mod.get_current_user(None)), "astrbot-plugin-page")`

### test_memory_ui_service_writes_real_schema_columns
Summary: Tests memory ui service writes real schema columns
Asserts:
  - `self.assertEqual(reflection["status"], "ok")`
  - `self.assertTrue(str(event["id"]).startswith("mem_webui_"))`
  - `self.assertIsInstance(node["id"], int)`
  - `self.assertIsInstance(jargon["id"], str)`
  - `self.assertEqual(event["mode"], "canonical_redirect")`
  - `self.assertEqual(event_count, 0)`
  - `self.assertEqual(canonical_row["session_id"], "PLUGIN_PAGE_SMOKE")`
  - `self.assertEqual(canonical_row["tags"], '["codex"]')`
  - `self.assertEqual(reflection_row["reflection"], "updated reflection")`
  - `self.assertEqual(node_row["name"], "updated node")`
  - `self.assertEqual(jargon_row["kind"], "jargon")`
  - `self.assertEqual(jargon_row["content"], "smoke jargon")`
  - `self.assertEqual(jargon_row["status"], "review_pending")`
  - `self.assertEqual(jargon_meta["raw_content"], "smoke jargon")`
  - `self.assertEqual(jargon_meta["meaning"], "updated meaning")`

### test_memory_service_exposes_canonical_memory_and_legacy_marker
Summary: Tests memory service exposes canonical memory and legacy marker
Asserts:
  - `self.assertEqual(canonical["total"], 1)`
  - `self.assertEqual(canonical["items"][0]["id"], "mem-ui-1")`
  - `self.assertEqual(detail["data"]["id"], "mem-ui-1")`
  - `self.assertTrue(deleted["changed"])`
  - `self.assertEqual(legacy_event["mode"], "canonical_redirect")`
  - `self.assertTrue(legacy_rows[0]["legacy"])`
  - `self.assertEqual(legacy_rows[0]["canonical_id"], "mem-ui-1")`
  - `self.assertEqual(legacy_delete["mode"], "canonical_soft_delete")`

### test_memory_ui_service_runtime_bound_canonical_actions_use_services_not_sql_fallback
Summary: Tests memory ui service runtime bound canonical actions use services not sql fallback
Asserts:
  - `self.assertTrue(deleted["runtime_bound"])`
  - `self.assertTrue(restored["runtime_bound"])`
  - `self.assertTrue(staled["runtime_bound"])`
  - `self.assertTrue(merged["runtime_bound"])`
  - `self.assertEqual(updated["status"], "ok")`
  - `self.assertEqual(             [item[0] for item in calls],             ["soft_delete", "restore", "mark_stale", "mark_merged", "update_memory"],         )`

### test_memory_ui_service_runtime_bound_actions_can_run_with_maintenance_service_on_same_chat
Summary: Tests memory ui service runtime bound actions can run with maintenance service on same chat
Asserts:
  - `self.assertFalse(any(isinstance(item, Exception) for item in results))`
  - `self.assertIsNotNone(candidate)`
  - `self.assertIn(candidate.status, {"stale", "deleted"})`

### test_memory_ui_service_lists_and_reviews_canonical_jargon
Summary: Tests memory ui service lists and reviews canonical jargon
Asserts:
  - `self.assertEqual(len(pending), 1)`
  - `self.assertEqual(pending[0]["legacy_jargon_id"], 7)`
  - `self.assertEqual(approved["status"], "ok")`
  - `self.assertEqual(len(active), 1)`
  - `self.assertEqual(active[0]["status"], "active")`
  - `self.assertEqual(active[0]["scene"], "raid call")`
  - `self.assertEqual(pending[0]["review_reason"], "needs more evidence")`
  - `self.assertEqual(pending[0]["review_suggestion"], "confirm whether it is boss shorthand")`
  - `self.assertEqual(rejected["status"], "ok")`
  - `self.assertEqual(final_detail["data"]["status"], "rejected")`
  - `self.assertEqual(final_detail["data"]["metadata"]["review_status"], "rejected")`

### test_memory_route_file_exposes_jargon_review_endpoints
Summary: Tests memory route file exposes jargon review endpoints
Asserts:
  - `self.assertIn('@router.post("/jargon/{id}/approve")', content)`
  - `self.assertIn('@router.post("/jargon/{id}/reject")', content)`
  - `self.assertIn('@router.get("/observability/runtime")', content)`
  - `self.assertIn('@router.get("/observability/chats/{chat_id}")', content)`
  - `self.assertIn('@router.get("/observability/events")', content)`
  - `self.assertIn('@router.get("/observability/errors")', content)`

### test_memory_ui_service_runtime_status_prefers_new_memory_runtime_fields
Summary: Tests memory ui service runtime status prefers new memory runtime fields
Asserts:
  - `self.assertTrue(status["runtime_bound"])`
  - `self.assertTrue(status["instant_gate_ready"])`
  - `self.assertTrue(status["memory_pipeline_ready"])`
  - `self.assertTrue(status["session_summarizer_ready"])`
  - `self.assertEqual(status["memory_pipeline_status"]["buffered_chats"], 2)`
  - `self.assertEqual(status["observer_status"]["recent_error_count"], 1)`
  - `self.assertEqual(chat["data"]["chat_id"], "chat-1")`
  - `self.assertEqual(chat["data"]["pending_messages"], 4)`
  - `self.assertTrue(chat["data"]["worker_active"])`
  - `self.assertEqual(events["items"][0]["component"], "instant_gate")`
  - `self.assertEqual(events["items"][0]["display_title"], "即时记忆命中")`
  - `self.assertEqual(errors["items"][0]["level"], "error")`

### test_plugin_page_memory_tab_renders_observability_panels
Summary: Tests plugin page memory tab renders observability panels
Asserts:
  - `self.assertIn("记忆网络", js)`
  - `self.assertIn("记忆碎片 Events", js)`
  - `self.assertIn("每日反思 Reflections", js)`
  - `self.assertIn("实体图谱 Nodes", js)`
  - `self.assertIn("黑话字典 Jargon", js)`
  - `self.assertIn("memory-feedback", js)`
  - `self.assertIn('api.get("/memory-feedback?limit=50")', js)`
  - `self.assertIn('api.get("/memory-feedback/sources")', js)`
  - `self.assertIn('api.post(`/memory-feedback/${segment(button.dataset.disableFeedback)}/disable`)', js)`

### test_admin_service_exposes_memory_observability_views
Summary: Tests admin service exposes memory observability views
Asserts:
  - `self.assertTrue(overview["runtime_bound"])`
  - `self.assertEqual(overview["data"]["snapshot"]["buffered_chats"], 2)`
  - `self.assertEqual(overview["data"]["recent_errors"][0]["display_title"], "canonical_write_failed")`
  - `self.assertEqual(timeline["items"][0]["display_title"], "即时记忆命中")`
  - `self.assertEqual(chat["data"]["chat"]["chat_id"], "chat-1")`
  - `self.assertEqual(errors["items"][0]["level"], "error")`

### test_admin_service_exposes_cognition_unified_timeline
Summary: Tests admin service exposes cognition unified timeline
Asserts:
  - `self.assertEqual(result["items"][0]["kind"], "memory")`
  - `self.assertEqual(result["items"][1]["kind"], "tool")`
  - `self.assertEqual(result["items"][2]["kind"], "decision")`
  - `self.assertEqual(result["items"][0]["title"], "即时记忆命中")`

### test_runtime_observability_hub_supports_recent_snapshot_and_search
Summary: Tests runtime observability hub supports recent snapshot and search
Asserts:
  - `self.assertEqual(len(recent), 2)`
  - `self.assertEqual(errors[0]["domain"], "memory")`
  - `self.assertEqual(snapshot["domain_counts"]["scheduler"], 1)`
  - `self.assertEqual(snapshot["level_counts"]["error"], 1)`
  - `self.assertEqual(chat["retained_events"], 2)`
  - `self.assertEqual(search[0]["title"], "Memory summarize failed")`

### test_admin_service_exposes_observability_views_and_search
Summary: Tests admin service exposes observability views and search
Asserts:
  - `self.assertTrue(overview["runtime_bound"])`
  - `self.assertEqual(overview["data"]["snapshot"]["retained_events"], 2)`
  - `self.assertEqual(timeline["items"][0]["domain"], "heartflow")`
  - `self.assertEqual(chat["data"]["chat"]["chat_id"], "chat-1")`
  - `self.assertEqual(errors["items"][0]["domain"], "scheduler")`
  - `self.assertEqual(search["items"][0]["domain"], "scheduler")`

### test_settings_service_builds_effective_config_and_validates_schema
Summary: Tests settings service builds effective config and validates schema
Asserts:
  - `self.assertEqual(effective["reply"]["base_frequency"], 0.3)`
  - `self.assertIs(effective["reply"]["enabled"], True)`
  - `self.assertEqual(bad_field["status"], "error")`
  - `self.assertEqual(bad_type["status"], "error")`
  - `self.assertEqual(reset["data"]["base_frequency"], 0.7)`

### test_plugin_api_apply_config_updates_bound_runtime
Summary: Tests plugin api apply config updates bound runtime
Asserts:
  - `self.assertEqual(result["status"], "ok")`
  - `self.assertTrue(result["runtime_bound"])`
  - `self.assertIsNotNone(applied)`
  - `self.assertEqual(applied[0]["reply"]["base_frequency"], 0.2)`

### test_plugin_facade_apply_hot_config_refreshes_proactive_task_config_refs
Summary: Tests plugin facade apply hot config refreshes proactive task config refs
Asserts:
  - `self.assertTrue(result)`
  - `self.assertIs(facade.runtime.config, new_config)`
  - `self.assertEqual(refreshed, [new_config])`
  - `self.assertTrue(facade.runtime.rebuilt)`
  - `self.assertTrue(facade.runtime.synced)`

### test_persona_ui_service_exposes_readonly_slice_diagnostics
Summary: Tests persona ui service exposes readonly slice diagnostics
Asserts:
  - `self.assertEqual(data["persona_id"], "atri")`
  - `self.assertEqual(data["cache_key"], "atri")`
  - `self.assertEqual(data["summary"], "核心摘要")`
  - `self.assertEqual(data["shards"]["speech_style"], "短句自然")`
  - `self.assertTrue(data["pending_task"])`
  - `self.assertTrue(data["self_lore"]["available"])`
  - `self.assertNotIn("raw", data)`
  - `self.assertNotIn("raw_preview", data)`

### test_admin_service_exposes_runtime_and_observability_summaries
Summary: Tests admin service exposes runtime and observability summaries
Asserts:
  - `self.assertTrue(status["runtime_bound"])`
  - `self.assertEqual(decisions["items"][0]["social_intent"], "join")`
  - `self.assertEqual(decisions["items"][0]["failure_evidence"]["failure_kind"], "provider_failure_text")`
  - `self.assertEqual(tools["items"][0]["tool_tier"], "chat")`
  - `self.assertTrue(tools["items"][0]["failure_evidence"]["protocol_passthrough"])`
  - `self.assertEqual(turns["items"][0]["tools"]["final_tier"], "chat")`
  - `self.assertEqual(trace_events["items"][0]["failure_evidence"]["failure_kind"], "provider_failure_text")`
  - `self.assertTrue(impulses["items"][0]["visible_candidate_allowed"])`
  - `self.assertEqual(timeline["items"][0]["kind"], "action")`
  - `self.assertEqual(digests["items"][0]["source"], "heartflow_topic_digest")`

### test_admin_service_exposes_context_economy_template_metrics
Summary: Tests admin service exposes context economy template metrics
Asserts:
  - `self.assertEqual(overview["data"]["overview"]["total_calls"], 9)`
  - `self.assertEqual(overview["data"]["overview"]["total_rotates"], 6)`
  - `self.assertEqual(overview["data"]["overview"]["template_count"], 2)`
  - `self.assertAlmostEqual(overview["data"]["overview"]["provider_session_reuse_rate"], 0.4444, places=4)`
  - `self.assertEqual(overview["data"]["templates"][0]["template_id"], "persona_summary")`
  - `self.assertEqual(templates["items"][0]["template_id"], "persona_summary")`
  - `self.assertEqual(templates["items"][0]["template_version"], "v3")`
  - `self.assertEqual(templates["items"][0]["rotate_reasons"]["template_changed"], 3)`
  - `self.assertEqual(templates["items"][0]["provider_session_reuse_rate"], 0.2)`
  - `self.assertEqual(templates["items"][0]["workload_families"]["persona_summary"], 5)`
  - `self.assertEqual(templates["items"][1]["template_id"], "memory_global_summary")`
  - `self.assertEqual(filtered["items"][0]["template_id"], "persona_summary")`
  - `self.assertEqual(filtered["available_workload_families"], ["memory_global_summary", "persona_summary"])`
  - `self.assertEqual(calls_sorted["items"][0]["template_id"], "persona_summary")`
  - `self.assertEqual(calls_sorted["items"][1]["template_id"], "memory_global_summary")`

### test_admin_service_exposes_scheduler_diagnostics_views
Summary: Tests admin service exposes scheduler diagnostics views
Asserts:
  - `self.assertEqual(status["data"]["overview"]["scheduler_poll_mode"], "NORMAL")`
  - `self.assertEqual(status["data"]["scheduler_policy"]["active_profile"], "balanced")`
  - `self.assertEqual(selection["data"]["report"]["selected"], ["chat-1", "chat-2"])`
  - `self.assertEqual(selection["data"]["poll_mode_transition"]["current"], "NORMAL")`
  - `self.assertEqual(chat["data"]["phase"], "WAITING")`
  - `self.assertTrue(chat["data"]["state_present"])`
  - `self.assertEqual(chat["data"]["scheduler_pending_signals"]["selected_reason"], "selected_by_scheduler_score")`

### test_scheduler_chat_view_is_read_only_when_state_missing
Summary: Tests scheduler chat view is read only when state missing
Asserts:
  - `self.assertEqual(before, 0)`
  - `self.assertEqual(after, 0)`
  - `self.assertTrue(view["runtime_bound"])`
  - `self.assertFalse(view["data"]["state_present"])`
  - `self.assertEqual(view["data"]["chat_id"], "chat-missing")`
  - `self.assertEqual(view["data"]["scheduler_pending_signals"], {})`

### test_cognition_routes_expose_context_economy_endpoints
Summary: Tests cognition routes expose context economy endpoints
Asserts:
  - `self.assertIn('@router.get("/context-economy")', content)`
  - `self.assertIn('@router.get("/context-economy/templates")', content)`
  - `self.assertIn('@router.get("/scheduler/status")', content)`
  - `self.assertIn('@router.get("/scheduler/due-selection")', content)`
  - `self.assertIn('@router.get("/scheduler/chats/{chat_id}")', content)`
  - `self.assertIn('@router.get("/chats/{chat_id}/unified-timeline")', content)`
  - `self.assertIn('@router.get("/observability/overview")', content)`
  - `self.assertIn('@router.get("/observability/timeline")', content)`
  - `self.assertIn('@router.get("/observability/chats/{chat_id}")', content)`
  - `self.assertIn('@router.get("/observability/errors")', content)`
  - `self.assertIn('@router.get("/observability/search")', content)`

### test_dashboard_cognition_tab_renders_context_economy_panel
Summary: Tests dashboard cognition tab renders context economy panel
Asserts:
  - `self.assertIn("renderDashboardCognition", js)`
  - `self.assertIn("Scheduler Diagnostics", js)`
  - `self.assertIn("Batch / Backpressure", js)`
  - `self.assertIn("Chat Loop Drill-down", js)`
  - `self.assertIn("schedulerChatId", js)`
  - `self.assertIn("scheduler-chat-id", js)`
  - `self.assertIn("/cognition/scheduler/status", js)`
  - `self.assertIn("/cognition/scheduler/due-selection", js)`
  - `self.assertIn("/cognition/scheduler/chats/${segment(targetChat)}", js)`
  - `self.assertIn("loadSchedulerChatLoop", js)`
  - `self.assertIn("schedulerStatus", js)`
  - `self.assertIn("/cognition/observability/overview", js)`
  - `self.assertIn("observabilityOverview", js)`
  - `self.assertIn("unifiedTimeline", js)`
  - `self.assertIn("observabilityTimelinePath", js)`
  - `self.assertIn("/cognition/observability/timeline?", js)`
  - `self.assertIn("/cognition/observability/search?", js)`
  - `self.assertIn("Global Observability Timeline", js)`
  - `self.assertIn("Context Economy", html)`
  - `self.assertIn("contextEconomyTemplates", html)`
  - `self.assertIn("contextEconomyFilterText", html)`
  - `self.assertIn("contextEconomyWorkloadFamily", html)`
  - `self.assertIn("contextEconomyQuickView", html)`
  - `self.assertIn("contextEconomySortBy", html)`
  - `self.assertIn("Scheduler Diagnostics", html)`
  - `self.assertIn("Scheduler Overview", html)`
  - `self.assertIn("Batch / Backpressure", html)`
  - `self.assertIn("Chat Loop Drill-down", html)`
  - `self.assertIn("schedulerChatId", html)`
  - `self.assertIn("暂无 loop state。该 chat 尚未进入 scheduler 跟踪。", html)`
  - `self.assertIn("High Rotate", html)`
  - `self.assertIn("Low Reuse", html)`
  - `self.assertIn("High Traffic", html)`
  - `self.assertIn("快捷视图会切换模板排序", html)`
  - `self.assertIn('title="按 lane rotate 次数从高到低查看最不稳定的模板。"', html)`
  - `self.assertIn("/cognition/scheduler/status", js)`
  - `self.assertIn("/cognition/scheduler/due-selection", js)`
  - `self.assertIn("/cognition/scheduler/chats/", js)`
  - `self.assertIn("loadSchedulerChatLoop", js)`
  - `self.assertIn("schedulerStatus", js)`
  - `self.assertIn("/cognition/context-economy?limit=20", js)`
  - `self.assertIn("loadContextEconomyTemplates", js)`
  - `self.assertIn("setContextEconomyQuickView", js)`
  - `self.assertIn("/cognition/context-economy/templates?", js)`
  - `self.assertIn("provider_session_reuse_rate", html)`
  - `self.assertIn("chatTraceEvents", html)`
  - `self.assertIn("Raw Trace Events", html)`
  - `self.assertIn("summarizeFailureEvidence", js)`
  - `self.assertIn("/cognition/chats/${encodedChat}/trace-events?limit=40", js)`
  - `self.assertIn("Observability Overview", html)`
  - `self.assertIn("Open Memory Diagnostics", html)`
  - `self.assertIn("observabilityOverview", js)`
  - `self.assertIn("memoryObservabilityChatId", js)`
  - `self.assertIn("openMemoryDrilldown", js)`
  - `self.assertIn("/cognition/observability/overview", js)`
  - `self.assertIn("Global Observability Timeline", html)`
  - `self.assertIn("cognitionUnifiedTimeline", js)`
  - `self.assertIn("loadCognitionUnifiedTimeline", js)`
  - `self.assertIn("/cognition/observability/timeline?", js)`
  - `self.assertIn("/cognition/observability/search?", js)`
  - `self.assertIn("applyTimelineQuickFilter", js)`
  - `self.assertIn("clearTimelineFilters", js)`
  - `self.assertIn("provider failures", html)`

### test_chat_trace_events_falls_back_to_turn_trace_embedded_log
Summary: Tests chat trace events falls back to turn trace embedded log
Asserts:
  - `self.assertEqual(result["total"], 1)`
  - `self.assertEqual(result["items"][0]["stage"], "execution.executor.model_pool_exhausted")`
  - `self.assertEqual(result["items"][0]["failure_evidence"]["failure_kind"], "provider_failure_text")`

### test_chat_trace_events_exposes_gateway_tool_call_failure_evidence
Summary: Tests chat trace events exposes gateway tool call failure evidence
Asserts:
  - `self.assertEqual(evidence["failure_kind"], "provider_failure_text")`
  - `self.assertEqual(evidence["attempted_models"], ["model-a"])`
  - `self.assertIn("PermissionDeniedError", evidence["raw_completion_preview"])`

### test_review_ui_service_is_canonical_first_and_degrades_to_readonly_when_runtime_missing
Summary: Tests review ui service is canonical first and degrades to readonly when runtime missing
Asserts:
  - `self.assertEqual(len(pending), 1)`
  - `self.assertEqual(pending[0]["id"], "mem-review-1")`
  - `self.assertEqual(created["status"], "degraded")`
  - `self.assertEqual(submitted["status"], "degraded")`
  - `self.assertEqual(legacy_count, 0)`

### test_review_ui_service_does_not_mask_bound_runtime_failures_as_empty_pending_list
Summary: Tests review ui service does not mask bound runtime failures as empty pending list
Asserts:
  - `self.assertRaisesRegex(RuntimeError, "boom")`

### test_review_ui_service_gracefully_handles_missing_canonical_memories_table
Summary: Tests review ui service gracefully handles missing canonical memories table
Asserts:
  - `self.assertEqual(pending, [])`
  - `self.assertEqual(reviews["items"], [])`
  - `self.assertEqual(reviews["total"], 0)`

### test_admin_expression_stats_reads_canonical_expression_patterns
Summary: Tests admin expression stats reads canonical expression patterns
Asserts:
  - `self.assertEqual(stats["data"]["total"], 3)`
  - `self.assertEqual(stats["data"]["pending"], 1)`
  - `self.assertEqual(stats["data"]["approved"], 1)`
  - `self.assertEqual(stats["data"]["rejected"], 1)`

### test_learning_expression_stats_reuses_canonical_expression_counts
Summary: Tests learning expression stats reuses canonical expression counts
Asserts:
  - `self.assertEqual(stats["data"]["total"], 3)`
  - `self.assertEqual(stats["data"]["pending"], 1)`
  - `self.assertEqual(stats["data"]["approved"], 1)`
  - `self.assertEqual(stats["data"]["rejected"], 1)`

### test_runtime_capability_imports_are_package_relative
Summary: Tests runtime capability imports are package relative
Asserts:
  - `self.assertNotIn("import_module(\"astrmai.", content)`
  - `self.assertIn("from .. import multimodal as multimodal_mod", content)`
  - `self.assertIn("from .. import workmode as workmode_mod", content)`
  - `self.assertIn("from .. import proactive as proactive_mod", content)`

### test_multimodal_capability_overview_is_json_serializable
Summary: Tests multimodal capability overview is json serializable
Asserts:
  - `self.assertIsInstance(overview["meme_service"]["memes_dir"], str)`

### test_aggregated_router_registers_admin_routes
Summary: Tests aggregated router registers admin routes
Asserts:
  - `self.assertIn("/runtime/status", paths)`
  - `self.assertIn("/heartflow/status", paths)`
  - `self.assertIn("/heartflow/impulses", paths)`
  - `self.assertIn("/memories/canonical/{memory_id}/restore", paths)`
  - `self.assertIn("/memories/canonical/{memory_id}/stale", paths)`
  - `self.assertIn("/memories/canonical/{memory_id}/merge", paths)`
  - `self.assertIn("/memories/migration/dry-run", paths)`
  - `self.assertIn("/memories/migration/execute", paths)`
  - `self.assertIn("/memories/migration/verify", paths)`
  - `self.assertIn("/memories/migration/repair", paths)`
  - `self.assertIn("/heartflow/chats/{chat_id}/impulses", paths)`
  - `self.assertIn("/heartflow/timeline", paths)`
  - `self.assertIn("/heartflow/chats/{chat_id}/timeline", paths)`
  - `self.assertIn("/heartflow/topic-digests", paths)`
  - `self.assertIn("/cognition/recent-decisions", paths)`
  - `self.assertIn("/cognition/recent-turns", paths)`
  - `self.assertIn("/cognition/chats/{chat_id}/turns", paths)`
  - `self.assertIn("/cognition/scheduler/status", paths)`
  - `self.assertIn("/cognition/scheduler/due-selection", paths)`
  - `self.assertIn("/cognition/scheduler/chats/{chat_id}", paths)`
  - `self.assertIn("/tools/status", paths)`
  - `self.assertIn("/memory-feedback", paths)`
  - `self.assertIn("/proactive/status", paths)`
  - `self.assertIn("/learning/status", paths)`
  - `self.assertIn("/chats/active", paths)`

### test_backend_route_service_factories_only_pass_plugin_api
Summary: Tests backend route service factories only pass plugin api
Asserts:
  - `self.assertIsInstance(service, getattr(service_mod, service_class_name))`

### test_backend_route_safe_endpoints_construct_without_typeerror
Summary: Tests backend route safe endpoints construct without typeerror
Asserts:
  - `self.assertEqual(result["status"], "ok")`

### test_backend_routes_align_supported_cognition_learning_and_tools_signatures
Summary: Tests backend routes align supported cognition learning and tools signatures
Asserts:
  - `self.assertEqual(reflector.calls, [("reflect_batch", "chat-1"), ("auto_audit", "chat-1")])`
  - `self.assertEqual(auto_check.calls, ["chat-1"])`
  - `self.assertEqual(result["status"], "ok", f"{route_module_name}.{handler_name} did not return ok")`

### test_backend_http_smoke_routes_stay_supported
Summary: Tests backend http smoke routes stay supported
Asserts:
  - `self.assertEqual(             reflector.calls,             [                 ("reflect_batch", "chat-1"),                 ("auto_audit", "chat-1"),                 ("reflect_batch", "chat-body"),                 ("auto_audit", "chat-body"),       ...`
  - `self.assertEqual(auto_check.calls, ["chat-1", "chat-body", "chat-query"])`
  - `self.assertEqual(response.status_code, 200)`
  - `self.assertEqual(response.json()["status"], "ok")`
  - `self.assertEqual(response.status_code, 200)`
  - `self.assertEqual(response.json()["status"], "ok")`
  - `self.assertEqual(response.status_code, 200)`
  - `self.assertEqual(response.json()["status"], "ok")`
  - `self.assertEqual(response.status_code, 200)`
  - `self.assertEqual(response.json()["status"], "ok")`
  - `self.assertEqual(response.status_code, 200)`
  - `self.assertEqual(response.json()["status"], "ok")`
  - `self.assertEqual(response.status_code, 200)`
  - `self.assertEqual(response.json()["status"], "ok")`
  - `self.assertEqual(response.status_code, 200)`
  - `self.assertEqual(response.json()["status"], "ok")`
  - `self.assertEqual(response.status_code, 200)`
  - `self.assertEqual(response.json()["status"], "ok")`
  - `self.assertEqual(response.status_code, 422)`
  - `self.assertEqual(response.json()["detail"], "chat_id is required")`

### test_heartflow_chats_uses_get_all_states_when_available
Summary: heartflow_chats() should prefer get_all_states() over _states.
Asserts:
  - `self.assertEqual(result["status"], "ok")`
  - `self.assertEqual(result["total"], 1)`
  - `self.assertTrue(called_public, "get_all_states() was not called")`

### test_heartflow_chats_falls_back_to_states_when_get_all_states_missing
Summary: heartflow_chats() should fall back to _states when get_all_states is absent.
Asserts:
  - `self.assertEqual(result["status"], "ok")`
  - `self.assertEqual(result["total"], 1)`

### test_domain_services_have_explicit_signatures
Summary: All 7 domain services should define methods with explicit parameters.
Asserts:
  - `self.assertNotIn("args", params,                     f"{cls_name}.{method_name}() still uses *args")`
  - `self.assertNotIn("kwargs", params,                     f"{cls_name}.{method_name}() still uses **kwargs")`
  - `self.assertIn("self", params,                     f"{cls_name}.{method_name}() missing self")`

### test_plugin_api_adapter_does_not_fallback_to_runtime_passthrough
Summary: Tests plugin api adapter does not fallback to runtime passthrough
Asserts:
  - `self.assertIsNone(adapter.get_planner())`

### test_apply_hot_config_fallback_logs_warning
Summary: _apply_hot_config() should use facade.apply_hot_config when available.
Asserts:
  - `self.assertTrue(result)`
  - `self.assertTrue(called_facade, "facade.apply_hot_config not called")`

### test_apply_hot_config_returns_false_when_no_facade
Summary: _apply_hot_config() should return False when facade is None.
Asserts:
  - `self.assertFalse(result)`

### test_dashboard_repository_count_table_enforces_whitelist
Summary: Tests dashboard repository count table enforces whitelist
Asserts:
  - `self.assertEqual(asyncio.run(run()), [0, 0, 0])`
  - `self.assertRaises(ValueError)`

## tests/unit/memory/test_memory_v2_services.py (51 tests)

### test_write_service_allows_legal_braced_text
Summary: Tests write service allows legal braced text
Asserts:
  - `self.assertTrue(memory_id)`
  - `self.assertIsNotNone(candidate)`
  - `self.assertEqual(candidate.content, content)`

### test_write_service_skips_fenced_json_payload
Summary: Tests write service skips fenced json payload
Asserts:
  - `self.assertEqual(memory_id, "")`

### test_write_service_skips_error_json_payload
Summary: Tests write service skips error json payload
Asserts:
  - `self.assertEqual(memory_id, "")`

### test_write_service_allows_json_payload_without_error_keys_even_if_value_contains_noisy_tokens
Summary: Tests write service allows json payload without error keys even if value contains noisy tokens
Asserts:
  - `self.assertTrue(memory_id)`
  - `self.assertIsNotNone(candidate)`
  - `self.assertEqual(candidate.content, content)`

### test_query_filters_layers_excludes_and_stale_by_default
Summary: Tests query filters layers excludes and stale by default
Asserts:
  - `self.assertEqual(await retrieval.retrieve(query), [])`
  - `self.assertEqual(len(excluded), 1)`
  - `self.assertEqual(await retrieval.retrieve(query), [])`

### test_eav_fact_newer_write_supersedes_older_fact
Summary: Tests eav fact newer write supersedes older fact
Asserts:
  - `self.assertEqual(older.status, "superseded")`
  - `self.assertEqual(older.superseded_by, newer_id)`
  - `self.assertEqual(newer.status, "active")`
  - `self.assertIsNotNone(active)`
  - `self.assertEqual(active.id, newer_id)`
  - `self.assertEqual(active.status, "active")`
  - `self.assertNotEqual(active.id, older_id)`

### test_eav_fact_older_backfill_is_immediately_superseded
Summary: Tests eav fact older backfill is immediately superseded
Asserts:
  - `self.assertEqual(active.status, "active")`
  - `self.assertEqual(late_old.status, "superseded")`
  - `self.assertEqual(late_old.superseded_by, active_id)`

### test_eav_fact_newer_write_supersedes_all_active_older_versions
Summary: Tests eav fact newer write supersedes all active older versions
Asserts:
  - `self.assertEqual(oldest.status, "superseded")`
  - `self.assertEqual(oldest.superseded_by, newest_id)`
  - `self.assertEqual(middle.status, "superseded")`
  - `self.assertEqual(middle.superseded_by, newest_id)`
  - `self.assertEqual(newest.status, "active")`

### test_eav_fact_older_backfill_cascades_duplicate_active_versions_to_latest_old
Summary: Tests eav fact older backfill cascades duplicate active versions to laold
Asserts:
  - `self.assertEqual(latest_old.status, "active")`
  - `self.assertEqual(duplicate_old.status, "superseded")`
  - `self.assertEqual(duplicate_old.superseded_by, latest_old_id)`
  - `self.assertEqual(very_old.status, "superseded")`
  - `self.assertEqual(very_old.superseded_by, latest_old_id)`

### test_injection_trace_is_recorded_and_tool_excludes_injected_ids
Summary: Tests injection trace is recorded and tool excludes injected ids
Asserts:
  - `self.assertTrue(bundle.rendered_prompt_block)`
  - `self.assertTrue(injected_ids)`
  - `self.assertEqual(result.already_injected_ids, injected_ids)`
  - `self.assertFalse(set(injected_ids) & {item.id for item in result.items})`

### test_jargon_auto_injection_flows_through_main_memory_bundle
Summary: Tests jargon auto injection flows through main memory bundle
Asserts:
  - `self.assertIn("[jargon]", bundle.rendered_prompt_block)`
  - `self.assertIn("bigbird -> a raid boss nickname (scene: raid call)", bundle.rendered_prompt_block)`
  - `self.assertIn("jargon", trace.layers)`
  - `self.assertIn(jargon_id, trace.selected_ids)`
  - `self.assertEqual(result.already_injected_ids, trace.selected_ids)`
  - `self.assertEqual(result.items, [])`

### test_self_lore_query_uses_query_and_persona_filters
Summary: Tests self lore query uses query and persona filters
Asserts:
  - `self.assertEqual(len(result.items), 1)`
  - `self.assertEqual(result.items[0].persona_id, "persona-a")`
  - `self.assertIn("gentle", result.items[0].content)`

### test_retrieval_persona_lore_uses_same_session_scope_for_canonical_and_hybrid
Summary: Tests retrieval persona lore uses same session scope for canonical and hybrid
Asserts:
  - `self.assertEqual(len(rows), 1)`
  - `self.assertEqual(rows[0].persona_id, "persona-a")`
  - `self.assertEqual(engine.calls[0]["session_id"], "__self_lore__")`

### test_maintenance_marks_stale_restores_on_access_then_deletes_after_grace
Summary: Tests maintenance marks stale restores on access then deletes after grace
Asserts:
  - `self.assertEqual(deleted, 0)`
  - `self.assertEqual(                 await retrieval.retrieve(self.contracts.MemoryQuery(query="Alice", session_id="chat-1")),                 [],             )`
  - `self.assertEqual(len(stale), 1)`
  - `self.assertEqual(stale[0].status, "stale")`
  - `self.assertEqual(restored[0].status, "active")`
  - `self.assertEqual(deleted, 1)`

### test_store_concurrent_same_session_writes_do_not_raise_database_locked
Summary: Tests store concurrent same session writes do not raise database locked
Asserts:
  - `self.assertFalse(any(isinstance(item, Exception) for item in results))`
  - `self.assertIsNotNone(candidate)`
  - `self.assertIn(candidate.status, {"active", "deleted"})`

### test_schema_migration_imports_legacy_documents_once
Summary: Tests schema migration imports legacy documents once
Asserts:
  - `self.assertEqual(await store.import_legacy_documents(), 1)`
  - `self.assertEqual(await store.import_legacy_documents(), 0)`
  - `self.assertEqual(len(rows), 1)`
  - `self.assertEqual(rows[0].metadata["legacy_doc_id"], 1)`

### test_visibility_separates_auto_and_tool_retrieval
Summary: Tests visibility separates auto and tool retrieval
Asserts:
  - `self.assertEqual(auto_rows, [])`
  - `self.assertEqual(len(tool_rows.items), 1)`
  - `self.assertEqual(tool_rows.items[0].visibility, "tool_only")`

### test_review_pending_jargon_is_hidden_from_default_retrieval_until_approved
Summary: Tests review pending jargon is hidden from default retrieval until approved
Asserts:
  - `self.assertEqual(rows, [])`
  - `self.assertEqual(len(rows.items), 1)`
  - `self.assertEqual(rows.items[0].kind, "jargon")`

### test_projector_rebuilds_without_duplicate_canonical_projection
Summary: Tests projector rebuilds without duplicate canonical projection
Asserts:
  - `self.assertEqual(rebuilt, 1)`
  - `self.assertEqual(len(engine.retriever.added), 3)`
  - `self.assertTrue(all(item[1]["canonical_id"] == memory_id for item in engine.retriever.added))`
  - `self.assertGreaterEqual(len(engine.deleted), 3)`

### test_projection_failure_is_pending_and_repairable
Summary: Tests projection failure is pending and repairable
Asserts:
  - `self.assertIn(memory_id, report["missing_projection_ids"])`
  - `self.assertEqual(repaired["rebuilt_missing"], 1)`
  - `self.assertEqual(engine.retriever.added[0][1]["canonical_id"], memory_id)`

### test_hybrid_projection_fallback_must_pass_canonical_status_check
Summary: Tests hybrid projection fallback must pass canonical status check
Asserts:
  - `self.assertEqual(rows, [])`

### test_instant_memory_gate_writes_directly_to_canonical_store
Summary: Tests instant memory gate writes directly to canonical store
Asserts:
  - `self.assertTrue(result.hit)`
  - `self.assertTrue(any("小明" in item.summary or "小明" in item.content for item in rows))`

### test_instant_memory_gate_authority_correction_uses_eav_key
Summary: Tests instant memory gate authority correction uses eav key
Asserts:
  - `self.assertEqual(request.kind, "fact")`
  - `self.assertEqual(request.dedup_key, "zlj:asset:server_count")`
  - `self.assertEqual(payload["decision_action"], "authority_override")`

### test_instant_memory_gate_short_term_state_degrades_to_topic
Summary: Tests instant memory gate short term state degrades to topic
Asserts:
  - `self.assertEqual(request.kind, "topic")`
  - `self.assertTrue(request.metadata.get("volatile_state"))`
  - `self.assertEqual(payload["decision_action"], "volatile_state_write")`

### test_instant_memory_gate_fallback_when_claim_extraction_fails
Summary: Tests instant memory gate fallback when claim extraction fails
Asserts:
  - `self.assertEqual(request.kind, "fact")`
  - `self.assertTrue(payload["fallback_used"])`
  - `self.assertEqual(payload["decision_action"], "legacy_fallback")`

### test_instant_memory_llm_backfill_uses_runtime_think_level_signal
Summary: Tests instant memory llm backfill uses runtime think level signal
Asserts:
  - `self.assertEqual(gateway.calls, 1)`
  - `self.assertTrue(result.hit)`
  - `self.assertEqual(len(llm_rows), 1)`
  - `self.assertEqual(llm_rows[0].summary, "用户想在周末去植物园散步")`
  - `self.assertTrue((llm_rows[0].metadata or {}).get("fact_scope"))`

### test_instant_memory_llm_backfill_respects_threshold_and_cooldown
Summary: Tests instant memory llm backfill respects threshold and cooldown
Asserts:
  - `self.assertFalse(low_gate.should_run_llm_backfill(low_turn, session_rounds=0, last_check=0.0, now=100.0))`
  - `self.assertEqual(low_gateway.calls, 0)`
  - `self.assertEqual([item for item in rows if item.source == "instant_gate_llm"], [])`
  - `self.assertFalse(high_gate.should_run_llm_backfill(second_turn, session_rounds=5, last_check=100.0, now=101.0))`
  - `self.assertEqual(high_gateway.calls, 1)`
  - `self.assertEqual(len(llm_rows), 1)`

### test_instant_memory_legacy_backfill_remains_user_only_compat_path
Summary: Tests instant memory legacy backfill remains user only compat path
Asserts:
  - `self.assertTrue(result.hit)`
  - `self.assertEqual(len(gateway.calls), 1)`
  - `self.assertFalse(gateway.calls[0]["kwargs"].get("system_prompt"))`
  - `self.assertEqual(len(llm_rows), 1)`

### test_instant_memory_llm_backfill_falls_back_on_gateway_signature_typeerror
Summary: Tests instant memory llm backfill falls back on gateway signature typeerror
Asserts:
  - `self.assertTrue(result.hit)`
  - `self.assertEqual(len(gateway.calls), 2)`
  - `self.assertEqual(len(llm_rows), 1)`

### test_instant_memory_llm_backfill_returns_empty_when_lane_resolution_fails
Summary: Tests instant memory llm backfill returns empty when lane resolution fails
Asserts:
  - `self.assertFalse(result.hit)`
  - `self.assertEqual(gateway.calls, [])`
  - `self.assertEqual(rows, [])`

### test_search_prefers_fts_and_basic_terms_still_work
Summary: Tests search prefers fts and basic terms still work
Asserts:
  - `self.assertEqual(len(rows), 1)`
  - `self.assertIn("green bookmarks", rows[0].content)`
  - `self.assertEqual(len(raw), 1)`

### test_deep_retrieval_reranks_and_attaches_guidance
Summary: Tests deep retrieval reranks and attaches guidance
Asserts:
  - `self.assertEqual(rows[0].id, target_id)`
  - `self.assertIn("bookmark", rows[0].metadata["deep_guidance"])`
  - `self.assertEqual({item.id for item in rows}, {first_id, target_id})`

### test_temporal_rerank_promotes_recent_relevant_memory_without_reviving_noise
Summary: Tests temporal rerank promotes recent relevant memory without reviving noise
Asserts:
  - `self.assertEqual(ranked[0].id, "new-relevant")`
  - `self.assertEqual(ranked[-1].id, "new-noise")`

### test_temporal_rerank_keeps_fact_resilient
Summary: Tests temporal rerank keeps fact resilient
Asserts:
  - `self.assertEqual(ranked[1].id, "fact-old")`
  - `self.assertGreater(ranked[1].relevance_score, 0.6)`

### test_search_scoring_weights_sum_to_one
Summary: Tests search scoring weights sum to one
Asserts:
  - `self.assertAlmostEqual(total, 1.0)`

### test_fuse_candidates_uses_configured_conflict_penalty
Summary: Tests fuse candidates uses configured conflict penalty
Asserts:
  - `self.assertEqual(len(fused), 1)`
  - `self.assertAlmostEqual(fused[0].metadata["_score_breakdown"]["conflict_penalty"], 0.35, places=4)`

### test_hybrid_search_batch_hydrates_canonical_candidates_in_result_order
Summary: Tests hybrid search batch hydrates canonical candidates in result order
Asserts:
  - `self.assertEqual([item.id for item in candidates], ["mem-1", "idx_1", "mem-2"])`
  - `self.assertEqual(store.batch_calls, [(["mem-1", "mem-2"], False)])`
  - `self.assertEqual(store.get_by_id_calls, [])`
  - `self.assertGreaterEqual(candidates[0].relevance_score, 0.7)`
  - `self.assertGreaterEqual(candidates[2].relevance_score, 0.8)`

### test_deep_retrieval_hydrates_missing_metadata_before_rerank
Summary: Tests deep retrieval hydrates missing metadata before rerank
Asserts:
  - `self.assertTrue(by_id["mem-hydrated"].metadata_hydrated)`
  - `self.assertEqual(by_id["mem-hydrated"].access_count, 7)`
  - `self.assertEqual(by_id["mem-hydrated"].kind, "fact")`
  - `self.assertFalse(by_id["mem-missing"].metadata_hydrated)`
  - `self.assertEqual(by_id["mem-missing"].created_at, 0.0)`

### test_deep_retrieval_limits_llm_rerank_to_temporal_top_window
Summary: Tests deep retrieval limits llm rerank to temporal top window
Asserts:
  - `self.assertEqual(len(gateway.candidate_batches), 1)`
  - `self.assertEqual(len(gateway.candidate_batches[0]), 8)`
  - `self.assertEqual(set(gateway.candidate_batches[0]), set(ids[:8]))`
  - `self.assertEqual(len(rows), 5)`

### test_maintenance_temporal_hot_score_marks_low_heat_non_fact_stale
Summary: Tests maintenance temporal hot score marks low heat non fact stale
Asserts:
  - `self.assertGreaterEqual(report["marked_stale"], 1)`
  - `self.assertEqual(topic.status, "stale")`
  - `self.assertEqual(fact.status, "active")`

### test_maintenance_run_once_keeps_protected_stale_records
Summary: Tests maintenance run once keeps protected stale records
Asserts:
  - `self.assertEqual(report["physically_deleted"], 1)`
  - `self.assertIsNotNone(await store.get_canonical(protected_id, include_inactive=True))`
  - `self.assertIsNone(await store.get_canonical(disposable_id, include_inactive=True))`
  - `self.assertEqual(int(protected_fts[0] or 0), 1)`
  - `self.assertEqual(int(disposable_fts[0] or 0), 0)`

### test_maintenance_run_once_purges_old_jargon_candidates_but_keeps_protected
Summary: Tests maintenance run once purges old jargon candidates but keeps protected
Asserts:
  - `self.assertEqual(report["jargon_pending_deleted"], 1)`
  - `self.assertEqual(report["jargon_pending_human_deleted"], 1)`
  - `self.assertEqual(report["jargon_rejected_deleted"], 1)`
  - `self.assertEqual(report["protected_jargon_skipped"], 1)`
  - `self.assertIsNone(await store.get_canonical(pending_id, include_inactive=True))`
  - `self.assertIsNone(await store.get_canonical(rejected_id, include_inactive=True))`
  - `self.assertIsNone(await store.get_canonical(pending_human_id, include_inactive=True))`
  - `self.assertIsNotNone(await store.get_canonical(protected_id, include_inactive=True))`

### test_projector_checks_and_repairs_consistency
Summary: Tests projector checks and repairs consistency
Asserts:
  - `self.assertIn("missing-orphan", report["orphan_projection_ids"])`
  - `self.assertIn(inactive_id, report["inactive_projection_ids"])`
  - `self.assertIn(missing_id, report["duplicate_projection_ids"])`
  - `self.assertEqual(repaired["deduplicated"], 1)`
  - `self.assertTrue(engine.cleaned)`

### test_migration_report_exposes_counts
Summary: Tests migration report exposes counts
Asserts:
  - `self.assertEqual(report["schema_version"], 2)`
  - `self.assertGreaterEqual(report["canonical_counts"]["active"], 1)`
  - `self.assertIn("migrations", report)`

### test_legacy_canonical_migration_ignores_duplicate_primary_keys
Summary: Tests legacy canonical migration ignores duplicate primary keys
Asserts:
  - `self.assertEqual(rows, [("mem-1", "target duplicate"), ("mem-2", "legacy new")])`

### test_memory_engine_run_documents_query_requires_explicit_db_path
Summary: Tests memory engine run documents query requires explicit db path
Asserts:
  - `self.assertEqual(doc_rows, [("doc memory",)])`
  - `self.assertEqual(canonical_rows, [("canonical memory",)])`
  - `self.assertRaisesRegex(ValueError, "db_path must be explicitly provided")`

### test_migration_service_dry_run_execute_verify_and_repair
Summary: Tests migration service dry run execute verify and repair
Asserts:
  - `self.assertEqual(dry_run["totals"]["importable"], 4)`
  - `self.assertEqual(dry_run["totals"]["duplicates"], 1)`
  - `self.assertEqual(dry_run["totals"]["skipped"], 3)`
  - `self.assertEqual(executed["imported"]["documents"], 1)`
  - `self.assertEqual(executed["imported"]["MemoryEvent"], 1)`
  - `self.assertEqual(executed["imported"]["persona_cache"], 1)`
  - `self.assertEqual(executed["imported"]["Jargon"], 1)`
  - `self.assertIn("migration", verified)`
  - `self.assertEqual(verified["legacy"]["unmapped_memory_events"], 0)`
  - `self.assertEqual(verified["legacy"]["unmapped_jargons"], 0)`
  - `self.assertEqual(verified["jargon"]["missing_meaning"], 0)`
  - `self.assertEqual(verified["jargon"]["missing_review_status"], 0)`
  - `self.assertEqual(verified["jargon"]["active_non_approved_metadata"], 0)`
  - `self.assertEqual(verified["jargon"]["pending_human_without_review_suggestion"], 0)`
  - `self.assertEqual(verified["jargon"]["visibility_anomalies"], 0)`
  - `self.assertTrue(await store.find_ids_by_source_ref("documents:1"))`
  - `self.assertTrue(await store.find_ids_by_source_ref("MemoryEvent:evt-1"))`
  - `self.assertTrue(await store.find_ids_by_source_ref("persona_cache:persona-a"))`
  - `self.assertTrue(await store.find_ids_by_source_ref("Jargon:1"))`
  - `self.assertEqual(repaired["mode"], "repair")`
  - `self.assertIn("filled_review_status", repaired["jargon"])`

### test_expression_pattern_service_writes_retrieves_and_updates_canonical_records
Summary: Tests expression pattern service writes retrieves and updates canonical records
Asserts:
  - `self.assertEqual([item.id for item in rows], [pattern_id])`
  - `self.assertEqual(updated.status, "rejected")`
  - `self.assertEqual(hidden, [])`

### test_learning_expression_pattern_cannot_write_directly_to_approved
Summary: Tests learning expression pattern cannot write directly to approved
Asserts:
  - `self.assertEqual(pattern.review_status, "pending")`
  - `self.assertEqual(pattern.status, "review_pending")`
  - `self.assertEqual(rows, [])`

### test_maintenance_purges_old_expression_candidates_but_keeps_protected
Summary: Tests maintenance purges old expression candidates but keeps protected
Asserts:
  - `self.assertEqual(report["expression_pending_deleted"], 1)`
  - `self.assertEqual(report["protected_expression_skipped"], 1)`
  - `self.assertIsNone(await store.get_canonical(pending_id, include_inactive=True))`
  - `self.assertIsNotNone(await store.get_canonical(protected_id, include_inactive=True))`

### test_migration_service_imports_legacy_expression_patterns
Summary: Tests migration service imports legacy expression patterns
Asserts:
  - `self.assertEqual(dry_run["sources"]["ExpressionPattern"]["importable"], 1)`
  - `self.assertEqual(executed["imported"]["ExpressionPattern"], 1)`
  - `self.assertTrue(await store.find_ids_by_source_ref("ExpressionPattern:1"))`
  - `self.assertEqual(verified["legacy"]["unmapped_expression_patterns"], 0)`
  - `self.assertEqual(verified["expression_pattern"]["missing_review_status"], 0)`

## tests/test_chat_loop_kernel_refactor.py (44 tests)

### test_message_and_heartbeat_ticks_create_and_reuse_state
Summary: Tests message and heartbeat ticks create and reuse state
Asserts:
  - `self.assertEqual(first.decision.action, "INGRESS_MESSAGE")`
  - `self.assertEqual(first.dispatch_result, "ENGAGED")`
  - `self.assertEqual(second.decision.action, "NOOP")`
  - `self.assertEqual(second.dispatch_result["action"], "NOOP")`
  - `self.assertEqual(first.state.chat_id, second.state.chat_id)`
  - `self.assertEqual(status["tracked_chats"], 1)`
  - `self.assertEqual(status["decision_mode"], "single_primary_action")`
  - `self.assertTrue(status["private_wait_visible_in_heartbeat"])`
  - `self.assertTrue(status["heartflow_preview_readonly"])`
  - `self.assertEqual(status["dream_scope"], "global_throttle")`
  - `self.assertEqual(calls[0], ("message", "default:GroupMessage:group-1"))`
  - `self.assertEqual(calls[1], ("heartbeat", "default:GroupMessage:group-1", "NOOP"))`

### test_busy_heartbeat_skips_dispatch
Summary: Tests busy heartbeat skips dispatch
Asserts:
  - `self.assertEqual(result.decision.action, "SKIP_BUSY")`
  - `self.assertFalse(result.decision.should_dispatch)`
  - `self.assertEqual(result.decision.next_tick_delay, 5.0)`
  - `self.assertEqual(result.decision.metadata["scheduler_bucket"], "fast_recheck")`
  - `self.assertEqual(calls, [])`

### test_background_dispatch_failure_records_retry_state
Summary: Tests background dispatch failure records retry state
Asserts:
  - `self.assertEqual(state.last_decision, "COMPACTION_EVALUATE")`
  - `self.assertGreater(state.retry_backoff_until, 0.0)`
  - `self.assertEqual(state.next_tick_at, state.retry_backoff_until)`
  - `self.assertTrue(state.pending_signals["dispatch_failed"])`
  - `self.assertEqual(state.pending_signals["dispatch_error_type"], "RuntimeError")`
  - `self.assertEqual(state.pending_signals["dispatch_error_reason"], "bridge failed")`
  - `self.assertRaises(RuntimeError)`

### test_message_resume_wait_produces_resume_action
Summary: Tests message resume wait produces resume action
Asserts:
  - `self.assertEqual(result.decision.action, "RESUME_WAIT")`
  - `self.assertEqual(result.decision.reason, "wait_resumed")`

### test_group_wait_arm_forces_wait_during_heartbeat
Summary: Tests group wait arm forces wait during heartbeat
Asserts:
  - `self.assertEqual(result.decision.action, "WAIT")`
  - `self.assertEqual(result.decision.metadata["wait_scope"], "group")`
  - `self.assertGreaterEqual(result.decision.next_tick_delay, 1.0)`
  - `self.assertLessEqual(result.decision.next_tick_delay, 20.0)`
  - `self.assertEqual(result.decision.metadata["scheduler_bucket"], "wait_recheck")`

### test_message_interrupts_non_matching_wait_in_same_chat
Summary: Tests message interrupts non matching wait in same chat
Asserts:
  - `self.assertEqual(result.decision.action, "INTERRUPT_WAIT")`
  - `self.assertEqual(calls, ["INTERRUPT_WAIT"])`
  - `self.assertEqual(result.state.wait_status, "interrupted")`

### test_quiet_hours_blocks_wakeup_and_heartflow
Summary: Tests quiet hours blocks wakeup and heartflow
Asserts:
  - `self.assertEqual(result.decision.action, "NOOP")`
  - `self.assertEqual(result.decision.reason, "quiet_hours")`
  - `self.assertTrue(result.decision.metadata["quiet_active"])`
  - `self.assertEqual(result.decision.metadata["quiet_blocks"], ["PROACTIVE_WAKEUP", "HEARTFLOW_EVALUATE"])`
  - `self.assertEqual(result.decision.next_tick_delay, 300.0)`
  - `self.assertEqual(result.decision.metadata["scheduler_bucket"], "idle_backoff")`

### test_per_action_cooldown_blocks_only_same_action
Summary: Tests per action cooldown blocks only same action
Asserts:
  - `self.assertEqual(result.decision.action, "HEARTFLOW_EVALUATE")`

### test_wakeup_candidate_in_cooldown_is_blocked_by_kernel
Summary: Tests wakeup candidate in cooldown is blocked by kernel
Asserts:
  - `self.assertEqual(result.decision.action, "NOOP")`
  - `self.assertEqual(result.decision.reason, "cooldown_blocked")`
  - `self.assertEqual(result.decision.metadata["cooldown_blocks"], ["wakeup"])`
  - `self.assertTrue(result.decision.metadata["wakeup_candidate_present"])`
  - `self.assertEqual(result.decision.next_tick_delay, 300.0)`
  - `self.assertGreater(result.decision.metadata["earliest_blocking_cooldown"], 0.0)`

### test_external_tick_reuses_state_and_records_external_source
Summary: Tests external tick reuses state and records external source
Asserts:
  - `self.assertEqual(first.state.chat_id, second.state.chat_id)`
  - `self.assertEqual(second.state.last_trigger, "external")`
  - `self.assertEqual(second.state.last_decision, "INGRESS_EXTERNAL")`
  - `self.assertEqual(second.decision.action, "INGRESS_EXTERNAL")`
  - `self.assertEqual(second.decision.metadata["source"], "proactive_dispatcher")`
  - `self.assertEqual(status["tracked_chats"], 1)`
  - `self.assertEqual(calls[-1], ("external", "default:GroupMessage:group-1", "proactive_dispatcher", "INGRESS_EXTERNAL"))`

### test_wait_targets_force_wait_during_heartbeat
Summary: Tests wait targets force wait during heartbeat
Asserts:
  - `self.assertEqual(result.decision.action, "WAIT")`
  - `self.assertEqual(result.decision.reason, "wait_state:wait_targets")`
  - `self.assertEqual(result.decision.metadata["wait_scope"], "runtime_wait_targets")`

### test_private_wait_forces_wait_during_heartbeat
Summary: Tests private wait forces wait during heartbeat
Asserts:
  - `self.assertEqual(result.decision.action, "WAIT")`
  - `self.assertEqual(result.decision.reason, "wait_state:private_wait")`
  - `self.assertEqual(result.decision.metadata["wait_scope"], "private")`
  - `self.assertEqual(bridge_calls, [])`

### test_heartbeat_prefers_wakeup_signal_when_eligible
Summary: Tests heartbeat prefers wakeup signal when eligible
Asserts:
  - `self.assertEqual(result.decision.action, "PROACTIVE_WAKEUP")`
  - `self.assertEqual(result.dispatch_result, {"bridge": "wakeup"})`
  - `self.assertEqual(bridge_calls, [("chat-1", "PROACTIVE_WAKEUP", "wakeup_signal")])`
  - `self.assertEqual(result.decision.next_tick_delay, 15.0)`
  - `self.assertEqual(result.decision.metadata["scheduler_bucket"], "post_dialogue")`

### test_heartbeat_prefers_heartflow_when_wakeup_not_ready
Summary: Tests heartbeat prefers heartflow when wakeup not ready
Asserts:
  - `self.assertEqual(result.decision.action, "HEARTFLOW_EVALUATE")`
  - `self.assertEqual(result.dispatch_result, {"bridge": "heartflow"})`
  - `self.assertEqual(bridge_calls, [("chat-1", "HEARTFLOW_EVALUATE", "heartflow_signal:prepare_reply")])`
  - `self.assertEqual(result.decision.metadata["heartflow_preview_mode"], "readonly")`
  - `self.assertEqual(result.decision.next_tick_delay, 15.0)`

### test_heartbeat_prefers_compaction_over_memory_and_dream
Summary: Tests heartbeat prefers compaction over memory and dream
Asserts:
  - `self.assertEqual(result.decision.action, "COMPACTION_EVALUATE")`
  - `self.assertEqual(result.decision.metadata["maintenance_priority_winner"], "compaction")`
  - `self.assertEqual(             result.decision.metadata["skipped_lower_priority_actions"],             ["MEMORY_MAINTENANCE", "DREAM_MAINTENANCE"],         )`
  - `self.assertEqual(result.decision.next_tick_delay, 120.0)`
  - `self.assertEqual(result.decision.metadata["scheduler_bucket"], "maintenance_backoff")`

### test_heartbeat_prefers_memory_over_dream_when_compaction_not_ready
Summary: Tests heartbeat prefers memory over dream when compaction not ready
Asserts:
  - `self.assertEqual(result.decision.action, "MEMORY_MAINTENANCE")`
  - `self.assertEqual(result.decision.metadata["maintenance_priority_winner"], "memory")`
  - `self.assertEqual(result.decision.metadata["skipped_lower_priority_actions"], ["DREAM_MAINTENANCE"])`
  - `self.assertEqual(result.dispatch_result, {"bridge": "MEMORY_MAINTENANCE"})`
  - `self.assertEqual(result.decision.next_tick_delay, 120.0)`

### test_dream_summary_marks_global_throttle_reason
Summary: Tests dream summary marks global throttle reason
Asserts:
  - `self.assertEqual(result.decision.action, "NOOP")`
  - `self.assertEqual(result.decision.metadata["dream_throttle_scope"], "global")`
  - `self.assertEqual(result.decision.metadata["dream_reason"], "dream_global_cooldown")`
  - `self.assertEqual(result.decision.next_tick_delay, 300.0)`

### test_select_due_chats_prefers_new_waiting_and_due_over_future_idle
Summary: Tests select due chats prefers new waiting and due over future idle
Asserts:
  - `self.assertEqual(due_chats, ["chat-new", "chat-wait", "chat-due"])`

### test_describe_due_selection_applies_fairness_penalty_and_starvation_boost
Summary: Tests describe due selection applies fairness penalty and starvation boost
Asserts:
  - `self.assertEqual(report["selected"], ["chat-maint", "chat-hot"])`
  - `self.assertEqual(report["poll_mode"], "FAST")`
  - `self.assertEqual(report["maintenance_budget_total"], 0)`
  - `self.assertGreater(report["score_breakdown"]["chat-hot"]["fairness_penalty"], 0.0)`
  - `self.assertGreater(report["score_breakdown"]["chat-maint"]["maintenance_boost"], 0.0)`

### test_maintenance_budget_blocks_dispatch_even_when_candidate_is_ready
Summary: Tests maintenance budget blocks dispatch even when candidate is ready
Asserts:
  - `self.assertEqual(result.decision.action, "NOOP")`
  - `self.assertEqual(result.decision.reason, "maintenance_budget_blocked")`
  - `self.assertTrue(result.decision.metadata["maintenance_blocked_by_budget"])`
  - `self.assertEqual(result.decision.metadata["maintenance_budget_state"]["remaining"], 0)`
  - `self.assertEqual(result.state.phase, "MAINTENANCE")`
  - `self.assertEqual(bridge_calls, [])`

### test_maintenance_budget_blocked_degrades_to_idle_after_three_rounds
Summary: Tests maintenance budget blocked degrades to idle after three rounds
Asserts:
  - `self.assertEqual(first_phase, "MAINTENANCE")`
  - `self.assertEqual(second_phase, "MAINTENANCE")`
  - `self.assertEqual(third_phase, "IDLE")`
  - `self.assertTrue(third.decision.metadata["maintenance_phase_downgraded"])`
  - `self.assertEqual(third.decision.metadata["maintenance_budget_blocked_rounds"], 3)`
  - `self.assertEqual(third.decision.metadata["schedule_reason"], "maintenance_budget_blocked_idle_downgrade")`

### test_proactive_dispatch_marks_pending_heartflow_when_both_signals_exist
Summary: Tests proactive dispatch marks pending heartflow when both signals exist
Asserts:
  - `self.assertEqual(result.decision.action, "PROACTIVE_WAKEUP")`
  - `self.assertTrue(result.decision.metadata["pending_heartflow"])`
  - `self.assertEqual(result.state.pending_signals["pending_heartflow"], True)`
  - `self.assertEqual(result.state.pending_signals["pending_heartflow_reason"], "heartflow_signal:prepare_reply")`

### test_pending_heartflow_is_replayed_on_next_heartbeat
Summary: Tests pending heartflow is replayed on next heartbeat
Asserts:
  - `self.assertEqual(result.decision.action, "HEARTFLOW_EVALUATE")`
  - `self.assertEqual(result.decision.reason, "heartflow_signal:prepare_reply")`
  - `self.assertTrue(result.decision.metadata["pending_heartflow_consumed"])`
  - `self.assertFalse(result.state.pending_signals["pending_heartflow"])`
  - `self.assertEqual(bridge_calls, [("chat-1", "HEARTFLOW_EVALUATE", "heartflow_signal:prepare_reply")])`

### test_direct_heartbeat_maintenance_uses_fallback_budget_state_in_metadata
Summary: Tests direct heartbeat maintenance uses fallback budget state in metadata
Asserts:
  - `self.assertEqual(result.decision.action, "COMPACTION_EVALUATE")`
  - `self.assertEqual(result.decision.metadata["maintenance_budget_state"]["total"], 1)`
  - `self.assertEqual(result.decision.metadata["maintenance_budget_state"]["remaining"], 1)`
  - `self.assertEqual(result.state.pending_signals["maintenance_budget_state"]["total"], 1)`
  - `self.assertEqual(result.state.pending_signals["maintenance_budget_state"]["remaining"], 1)`

### test_maintenance_budget_escalates_when_backlog_lives_in_skipped_by_batch
Summary: Tests maintenance budget escalates when backlog lives in skipped by batch
Asserts:
  - `self.assertEqual(first["selected"], [])`
  - `self.assertEqual(first["skipped_by_batch"], ["chat-maint"])`
  - `self.assertEqual(first["maintenance_budget_total"], 1)`
  - `self.assertEqual(second["maintenance_budget_total"], 2)`

### test_forced_promotion_selects_starved_chat_before_hot_chat
Summary: Tests forced promotion selects starved chat before hot chat
Asserts:
  - `self.assertEqual(report["selected"], ["chat-starved"])`
  - `self.assertEqual(report["forced_promotions_selected"], ["chat-starved"])`
  - `self.assertEqual(report["score_breakdown"]["chat-starved"]["selected_reason"], "selected_by_forced_promotion")`
  - `self.assertTrue(report["score_breakdown"]["chat-starved"]["forced_promotion_eligible"])`
  - `self.assertEqual(refreshed.missed_due_passes, kernel.STARVATION_PASS_THRESHOLDS["MAINTENANCE"])`

### test_due_selection_exposes_quota_and_backpressure_summary
Summary: Tests due selection exposes quota and backpressure summary
Asserts:
  - `self.assertTrue(report["busy_backpressure_active"])`
  - `self.assertEqual(report["batch_plan"]["dialogue_slots"], 16)`
  - `self.assertGreater(report["batch_fill_rate"], 0.0)`
  - `self.assertIn("busy_ratio", report["batch_pressure"])`
  - `self.assertEqual(report["quota_skip_counts"]["skipped_by_maintenance_quota"], 6)`
  - `self.assertEqual(len(report["maintenance_selected"]), 4)`

### test_describe_due_selection_is_observe_only_until_commit
Summary: Tests describe due selection is observe only until commit
Asserts:
  - `self.assertEqual(report["selected"], [])`
  - `self.assertEqual(after_describe, 2)`
  - `self.assertEqual(after_commit, 3)`

### test_maintenance_quota_is_hard_limit_even_with_overflow_capacity
Summary: Tests maintenance quota is hard limit even with overflow capacity
Asserts:
  - `self.assertEqual(report["batch_plan"]["maintenance_slots"], 4)`
  - `self.assertEqual(len(report["maintenance_selected"]), 4)`
  - `self.assertEqual(report["quota_skip_counts"]["skipped_by_maintenance_quota"], 4)`

### test_dispatch_failure_still_commits_heartbeat_state_with_fast_recheck
Summary: Tests dispatch failure still commits heartbeat state with fast recheck
Asserts:
  - `self.assertEqual(state.last_decision, "PROACTIVE_WAKEUP")`
  - `self.assertTrue(bool(state.phase))`
  - `self.assertGreater(state.last_tick_at, 0.0)`
  - `self.assertGreater(state.next_tick_at, before)`
  - `self.assertEqual(state.pending_signals["schedule_reason"], "dispatch_failure_recheck")`
  - `self.assertTrue(state.pending_signals["dispatch_failed"])`
  - `self.assertEqual(state.pending_signals["dispatch_error_type"], "RuntimeError")`
  - `self.assertEqual(state.pending_signals["dispatch_error_reason"], "bridge boom")`
  - `self.assertEqual(state.pending_signals["dispatch_failure_backoff"], 5.0)`
  - `self.assertRaises(RuntimeError)`

### test_post_dispatch_cooldown_reason_is_not_overwritten_by_base_pending_signals
Summary: Tests post dispatch cooldown reason is not overwritten by base pending signals
Asserts:
  - `self.assertEqual(result.state.pending_signals["wakeup_cooldown_reason"], "dispatch_reason")`
  - `self.assertEqual(result.state.pending_signals["schedule_reason"], "proactive_wakeup")`

### test_message_entry_routes_through_kernel_after_guards
Summary: Tests message entry routes through kernel after guards
Asserts:
  - `self.assertEqual(results, [])`
  - `self.assertIn(("handle_group_reply_wait", "default:GroupMessage:group-1"), facade_calls)`
  - `self.assertIn(("track_incoming_user_activity", "user-1"), facade_calls)`
  - `self.assertIn(("try_consume_reflect_feedback", "user-1"), facade_calls)`
  - `self.assertIn(("record_and_dispatch_attention", "default:GroupMessage:group-1"), facade_calls)`
  - `self.assertIn(("cancel_group_wait_if_interrupted", "BUFFERED"), facade_calls)`
  - `self.assertIn(("suppress_default_llm_if_engaged", "BUFFERED", False), facade_calls)`

### test_message_entry_anonymous_sender_skips_user_activity_tracking
Summary: Tests message entry anonymous sender skips user activity tracking
Asserts:
  - `self.assertEqual(asyncio.run(_run()), [])`
  - `self.assertNotIn(("track", "800000001234"), facade_calls)`
  - `self.assertIn(("record_and_dispatch_attention", "800000001234", True), facade_calls)`

### test_message_entry_permission_deny_stops_before_wait_and_attention
Summary: Tests message entry permission deny stops before wait and attention
Asserts:
  - `self.assertEqual(asyncio.run(_run()), [])`
  - `self.assertEqual(calls, ["poke", ("permission", "default:GroupMessage:group-1")])`

### test_message_entry_attention_error_yields_runtime_fallback_text
Summary: Tests message entry attention error yields runtime fallback text
Asserts:
  - `self.assertEqual(asyncio.run(_run()), [{"type": "plain", "text": "稍后再试"}])`

### test_message_entry_yields_ghost_message_when_default_llm_is_suppressed
Summary: Tests message entry yields ghost message when default llm is suppressed
Asserts:
  - `self.assertEqual(asyncio.run(_run()), [{"type": "plain", "text": "[[ghost]]"}])`

### test_group_decrease_notice_clears_only_runtime_state_for_bot_self
Summary: Tests group decrease notice clears only runtime state for bot self
Asserts:
  - `self.assertTrue(handled)`
  - `self.assertEqual(calls.count("default:GroupMessage:123456"), 6)`
  - `self.assertIn(("default:GroupMessage:123456", "bot_left_group"), calls)`

### test_message_entry_self_message_stops_before_kernel
Summary: Tests message entry self message stops before kernel
Asserts:
  - `self.assertEqual(results, [])`
  - `self.assertEqual(calls, [])`

### test_peek_loop_state_does_not_create_state
Summary: Tests peek loop state does not create state
Asserts:
  - `self.assertEqual(before, 0)`
  - `self.assertIsNone(state)`
  - `self.assertEqual(after, 0)`

### test_describe_status_sync_exposes_scheduler_policy_profiles
Summary: Tests describe status sync exposes scheduler policy profiles
Asserts:
  - `self.assertEqual(policy["active_profile"], "balanced")`
  - `self.assertIn("dialogue_first", policy["available_profiles"])`
  - `self.assertIn("maintenance_friendly", policy["available_profiles"])`
  - `self.assertEqual(policy["current"]["fairness_penalty_multiplier"], kernel.FAIRNESS_PENALTY_MULTIPLIER)`
  - `self.assertEqual(             policy["current"]["forced_promotion_pass_thresholds"]["MAINTENANCE"],             kernel.STARVATION_PASS_THRESHOLDS["MAINTENANCE"],         )`

### test_scheduler_policy_sync_reflects_active_testing_profile
Summary: Tests scheduler policy sync reflects active testing profile
Asserts:
  - `self.assertEqual(policy["active_profile"], "maintenance_friendly")`
  - `self.assertEqual(policy["current"]["maintenance_batch_slots"], 6)`
  - `self.assertEqual(policy["current"]["fairness_penalty_multiplier"], 10.0)`

### test_scheduler_policy_profiles_offer_distinct_tuning_matrix
Summary: Tests scheduler policy profiles offer distinct tuning matrix
Asserts:
  - `self.assertLess(             profiles["maintenance_friendly"]["maintenance_boost_divisor_seconds"],             profiles["balanced"]["maintenance_boost_divisor_seconds"],         )`
  - `self.assertGreater(             profiles["dialogue_first"]["fairness_penalty_multiplier"],             profiles["balanced"]["fairness_penalty_multiplier"],         )`
  - `self.assertGreater(             profiles["maintenance_friendly"]["maintenance_batch_slots"],             profiles["dialogue_first"]["maintenance_batch_slots"],         )`
  - `self.assertGreater(             profiles["dialogue_first"]["forced_promotion_pass_thresholds"]["IDLE"],             profiles["balanced"]["forced_promotion_pass_thresholds"]["IDLE"],         )`

### test_scheduler_profiles_change_due_selection_behavior
Summary: Tests scheduler profiles change due selection behavior
Asserts:
  - `self.assertEqual(len(balanced["maintenance_selected"]), 4)`
  - `self.assertEqual(len(maintenance_friendly["maintenance_selected"]), 6)`
  - `self.assertGreater(             balanced["quota_skip_counts"]["skipped_by_maintenance_quota"],             maintenance_friendly["quota_skip_counts"]["skipped_by_maintenance_quota"],         )`

### test_scheduler_profiles_change_forced_promotion_threshold
Summary: Tests scheduler profiles change forced promotion threshold
Asserts:
  - `self.assertFalse(dialogue_first["score_breakdown"]["chat-maint"]["forced_promotion_eligible"])`
  - `self.assertTrue(balanced["score_breakdown"]["chat-maint"]["forced_promotion_eligible"])`

## tests/test_proactive_scheduler_refactor.py (25 tests)

### test_proactive_task_exposes_local_scheduler_status
Summary: Tests proactive task exposes local scheduler status
Asserts:
  - `self.assertIn("dream_ready", status)`
  - `self.assertFalse(status["running"])`
  - `self.assertIn("dream_scheduler", status)`
  - `self.assertIn("group_signin", status)`
  - `self.assertIn("heartflow", status)`
  - `self.assertFalse(status["heartflow"]["enabled"])`
  - `self.assertFalse(status["chat_loop_kernel_bound"])`
  - `self.assertEqual(status["heartbeat_mode"], "kernel_mediated")`
  - `self.assertTrue(status["private_wait_visible_in_heartbeat"])`
  - `self.assertTrue(status["heartflow_preview_readonly"])`
  - `self.assertEqual(status["dream_scope"], "global_throttle")`
  - `self.assertEqual(status["scheduler_poll_mode"], "FAST")`
  - `self.assertEqual(status["scheduler_poll_interval"], 5.0)`
  - `self.assertEqual(status["global_maintenance_interval"], 60.0)`

### test_configure_accepts_deps_and_binds_planner_heartflow_manager
Summary: Tests configure accepts deps and binds planner heartflow manager
Asserts:
  - `self.assertTrue(task.dream_scheduler.dream_visible)`
  - `self.assertIs(planner.heartflow_manager, task.heartflow_manager)`

### test_proactive_task_refresh_config_propagates_to_runtime_children
Summary: Tests proactive task refresh config propagates to runtime children
Asserts:
  - `self.assertIs(task.config, new_config)`
  - `self.assertIs(task.gateway.config, new_config)`
  - `self.assertIs(task.proactive_dispatcher.config, new_config)`
  - `self.assertIs(task.wakeup_service.config, new_config)`
  - `self.assertIs(task.group_signin_service.config, new_config)`
  - `self.assertIs(task.decay_service.config, new_config)`
  - `self.assertIs(task.diary_service.config, new_config)`
  - `self.assertIs(task.dream_scheduler.config, new_config)`
  - `self.assertIs(task.heartflow_manager.config, new_config)`
  - `self.assertIs(task.dream_generator.config, new_config)`
  - `self.assertIs(task.dream_agent.config, new_config)`

### test_start_initializes_global_maintenance_clock
Summary: Tests start initializes global maintenance clock
Asserts:
  - `self.assertGreater(task._last_global_maintenance_run, 0.0)`

### test_loop_no_longer_uses_first_tick_continue_for_global_maintenance
Summary: Tests loop no longer uses first tick continue for global maintenance
Asserts:
  - `self.assertNotIn("if self._last_global_maintenance_run <= 0:", content)`
  - `self.assertNotIn("self._last_global_maintenance_run = now\n                    continue", content)`

### test_chat_heartbeat_pass_routes_active_chats_through_kernel
Summary: Tests chat heartbeat pass routes active chats through kernel
Asserts:
  - `self.assertEqual(results, [{"chat_id": "chat-1", "action": "NOOP", "reason": "no_signal_ready"}])`
  - `self.assertEqual(status["due_chat_count"], 1)`
  - `self.assertEqual(status["skipped_not_due_count"], 1)`
  - `self.assertEqual(status["scheduler_poll_mode"], "NORMAL")`
  - `self.assertEqual(status["scheduler_poll_interval"], 10.0)`
  - `self.assertEqual(status["due_phase_mix"], {"MAINTENANCE": 1})`
  - `self.assertEqual(status["maintenance_budget_total"], 1)`
  - `self.assertEqual(status["maintenance_budget_remaining"], 1)`
  - `self.assertEqual(status["scheduler_batch_limit"], 32)`
  - `self.assertEqual(status["scheduler_batch_plan"]["maintenance_slots"], 12)`
  - `self.assertEqual(status["forced_promotion_count"], 1)`
  - `self.assertEqual(status["last_selection_summary"]["maintenance_selected_count"], 1)`
  - `self.assertTrue(status["maintenance_backpressure_active"])`
  - `self.assertEqual(status["scheduler_policy"]["active_profile"], "maintenance_friendly")`
  - `self.assertEqual(status["scheduler_policy"]["current"]["maintenance_batch_slots"], 6)`
  - `self.assertIn("forced_promotion_count", status["kernel_due_selection_summary"])`
  - `self.assertEqual(status["poll_mode_transition"]["previous"], "FAST")`
  - `self.assertEqual(status["poll_mode_transition"]["current"], "NORMAL")`

### test_handle_chat_heartbeat_marks_observe_only_dispatch_mode
Summary: Tests handle chat heartbeat marks observe only dispatch mode
Asserts:
  - `self.assertEqual(decision.metadata["dispatch_mode"], "observe_only")`
  - `self.assertEqual(result["dispatch_mode"], "observe_only")`

### test_bind_chat_loop_kernel_registers_signal_sources_and_bridges
Summary: Tests bind chat loop kernel registers signal sources and bridges
Asserts:
  - `self.assertTrue(status["dispatch_bridges"]["PROACTIVE_WAKEUP"])`
  - `self.assertTrue(status["dispatch_bridges"]["HEARTFLOW_EVALUATE"])`
  - `self.assertTrue(status["dispatch_bridges"]["DREAM_MAINTENANCE"])`
  - `self.assertTrue(status["dispatch_bridges"]["MEMORY_MAINTENANCE"])`
  - `self.assertTrue(status["dispatch_bridges"]["COMPACTION_EVALUATE"])`

### test_bridge_handlers_are_kernel_mediated
Summary: Tests bridge handlers are kernel mediated
Asserts:
  - `self.assertEqual(wake["dispatch_mode"], "kernel_mediated")`
  - `self.assertEqual(heart["dispatch_mode"], "kernel_mediated")`
  - `self.assertEqual(memory["dispatch_mode"], "kernel_mediated")`
  - `self.assertEqual(dream["dispatch_mode"], "kernel_mediated")`
  - `self.assertEqual(comp["dispatch_mode"], "kernel_mediated")`
  - `self.assertEqual(memory["bridge"], "MEMORY_MAINTENANCE")`
  - `self.assertTrue(memory["result"]["performed"])`
  - `self.assertEqual(dream["result"]["throttle_scope"], "global")`

### test_heartflow_cooldown_requires_visible_dispatch
Summary: Tests heartflow cooldown requires visible dispatch
Asserts:
  - `self.assertEqual(hidden["cooldown_until"], 0.0)`
  - `self.assertFalse(hidden["visible_dispatch_performed"])`
  - `self.assertGreater(visible["cooldown_until"], 0.0)`
  - `self.assertTrue(visible["visible_dispatch_performed"])`
  - `self.assertEqual(cooldown_calls, [("chat-1", "heartflow", "heartflow_dispatch")])`

### test_heartflow_preview_is_readonly
Summary: Tests heartflow preview is readonly
Asserts:
  - `self.assertEqual(after_session, before_session)`
  - `self.assertEqual(manager._pulses_by_chat["chat-1"], before_pulses)`
  - `self.assertEqual(manager._impulse_decisions_by_chat["chat-1"], before_impulses)`
  - `self.assertEqual(manager._action_decisions_by_chat["chat-1"], before_actions)`

### test_dream_scheduler_reports_global_throttle
Summary: Tests dream scheduler reports global throttle
Asserts:
  - `self.assertFalse(eligibility["eligible"])`
  - `self.assertEqual(eligibility["reason"], "dream_global_cooldown")`
  - `self.assertEqual(eligibility["throttle_scope"], "global")`
  - `self.assertFalse(result["performed"])`
  - `self.assertEqual(result["reason"], "dream_global_cooldown")`
  - `self.assertEqual(result["throttle_scope"], "global")`

### test_group_profile_target_prefers_top_non_self_speaker
Summary: Tests group profile target prefers top non self speaker
Asserts:
  - `self.assertEqual(result, ("user-1", "Alice", 2))`

### test_proactive_task_no_longer_imports_legacy_proactive_helper
Summary: Tests proactive task no longer imports legacy proactive helper
Asserts:
  - `self.assertNotIn("LegacyProactiveTask", content)`

### test_scheduler_loop_no_longer_directly_runs_chat_services
Summary: Tests scheduler loop no longer directly runs chat services
Asserts:
  - `self.assertNotIn("await self.wakeup_service.run_once()", content)`
  - `self.assertNotIn("await self.heartflow_manager.tick()", content)`
  - `self.assertNotIn("self._fire_background_task(self.dream_scheduler.run_once())", content)`

### test_wakeup_routes_intent_through_dispatcher_before_energy_cost
Summary: Tests wakeup routes intent through dispatcher before energy cost
Asserts:
  - `self.assertEqual(len(dispatcher.intents), 1)`
  - `self.assertEqual(dispatcher.intents[0].source, "wakeup")`
  - `self.assertEqual(dispatcher.intents[0].suggested_action_tier, "chat")`
  - `self.assertEqual(state_engine.energy_calls, [])`
  - `self.assertEqual(state_engine.energy_calls, [("group:10001", 5)])`
  - `self.assertGreater(state.next_wakeup_timestamp, now)`

### test_dispatcher_blocks_wakeup_and_heartflow_during_quiet_hours
Summary: Tests dispatcher blocks wakeup and heartflow during quiet hours
Asserts:
  - `self.assertEqual([item.blocked_reason for item in decisions], ["quiet_hours", "quiet_hours"])`
  - `self.assertTrue(all(item.safety_checks["quiet_hours"] for item in decisions))`

### test_dispatcher_injects_proactive_events_through_kernel_bound_gate
Summary: Tests dispatcher injects proactive events through kernel bound gate
Asserts:
  - `self.assertTrue(decision.allowed)`
  - `self.assertEqual(calls, [("group:10001", "external", "proactive_dispatcher", True)])`

### test_wakeup_quiet_hours_block_does_not_consume_energy_or_cooldown
Summary: Tests wakeup quiet hours block does not consume energy or cooldown
Asserts:
  - `self.assertEqual(state_engine.energy_calls, [])`
  - `self.assertEqual(state.next_wakeup_timestamp, 0)`

### test_wakeup_guidance_is_human_low_pressure
Summary: Tests wakeup guidance is human low pressure
Asserts:
  - `self.assertIn("one short natural line", guidance)`
  - `self.assertNotIn("threshold", guidance.lower())`
  - `self.assertNotIn("system", guidance.lower())`
  - `self.assertNotIn("anyone here", guidance.lower())`

### test_wakeup_build_signal_uses_life_defaults_when_config_missing
Summary: Tests wakeup build signal uses life defaults when config missing
Asserts:
  - `self.assertTrue(signal["eligible"])`
  - `self.assertEqual(signal["reason"], "silence_threshold_reached")`
  - `self.assertEqual(signal["wakeup_cost"], float(defaults.wakeup_cost))`
  - `self.assertEqual(signal["wakeup_cooldown"], float(defaults.wakeup_cooldown))`

### test_wakeup_run_for_chat_falls_back_when_life_config_missing
Summary: Tests wakeup run for chat falls back when life config missing
Asserts:
  - `self.assertEqual(len(dispatcher.intents), 1)`
  - `self.assertEqual(dispatcher.intents[0].cost, float(defaults.wakeup_cost))`
  - `self.assertEqual(dispatcher.intents[0].cooldown, float(defaults.wakeup_cooldown))`
  - `self.assertEqual(state_engine.energy_calls, [("group:10001", defaults.wakeup_cost)])`
  - `self.assertGreater(state.next_wakeup_timestamp, now)`

### test_wakeup_partial_life_config_merges_missing_defaults
Summary: Tests wakeup partial life config merges missing defaults
Asserts:
  - `self.assertTrue(signal["eligible"])`
  - `self.assertEqual(signal["wakeup_cost"], float(defaults.wakeup_cost))`
  - `self.assertEqual(signal["wakeup_cooldown"], float(defaults.wakeup_cooldown))`

### test_wakeup_run_for_chat_uses_safe_life_fallback_when_signal_omits_cost
Summary: Tests wakeup run for chat uses safe life fallback when signal omits cost
Asserts:
  - `self.assertTrue(result["performed"])`
  - `self.assertEqual(len(dispatcher.intents), 1)`
  - `self.assertEqual(dispatcher.intents[0].cost, 7.0)`
  - `self.assertEqual(dispatcher.intents[0].cooldown, float(defaults.wakeup_cooldown))`
  - `self.assertEqual(state_engine.energy_calls, [("group:10001", 7.0)])`

### test_dispatcher_history_reflects_queued_before_async_completion
Summary: Tests dispatcher history reflects queued before async completion
Asserts:
  - `self.assertTrue(decision.synthetic_event_queued)`
  - `self.assertFalse(decision.reply_sent)`
  - `self.assertEqual(decision.status, "queued")`
  - `self.assertTrue(history["decision"]["synthetic_event_queued"])`
  - `self.assertFalse(history["decision"]["reply_sent"])`
  - `self.assertEqual(history["decision"]["status"], "queued")`
  - `self.assertEqual(history["status"], "queued")`
  - `self.assertTrue(history_after["decision"]["reply_sent"])`
  - `self.assertEqual(history_after["decision"]["reply_preview"], "hello world")`
  - `self.assertEqual(history_after["decision"]["status"], "sent")`
  - `self.assertEqual(history_after["status"], "sent")`

## tests/test_persona_context_refactor.py (26 tests)

### test_persona_summary_generates_and_persists_first_person_rewrite
Summary: Tests persona summary generates and persists first person rewrite
Asserts:
  - `self.assertEqual(payload["first_person_rewrite"], "I stay in character and answer naturally.")`
  - `self.assertEqual(             persistence.cache["persona-1"]["first_person_rewrite"],             "I stay in character and answer naturally.",         )`

### test_persona_cache_hit_without_first_person_field_falls_back_safely
Summary: Tests persona cache hit without first person field falls back safely
Asserts:
  - `self.assertEqual(payload["first_person_rewrite"], "summary fallback")`
  - `self.assertEqual(payload["summary"], "summary fallback")`

### test_persona_summary_reads_threshold_from_real_performance_config
Summary: Tests persona summary reads threshold from real performance config
Asserts:
  - `self.assertEqual(payload["summary"], "short prompt")`
  - `self.assertEqual(payload["first_person_rewrite"], "short prompt")`
  - `self.assertEqual(gateway.calls, [])`

### test_persona_summary_empty_prompt_uses_ready_fallback_without_gateway
Summary: Tests persona summary empty prompt uses ready fallback without gateway
Asserts:
  - `self.assertEqual(payload["summary"], "")`
  - `self.assertEqual(payload["first_person_rewrite"], "")`
  - `self.assertTrue(payload["is_full_ready"])`
  - `self.assertEqual(payload["raw"], "")`
  - `self.assertEqual(gateway.calls, [])`

### test_persona_summary_concurrent_requests_share_single_generation
Summary: Tests persona summary concurrent requests share single generation
Asserts:
  - `self.assertEqual(first["summary"], "core summary")`
  - `self.assertEqual(second["summary"], "core summary")`
  - `self.assertEqual(calls["core"], 1)`
  - `self.assertEqual(calls["style"], 1)`
  - `self.assertEqual(calls["rewrite"], 1)`
  - `self.assertEqual(calls["background"], 1)`

### test_persona_background_shard_failure_keeps_cache_recoverable_and_clears_pending
Summary: Tests persona background shard failure keeps cache recoverable and clears pending
Asserts:
  - `self.assertFalse(summarizer.cache["persona-shards"]["is_full_ready"])`
  - `self.assertEqual(summarizer.cache["persona-shards"]["shards"], {})`
  - `self.assertNotIn("persona-shards", summarizer.pending_tasks)`

### test_persona_core_identity_template_and_fallback_use_same_expert_role_shell
Summary: Tests persona core identity template and fallback use same expert role shell
Asserts:
  - `self.assertEqual(             template_call["kwargs"]["system_prompt"].split("\n\n")[0],             "你是一个资深的角色扮演设定提取专家。",         )`
  - `self.assertEqual(             fallback_call["kwargs"]["system_prompt"],             "你是一个资深的角色扮演设定提取专家。",         )`

### test_persona_remaining_shards_use_expected_templates
Summary: Tests persona remaining shards use expected templates
Asserts:
  - `self.assertEqual(             results,             [f"shard:{template_id.value}" for _method, template_id in shard_methods],         )`
  - `self.assertEqual(             [template_id for template_id, _kwargs in calls],             [template_id for _method, template_id in shard_methods],         )`
  - `self.assertEqual(kwargs["original_prompt"], "raw persona facts")`
  - `self.assertEqual(kwargs["cache_key"], "persona-shards")`
  - `self.assertFalse(kwargs["is_json"])`
  - `self.assertIn("raw persona facts", kwargs["fallback_prompt"])`

### test_persona_cache_recovery_creates_background_task_when_not_ready
Summary: Tests persona cache recovery creates background task when not ready
Asserts:
  - `self.assertEqual(payload["summary"], "cached summary")`
  - `self.assertEqual(calls, [("cached raw persona", "persona-recovery")])`
  - `self.assertNotIn("persona-recovery", summarizer.pending_tasks)`
  - `self.assertFalse(task.done())`

### test_first_person_rewrite_without_template_uses_persona_lane
Summary: Tests first person rewrite without template uses persona lane
Asserts:
  - `self.assertEqual(result, "I answer briefly in my own voice.")`
  - `self.assertEqual(calls[0][1], "persona-1")`
  - `self.assertIn("brief summary", calls[0][0])`
  - `self.assertEqual(             calls[0][2]["system_prompt"],             "Rewrite persona summaries into concise first-person self-awareness text.",         )`
  - `self.assertFalse(calls[0][2]["is_json"])`

### test_first_person_rewrite_with_template_passes_envelope_to_persona_lane
Summary: Tests first person rewrite with template passes envelope to persona lane
Asserts:
  - `self.assertEqual(result, "I use the rendered template.")`
  - `self.assertEqual(             rendered,             [                 (                     self.persona_mod.PromptTemplateId.PERSONA_FIRST_PERSON_REWRITE,                     {                         "original_prompt": "raw persona",            ...`
  - `self.assertEqual(lane_calls[0][0:2], ("template prompt", "persona-1"))`
  - `self.assertEqual(lane_calls[0][2]["system_prompt"], "template system")`
  - `self.assertIs(lane_calls[0][2]["template_envelope"], envelope)`

### test_persona_cache_hit_without_id_persists_generated_rewrite
Summary: Tests persona cache hit without id persists generated rewrite
Asserts:
  - `self.assertEqual(payload["first_person_rewrite"], "I use the repaired rewrite.")`
  - `self.assertEqual(             persistence.cache["session_chat-1"]["first_person_rewrite"],             "I use the repaired rewrite.",         )`
  - `self.assertEqual(len(persistence.saved_snapshots), 1)`

### test_first_person_rewrite_rejects_empty_and_too_short_results
Summary: Tests first person rewrite rejects empty and too short results
Asserts:
  - `self.assertEqual(             asyncio.run(                 summarizer._build_first_person_rewrite(                     original_prompt="",                     summary="",                     style="",                     cache_key="persona-empty",...`
  - `self.assertEqual(             asyncio.run(                 summarizer._build_first_person_rewrite(                     original_prompt="raw",                     summary="summary",                     style="calm",                     cache_key="p...`

### test_persist_cache_falls_back_to_sync_persistence
Summary: Tests persist cache falls back to sync persistence
Asserts:
  - `self.assertEqual(persistence.saved, {"persona-1": {"summary": "cached"}})`

### test_context_engine_prefers_first_person_rewrite_and_honors_disable_rag_injection
Summary: Tests context engine prefers first person rewrite and honors disable rag injection
Asserts:
  - `self.assertIn("I know who I am and I answer in my own voice.", system_prompt)`
  - `self.assertNotIn("She is described in third person.", system_prompt)`
  - `self.assertIsInstance(style_variant, str)`
  - `self.assertEqual(proactive_recall, "")`
  - `self.assertEqual(recall_block, "")`
  - `self.assertEqual(memory_engine.calls, [])`

### test_context_engine_private_block_and_rules_use_first_person_wording
Summary: Tests context engine private block and rules use first person wording
Asserts:
  - `self.assertIn("我现在正在和 小明（张三） 私聊", private_block)`
  - `self.assertIn("我对 ta 的标签印象：熟人 / 夜猫子", private_block)`
  - `self.assertIn("这轮可参考的近期私聊记忆点：昨晚聊过电影；会在半夜突然发消息", dynamic_private_block)`
  - `self.assertIn("我的表达底线：", rules_block)`
  - `self.assertIn("我只说会真正发到聊天窗口里的自然话。", rules_block)`
  - `self.assertIn("我不直接复述记忆原文", rules_block)`
  - `self.assertIn("不暴露记忆闪回、注入、提示词这类机制", rules_block)`
  - `self.assertNotIn("如果本轮系统提供了可用动作", rules_block)`
  - `self.assertNotIn("不暴露工具过程或机制", rules_block)`
  - `self.assertNotIn("不要在开头", rules_block)`

### test_context_engine_prefers_profile_prompt_bundle_from_state_engine
Summary: Tests context engine prefers profile prompt bundle from state engine
Asserts:
  - `self.assertIn("阿明（张三）", stable_private_block)`
  - `self.assertIn("偏好画像", stable_private_block)`
  - `self.assertIn("昨晚聊过电影", dynamic_private_block)`

### test_context_engine_wraps_proactive_recall_as_internal_reference
Summary: Tests context engine wraps proactive recall as internal reference
Asserts:
  - `self.assertEqual(memory_engine.calls, [])`
  - `self.assertEqual(recall_block, "")`

### test_context_engine_keeps_agency_context_out_of_system_prompt
Summary: Tests context engine keeps agency context out of system prompt
Asserts:
  - `self.assertNotIn("agency", system_prompt.lower())`

### test_context_engine_pushes_dynamic_state_and_behavior_out_of_system_prompt
Summary: Tests context engine pushes dynamic state and behavior out of system prompt
Asserts:
  - `self.assertNotIn("此刻回应倾向", system_prompt)`
  - `self.assertNotIn("我现在心情", system_prompt)`
  - `self.assertIn("此刻回应倾向", envelope.situational_context_block)`
  - `self.assertIn("我现在心情", envelope.situational_context_block)`
  - `self.assertEqual(_proactive_recall, "")`

### test_context_engine_moves_stable_expression_and_jargon_into_soft_background
Summary: Tests context engine moves stable expression and jargon into soft background
Asserts:
  - `self.assertNotIn("Use short fragments.", system_prompt)`
  - `self.assertNotIn("黑话说明：DDL 指截止时间", system_prompt)`
  - `self.assertNotIn("Keep this turn short; avoid another long reply.", system_prompt)`
  - `self.assertNotIn("我会先回应眼前这条消息，不突然另起话题。", system_prompt)`
  - `self.assertNotIn("我会优先回应当前这条消息，不突然另起话题。", system_prompt)`
  - `self.assertNotIn("群里最近会说：摸了、开摆", system_prompt)`
  - `self.assertIn("Use short fragments.", envelope.soft_background_block)`
  - `self.assertIn("黑话说明：DDL 指截止时间", envelope.soft_background_block)`
  - `self.assertIn("Keep this turn short; avoid another long reply.", envelope.soft_background_block)`
  - `self.assertIn("群里最近会说：摸了、开摆", envelope.situational_context_block)`
  - `self.assertNotIn("Keep this turn short; avoid another long reply.", envelope.situational_context_block)`
  - `self.assertIn("我会先回应眼前这条消息，不突然另起话题。", envelope.planner_runtime_instruction_block)`
  - `self.assertIn("我会优先回应当前这条消息，不突然另起话题。", envelope.planner_runtime_instruction_block)`

### test_context_engine_no_longer_splits_expression_text_for_dynamic_turn_cues
Summary: Tests context engine no longer splits expression text for dynamic turn cues
Asserts:
  - `self.assertNotIn("Keep this turn short; avoid another long reply.", system_prompt)`
  - `self.assertIn("Keep this turn short; avoid another long reply.", envelope.soft_background_block)`
  - `self.assertNotIn("Keep this turn short; avoid another long reply.", envelope.situational_context_block)`
  - `self.assertIn("群里最近会说：摸了、开摆", envelope.situational_context_block)`

### test_context_engine_accepts_legacy_kwargs_as_compatibility_aliases
Summary: Tests context engine accepts legacy kwargs as compatibility aliases
Asserts:
  - `self.assertNotIn("legacy habit", system_prompt)`
  - `self.assertNotIn("legacy jargon", system_prompt)`
  - `self.assertIn("legacy habit", envelope.soft_background_block)`
  - `self.assertIn("legacy jargon", envelope.soft_background_block)`
  - `self.assertIn("legacy slang", envelope.situational_context_block)`
  - `self.assertNotIn("legacy slang", system_prompt)`

### test_context_engine_prefers_new_kwargs_over_legacy_aliases
Summary: Tests context engine prefers new kwargs over legacy aliases
Asserts:
  - `self.assertNotIn("new habit", system_prompt)`
  - `self.assertNotIn("new jargon", system_prompt)`
  - `self.assertNotIn("legacy habit", system_prompt)`
  - `self.assertNotIn("legacy jargon", system_prompt)`
  - `self.assertIn("new habit", envelope.soft_background_block)`
  - `self.assertIn("new jargon", envelope.soft_background_block)`
  - `self.assertNotIn("legacy habit", envelope.soft_background_block)`
  - `self.assertNotIn("legacy jargon", envelope.soft_background_block)`
  - `self.assertIn("new slang", envelope.situational_context_block)`
  - `self.assertNotIn("legacy slang", envelope.situational_context_block)`

### test_context_engine_records_prefix_block_lengths_in_status
Summary: Tests context engine records prefix block lengths in status
Asserts:
  - `self.assertEqual(status["prefix_changed_reason"], "first_seen")`
  - `self.assertTrue(status["semantic_system_hash"])`
  - `self.assertGreater(status["semantic_system_length"], 0)`
  - `self.assertGreater(status["frozen_prefix_length"], 0)`
  - `self.assertGreaterEqual(status["semi_stable_length"], 0)`
  - `self.assertIn("persona_core", status["frozen_prefix_blocks"])`
  - `self.assertIn("style_block", status["frozen_prefix_blocks"])`
  - `self.assertIn("system_rules", status["frozen_prefix_blocks"])`
  - `self.assertIn("cold_summary", status["semi_stable_blocks"])`
  - `self.assertIn("stable_expression", status["semi_stable_blocks"])`
  - `self.assertGreater(status["frozen_prefix_blocks"]["persona_core"], 0)`
  - `self.assertTrue(status["system_rules_items"])`
  - `self.assertIn("current_message_first", status["system_rules_candidate_items"])`
  - `self.assertIn(             "current_message_first",             {item["key"] for item in status["system_rules_items"]},         )`

### test_context_engine_compresses_cold_summary_for_soft_background
Summary: Tests context engine compresses cold summary for soft background
Asserts:
  - `self.assertIn("冷区背景摘要", compressed)`
  - `self.assertLessEqual(len(compressed), 240)`
  - `self.assertNotIn("后来我们围绕考试焦虑聊了很多。然后", compressed)`
  - `self.assertNotIn("最后还在想要不要找我再确认一次重点。", compressed)`

## tests/unit/conversation/test_context_runtime_wiring.py (16 tests)

### test_core_services_wires_dialogue_store_and_compaction_when_enabled
Summary: Tests core services wires dialogue store and compaction when enabled
Asserts:
  - `self.assertIsNotNone(core.dialogue_store)`
  - `self.assertIsNotNone(core.context_compaction)`
  - `self.assertIs(core.state_engine.dialogue_store, core.dialogue_store)`
  - `self.assertIs(core.state_engine.context_compaction, core.context_compaction)`
  - `self.assertIs(core.gateway.dialogue_store, core.dialogue_store)`
  - `self.assertIs(core.gateway.context_compaction, core.context_compaction)`
  - `self.assertIs(core.db_service.dialogue_store, core.dialogue_store)`
  - `self.assertIs(core.db_service.context_compaction, core.context_compaction)`
  - `self.assertIsInstance(core, runtime_context_mod.CoreServices)`
  - `self.assertEqual(core.context_compaction.compaction_trigger_segments, 3)`
  - `self.assertEqual(core.context_compaction.compaction_keep_recent_segments, 1)`

### test_core_services_skips_dialogue_store_and_compaction_when_disabled
Summary: Tests core services skips dialogue store and compaction when disabled
Asserts:
  - `self.assertIsNone(core.dialogue_store)`
  - `self.assertIsNone(core.context_compaction)`
  - `self.assertIsNone(core.db_service.dialogue_store)`
  - `self.assertIsNone(core.db_service.context_compaction)`

### test_runtime_exports_context_compaction_and_prefix_cache_can_be_disabled
Summary: Tests runtime exports context compaction and prefix cache can be disabled
Asserts:
  - `self.assertIs(runtime.context_compaction, marker)`
  - `self.assertIs(exported["context_compaction"], marker)`
  - `self.assertEqual(engine.get_last_prefix_hash("chat-1"), "")`
  - `self.assertEqual(status["prefix_changed_reason"], "disabled")`
  - `self.assertFalse(status["prefix_stable"])`
  - `self.assertEqual(status["frozen_prefix_length"], 0)`
  - `self.assertEqual(status["semi_stable_length"], 0)`
  - `self.assertEqual(status["frozen_prefix_blocks"], {})`
  - `self.assertEqual(status["semi_stable_blocks"], {})`

### test_bootstrap_build_attaches_chat_loop_kernel
Summary: Tests bootstrap build attaches chat loop kernel
Asserts:
  - `self.assertEqual(runtime.chat_loop_kernel, "kernel-marker")`

### test_runtime_diagnostics_include_chat_loop_status
Summary: Tests runtime diagnostics include chat loop status
Asserts:
  - `self.assertTrue(diagnostics["components"]["chat_loop_kernel"])`
  - `self.assertEqual(diagnostics["chat_loop"]["tracked_chats"], 2)`

### test_bootstrap_uses_new_compaction_defaults_when_config_values_are_absent
Summary: Tests bootstrap uses new compaction defaults when config values are absent
Asserts:
  - `self.assertEqual(core.context_compaction.compaction_trigger_segments, 40)`
  - `self.assertEqual(core.context_compaction.compaction_keep_recent_segments, 16)`

### test_recent_turn_traces_can_read_persisted_samples
Summary: Tests recent turn traces can read persisted samples
Asserts:
  - `self.assertEqual(result["total"], 1)`
  - `self.assertEqual(result["items"][0]["conversation_compression"]["warm_summary_preview"], "summary")`

### test_chat_trace_events_prefers_raw_trace_store
Summary: Tests chat trace events prefers raw trace store
Asserts:
  - `self.assertEqual(result["total"], 1)`
  - `self.assertEqual(result["items"][0]["stage"], "execution.executor.model_failure")`
  - `self.assertEqual(result["items"][0]["failure_evidence"]["failure_kind"], "provider_failure_text")`

### test_trace_stores_write_json_atomically
Summary: Tests trace stores write json atomically
Asserts:
  - `self.assertEqual(raw_payload["by_chat"]["chat-1"][0]["stage"], "raw")`
  - `self.assertEqual(turn_payload["by_chat"]["chat-1"][0]["stage"], "turn")`

### test_planner_trace_runtime_reads_store_fields_without_threadsafe_bridge
Summary: Tests planner trace runtime reads store fields without threadsafe bridge
Asserts:
  - `self.assertEqual(turn_context.attention.cold_summary_preview, "cold summary here")`
  - `self.assertEqual(turn_context.continuity.dialogue_store_version, "segments:7")`
  - `self.assertEqual(turn_context.continuity.compaction_status, "DEFERRED_FOR_STABILITY")`
  - `self.assertEqual(turn_context.continuity.compaction_eligibility_reason, "focus_tail_overlap")`
  - `self.assertTrue(turn_context.continuity.focus_tail_overlap)`
  - `self.assertEqual(turn_context.attention.warm_summary_preview, "warm summary")`
  - `self.assertEqual(turn_context.attention.warm_quotes_preview, "warm quotes")`
  - `self.assertIn("topic:", turn_context.attention.warm_topics_preview)`
  - `self.assertEqual(turn_context.attention.recent_transcript_preview, "recent fallback line")`
  - `self.assertEqual(turn_context.attention.reply_prompt_focus_anchor, "keep exact mainline")`
  - `self.assertEqual(turn_context.continuity.cold_summary_section_counts["topics"], 1)`
  - `self.assertEqual(turn_context.continuity.message_count_since_last_compaction, 96)`
  - `self.assertEqual(turn_context.continuity.next_eval_at_count, 100)`
  - `self.assertEqual(turn_context.continuity.final_score, 71.0)`
  - `self.assertFalse(turn_context.continuity.is_safe_to_compact)`
  - `self.assertIn("answered_unconfirmed", turn_context.continuity.closure_signals)`
  - `self.assertEqual(turn_context.continuity.forced_pending_message_delta, 4)`
  - `self.assertEqual(turn_context.continuity.last_safe_window_seen_at_count, 90)`
  - `self.assertEqual(turn_context.continuity.post_compaction_recovery_rounds, 2)`
  - `self.assertEqual(turn_context.continuity.evaluation_count, 90)`
  - `self.assertEqual(turn_context.continuity.current_message_count, 96)`
  - `self.assertEqual(turn_context.continuity.queued_eval_node, 90)`
  - `self.assertEqual(turn_context.continuity.pending_eval_nodes_count, 2)`
  - `self.assertEqual(turn_context.continuity.pending_eval_nodes, [100, 110])`
  - `self.assertTrue(turn_context.continuity.force_execute_on_next_safe_hook)`
  - `self.assertEqual(turn_context.continuity.safe_hook_block_reason, "forced_waiting_for_safe_hook")`
  - `self.assertEqual(turn_context.continuity.last_hook_source, "assistant")`
  - `self.assertEqual(turn_context.continuity.last_safe_hook_checked_at, 96)`
  - `self.assertEqual(turn_context.continuity.prefix_hash, "abc123")`
  - `self.assertEqual(turn_context.continuity.semantic_system_hash, "semantic123")`
  - `self.assertEqual(turn_context.continuity.semantic_system_length, 240)`
  - `self.assertTrue(turn_context.continuity.prefix_stable)`

### test_update_turn_trace_runtime_captures_request_trace_fields
Summary: Tests update turn trace runtime captures request trace fields
Asserts:
  - `self.assertEqual(turn_context.continuity.semantic_system_hash, "semantic8888")`
  - `self.assertEqual(turn_context.continuity.semantic_system_length, 260)`
  - `self.assertEqual(turn_context.continuity.gateway_system_hash, "gateway1111")`
  - `self.assertEqual(turn_context.continuity.gateway_prompt_hash, "gateway2222")`
  - `self.assertEqual(turn_context.continuity.provider_visible_system_hash, "syshash1111")`
  - `self.assertEqual(turn_context.continuity.provider_visible_prompt_hash, "prompthash2222")`
  - `self.assertEqual(turn_context.continuity.post_hook_system_hash, "posthook3333")`
  - `self.assertEqual(turn_context.continuity.request_session_id, "session-abc")`
  - `self.assertEqual(turn_context.continuity.request_cache_control, '{"type":"ephemeral"}')`
  - `self.assertEqual(turn_context.continuity.request_provider_family, "anthropic")`
  - `self.assertEqual(turn_context.continuity.request_model_id, "claude-3-5-sonnet")`
  - `self.assertEqual(turn_context.continuity.usage_input_tokens, 900)`
  - `self.assertEqual(turn_context.continuity.usage_input_cached, 700)`
  - `self.assertEqual(turn_context.continuity.usage_output_tokens, 80)`
  - `self.assertTrue(turn_context.continuity.cache_ready)`
  - `self.assertTrue(turn_context.continuity.cache_hit)`
  - `self.assertTrue(turn_context.continuity.cache_hit_evidence_supported)`
  - `self.assertIn("explicit_cache_hint", turn_context.continuity.cache_ready_reasons)`
  - `self.assertIn("session_reuse", turn_context.continuity.cache_ready_reasons)`
  - `self.assertIn("cache_affinity_enabled", turn_context.continuity.cache_ready_reasons)`

### test_update_turn_trace_runtime_marks_provider_visible_hash_stable_against_previous_turn
Summary: Tests update turn trace runtime marks provider visible hash stable against previous turn
Asserts:
  - `self.assertIn("provider_visible_hash_stable", turn_context.continuity.cache_ready_reasons)`

### test_recent_fallback_relaxes_during_post_compaction_recovery
Summary: Tests recent fallback relaxes during post compaction recovery
Asserts:
  - `self.assertTrue(include_recent)`
  - `self.assertEqual(reason, "post_compaction_recovery")`

### test_recent_fallback_keeps_tail_followup_context_for_direct_question
Summary: Tests recent fallback keeps tail followup context for direct question
Asserts:
  - `self.assertTrue(include_recent)`
  - `self.assertEqual(reason, "tail_followup_recent")`

### test_recent_fallback_skips_tail_recent_when_recent_tail_is_not_same_chain
Summary: Tests recent fallback skips tail recent when recent tail is not same chain
Asserts:
  - `self.assertFalse(include_recent)`
  - `self.assertEqual(reason, "warm_sufficient")`

### test_recent_fallback_keeps_recent_for_vision_mainline_question
Summary: Tests recent fallback keeps recent for vision mainline question
Asserts:
  - `self.assertTrue(include_recent)`
  - `self.assertEqual(reason, "vision_mainline_recent")`

## tests/test_planner_side_inputs_refactor.py (21 tests)

### test_mode_instructions_use_first_person_without_legacy_markers
Summary: Tests mode instructions use first person without legacy markers
Asserts:
  - `self.assertIsNone(result)`
  - `self.assertIn("对方这次是在让我帮忙办事。", envelope.planner_runtime_instruction_block)`
  - `self.assertIn("对方刚才说的是：“帮我查一下天气”。我这轮就先接住这一条来回。", envelope.planner_runtime_instruction_block)`
  - `self.assertIn("有人在喊我，我得马上用简短直接的话接住这次呼唤，不绕远路。", envelope.planner_runtime_instruction_block)`
  - `self.assertNotIn(">>>", envelope.planner_runtime_instruction_block)`
  - `self.assertNotIn("你现在的首要任务是", envelope.planner_runtime_instruction_block)`

### test_mode_instructions_write_into_runtime_instruction_block
Summary: Tests mode instructions write into runtime instruction block
Asserts:
  - `self.assertIn("对方这次是在让我帮忙办事。", envelope.planner_runtime_instruction_block)`
  - `self.assertIn("对方刚才说的是：“请直接说重点”。我这轮就先接住这一条来回。", envelope.planner_runtime_instruction_block)`
  - `self.assertIn("有人在喊我，我得马上用简短直接的话接住这次呼唤，不绕远路。", envelope.planner_runtime_instruction_block)`
  - `self.assertLessEqual(             len(envelope.planner_runtime_instruction_block),             self.side_inputs_mod.PlannerSideInputMixin.PLANNER_RUNTIME_INSTRUCTION_MAX_CHARS,         )`

### test_private_jump_context_uses_first_person_memory_recall
Summary: Tests private jump context uses first person memory recall
Asserts:
  - `self.assertIsNone(result)`
  - `self.assertIn("刚才我还在群聊里和大家说话", envelope.planner_runtime_instruction_block)`
  - `self.assertIn("【我刚才的悄悄话】：晚上再接着聊呀", envelope.planner_runtime_instruction_block)`
  - `self.assertIn("对方现在这句，多半就是接着我刚才那次跨界私聊在回我。", envelope.planner_runtime_instruction_block)`
  - `self.assertEqual(ctx.shared_dict["astrmai_space_jumps"], {})`

### test_mode_instructions_truncate_long_user_message
Summary: Tests mode instructions truncate long user message
Asserts:
  - `self.assertLessEqual(             len(envelope.planner_runtime_instruction_block),             self.side_inputs_mod.PlannerSideInputMixin.PLANNER_RUNTIME_INSTRUCTION_MAX_CHARS,         )`
  - `self.assertNotIn(long_message, envelope.planner_runtime_instruction_block)`

### test_private_jump_context_clamps_long_history_and_message
Summary: Tests private jump context clamps long history and message
Asserts:
  - `self.assertLessEqual(             len(envelope.planner_runtime_instruction_block),             self.side_inputs_mod.PlannerSideInputMixin.PLANNER_RUNTIME_INSTRUCTION_MAX_CHARS,         )`
  - `self.assertLessEqual(             envelope.planner_runtime_instruction_block.count("[群友]:") + envelope.planner_runtime_instruction_block.count("[你]:"),             self.side_inputs_mod.PlannerSideInputMixin.PRIVATE_JUMP_MAX_HISTORY_MESSAGES,      ...`

### test_action_modifier_energy_scale_and_query_fallbacks
Summary: Tests action modifier energy scale and query fallbacks
Asserts:
  - `self.assertEqual([tool.name for tool in normal], [tool.name for tool in tools])`
  - `self.assertEqual(             [tool.name for tool in exhausted],             ["wait_and_listen", "omni_perception_query", "self_lore_query"],         )`
  - `self.assertEqual(             [tool.name for tool in hostile],             ["wait_and_listen", "omni_perception_query", "self_lore_query"],         )`
  - `self.assertEqual(             [tool.name for tool in chat_exhausted],             ["message_reaction_action", "message_emoji_like_action", "proactive_like_action"],         )`
  - `self.assertEqual([tool.name for tool in chat_hostile], ["message_reaction_action", "message_emoji_like_action"])`
  - `self.assertEqual([tool.name for tool in cautious], ["message_reaction_action"])`
  - `self.assertEqual([tool.name for tool in cooldown], ["message_reaction_action", "message_emoji_like_action"])`
  - `self.assertEqual([tool.name for tool in sharp_cooldown], ["message_reaction_action", "message_emoji_like_action"])`
  - `self.assertEqual([tool.name for tool in long_reply_cooldown], ["omni_perception_query"])`
  - `self.assertEqual([tool.name for tool in traced], ["message_reaction_action", "message_emoji_like_action", "proactive_like_action"])`
  - `self.assertIn("energy_exhausted(0.05)", trace.filter_reasons)`
  - `self.assertEqual(trace.filter_steps[0]["stage"], "action_modifier.energy")`
  - `self.assertEqual(trace.filter_steps[0]["category"], "energy")`
  - `self.assertEqual(trace.removed_by_energy, ["proactive_meme"])`
  - `self.assertEqual(mood_trace.removed_by_mood, ["proactive_meme"])`
  - `self.assertIn("proactive_meme", hostile_trace.removed_by_hostility)`
  - `self.assertEqual(caution_trace.removed_by_caution, ["proactive_poke", "construct_at_event"])`
  - `self.assertEqual([tool.name for tool in returned_tools], ["message_reaction_action", "message_emoji_like_action"])`
  - `self.assertIs(returned_trace, cooldown_trace)`
  - `self.assertEqual(set(cooldown_trace.removed_by_cooldown), {"proactive_meme", "proactive_like_action"})`

### test_guarded_stance_filters_proactive_social_tools_and_records_trace
Summary: Tests guarded stance filters proactive social tools and records trace
Asserts:
  - `self.assertEqual(             [tool.name for tool in filtered],             ["message_reaction_action", "omni_perception_query"],         )`
  - `self.assertEqual(             trace.removed_by_stance,             ["proactive_meme", "proactive_poke", "construct_at_event"],         )`
  - `self.assertTrue(any(step["stage"] == "action_modifier.stance" for step in trace.filter_steps))`
  - `self.assertIn("stance_guarded_guard", trace.filter_reasons)`

### test_guarded_stance_records_removed_tools_in_dict_trace_fallback
Summary: Tests guarded stance records removed tools in dict trace fallback
Asserts:
  - `self.assertEqual([tool.name for tool in filtered], ["message_reaction_action"])`
  - `self.assertEqual(trace["removed_by_stance"], ["proactive_meme", "proactive_poke"])`
  - `self.assertEqual(trace["filter_steps"][0]["category"], "stance")`

### test_low_trust_filters_real_intrusive_tools
Summary: Tests low trust filters real intrusive tools
Asserts:
  - `self.assertEqual(             [tool.name for tool in filtered],             ["message_reaction_action", "message_emoji_like_action"],         )`
  - `self.assertIn("low_trust(-20)", trace["filter_reasons"])`
  - `self.assertIn("proactive_poke", trace["removed_by_hostility"])`

### test_guarded_stance_scales_follow_up_probability
Summary: Tests guarded stance scales follow up probability
Asserts:
  - `self.assertIsNone(result)`
  - `self.assertNotIn("called", calls)`
  - `self.assertIn("stance_guarded", trace.signals)`
  - `self.assertIn("follow_up_probability_scaled:0.35", trace.signals)`
  - `self.assertEqual(trace.skipped_reason, "probability_gate")`

### test_all_mode_plain_chat_loads_chat_tier_and_tool_intent_loads_full_pfc_tools
Summary: Tests all mode plain chat loads chat tier and tool intent loads full pfc tools
Asserts:
  - `self.assertEqual(             _normalized_tool_names(plain_tools),             {"proactive_meme", "message_reaction_action", "message_emoji_like_action", "proactive_like_action"},         )`
  - `self.assertEqual(plain_event.get_extra("astrmai_tool_tier"), "chat")`
  - `self.assertEqual(plain_turn.tools.final_tier, "chat")`
  - `self.assertEqual(plain_turn.tools.requested_tier, "")`
  - `self.assertFalse(plain_turn.tools.explicit_tool_intent)`
  - `self.assertEqual(             set(plain_turn.tools.available_tools),             {"proactive_meme", "message_reaction_action", "message_emoji_like_action", "proactive_like_action"},         )`
  - `self.assertEqual(             set(plain_turn.tools.filtered_tools),             {"proactive_meme", "message_reaction_action", "message_emoji_like_action", "proactive_like_action"},         )`
  - `self.assertTrue(ctx.shared_dict["disable_rag_injection"])`
  - `self.assertIsNotNone(intent_tools)`
  - `self.assertIn("omni_perception_query", intent_names)`
  - `self.assertIn("self_lore_query", intent_names)`
  - `self.assertIn("custom_face_catalog_query", intent_names)`
  - `self.assertIn("group_sign_action", intent_names)`
  - `self.assertEqual(intent_event.get_extra("astrmai_tool_tier"), "full")`
  - `self.assertEqual(intent_event.get_extra("astrmai_turn_context").tools.final_tier, "full")`
  - `self.assertTrue(intent_event.get_extra("astrmai_turn_context").tools.explicit_tool_intent)`
  - `self.assertTrue(tool_ctx.shared_dict["disable_rag_injection"])`
  - `self.assertIn("omni_perception_query", english_names)`
  - `self.assertIn("regret_and_withdraw_action", english_names)`
  - `self.assertEqual(english_event.get_extra("astrmai_tool_tier"), "full")`
  - `self.assertTrue(english_event.get_extra("astrmai_turn_context").tools.explicit_tool_intent)`

### test_low_energy_downgrades_requested_full_tier_without_explicit_tool_intent
Summary: Tests low energy downgrades requested full tier without explicit tool intent
Asserts:
  - `self.assertEqual(event.get_extra("astrmai_tool_tier"), "chat")`
  - `self.assertEqual(             _normalized_tool_names(tools),             {"proactive_meme", "message_reaction_action", "message_emoji_like_action", "proactive_like_action"},         )`
  - `self.assertEqual(trace.requested_tier, "full")`
  - `self.assertEqual(trace.final_tier, "chat")`
  - `self.assertIn("full_tier", trace.removed_by_energy)`
  - `self.assertTrue(any(step["stage"] == "planner.tier_state_guard" for step in trace.filter_steps))`

### test_explicit_tool_intent_bypasses_low_energy_full_tier_downgrade
Summary: Tests explicit tool intent bypasses low energy full tier downgrade
Asserts:
  - `self.assertEqual(event.get_extra("astrmai_tool_tier"), "full")`
  - `self.assertIn("omni_perception_query", _normalized_tool_names(tools))`
  - `self.assertEqual(trace.final_tier, "full")`
  - `self.assertNotIn("full_tier", trace.removed_by_energy)`

### test_guarded_chat_intent_adds_guarded_chat_tools_without_full_pfc
Summary: Tests guarded chat intent adds guarded chat tools without full pfc
Asserts:
  - `self.assertIn("proactive_poke", tool_names)`
  - `self.assertIn("construct_at_event", tool_names)`
  - `self.assertNotIn("omni_perception_query", tool_names)`
  - `self.assertNotIn("wait_and_listen", tool_names)`
  - `self.assertEqual(event.get_extra("astrmai_tool_tier"), "chat")`
  - `self.assertIn("proactive_poke", at_tool_names)`
  - `self.assertIn("construct_at_event", at_tool_names)`

### test_guarded_chat_intent_does_not_match_unrelated_poke_words
Summary: Tests guarded chat intent does not match unrelated poke words
Asserts:
  - `self.assertNotIn("proactive_poke", tool_names)`
  - `self.assertNotIn("construct_at_event", tool_names)`
  - `self.assertEqual(                     tool_names,                     {"proactive_meme", "message_reaction_action", "message_emoji_like_action", "proactive_like_action"},                 )`

### test_core_only_plain_chat_uses_chat_tier_instead_of_full_pfc
Summary: Tests core only plain chat uses chat tier instead of full pfc
Asserts:
  - `self.assertEqual(             _normalized_tool_names(tools),             {"proactive_meme", "message_reaction_action", "message_emoji_like_action", "proactive_like_action"},         )`
  - `self.assertEqual(event.get_extra("astrmai_tool_tier"), "chat")`
  - `self.assertTrue(ctx.shared_dict["disable_rag_injection"])`

### test_agency_tier_none_and_social_intent_constrain_tools
Summary: Tests agency tier none and social intent constrain tools
Asserts:
  - `self.assertEqual(none_tools, [])`
  - `self.assertEqual(none_event.get_extra("astrmai_tool_tier"), "none")`
  - `self.assertEqual(             _normalized_tool_names(comfort_tools),             {"message_reaction_action", "message_emoji_like_action", "proactive_like_action"},         )`
  - `self.assertEqual(             _normalized_tool_names(recall_tools),             {"omni_perception_query", "self_lore_query", "custom_face_catalog_query"},         )`
  - `self.assertEqual(recall_turn.tools.allowed_families, ["query"])`
  - `self.assertIn("allowed_families(query)", recall_turn.tools.filter_reasons)`
  - `self.assertIn("proactive_meme", recall_turn.tools.removed_by_social_intent)`

### test_pushback_intent_does_not_expose_chat_tools_by_default
Summary: Tests pushback intent does not expose chat tools by default
Asserts:
  - `self.assertEqual(tools, [])`
  - `self.assertEqual(event.get_extra("astrmai_tool_tier"), "none")`
  - `self.assertIn("social_intent(pushback)_forces_none", pushback_trace.filter_reasons)`
  - `self.assertIn("requested_tier_none", pushback_trace.filter_reasons)`

### test_sys3_tool_call_mode_bypasses_all_mode_keyword_gate
Summary: Tests sys3 tool call mode bypasses all mode keyword gate
Asserts:
  - `self.assertIsNotNone(tools)`
  - `self.assertIn("sys3_light_tool", [tool.name for tool in tools])`
  - `self.assertTrue(ctx.shared_dict["disable_rag_injection"])`

### test_sys3_tool_call_mode_sets_sys3_tier
Summary: Tests sys3 tool call mode sets sys3 tier
Asserts:
  - `self.assertIsNotNone(tools)`
  - `self.assertIn("sys3_light_tool", [tool.name for tool in tools])`
  - `self.assertEqual(event.get_extra("astrmai_tool_tier"), "sys3")`
  - `self.assertTrue(ctx.shared_dict["disable_rag_injection"])`

### test_load_planning_side_inputs_uses_async_evolution_manager_api
Summary: Tests load planning side inputs uses async evolution manager api
Asserts:
  - `self.assertEqual(result["slang_context"], "async slang context")`
  - `self.assertEqual(result["goal_text"], "goal context")`
  - `self.assertEqual(result["expression_habits"], "expression habits")`
  - `self.assertEqual(result["situational_style_cues"], "async slang context")`
  - `self.assertEqual(calls, [("async", "chat-1", 5)])`

## tests/unit/conversation/test_group_dialogue_store_and_compaction.py (30 tests)

### test_compaction_engine_exposes_split_helper_components
Summary: Tests compaction engine exposes split helper components
Asserts:
  - `self.assertTrue(hasattr(engine, "safety_analyzer"))`
  - `self.assertTrue(hasattr(engine, "window_selector"))`
  - `self.assertTrue(hasattr(engine, "compaction_executor"))`

### test_warm_transcript_and_cold_summary_lifecycle
Summary: Tests warm transcript and cold summary lifecycle
Asserts:
  - `self.assertIn("Alice: Hello there", warm)`
  - `self.assertIn("Bot: Hi", warm)`
  - `self.assertEqual((await store.snapshot_counts("chat-1"))["tokens"], 6)`
  - `self.assertEqual(await store.get_cold_summary("chat-1"), "summary line")`

### test_warm_context_bundle_keeps_structure_and_excludes_cold_summary
Summary: Tests warm context bundle keeps structure and excludes cold summary
Asserts:
  - `self.assertNotIn("older summary", warm)`
  - `self.assertIn("Alice", warm)`
  - `self.assertIn("Bob", warm)`
  - `self.assertTrue(bundle.topic_preview)`

### test_warm_summary_uses_topic_units_instead_of_simple_counts
Summary: Tests warm summary uses topic units instead of simple counts
Asserts:
  - `self.assertTrue(bundle.summary_text)`
  - `self.assertNotIn("Alice:", bundle.summary_text)`
  - `self.assertNotIn("Bot:", bundle.summary_text)`
  - `self.assertIn("topic:", bundle.topic_preview)`
  - `self.assertIn("event:", bundle.topic_preview)`

### test_warm_summary_keeps_bot_directed_mainline_over_later_smalltalk
Summary: Tests warm summary keeps bot directed mainline over later smalltalk
Asserts:
  - `self.assertIn("still keep", bundle.summary_text)`
  - `self.assertNotIn("background smalltalk 5", bundle.summary_text)`
  - `self.assertIn("recent fallback", bundle.quote_text)`

### test_warm_quotes_keep_latest_direct_question_when_visual_context_is_present
Summary: Tests warm quotes keep ladirect question when visual context is present
Asserts:
  - `self.assertIn("keep the compaction mainline", bundle.quote_text)`
  - `self.assertIn("screenshot", bundle.quote_text.lower())`

### test_warm_quotes_do_not_promote_plain_group_question_over_bot_directed_question
Summary: Tests warm quotes do not promote plain group question over bot directed question
Asserts:
  - `self.assertIn("compaction mainline", bundle.quote_text)`
  - `self.assertNotIn("order lunch", bundle.quote_text.lower())`

### test_compaction_not_ready_before_80_messages
Summary: Tests compaction not ready before 80 messages
Asserts:
  - `self.assertFalse(result.triggered)`
  - `self.assertEqual(result.state, "NOT_READY")`
  - `self.assertEqual(result.message_count_since_last_compaction, 79)`

### test_compaction_waits_for_next_node_at_80_when_score_not_enough
Summary: Tests compaction waits for next node at 80 when score not enough
Asserts:
  - `self.assertFalse(result.triggered)`
  - `self.assertEqual(result.state, "WAIT_NEXT_NODE")`
  - `self.assertEqual(result.next_eval_at_count, 90)`

### test_pending_task_still_queues_crossed_eval_nodes
Summary: Tests pending task still queues crossed eval nodes
Asserts:
  - `self.assertEqual(queued.skipped_reason, "evaluation_already_scheduled")`
  - `self.assertEqual(engine._state_for_chat("chat-1")["pending_eval_nodes"], [80])`
  - `self.assertEqual(result.evaluation_count, 80)`
  - `self.assertEqual(result.current_message_count, 80)`
  - `self.assertEqual(result.state, "WAIT_NEXT_NODE")`

### test_multiple_crossed_eval_nodes_are_preserved_in_queue
Summary: Tests multiple crossed eval nodes are preserved in queue
Asserts:
  - `self.assertEqual(engine._state_for_chat("chat-1")["pending_eval_nodes"], [80, 90, 100])`
  - `self.assertEqual(queued.skipped_reason, "evaluation_already_scheduled")`

### test_compaction_forced_at_120_when_safe
Summary: Tests compaction forced at 120 when safe
Asserts:
  - `self.assertTrue(result.triggered)`
  - `self.assertEqual(result.state, "COOLDOWN")`
  - `self.assertEqual(result.message_count_since_last_compaction, 0)`

### test_compaction_enters_forced_pending_when_120_but_chain_active
Summary: Tests compaction enters forced pending when 120 but chain active
Asserts:
  - `self.assertFalse(result.triggered)`
  - `self.assertEqual(result.state, "FORCED_PENDING")`
  - `self.assertEqual(result.reason, "awaiting_followup_chain")`

### test_compaction_skips_when_focus_thread_overlaps_old_zone_tail
Summary: Tests compaction skips when focus thread overlaps old zone tail
Asserts:
  - `self.assertFalse(result.triggered)`
  - `self.assertEqual(result.state, "FORCED_PENDING")`
  - `self.assertEqual(result.reason, "focus_tail_overlap")`
  - `self.assertTrue(result.focus_tail_overlap)`

### test_compaction_prefers_provider_summary_when_available
Summary: Tests compaction prefers provider summary when available
Asserts:
  - `self.assertTrue(result.triggered)`
  - `self.assertIn("[topics]", result.summary)`
  - `self.assertEqual(fake_context.kwargs["chat_provider_id"], "chat-provider")`

### test_compaction_falls_back_to_rule_summary_when_provider_fails
Summary: Tests compaction falls back to rule summary when provider fails
Asserts:
  - `self.assertTrue(result.triggered)`
  - `self.assertIn("[topics]", result.summary)`

### test_compaction_provider_kwargs_use_dedicated_lane_and_reuse_session
Summary: Tests compaction provider kwargs use dedicated lane and reuse session
Asserts:
  - `self.assertIn("session_id", kwargs1)`
  - `self.assertEqual(kwargs1["session_id"], kwargs2["session_id"])`
  - `self.assertIn("@@astrmai:bg:compaction:", str(kwargs1["session_id"]))`
  - `self.assertTrue(str(kwargs1["session_id"]).endswith("compaction_summary_v2:v2:section_summary"))`
  - `self.assertEqual(trace1["lane_scope_id"], "chat-1")`
  - `self.assertEqual(trace1["template_id"], "compaction_summary_v2")`
  - `self.assertEqual(trace1["template_version"], "v2")`
  - `self.assertEqual(template_stats["provider_session_usage_rate"], 1.0)`
  - `self.assertEqual(template_stats["provider_session_reuse_rate"], 0.5)`

### test_compaction_provider_kwargs_tolerate_missing_policy_and_keep_trace_recordable
Summary: Tests compaction provider kwargs tolerate missing policy and keep trace recordable
Asserts:
  - `self.assertEqual(kwargs, {})`
  - `self.assertNotIn("session_id", kwargs)`
  - `self.assertNotIn("cache_control", kwargs)`
  - `self.assertEqual(trace["workload_family"], "compaction_summary")`
  - `self.assertEqual(trace["template_id"], "compaction_summary_v2")`
  - `self.assertEqual(trace["template_version"], "v2")`
  - `self.assertEqual(trace["schema_id"], "section_summary")`
  - `self.assertFalse(trace["provider_session_enabled"])`
  - `self.assertFalse(trace["provider_cache_hint_enabled"])`
  - `self.assertEqual(template_stats["call_count"], 1)`
  - `self.assertEqual(template_stats["provider_session_usage_rate"], 0.0)`

### test_compaction_summary_paths_keep_recording_trace_when_policy_resolution_is_missing
Summary: Tests compaction summary paths keep recording trace when policy resolution is missing
Asserts:
  - `self.assertEqual(summary_v1, "summary text")`
  - `self.assertEqual(summary_v2, "summary text")`
  - `self.assertEqual(snapshot["compaction_summary"]["call_count"], 2)`
  - `self.assertEqual(snapshot["_templates"]["compaction_summary_v1@v1"]["call_count"], 1)`
  - `self.assertEqual(snapshot["_templates"]["compaction_summary_v2@v2"]["call_count"], 1)`
  - `self.assertEqual(snapshot["_templates"]["compaction_summary_v1@v1"]["provider_session_usage_rate"], 0.0)`
  - `self.assertEqual(snapshot["_templates"]["compaction_summary_v2@v2"]["provider_session_usage_rate"], 0.0)`

### test_compaction_persists_structured_cold_summary
Summary: Tests compaction persists structured cold summary
Asserts:
  - `self.assertTrue(result.triggered)`
  - `self.assertIsNotNone(structure)`
  - `self.assertGreaterEqual(counts["topics"], 1)`
  - `self.assertGreaterEqual(counts["open_items"], 1)`

### test_cold_merge_closes_open_item_when_decision_resolves_it
Summary: Tests cold merge closes open item when decision resolves it
Asserts:
  - `self.assertEqual([unit.text for unit in merged.open_items], [])`
  - `self.assertEqual(len(merged.decisions), 1)`

### test_compaction_save_failure_enters_cooldown_without_losing_segments
Summary: Tests compaction save failure enters cooldown without losing segments
Asserts:
  - `self.assertEqual(first.skipped_reason, "summary_save_failed")`
  - `self.assertEqual(after_first["segments"], before["segments"])`
  - `self.assertEqual(second.skipped_reason, "cooldown")`

### test_compaction_summary_empty_keeps_segments
Summary: Tests compaction summary empty keeps segments
Asserts:
  - `self.assertEqual(result.skipped_reason, "summary_empty")`
  - `self.assertEqual(after["segments"], before["segments"])`

### test_warm_transcript_prefers_recent_messages_under_token_budget
Summary: Tests warm transcript prefers recent messages under token budget
Asserts:
  - `self.assertNotIn("1111", bundle.quote_text)`
  - `self.assertIn("2222", bundle.quote_text)`
  - `self.assertIn("3333", bundle.quote_text)`

### test_forced_pending_compacts_after_20_more_messages
Summary: Tests forced pending compacts after 20 more messages
Asserts:
  - `self.assertFalse(first.triggered)`
  - `self.assertEqual(first.state, "FORCED_PENDING")`
  - `self.assertFalse(second.triggered)`
  - `self.assertEqual(second.state, "FORCED_PENDING")`
  - `self.assertTrue(second.force_execute_on_next_safe_hook)`
  - `self.assertEqual(second.reason, "forced_waiting_for_safe_hook")`
  - `self.assertTrue(third.triggered)`
  - `self.assertEqual(third.state, "COOLDOWN")`

### test_deferred_state_can_compact_when_safe_window_reopens_before_next_node
Summary: Tests deferred state can compact when safe window reopens before next node
Asserts:
  - `self.assertTrue(result.triggered)`
  - `self.assertEqual(result.state, "COOLDOWN")`
  - `self.assertGreaterEqual(result.last_safe_window_seen_at_count, 101)`

### test_trace_status_exposes_signal_buckets_and_recovery_rounds
Summary: Tests trace status exposes signal buckets and recovery rounds
Asserts:
  - `self.assertEqual(result.state, "WAIT_NEXT_NODE")`
  - `self.assertIn("closure_signals", trace)`
  - `self.assertIn("tail_activity_signals", trace)`
  - `self.assertIn("topic_density_signals", trace)`
  - `self.assertIn("stability_signals", trace)`
  - `self.assertIn("benefit_signals", trace)`
  - `self.assertIn("evaluation_count", trace)`
  - `self.assertIn("current_message_count", trace)`
  - `self.assertIn("pending_eval_nodes", trace)`
  - `self.assertIn("force_execute_on_next_safe_hook", trace)`
  - `self.assertEqual(trace["post_compaction_recovery_rounds"], 0)`

### test_post_compaction_recovery_rounds_decrease_on_user_messages
Summary: Tests post compaction recovery rounds decrease on user messages
Asserts:
  - `self.assertTrue(result.triggered)`
  - `self.assertEqual(result.post_compaction_recovery_rounds, 2)`
  - `self.assertEqual(trace["post_compaction_recovery_rounds"], 1)`

### test_trace_status_does_not_mutate_compaction_state
Summary: Tests trace status does not mutate compaction state
Asserts:
  - `self.assertTrue(first.triggered)`
  - `self.assertEqual(engine._state_for_chat("chat-1")["last_state"], "COOLDOWN")`
  - `self.assertEqual(trace_status["state"], "COOLDOWN")`
  - `self.assertEqual(engine._state_for_chat("chat-1")["last_state"], "COOLDOWN")`

### test_compaction_executor_delegates_without_losing_focus_context
Summary: Tests compaction executor delegates without losing focus context
Asserts:
  - `self.assertTrue(result.triggered)`
  - `self.assertEqual(result.state, "COOLDOWN")`
  - `self.assertIs(trace["focus_context"], focus_context)`
  - `self.assertEqual(             engine.calls,             [                 ("compact", "chat-1", focus_context),                 ("trace", "chat-1", focus_context),             ],         )`

## tests/test_heartflow_refactor.py (20 tests)

### test_heartflow_tick_ignores_empty_runtime
Summary: Tests heartflow tick ignores empty runtime
Asserts:
  - `self.assertEqual(status["active_chats"], 0)`
  - `self.assertEqual(status["pending_pulses"], 0)`

### test_heartflow_builds_state_for_active_chat
Summary: Tests heartflow builds state for active chat
Asserts:
  - `self.assertIsNotNone(state)`
  - `self.assertGreater(state.interest, 0.70)`
  - `self.assertGreater(state.engagement, 0.50)`

### test_no_reply_counter_decays_after_threshold
Summary: Tests no reply counter decays after threshold
Asserts:
  - `self.assertEqual(manager.get_session("chat-1").consecutive_no_reply_count, 2)`

### test_no_reply_counter_decays_on_non_reply_actions
Summary: Tests no reply counter decays on non reply actions
Asserts:
  - `self.assertEqual(manager.get_session("chat-1").consecutive_no_reply_count, 0)`

### test_heartflow_creates_session_and_records_hidden_action
Summary: Tests heartflow creates session and records hidden action
Asserts:
  - `self.assertIsNotNone(session)`
  - `self.assertIsNotNone(action)`
  - `self.assertEqual(session.tick_count, 1)`
  - `self.assertGreater(session.topic_heat, 0.0)`
  - `self.assertIn(action.action_type, {"observe", "prepare_reply"})`
  - `self.assertIn("session=", hidden)`
  - `self.assertIn("latest_heartflow_action=", hidden)`

### test_heartflow_low_cost_session_does_not_dispatch_candidate
Summary: Tests heartflow low cost session does not dispatch candidate
Asserts:
  - `self.assertTrue(session.low_cost_retained)`
  - `self.assertIn(action.action_type, {"observe", "complete_topic"})`
  - `self.assertFalse(action.should_dispatch_candidate)`
  - `self.assertEqual(len(dispatcher.intents), 0)`

### test_heartflow_low_energy_prefers_observe
Summary: Tests heartflow low energy prefers observe
Asserts:
  - `self.assertLess(state.talk_willingness, 0.25)`
  - `self.assertEqual(state.recent_impulse, "observe")`
  - `self.assertEqual(pulse.suggested_action_tier, "none")`
  - `self.assertIsNotNone(decision)`
  - `self.assertTrue(decision.hidden_only)`
  - `self.assertFalse(decision.visible_candidate_allowed)`
  - `self.assertEqual(decision.blocked_reason, "cool_down")`
  - `self.assertIn("low_talk", manager.get_hidden_context("chat-1"))`

### test_heartflow_wait_action_blocks_dispatch
Summary: Tests heartflow wait action blocks dispatch
Asserts:
  - `self.assertEqual(action.action_type, "wait")`
  - `self.assertFalse(decision.visible_candidate_allowed)`
  - `self.assertEqual(decision.blocked_reason, "user_waiting")`

### test_heartflow_proactive_hint_can_become_visible_candidate_but_not_dispatch
Summary: Tests heartflow proactive hint can become visible candidate but not dispatch
Asserts:
  - `self.assertTrue(decision.visible_candidate_allowed)`
  - `self.assertTrue(decision.requires_synthetic_event)`
  - `self.assertFalse(decision.hidden_only)`
  - `self.assertFalse(decision.dispatch_enabled)`
  - `self.assertFalse(decision.synthetic_event_queued)`
  - `self.assertGreaterEqual(decision.safety_checks["visible_candidate_score"], 0.72)`
  - `self.assertIn("Heartflow proactive_hint", decision.synthetic_event_preview)`

### test_heartflow_quiet_hours_blocks_visible_candidate
Summary: Tests heartflow quiet hours blocks visible candidate
Asserts:
  - `self.assertFalse(decision.visible_candidate_allowed)`
  - `self.assertEqual(decision.blocked_reason, "quiet_hours")`
  - `self.assertTrue(decision.safety_checks["quiet_hours"])`

### test_heartflow_base_frequency_adjusts_candidate_threshold
Summary: Tests heartflow base frequency adjusts candidate threshold
Asserts:
  - `self.assertGreater(decision.safety_checks["base_frequency_factor"], 1.0)`
  - `self.assertGreater(decision.safety_checks["visible_candidate_threshold"], 0.72)`
  - `self.assertEqual(             decision.safety_checks["topic_source_priority"],             ["conversation_continuity", "recent_memory", "fresh_small_talk"],         )`

### test_heartflow_visible_candidate_dispatches_through_dispatcher
Summary: Tests heartflow visible candidate dispatches through dispatcher
Asserts:
  - `self.assertEqual(len(dispatcher.intents), 1)`
  - `self.assertEqual(dispatcher.intents[0].source, "heartflow")`
  - `self.assertTrue(decision.dispatch_enabled)`
  - `self.assertTrue(decision.synthetic_event_queued)`
  - `self.assertFalse(decision.hidden_only)`
  - `self.assertEqual(decision.safety_checks["dispatch_intent_id"], "intent-1")`

### test_heartflow_visible_candidate_guidance_uses_topic_then_memory
Summary: Tests heartflow visible candidate guidance uses topic then memory
Asserts:
  - `self.assertIn("Topic source: conversation_continuity", guidance)`
  - `self.assertIn("Current chat clue: talking about exam plans", guidance)`
  - `self.assertIn("Optional private memory hint", guidance)`
  - `self.assertNotIn("threshold", guidance.lower())`

### test_heartflow_impulse_blocks_when_user_is_waiting_or_cooldown_hits
Summary: Tests heartflow impulse blocks when user is waiting or cooldown hits
Asserts:
  - `self.assertFalse(waiting.visible_candidate_allowed)`
  - `self.assertEqual(waiting.blocked_reason, "user_waiting")`
  - `self.assertFalse(cooldown.visible_candidate_allowed)`
  - `self.assertEqual(cooldown.blocked_reason, "cooldown")`

### test_heartflow_feedback_bridge_flushes_after_six_pulses
Summary: Tests heartflow feedback bridge flushes after six pulses
Asserts:
  - `self.assertTrue(flushed)`
  - `self.assertEqual(len(memory.feedback_calls), 1)`
  - `self.assertEqual(memory.feedback_calls[0]["source"], "heartflow")`
  - `self.assertIn("main_impulse=join", memory.feedback_calls[0]["summary"])`
  - `self.assertIn("Join only", memory.feedback_calls[0]["guidance"])`

### test_heartflow_timeline_merges_pulse_action_and_impulse
Summary: Tests heartflow timeline merges pulse action and impulse
Asserts:
  - `self.assertEqual([item["kind"] for item in timeline[:3]], ["impulse_decision", "action", "pulse"])`
  - `self.assertEqual(timeline[0]["summary"], "hidden_impulse")`

### test_heartflow_topic_digest_service_writes_and_cools_down
Summary: Tests heartflow topic digest service writes and cools down
Asserts:
  - `self.assertEqual(memory.feedback_calls[0]["source"], "heartflow_topic_digest")`
  - `self.assertIn("talking about exam plans", memory.feedback_calls[0]["summary"])`
  - `self.assertIn("do not quote", memory.feedback_calls[0]["guidance"].lower())`
  - `self.assertEqual(history[0].status, "skipped")`
  - `self.assertEqual(history[0].skip_reason, "cooldown")`

### test_heartflow_tick_chat_cleans_stale_history_buckets
Summary: Tests heartflow tick chat cleans stale history buckets
Asserts:
  - `self.assertTrue(payload["performed"])`
  - `self.assertNotIn("stale-chat", manager._states)`
  - `self.assertNotIn("stale-chat", manager._pulses_by_chat)`
  - `self.assertNotIn("stale-chat", manager._action_decisions_by_chat)`
  - `self.assertNotIn("stale-chat", manager._impulse_decisions_by_chat)`
  - `self.assertIn("chat-1", manager._states)`
  - `self.assertEqual(status["active_chats"], 1)`
  - `self.assertEqual(status["pending_pulses"], 1)`

### test_heartflow_tick_batch_cleans_history_only_once_per_cycle
Summary: Tests heartflow tick batch cleans history only once per cycle
Asserts:
  - `self.assertEqual(cleanup_calls["sessions"], 1)`
  - `self.assertEqual(cleanup_calls["history"], 1)`
  - `self.assertIn("chat-1", manager._states)`
  - `self.assertIn("chat-2", manager._states)`

### test_cognitive_loop_reads_heartflow_hidden_context
Summary: Tests cognitive loop reads heartflow hidden context
Asserts:
  - `self.assertIsNotNone(decision)`
  - `self.assertIn("Heartflow state", gateway.calls[0]["prompt"])`
  - `self.assertIn("join carefully", gateway.calls[0]["prompt"])`

## tests/integration/host/test_host_mock_validation.py (5 tests)

### test_real_astrbot_host_can_register_refactor_plugin
Summary: Tests real astrbot host can register refactor plugin
Asserts:
  - `self.assertEqual(result.returncode, 0, msg=output)`
  - `self.assertIn("PLUGIN_FOUND=True", output)`

### test_message_entry_and_command_paths_work_with_mock_events
Summary: Tests message entry and command paths work with mock events
Asserts:
  - `self.assertEqual(ordinary_results, [])`
  - `self.assertEqual(at_results, [{"type": "plain", "text": "(ghost)"}])`
  - `self.assertEqual(reply_results, [{"type": "plain", "text": "(ghost)"}])`
  - `self.assertEqual(private_results, [{"type": "plain", "text": "(ghost)"}])`
  - `self.assertEqual(help_results, [{"type": "plain", "text": "mock-help"}])`
  - `self.assertEqual(work_results, [{"type": "plain", "text": "mock-work"}])`
  - `self.assertIn(("attention", "group"), calls)`
  - `self.assertIn(("attention", "at"), calls)`
  - `self.assertIn(("attention", "reply"), calls)`
  - `self.assertIn(("attention", "private"), calls)`

### test_main_entry_routes_into_real_plugin_facade_message_chain_smoke
Summary: Tests main entry routes into real plugin facade message chain smoke
Asserts:
  - `self.assertIsInstance(plugin.facade, main_mod.PluginFacade)`
  - `self.assertEqual(results, [{"type": "plain", "text": "(ghost)"}])`
  - `self.assertEqual(                 attention_calls,                 [                     ("record", "default:GroupMessage:group-1"),                     ("attention", "default:GroupMessage:group-1"),                 ],             )`
  - `self.assertEqual(activity_updates, ["user-1"])`
  - `self.assertIn("ingress.enter", trace_stages)`
  - `self.assertIn("ingress.after_attention", trace_stages)`

### test_mock_system2_pipeline_preserves_state_lane_executor_reply_followup_chain
Summary: Tests mock system2 pipeline preserves state lane executor reply followup chain
Asserts:
  - `self.assertTrue(reply_sent)`
  - `self.assertEqual(                 call_order,                 ["lock", "consume_energy", "ensure_lane", "planner", "executor", "reply_service", "wait_targets", "group_wait"],             )`
  - `self.assertEqual(runtime.reply_engine.replies, [(event.unified_msg_origin, "mock-reply")])`
  - `self.assertEqual(                 runtime.runtime_coordinator.wait_updates,                 [(event.unified_msg_origin, ["user-2"], "Bob")],             )`

### test_mock_multimodal_workmode_proactive_and_webui_minimal_smoke
Summary: Tests mock multimodal workmode proactive and webui minimal smoke
Asserts:
  - `self.assertIn("chat-1:pic-1", stored)`
  - `self.assertEqual(stored["chat-1:pic-1"].description, "mock vision")`
  - `self.assertTrue(running)`
  - `self.assertIn("dream_scheduler", proactive_status)`
  - `self.assertIn("transfer_to_cron", static_agents)`
  - `self.assertIn("transfer_to_computer", static_agents)`
  - `self.assertEqual(app.title, "AstrMai WebUI")`
  - `self.assertIn("/api/dashboard", route_paths)`
  - `self.assertIn("/api/users", route_paths)`
  - `self.assertIn("/api/memories/events", route_paths)`
  - `self.assertIn("/api/memories/nodes", route_paths)`
  - `self.assertIn("/api/reviews", route_paths)`

## tests/original_ported/test_prompt_refiner_focus_layout_ported.py (17 tests)

### test_focus_sections_precede_ambient_background
Summary: Tests focus sections precede ambient background
Asserts:
  - `self.assertEqual(final_system_prompt, "system prompt only")`
  - `self.assertLess(final_prompt.index("---前因---\nFocus block"), final_prompt.index("---旁边在聊的---\nBob: stay on topic"))`
  - `self.assertIn("---眼前正在对我说的---\n<user_input>\nAlice: why not?\n</user_input>", final_prompt)`
  - `self.assertIn("---补充---\nRelated\nAstrMai: no, that is not allowed", final_prompt)`
  - `self.assertIn("Carol: I am reading too", final_prompt)`

### test_transcript_dedup_keeps_semantic_context_sections
Summary: Tests transcript dedup keeps semantic context sections
Asserts:
  - `self.assertNotIn("Alice: why not?", transcript_block)`
  - `self.assertNotIn("Carol: longer duplicated clue", transcript_block)`
  - `self.assertIn("Bob: ok", transcript_block)`
  - `self.assertIn("Dave: separate transcript line", transcript_block)`
  - `self.assertIn("---前因---\nCarol: longer duplicated clue", final_prompt)`

### test_refiner_places_cognitive_drive_in_user_prompt
Summary: Tests refiner places cognitive drive in user prompt
Asserts:
  - `self.assertEqual(final_system_prompt, "system prompt only")`
  - `self.assertIn("---内在驱动---", final_prompt)`
  - `self.assertIn("agency posture: keep pushback restrained", final_prompt)`
  - `self.assertIn("mode note: keep it brief and answer the current ask", final_prompt)`
  - `self.assertNotIn("---当前状态与约束---", final_prompt)`
  - `self.assertGreater(             final_prompt.index("---内在驱动---"),             final_prompt.index("---眼前正在对我说的---\n<user_input>\nAlice: why not?\n</user_input>"),         )`

### test_refiner_places_soft_background_in_prompt_not_system
Summary: Tests refiner places soft background in prompt not system
Asserts:
  - `self.assertEqual(final_system_prompt, "system prompt only")`
  - `self.assertNotIn("冷区摘要：旧话题", final_system_prompt)`
  - `self.assertIn("---背景理解（仅作背景，不要主动续写旧话题，不要覆盖当前用户当前问题）---", final_prompt)`
  - `self.assertIn("冷区摘要：旧话题", final_prompt)`
  - `self.assertIn("answer the current question first", final_prompt)`
  - `self.assertLess(             final_prompt.index("---背景理解（仅作背景，不要主动续写旧话题，不要覆盖当前用户当前问题）---"),             final_prompt.index("answer the current question first"),         )`

### test_refiner_places_focus_before_recent_warm_memory_and_runtime_guidance
Summary: Tests refiner places focus before recent warm memory and runtime guidance
Asserts:
  - `self.assertLess(final_prompt.index("---眼前正在对我说的---"), final_prompt.index("---前因---"))`
  - `self.assertLess(final_prompt.index("---前因---"), final_prompt.index("---对话记录"))`
  - `self.assertLess(final_prompt.index("---对话记录"), final_prompt.index("---旁边在聊的---"))`
  - `self.assertLess(final_prompt.index("---旁边在聊的---"), final_prompt.index("---记忆闪回"))`
  - `self.assertLess(final_prompt.index("---记忆闪回"), final_prompt.index("---背景理解"))`
  - `self.assertLess(final_prompt.index("---背景理解"), final_prompt.index("---内在驱动---"))`
  - `self.assertLess(final_prompt.index("---内在驱动---"), final_prompt.index("---本轮指引---"))`

### test_refiner_skips_soft_background_in_fast_mode
Summary: Tests refiner skips soft background in fast mode
Asserts:
  - `self.assertNotIn("冷区摘要：旧话题", final_prompt)`
  - `self.assertEqual(envelope.soft_background_skipped_reason, "fast_mode")`
  - `self.assertEqual(envelope.soft_background_rendered_chars, 0)`

### test_refiner_skips_soft_background_when_near_context_priority
Summary: Tests refiner skips soft background when near context priority
Asserts:
  - `self.assertNotIn("冷区摘要：旧话题", final_prompt)`
  - `self.assertEqual(envelope.soft_background_skipped_reason, "near_context_priority")`
  - `self.assertEqual(envelope.soft_background_rendered_chars, 0)`

### test_refiner_trims_soft_background_from_low_priority_tail_first
Summary: Tests refiner trims soft background from low priority tail first
Asserts:
  - `self.assertIn("冷区摘要：", final_prompt)`
  - `self.assertNotIn("术语说明：", final_prompt)`
  - `self.assertNotIn("群聊黑话：", final_prompt)`
  - `self.assertIn("stable_jargon", envelope.soft_background_trimmed_sections)`
  - `self.assertIn("stable_slang", envelope.soft_background_trimmed_sections)`
  - `self.assertLessEqual(             envelope.soft_background_rendered_chars,             self.prompt_refiner_mod.PromptRefiner.SOFT_BACKGROUND_BUDGET_CHARS,         )`

### test_refiner_applies_flexible_budget_without_dropping_focus_direct_or_recent
Summary: Tests refiner applies flexible budget without dropping focus direct or recent
Asserts:
  - `self.assertIn("---眼前正在对我说的---\n<user_input>\nAlice: why not?\n</user_input>", final_prompt)`
  - `self.assertIn("---前因---\nFocus block that must stay", final_prompt)`
  - `self.assertIn("---对话记录", final_prompt)`
  - `self.assertIn("Turn 11: recent mainline detail", final_prompt)`
  - `self.assertNotIn("cold summary " + ("C" * 40), final_prompt)`
  - `self.assertIn("soft_background", envelope.flex_context_trimmed_sections)`
  - `self.assertGreater(envelope.recent_context_rendered_chars, 0)`
  - `self.assertGreater(envelope.warm_context_rendered_chars, 0)`
  - `self.assertIn("focus_message", envelope.flex_context_protected_sections)`
  - `self.assertIn("direct_context", envelope.flex_context_protected_sections)`

### test_refiner_compresses_memory_before_dropping_recent
Summary: Tests refiner compresses memory before dropping recent
Asserts:
  - `self.assertIn("---对话记录", final_prompt)`
  - `self.assertIn("Recent 7:", final_prompt)`
  - `self.assertIn("memory:preview", envelope.flex_context_trimmed_sections)`
  - `self.assertLessEqual(             envelope.memory_context_rendered_chars,             self.prompt_refiner_mod.PromptRefiner.MEMORY_PREVIEW_TARGET_CHARS * 2 + 1,         )`

### test_refiner_tail_truncates_recent_as_last_resort
Summary: Tests refiner tail truncates recent as last resort
Asserts:
  - `self.assertIn("---对话记录", final_prompt)`
  - `self.assertIn("Recent 17:", final_prompt)`
  - `self.assertIn("recent:tail_truncated", envelope.flex_context_trimmed_sections)`
  - `self.assertLessEqual(             envelope.recent_context_rendered_chars,             len(very_long_recent),         )`
  - `self.assertNotIn("direct_context", envelope.flex_context_trimmed_sections)`

### test_refiner_skips_time_anchor_for_plain_chat
Summary: Tests refiner skips time anchor for plain chat
Asserts:
  - `self.assertNotIn("现在是", final_prompt)`

### test_refiner_keeps_time_anchor_for_relative_time_question
Summary: Tests refiner keeps time anchor for relative time question
Asserts:
  - `self.assertIn("现在是", final_prompt)`

### test_refiner_keeps_time_anchor_for_proactive_event
Summary: Tests refiner keeps time anchor for proactive event
Asserts:
  - `self.assertIn("现在是", final_prompt)`

### test_refiner_keeps_time_anchor_for_schedule_request
Summary: Tests refiner keeps time anchor for schedule request
Asserts:
  - `self.assertIn("现在是", final_prompt)`

### test_refiner_keeps_time_anchor_for_wait_resume_signal
Summary: Tests refiner keeps time anchor for wait resume signal
Asserts:
  - `self.assertIn("现在是", final_prompt)`

### test_refiner_keeps_time_anchor_for_post_compaction_recovery_rounds
Summary: Tests refiner keeps time anchor for post compaction recovery rounds
Asserts:
  - `self.assertIn("现在是", final_prompt)`

## tests/test_planner_cognitive_loop_refactor.py (18 tests)

### test_planner_skips_executor_for_wait_and_ignore_actions
Summary: Tests planner skips executor for wait and ignore actions
Asserts:
  - `self.assertEqual(result, "")`
  - `self.assertEqual(planner.executor.calls, [])`
  - `self.assertEqual(event.get_extra("astrmai_cognitive_action"), action)`
  - `self.assertEqual(len(planner.turn_trace_history), 1)`
  - `self.assertEqual(planner.turn_trace_history[0]["status"], f"skipped_{action}")`
  - `self.assertEqual(planner.turn_trace_history[0]["cognitive"]["action"], action)`

### test_plan_and_execute_delegates_only_to_prepared_execution_chain
Summary: Tests plan and execute delegates only to prepared execution chain
Asserts:
  - `self.assertEqual(result, "delegated-ok")`
  - `self.assertEqual([item[0] for item in seen], ["prepare", "continue"])`
  - `self.assertEqual(seen[1][2], {"prepared": True})`

### test_plan_and_execute_honors_cognitive_gate_without_calling_decide
Summary: Tests plan and execute honors cognitive gate without calling decide
Asserts:
  - `self.assertEqual(result, "ok")`
  - `self.assertEqual(planner.cognitive_loop.gate_calls, 1)`
  - `self.assertEqual(planner.cognitive_loop.mark_calls, [("test_gate_skip", False)])`
  - `self.assertEqual(event.get_extra("astrmai_cognitive_loop_skipped_reason"), "test_gate_skip")`
  - `self.assertEqual(len(planner.executor.calls), 1)`

### test_planner_settles_no_send_relationship_for_negative_ignore_turn
Summary: Tests planner settles no send relationship for negative ignore turn
Asserts:
  - `self.assertEqual(result, "")`
  - `self.assertEqual(observed["user_id"], "user-1")`
  - `self.assertEqual(observed["group_id"], event.unified_msg_origin)`
  - `self.assertEqual(observed["message_text"], "你这个废物闭嘴")`
  - `self.assertEqual(observed["skipped_reason"], "ignore")`

### test_planner_routes_tool_call_decision_into_sys3_tool_mode
Summary: Tests planner routes tool call decision into sys3 tool mode
Asserts:
  - `self.assertEqual(event.get_extra("judge_action"), "TOOL_CALL")`
  - `self.assertEqual(len(planner.executor.calls), 1)`
  - `self.assertIn("light-tool", planner.executor.calls[0]["tools"])`
  - `self.assertEqual(planner.prompt_refiner.calls[0]["style_variant"], "自然简短")`
  - `self.assertEqual(planner.prompt_refiner.calls[0]["proactive_recall"], "主动记忆提示")`

### test_planner_applies_reply_memory_policy_and_guidance
Summary: Tests planner applies reply memory policy and guidance
Asserts:
  - `self.assertIsNotNone(envelope)`
  - `self.assertIn("confirm briefly before answering", "".join(envelope.guidance_lines))`
  - `self.assertIn("只接当前线索", "".join(envelope.guidance_lines))`
  - `self.assertTrue(planner.context_engine.context.shared_dict.get("disable_rag_injection"))`
  - `self.assertEqual(event.get_extra("retrieve_keys"), [])`
  - `self.assertIn("focus on this sentence first", event.get_extra("sys1_thought"))`
  - `self.assertEqual(planner.executor.calls[0]["prompt"], "final prompt")`
  - `self.assertEqual(len(planner.turn_trace_history), 1)`
  - `self.assertEqual(turn_trace["status"], "executed")`
  - `self.assertEqual(turn_trace["cognitive"]["memory_policy"], "none")`
  - `self.assertGreaterEqual(turn_trace["continuity"]["system_prompt_length"], 0)`
  - `self.assertGreaterEqual(turn_trace["continuity"]["prompt_length"], 0)`
  - `self.assertGreaterEqual(turn_trace["continuity"]["frozen_prefix_length"], 0)`
  - `self.assertNotIn("focus on this sentence first", rendered_trace)`
  - `self.assertNotIn("final prompt", rendered_trace)`

### test_planner_moves_long_reply_and_mode_runtime_instructions_to_prompt_blocks
Summary: Tests planner moves long reply and mode runtime instructions to prompt blocks
Asserts:
  - `self.assertEqual(stable, "Use short fragments.")`
  - `self.assertEqual(dynamic, "Keep this turn short; avoid another long reply.")`
  - `self.assertEqual(planner.executor.calls[0]["system_prompt"], "system prompt only")`
  - `self.assertIn("soft_background", planner.turn_trace_history[0]["continuity"]["dynamic_prompt_blocks"])`
  - `self.assertGreaterEqual(             planner.turn_trace_history[0]["continuity"]["dynamic_prompt_length"],             len(envelope.cognitive_drive_block),         )`

### test_planner_uses_planner_reasoning_when_cognitive_drive_fallback_needs_it
Summary: Tests planner uses planner reasoning when cognitive drive fallback needs it
Asserts:
  - `self.assertEqual(envelope.cognitive_drive_block, planner_reasoning)`

### test_planner_writes_agency_extras_and_reflection
Summary: Tests planner writes agency extras and reflection
Asserts:
  - `self.assertEqual(event.get_extra("astrmai_reply_need"), "reply")`
  - `self.assertEqual(event.get_extra("astrmai_social_intent"), "pushback")`
  - `self.assertEqual(event.get_extra("astrmai_action_tier"), "none")`
  - `self.assertEqual(event.get_extra("astrmai_stance"), "guarded")`
  - `self.assertEqual(event.get_extra("astrmai_state_bias"), "keep the line short")`
  - `self.assertAlmostEqual(event.get_extra("astrmai_attack_confidence"), 0.91)`
  - `self.assertIsNotNone(turn_context)`
  - `self.assertEqual(turn_context.cognitive.social_intent, "pushback")`
  - `self.assertEqual(turn_context.cognitive.action_tier, "none")`
  - `self.assertAlmostEqual(turn_context.cognitive.attack_confidence, 0.91)`
  - `self.assertIn("克制反驳", guidance_text)`
  - `self.assertIn("不辱骂", guidance_text)`
  - `self.assertIn("Keep this turn brief and avoid proactive expansion.", guidance_text)`
  - `self.assertIn("pushback", summary)`
  - `self.assertIn("sharp_reply", summary)`
  - `self.assertEqual(planner.turn_trace_history[-1]["tools"]["final_tier"], "none")`

### test_planner_waits_for_short_non_direct_group_ambient_message
Summary: Tests planner waits for short non direct group ambient message
Asserts:
  - `self.assertEqual(result, "")`
  - `self.assertEqual(planner.executor.calls, [])`
  - `self.assertEqual(event.get_extra("astrmai_reply_need"), "wait")`
  - `self.assertEqual(event.get_extra("astrmai_social_intent"), "observe")`
  - `self.assertIn("group_ambient_short_wait", event.get_extra("astrmai_risk_flags"))`
  - `self.assertEqual(planner.turn_trace_history[-1]["status"], "skipped_wait")`

### test_planner_downgrades_pushback_during_sharp_reply_cooldown
Summary: Tests planner downgrades pushback during sharp reply cooldown
Asserts:
  - `self.assertEqual(event.get_extra("astrmai_social_intent"), "boundary")`
  - `self.assertIn("sharp_reply_cooldown", event.get_extra("astrmai_risk_flags"))`
  - `self.assertIn("pushback_downgraded", event.get_extra("astrmai_risk_flags"))`

### test_planner_feeds_previous_agency_reflection_to_cognitive_loop
Summary: Tests planner feeds previous agency reflection to cognitive loop
Asserts:
  - `self.assertIn("最近我的短期行动残留", second_event.get_extra("astrmai_agency_reflection_summary"))`
  - `self.assertIn("Conversation continuity", continuity)`
  - `self.assertIn("current_topic=", continuity)`
  - `self.assertIn("goal_status=", continuity)`
  - `self.assertIn("last_social_intent=", continuity)`
  - `self.assertIn("tease", continuity)`
  - `self.assertIn("reply", continuity)`
  - `self.assertEqual(turn_context.continuity.conversation_summary, continuity)`
  - `self.assertTrue(turn_context.continuity.current_topic)`
  - `self.assertTrue(turn_context.continuity.goal_status)`
  - `self.assertNotIn("current_goal=", planner.executor.calls[-1]["prompt"])`
  - `self.assertNotIn("goal_status=", planner.executor.calls[-1]["prompt"])`

### test_planner_wait_decision_does_not_refresh_conversation_goal
Summary: Tests planner wait decision does not refresh conversation goal
Asserts:
  - `self.assertEqual(after["current_topic"], before["current_topic"])`
  - `self.assertEqual(after["current_goal"], before["current_goal"])`
  - `self.assertEqual(after["turn_count"], before["turn_count"])`

### test_planner_feeds_long_term_memory_feedback_to_cognitive_loop
Summary: Tests planner feeds long term memory feedback to cognitive loop
Asserts:
  - `self.assertIn("Long-term behavior and memory feedback", feedback)`
  - `self.assertIn("avoid repeated meme", feedback)`
  - `self.assertEqual(planner.cognitive_loop.calls[0][0].get_extra("astrmai_memory_feedback_summary"), feedback)`

### test_planner_feeds_heartflow_context_to_cognitive_loop
Summary: Tests planner feeds heartflow context to cognitive loop
Asserts:
  - `self.assertIn("join carefully", event.get_extra("astrmai_heartflow_context"))`
  - `self.assertEqual(event.get_extra("astrmai_heartflow_pulse"), "prepare_reply")`
  - `self.assertAlmostEqual(event.get_extra("astrmai_heartflow_interest"), 0.82)`
  - `self.assertIn("join carefully", turn_context.continuity.heartflow_context)`
  - `self.assertAlmostEqual(turn_context.continuity.heartflow_interest, 0.82)`
  - `self.assertEqual(planner.cognitive_loop.calls[0][0].get_extra("astrmai_heartflow_context"), event.get_extra("astrmai_heartflow_context"))`

### test_planner_injects_dynamic_tool_guidance_when_tools_are_available
Summary: Tests planner injects dynamic tool guidance when tools are available
Asserts:
  - `self.assertIn("本轮可用动作：查询记忆/画像、查询自我设定、戳一戳。只有确实合适时才使用，普通闲聊直接回复。", guidance_text)`
  - `self.assertIn("等待只在对方明显没说完", guidance_text)`
  - `self.assertIn("撤回只在用户明确要求", guidance_text)`
  - `self.assertEqual(guidance_text.count("查询记忆/画像"), 1)`
  - `self.assertIn("本轮可用动作", "\n".join(planner.prompt_refiner.calls[0]["prompt_envelope"].guidance_lines))`

### test_planner_injects_chat_tier_tool_guidance
Summary: Tests planner injects chat tier tool guidance
Asserts:
  - `self.assertIn("如果气氛合适，可以顺手发表情包、轻轻互动或点个赞", guidance_text)`
  - `self.assertIn("戳人或@别人只在非常自然、明确相关时使用", guidance_text)`
  - `self.assertNotIn("等待只在对方明显没说完", guidance_text)`
  - `self.assertNotIn("撤回只在用户明确要求", guidance_text)`
  - `self.assertNotIn("本轮可用动作：", guidance_text)`

### test_planner_does_not_inject_dynamic_tool_guidance_without_tools
Summary: Tests planner does not inject dynamic tool guidance without tools
Asserts:
  - `self.assertNotIn("本轮可用动作", "\n".join(envelope.guidance_lines))`

## tests/test_memory_refactor.py (21 tests)

### test_react_retriever_saves_trace_using_contract
Summary: Tests react retriever saves trace using contract
Asserts:
  - `self.assertEqual(len(retriever.db_service.saved), 1)`
  - `self.assertEqual(trace.chat_id, "chat-1")`
  - `self.assertEqual(json.loads(trace.source_layers), ["person"])`

### test_memory_processor_fallback_prompt_replaces_payload_without_format_errors
Summary: Tests memory processor fallback prompt replaces payload without format errors
Asserts:
  - `self.assertIn("history-x", rendered_history)`
  - `self.assertIn("facts-y", rendered_facts)`

### test_memory_processor_uses_chat_scoped_lane_for_non_global_session
Summary: Tests memory processor uses chat scoped lane for non global session
Asserts:
  - `self.assertEqual(len(gateway.calls), 2)`
  - `self.assertEqual(gateway.calls[0]["lane_key"].scope_id, "chat-42")`
  - `self.assertEqual(gateway.calls[0]["lane_key"].scope_kind, "chat")`

### test_memory_turn_pipeline_describes_eligibility_from_buffer
Summary: Tests memory turn pipeline describes eligibility from buffer
Asserts:
  - `self.assertTrue(result["eligible"])`
  - `self.assertTrue(result["candidate_present"])`
  - `self.assertEqual(result["reason"], "eligible")`
  - `self.assertEqual(result["pending_messages"], 6)`

### test_memory_turn_pipeline_maintenance_delegates_to_session_summarizer
Summary: Tests memory turn pipeline maintenance delegates to session summarizer
Asserts:
  - `self.assertTrue(result["performed"])`
  - `self.assertEqual(result["reason"], "summarized")`
  - `self.assertEqual(calls[0][0], "chat-2")`

### test_memory_turn_pipeline_record_turn_keeps_buffer_after_instant_hit
Summary: Tests memory turn pipeline record turn keeps buffer after instant hit
Asserts:
  - `self.assertTrue(result["performed"])`
  - `self.assertTrue(gate.hit)`
  - `self.assertTrue(turn.instant_gate_hit)`
  - `self.assertEqual(             pipeline._session_history_buffer["chat-3"]["buffer"],             ["用户/旁白：我叫小明", "Bot：好的"],         )`

### test_memory_turn_pipeline_ignores_proactive_turns
Summary: Tests memory turn pipeline ignores proactive turns
Asserts:
  - `self.assertFalse(result["performed"])`
  - `self.assertEqual(result["reason"], "proactive_ignored")`

### test_memory_turn_pipeline_event_drop_still_keeps_buffered_turn
Summary: Tests memory turn pipeline event drop still keeps buffered turn
Asserts:
  - `self.assertTrue(record_result["performed"])`
  - `self.assertEqual(             pipeline._session_history_buffer["chat-drop"]["buffer"],             ["用户/旁白：hello", "Bot：hi"],         )`
  - `self.assertTrue(eligibility["candidate_present"])`
  - `self.assertEqual(eligibility["reason"], "below_threshold")`
  - `self.assertRaises(RuntimeError)`

### test_memory_turn_pipeline_idle_timeout_becomes_eligible_even_below_threshold
Summary: Tests memory turn pipeline idle timeout becomes eligible even below threshold
Asserts:
  - `self.assertTrue(result["eligible"])`
  - `self.assertTrue(result["candidate_present"])`
  - `self.assertEqual(result["reason"], "idle_timeout")`

### test_memory_turn_pipeline_queue_full_publish_does_not_drop_buffer
Summary: Tests memory turn pipeline queue full publish does not drop buffer
Asserts:
  - `self.assertTrue(record_result["performed"])`
  - `self.assertFalse(turn.instant_gate_hit)`
  - `self.assertEqual(len(buffered), 2)`
  - `self.assertIn("hello", buffered[0])`
  - `self.assertIn("hi", buffered[1])`

### test_memory_turn_pipeline_sweep_loop_triggers_idle_timeout_maintenance
Summary: Tests memory turn pipeline sweep loop triggers idle timeout maintenance
Asserts:
  - `self.assertEqual(maintenance_calls, ["chat-idle-sweep"])`

### test_memory_turn_pipeline_maintenance_rolls_back_buffer_and_sets_cooldown_on_failure
Summary: Tests memory turn pipeline maintenance rolls back buffer and sets cooldown on failure
Asserts:
  - `self.assertFalse(result["performed"])`
  - `self.assertEqual(result["reason"], "summary_failed")`
  - `self.assertEqual(session["buffer"], ["u1", "a1", "u2", "a2"])`
  - `self.assertEqual(session["failures"], 1)`
  - `self.assertGreater(session["cooldown_until"], 0.0)`

### test_compat_summarizer_still_reexports_chat_history_summarizer
Summary: Tests compat summarizer still reexports chat history summarizer
Asserts:
  - `self.assertIsNotNone(summarizer)`

### test_compat_summarizer_describe_session_eligibility_forwards_to_pipeline
Summary: Tests compat summarizer describe session eligibility forwards to pipeline
Asserts:
  - `self.assertTrue(result["eligible"])`
  - `self.assertEqual(result["reason"], "eligible")`

### test_compat_summarizer_describe_session_eligibility_falls_back_without_pipeline
Summary: Tests compat summarizer describe session eligibility falls back without pipeline
Asserts:
  - `self.assertFalse(result["eligible"])`
  - `self.assertFalse(result["candidate_present"])`
  - `self.assertEqual(result["reason"], "memory_pipeline_unavailable")`
  - `self.assertEqual(result["threshold_messages"], 6)`

### test_compat_summarizer_run_once_for_session_forwards_to_pipeline
Summary: Tests compat summarizer run once for session forwards to pipeline
Asserts:
  - `self.assertEqual(calls, ["chat-run"])`
  - `self.assertEqual(result, {"performed": True, "chat_id": "chat-run"})`

### test_compat_summarizer_ingest_committed_turn_forwards_to_pipeline
Summary: Tests compat summarizer ingest committed turn forwards to pipeline
Asserts:
  - `self.assertEqual(pipeline.turns[0].chat_id, "chat-ingest")`
  - `self.assertEqual(pipeline.turns[0].user_text, "hello")`
  - `self.assertEqual(pipeline.turns[0].assistant_text, "hi")`
  - `self.assertEqual(pipeline.turns[0].source, "reply_post_send")`
  - `self.assertTrue(pipeline.turns[0].is_proactive)`
  - `self.assertIs(pipeline.recorded, pipeline.gated)`
  - `self.assertEqual(result["source"], "reply_post_send")`
  - `self.assertTrue(result["instant_gate_hit"])`
  - `self.assertEqual(result["pending_messages"], 4)`

### test_compat_summarizer_ingest_committed_turn_degrades_without_pipeline
Summary: Tests compat summarizer ingest committed turn degrades without pipeline
Asserts:
  - `self.assertEqual(             result,             {"performed": False, "reason": "memory_pipeline_unavailable", "source": "reply_post_send"},         )`

### test_compat_summarizer_ingest_committed_turn_preserves_source_when_record_skips
Summary: Tests compat summarizer ingest committed turn preserves source when record skips
Asserts:
  - `self.assertEqual(result, {"performed": False, "reason": "not_eligible", "source": "reply_post_send"})`

### test_compat_summarizer_start_and_stop_toggle_periodic_task
Summary: Tests compat summarizer start and stop toggle periodic task
Asserts:
  - `self.assertIs(first_task, second_task)`
  - `self.assertFalse(summarizer._running)`
  - `self.assertTrue(first_task.cancelled() or first_task.done())`

### test_compat_summarizer_periodic_loop_runs_eligible_sessions_and_prunes
Summary: Tests compat summarizer periodic loop runs eligible sessions and prunes
Asserts:
  - `self.assertEqual(             calls,             [                 ("describe", "chat-eligible"),                 ("maintenance", "chat-eligible"),                 ("prune", 0.4),             ],         )`

## tests/test_attention_gate_refactor.py (24 tests)

### test_focus_thread_selection_matches_legacy_behavior
Summary: Tests focus thread selection matches legacy behavior
Asserts:
  - `self.assertEqual(focus_thread["core_events"], [bot_event, focus_event])`
  - `self.assertEqual(focus_thread["ambient_events"], [unrelated])`

### test_resolve_event_context_keeps_reply_image_footprints_without_direct_vision
Summary: Tests resolve event context keeps reply image footprints without direct vision
Asserts:
  - `self.assertEqual(context["extracted_images"], ["reply.jpg"])`
  - `self.assertFalse(context["is_private"])`

### test_is_direct_wakeup_event_handles_missing_sensors_without_losing_fast_paths
Summary: Tests is direct wakeup event handles missing sensors without losing fast paths
Asserts:
  - `self.assertFalse(gate._is_direct_wakeup_event(ordinary, "bot-1"))`
  - `self.assertTrue(gate._is_direct_wakeup_event(direct, "bot-1"))`
  - `self.assertTrue(gate._is_direct_wakeup_event(bonus, "bot-1"))`

### test_process_event_fast_mode_engages_on_direct_wakeup
Summary: Tests process event fast mode engages on direct wakeup
Asserts:
  - `self.assertEqual(status, "ENGAGED")`
  - `self.assertEqual(event.get_extra("retrieve_keys"), ["CORE_ONLY"])`
  - `self.assertTrue(event.get_extra("is_fast_mode"))`
  - `self.assertEqual(event.get_extra("astrmai_group_direct_wakeup"), True)`
  - `self.assertIsNotNone(turn_context)`
  - `self.assertEqual(turn_context.perception.chat_id, "group-1")`
  - `self.assertTrue(turn_context.perception.is_strong_wakeup)`
  - `self.assertEqual(turn_context.attention.retrieve_keys, ["CORE_ONLY"])`
  - `self.assertTrue(turn_context.attention.is_fast_mode)`
  - `self.assertEqual(len(captured), 1)`

### test_process_event_coalesces_messages_while_debounce_is_open
Summary: Tests process event coalesces messages while debounce is open
Asserts:
  - `self.assertEqual(first_status, "BUFFERED")`
  - `self.assertEqual(second_status, "BUFFERED")`
  - `self.assertEqual(len(captured), 1)`
  - `self.assertEqual([event.message_str for event in captured[0][1]], ["first", "second"])`

### test_judge_wait_keeps_batch_for_next_pass_without_sys2
Summary: Tests judge wait keeps batch for next pass without sys2
Asserts:
  - `self.assertEqual(after_wait, ["not done"])`
  - `self.assertEqual(len(captured), 1)`
  - `self.assertEqual([event.message_str for event in captured[0][1]], ["not done", "now done"])`

### test_judge_ignore_keeps_focus_as_window_only
Summary: Tests judge ignore keeps focus as window only
Asserts:
  - `self.assertEqual(captured, [])`
  - `self.assertEqual(retained, ["skip me"])`

### test_strong_wakeup_skips_judge_gate
Summary: Tests strong wakeup skips judge gate
Asserts:
  - `self.assertEqual(judge.calls, [])`
  - `self.assertEqual(len(captured), 1)`

### test_judge_reply_action_maps_to_pass_through
Summary: Tests judge reply action maps to pass through
Asserts:
  - `self.assertEqual(focus.get_extra("judge_action"), "PASS")`
  - `self.assertIsNotNone(turn_context)`
  - `self.assertEqual(turn_context.attention.judge_action, "PASS")`
  - `self.assertEqual(turn_context.attention.retrieve_keys, ["ALL"])`
  - `self.assertEqual([event.message_str for event in turn_context.attention.window_events], ["please answer"])`
  - `self.assertEqual(len(captured), 1)`

### test_router_applies_primary_mood_before_judge_once
Summary: Tests router applies primary mood before judge once
Asserts:
  - `self.assertEqual(first.action, "PASS")`
  - `self.assertEqual(second.action, "PASS")`
  - `self.assertEqual(calls, [("default:GroupMessage:group-1", "need help")])`
  - `self.assertTrue(focus.get_extra("astrmai_primary_mood_applied"))`
  - `self.assertEqual(focus.get_extra("astrmai_primary_mood_tag"), "happy")`
  - `self.assertAlmostEqual(focus.get_extra("astrmai_primary_mood_value"), 0.45)`
  - `self.assertEqual(focus.get_extra("astrmai_primary_mood_source"), "attention_pre_judge")`

### test_process_event_applies_primary_mood_before_private_wait
Summary: Tests process event applies primary mood before private wait
Asserts:
  - `self.assertEqual(status, "PRIVATE_WAIT")`
  - `self.assertEqual(calls, [("default:FriendMessage:user-1", "今天有点难受")])`
  - `self.assertEqual(manager.calls, [("user-1", "今天有点难受", "default:FriendMessage:user-1")])`
  - `self.assertTrue(event.get_extra("astrmai_primary_mood_applied"))`
  - `self.assertEqual(event.get_extra("astrmai_primary_mood_tag"), "sad")`
  - `self.assertAlmostEqual(event.get_extra("astrmai_primary_mood_value"), -0.35)`
  - `self.assertEqual(event.get_extra("astrmai_primary_mood_source"), "attention_ingress")`

### test_process_event_applies_primary_mood_before_fast_wakeup_engage
Summary: Tests process event applies primary mood before fast wakeup engage
Asserts:
  - `self.assertEqual(status, "ENGAGED")`
  - `self.assertEqual(calls, [("group-1", "AstrMai")])`
  - `self.assertTrue(event.get_extra("astrmai_primary_mood_applied"))`
  - `self.assertEqual(event.get_extra("astrmai_primary_mood_source"), "attention_ingress")`

### test_debounce_normalizes_merged_events_once
Summary: Tests debounce normalizes merged events once
Asserts:
  - `self.assertEqual(call_count, 1)`
  - `self.assertEqual(len(captured), 1)`

### test_inject_external_event_routes_through_kernel_when_bound
Summary: Tests inject external event routes through kernel when bound
Asserts:
  - `self.assertEqual(result, "BUFFERED")`
  - `self.assertEqual(             calls,             [("default:GroupMessage:group-1", "external", "synthetic proactive nudge", "proactive_dispatcher")],         )`

### test_inject_external_event_falls_back_to_process_event_without_kernel
Summary: Tests inject external event falls back to process event without kernel
Asserts:
  - `self.assertEqual(result, "ENGAGED")`
  - `self.assertEqual(calls, [("default:GroupMessage:group-1", "external plugin reply", "external_result_bridge")])`

### test_debounce_worker_drain_loop_keeps_late_arrivals
Summary: Tests debounce worker drain loop keeps late arrivals
Asserts:
  - `self.assertEqual(first_status, "BUFFERED")`
  - `self.assertEqual(second_status, "BUFFERED")`
  - `self.assertGreaterEqual(len(captured), 2)`
  - `self.assertIn(["first"], captured)`
  - `self.assertIn(["first", "second"], captured)`
  - `self.assertFalse(session.is_evaluating)`
  - `self.assertEqual(session.accumulation_pool, [])`

### test_worker_failure_recovery_only_resets_failed_session
Summary: Tests worker failure recovery only resets failed session
Asserts:
  - `self.assertFalse(failed_session.is_evaluating)`
  - `self.assertTrue(healthy_session.is_evaluating)`
  - `self.assertTrue(spawned)`

### test_background_task_semaphore_limits_parallelism
Summary: Tests background task semaphore limits parallelism
Asserts:
  - `self.assertLessEqual(max_running, self.gate.BACKGROUND_TASK_MAX_CONCURRENCY)`

### test_context_compaction_engine_coalesces_execution_but_keeps_message_accounting
Summary: Tests context compaction engine coalesces execution but keeps message accounting
Asserts:
  - `self.assertEqual(first_result.state, "DONE")`
  - `self.assertEqual(second_result.skipped_reason, "evaluation_already_scheduled")`
  - `self.assertEqual(maybe_compact_calls, 1)`
  - `self.assertEqual(state["message_count_since_last_compaction"], 2)`

### test_fast_wakeup_bypasses_background_semaphore
Summary: Tests fast wakeup bypasses background semaphore
Asserts:
  - `self.assertEqual(status, "ENGAGED")`

### test_attention_window_ttl_keeps_recent_events_for_180_seconds
Summary: Tests attention window ttl keeps recent events for 180 seconds
Asserts:
  - `self.assertEqual(retained, [recent])`
  - `self.assertEqual(session.attention_window, [recent])`

### test_focus_thread_sorts_output_by_original_event_order
Summary: Tests focus thread sorts output by original event order
Asserts:
  - `self.assertEqual([event.message_str for event in focus_thread.core_events], ["first", "focus"])`
  - `self.assertEqual([event.message_str for event in focus_thread.related_events], ["related"])`
  - `self.assertEqual([event.message_str for event in focus_thread.ambient_events], ["ambient"])`

### test_throttle_gracefully_handles_missing_should_drop
Summary: Tests throttle gracefully handles missing should drop
Asserts:
  - `self.assertIsNone(result_with_none)`
  - `self.assertIsNone(result_with_plain_object)`

### test_repeater_echo_signature_cleanup_keeps_behavior
Summary: Tests repeater echo signature cleanup keeps behavior
Asserts:
  - `self.assertIsNone(first)`
  - `self.assertIsNone(second)`
  - `self.assertEqual(third, "repeater_echo")`

## tests/unit/memory/test_memory_query_optimization.py (27 tests)

### test_builder_off_preserves_legacy_query_and_top_k
Summary: Tests builder off preserves legacy query and top k
Asserts:
  - `self.assertEqual(query.query, "  我喜欢吃什么  ")`
  - `self.assertEqual(query.top_k, 7)`
  - `self.assertNotIn("primary_intent", query.metadata)`
  - `self.assertNotIn("candidate_limit", query.metadata)`

### test_builder_off_reaches_store_with_legacy_query
Summary: Tests builder off reaches store with legacy query
Asserts:
  - `self.assertEqual(store.calls[0][0], "  原始查询  ")`
  - `self.assertEqual(store.calls[0][1]["top_k"], 7)`
  - `self.assertIsNone(store.calls[0][1]["candidate_limit"])`

### test_normal_query_keeps_main_query_and_does_not_expand_recall
Summary: Tests normal query keeps main query and does not expand recall
Asserts:
  - `self.assertEqual(query.query, "我喜欢吃什么")`
  - `self.assertEqual(query.metadata["primary_intent"], "food_preference")`
  - `self.assertIn("吃", query.metadata["expansion_terms"])`
  - `self.assertNotIn("candidate_limit", query.metadata)`
  - `self.assertFalse(query.metadata["intent_rerank_enabled"])`

### test_common_queries_keep_normalized_query_as_retrieval_query
Summary: Tests common queries keep normalized query as retrieval query
Asserts:
  - `self.assertEqual(query.query, text)`
  - `self.assertEqual(query.metadata["retrieval_query"], text)`
  - `self.assertNotIn("candidate_limit", query.metadata)`
  - `self.assertEqual(query.top_k, 5)`

### test_multi_intent_and_short_entity_detection
Summary: Tests multi intent and short entity detection
Asserts:
  - `self.assertEqual(composite.metadata["primary_intent"], "food_preference")`
  - `self.assertEqual(             set(composite.metadata["intents"]),             {"recent_reference", "dislike", "food_preference"},         )`
  - `self.assertFalse(short_entity.metadata["is_low_information"])`
  - `self.assertEqual(general.metadata["primary_intent"], "preference_general")`
  - `self.assertNotIn("food_preference", general.metadata["intents"])`

### test_recent_context_uses_previous_context_but_not_current_query
Summary: Tests recent context uses previous context but not current query
Asserts:
  - `self.assertTrue(query.metadata["is_low_information"])`
  - `self.assertIn("火锅", query.metadata["context_terms"])`
  - `self.assertEqual(query.query.count("那个呢"), 1)`
  - `self.assertIn("火锅", query.query)`

### test_recent_context_uses_compact_terms_not_full_assistant_text
Summary: Tests recent context uses compact terms not full assistant text
Asserts:
  - `self.assertNotIn(assistant_text, query.query)`
  - `self.assertTrue({"口味", "芒果"} & set(query.metadata["context_terms"]))`
  - `self.assertTrue(all(len(term) <= 8 for term in query.metadata["context_terms"]))`

### test_flag_combinations_keep_query_and_candidate_controls_separate
Summary: Tests flag combinations keep query and candidate controls separate
Asserts:
  - `self.assertEqual(rerank_only.query, "我喜欢吃什么")`
  - `self.assertEqual(rerank_only.top_k, 5)`
  - `self.assertNotIn("candidate_limit", rerank_only.metadata)`
  - `self.assertTrue(rerank_only.metadata["intent_rerank_enabled"])`
  - `self.assertEqual(identity_full.top_k, 3)`
  - `self.assertLessEqual(identity_full.metadata["candidate_limit"], 12)`
  - `self.assertEqual(preference_full.top_k, 8)`
  - `self.assertEqual(preference_full.metadata["candidate_limit"], 24)`

### test_explicit_candidate_limit_is_forwarded_without_second_multiplier
Summary: Tests explicit candidate limit is forwarded without second multiplier
Asserts:
  - `self.assertEqual(result, [])`
  - `self.assertEqual(store.calls[0][1]["top_k"], 6)`
  - `self.assertEqual(store.calls[0][1]["candidate_limit"], 24)`

### test_builder_flags_drive_actual_store_limits
Summary: Tests builder flags drive actual store limits
Asserts:
  - `self.assertEqual(rerank_store.calls[0][1]["top_k"], 5)`
  - `self.assertIsNone(rerank_store.calls[0][1]["candidate_limit"])`
  - `self.assertEqual(full_store.calls[0][1]["top_k"], 8)`
  - `self.assertEqual(full_store.calls[0][1]["candidate_limit"], 24)`

### test_v2_store_candidate_limit_is_backward_compatible
Summary: Tests v2 store candidate limit is backward compatible
Asserts:
  - `self.assertEqual(MemoryV2Store._resolve_search_limit(5, None), 40)`
  - `self.assertEqual(MemoryV2Store._resolve_search_limit(6, 24), 24)`
  - `self.assertEqual(MemoryV2Store._resolve_search_limit(6, 3), 6)`

### test_builder_on_rerank_off_keeps_existing_candidate_order
Summary: Tests builder on rerank off keeps existing candidate order
Asserts:
  - `self.assertEqual([item.id for item in result], ["color", "food"])`
  - `self.assertNotIn("_final_relevance_score", result[0].metadata)`

### test_intent_rerank_prefers_food_and_preserves_score_breakdown
Summary: Tests intent rerank prefers food and preserves score breakdown
Asserts:
  - `self.assertEqual(result[0].id, "food")`
  - `self.assertIn("_base_relevance_score", result[0].metadata)`
  - `self.assertIn("_final_relevance_score", result[0].metadata)`
  - `self.assertIn("intent", result[0].metadata["_score_breakdown"])`
  - `self.assertEqual(candidates[1].metadata, {"matched_by": ["canonical_fts", "faiss"]})`

### test_general_preference_does_not_force_food_and_low_base_cannot_jump_to_first
Summary: Tests general preference does not force food and low base cannot jump to first
Asserts:
  - `self.assertEqual(general_result[0].id, "color")`
  - `self.assertEqual(food_result[0].id, "color")`
  - `self.assertLess(             food_result[1].metadata["_final_relevance_score"],             food_result[0].metadata["_final_relevance_score"],         )`

### test_rerank_deduplicates_canonical_id_and_merges_sources
Summary: Tests rerank deduplicates canonical id and merges sources
Asserts:
  - `self.assertEqual(len(result), 1)`
  - `self.assertEqual(set(result[0].metadata["matched_by"]), {"faiss", "canonical_fts"})`

### test_rerank_near_deduplicates_short_chinese_preference_phrases
Summary: Tests rerank near deduplicates short chinese preference phrases
Asserts:
  - `self.assertEqual(len(result), 1)`

### test_adaptive_top_k_has_neutral_confidence_fallback
Summary: Tests adaptive top k has neutral confidence fallback
Asserts:
  - `self.assertEqual(len(result), 3)`

### test_summary_trace_omits_query_and_memory_content
Summary: Tests summary trace omits query and memory content
Asserts:
  - `self.assertNotIn("隐私正文", str(summary))`
  - `self.assertNotIn("我的名字", str(summary))`
  - `self.assertEqual(summary["selected_ids"], ["secret-id"])`
  - `self.assertNotIn("retrieval_debug", query.metadata["_trace"])`

### test_persisted_summary_sanitizes_rewritten_queries_and_search_text
Summary: Tests persisted summary sanitizes rewritten queries and search text
Asserts:
  - `self.assertEqual(summary["rewritten_queries"], [])`
  - `self.assertNotIn("隐私查询", str(summary))`
  - `self.assertNotIn("隐私改写", str(summary))`
  - `self.assertEqual(summary["search_steps"], [{"matched_terms": ["term"]}])`

### test_debug_trace_includes_diagnostics_only_when_enabled
Summary: Tests debug trace includes diagnostics only when enabled
Asserts:
  - `self.assertEqual(debug["query"], "调试查询")`
  - `self.assertIn("调试正文", str(debug))`

### test_query_builder_debug_trace_has_query_layers_only_when_enabled
Summary: Tests query builder debug trace has query layers only when enabled
Asserts:
  - `self.assertNotIn("query_builder_debug", normal.metadata["_trace"])`
  - `self.assertEqual(payload["raw_query"], "那个呢")`
  - `self.assertEqual(payload["normalized_query"], "那个呢")`
  - `self.assertIn("芒果", payload["context_terms"])`
  - `self.assertIn("芒果", payload["retrieval_query"])`

### test_adaptive_injection_uses_query_limit_and_default_trace_has_no_content
Summary: Tests adaptive injection uses query limit and default trace has no content
Asserts:
  - `self.assertEqual(retrieval.query.top_k, 8)`
  - `self.assertEqual(len(bundle.items), 8)`
  - `self.assertEqual(bundle.trace.summary_preview, "")`
  - `self.assertNotIn("偏好 0", str(retrieval.query.metadata["_trace"]))`

### test_rerank_failure_falls_back_to_existing_order
Summary: Tests rerank failure falls back to existing order
Asserts:
  - `self.assertEqual([item.id for item in result], ["first", "second"])`
  - `self.assertIn("intent_rerank", query.metadata["_trace"]["degraded_components"])`

### test_hybrid_failure_degrades_to_canonical_results
Summary: Tests hybrid failure degrades to canonical results
Asserts:
  - `self.assertEqual([item.id for item in result], ["canonical"])`
  - `self.assertIn("hybrid", query.metadata["_trace"]["degraded_components"])`

### test_query_builder_failure_returns_legacy_query
Summary: Tests query builder failure returns legacy query
Asserts:
  - `self.assertEqual(query.query, "  原始查询  ")`
  - `self.assertEqual(query.top_k, 7)`
  - `self.assertEqual(query.metadata, {"visibility_mode": "auto"})`

### test_flags_can_be_read_from_config_debug_channel
Summary: Tests flags can be read from config debug channel
Asserts:
  - `self.assertFalse(flags.query_builder)`
  - `self.assertTrue(flags.intent_rerank)`
  - `self.assertTrue(flags.adaptive_top_k)`

### test_flags_can_be_read_from_real_config_model
Summary: Tests flags can be read from real config model
Asserts:
  - `self.assertFalse(flags.query_builder)`
  - `self.assertTrue(flags.intent_rerank)`
  - `self.assertTrue(flags.adaptive_top_k)`
  - `self.assertTrue(flags.debug_trace)`

## tests/test_cognitive_feedback_refactor.py (18 tests)

### test_memory_engine_records_feedback_in_cache_and_filters_recall
Summary: Tests memory engine records feedback in cache and filters recall
Asserts:
  - `self.assertEqual(len(signals), 1)`
  - `self.assertEqual(signals[0].source, "agency")`
  - `self.assertEqual(signals[0].guidance, "avoid repeated meme")`
  - `self.assertIn("normal memory content", recalled)`
  - `self.assertNotIn("cognitive_feedback", recalled)`

### test_memory_engine_record_cognitive_feedback_writes_feedback_request
Summary: Tests memory engine record cognitive feedback writes feedback request
Asserts:
  - `self.assertEqual(len(captured), 1)`
  - `self.assertEqual(request.kind, "feedback")`
  - `self.assertEqual(request.source, "agency")`
  - `self.assertEqual(request.session_id, "chat-1")`
  - `self.assertEqual(request.visibility, "tool_only")`
  - `self.assertEqual(request.tags, ["joke", "tone"])`
  - `self.assertTrue(request.metadata["cognitive_feedback"])`
  - `self.assertEqual(request.metadata["guidance"], "Prefer direct answers")`
  - `self.assertTrue(request.dedup_key.startswith("feedback:chat-1:agency:"))`
  - `self.assertIn("[cognitive_feedback:agency]", request.content)`
  - `self.assertEqual(engine._cognitive_feedback_cache["chat-1"][0].summary, "Use fewer repeated jokes")`

### test_memory_engine_get_cognitive_feedback_merges_cache_and_db_rows
Summary: Tests memory engine get cognitive feedback merges cache and db rows
Asserts:
  - `self.assertEqual([item.source for item in cache_signals], ["agency"])`
  - `self.assertEqual(cache_signals[0].summary, "cache summary")`
  - `self.assertEqual(len(db_calls), 1)`
  - `self.assertEqual(len(db_calls), 2)`
  - `self.assertEqual(len(parse_calls), 4)`
  - `self.assertEqual([item.source for item in db_signals], ["diary"])`
  - `self.assertEqual(db_signals[0].summary, "db summary")`
  - `self.assertEqual(db_signals[0].guidance, "db guidance")`
  - `self.assertEqual(db_signals[0].tags, ["calm", "focus"])`
  - `self.assertEqual(db_signals[0].importance, 0.9)`
  - `self.assertEqual([item.source for item in diary_only], ["diary"])`
  - `self.assertEqual(asyncio.run(db_engine.get_cognitive_feedback("", limit=5)), [])`

### test_memory_engine_disable_cognitive_feedback_filters_and_cleans_ttl
Summary: Tests memory engine disable cognitive feedback filters and cleans ttl
Asserts:
  - `self.assertNotIn(old_key, engine._disabled_cognitive_feedback_keys)`
  - `self.assertIn(engine._cognitive_feedback_key_str(signal), engine._disabled_cognitive_feedback_keys)`

### test_memory_engine_parse_cognitive_feedback_content
Summary: Tests memory engine parse cognitive feedback content
Asserts:
  - `self.assertIsNotNone(parsed)`
  - `self.assertEqual(parsed.source, "Agency")`
  - `self.assertEqual(parsed.chat_id, "chat-1")`
  - `self.assertEqual(parsed.summary, "Be concise")`
  - `self.assertEqual(parsed.guidance, "Avoid loops")`
  - `self.assertEqual(parsed.tags, ["tone", "direct"])`
  - `self.assertEqual(parsed.timestamp, 123.0)`
  - `self.assertEqual(parsed.importance, 0.8)`
  - `self.assertIsNone(             self.memory_mod.MemoryEngine._parse_cognitive_feedback_content("plain memory", chat_id="chat-1")         )`
  - `self.assertIsNone(             self.memory_mod.MemoryEngine._parse_cognitive_feedback_content(                 "[cognitive_feedback:agency]\ntags: only-tags",                 chat_id="chat-1",             )         )`

### test_memory_engine_store_topic_results_merges_similar_existing_topic
Summary: Tests memory engine store topic results merges similar existing topic
Asserts:
  - `self.assertEqual(len(written), 1)`
  - `self.assertEqual(request.kind, "topic")`
  - `self.assertEqual(request.session_id, "chat-1")`
  - `self.assertEqual(request.persona_id, "persona-1")`
  - `self.assertIn("Supplement:", request.content)`
  - `self.assertEqual(request.metadata["merged_from"], ["topic-old"])`
  - `self.assertEqual(merged, [(["topic-old"], "topic-new")])`

### test_memory_engine_ensure_faiss_initialized_ready_fast_path
Summary: Tests memory engine ensure faiss initialized ready fast path
Asserts:
  - `self.assertTrue(asyncio.run(engine._ensure_faiss_initialized()))`

### test_memory_engine_initialize_wires_services_and_runs_legacy_imports
Summary: Tests memory engine initialize wires services and runs legacy imports
Asserts:
  - `self.assertIs(engine.v2_store.index_projector, engine.index_projector)`
  - `self.assertIsInstance(engine.write_service, _Service)`
  - `self.assertIsInstance(engine.retrieval_service, _Service)`
  - `self.assertEqual(             calls,             [                 "store.initialize",                 "legacy.documents",                 "legacy.persona",                 "legacy.events",                 "legacy.jargons",                 "legacy...`

### test_memory_engine_start_background_tasks_wires_and_starts_pipeline
Summary: Tests memory engine start background tasks wires and starts pipeline
Asserts:
  - `self.assertEqual(started, [engine.memory_pipeline])`
  - `self.assertEqual(engine.memory_observer.args, ("trace-store",))`
  - `self.assertEqual(engine.memory_observer.kwargs["observability_hub"], "observability")`
  - `self.assertIs(engine.memory_pipeline.kwargs["observer"], engine.memory_observer)`
  - `self.assertEqual(engine.memory_pipeline.kwargs["event_bus"], "db-events")`

### test_memory_engine_legacy_imports_mark_applied_without_db_service
Summary: Tests memory engine legacy imports mark applied without db service
Asserts:
  - `self.assertEqual(results, [0, 0, 0])`
  - `self.assertEqual(             [item[0] for item in migrations],             ["2_memory_event_import", "2_jargon_import", "2_expression_pattern_import"],         )`
  - `self.assertTrue(all(item[1]["status"] == "applied" for item in migrations))`

### test_memory_engine_get_recent_memories_filters_feedback_rows
Summary: Tests memory engine get recent memories filters feedback rows
Asserts:
  - `self.assertEqual(result, ["visible memory"])`
  - `self.assertEqual(len(queries), 2)`
  - `self.assertIn("SELECT page_content", queries[1][0])`
  - `self.assertEqual(queries[1][1][0], "chat-1")`
  - `self.assertEqual(queries[1][2], engine.db_path)`

### test_agency_reflection_bridge_flushes_after_threshold
Summary: Tests agency reflection bridge flushes after threshold
Asserts:
  - `self.assertTrue(flushed)`
  - `self.assertEqual(len(memory.calls), 1)`
  - `self.assertEqual(memory.calls[0]["source"], "agency")`
  - `self.assertIn("main_intent=tease", memory.calls[0]["summary"])`
  - `self.assertIn("Avoid repeating", memory.calls[0]["guidance"])`

### test_cognitive_loop_reads_long_term_feedback_in_hidden_prompt
Summary: Tests cognitive loop reads long term feedback in hidden prompt
Asserts:
  - `self.assertIsNotNone(decision)`
  - `self.assertIn("Long-term behavior/memory feedback", gateway.calls[0]["prompt"])`
  - `self.assertIn("avoid repeated meme", gateway.calls[0]["prompt"])`

### test_dream_scheduler_builds_feedback_guidance_from_maintenance_tags
Summary: Tests dream scheduler builds feedback guidance from maintenance tags
Asserts:
  - `self.assertIn("consolidated memory", guidance)`
  - `self.assertIn("stale or noisy", guidance)`
  - `self.assertIn("jargon", guidance)`

### test_dream_scheduler_writes_cognitive_feedback
Summary: Tests dream scheduler writes cognitive feedback
Asserts:
  - `self.assertEqual(len(memory.feedback_calls), 1)`
  - `self.assertEqual(memory.feedback_calls[0]["source"], "dream")`
  - `self.assertIn("consolidated memory", memory.feedback_calls[0]["guidance"])`

### test_diary_service_writes_cognitive_feedback
Summary: Tests diary service writes cognitive feedback
Asserts:
  - `self.assertEqual(len(memory.feedback_calls), 1)`
  - `self.assertEqual(memory.feedback_calls[0]["source"], "diary")`
  - `self.assertIn("quiet diary summary", memory.feedback_calls[0]["summary"])`

### test_diary_service_writes_readable_chinese_memory_labels
Summary: Tests diary service writes readable chinese memory labels
Asserts:
  - `self.assertIn("[你的核心人设]", captured_prompt["prompt"])`
  - `self.assertIn("温柔陪伴型", captured_prompt["prompt"])`
  - `self.assertIn("今天没有显著事件。", captured_prompt["prompt"])`
  - `self.assertEqual(memory.memory_calls[0]["content"], "[内部日记] quiet diary summary")`

### test_diary_service_should_run_covers_full_early_morning_window
Summary: Tests diary service should run covers full early morning window
Asserts:
  - `self.assertTrue(service.should_run("", at_3))`
  - `self.assertTrue(service.should_run("", at_4))`
  - `self.assertFalse(service.should_run("", at_5))`
  - `self.assertFalse(service.should_run("2026-07-03", at_4))`

## tests/test_executor_refactor.py (17 tests)

### test_text_mode_runs_on_dialog_lane_and_records_reply
Summary: Tests text mode runs on dialog lane and records reply
Asserts:
  - `self.assertEqual(result, "lane-text-reply")`
  - `self.assertEqual(len(gateway.calls), 1)`
  - `self.assertEqual(mode, "chat")`
  - `self.assertEqual(kwargs["lane_key"].task_family, "dialog")`
  - `self.assertEqual(kwargs["base_origin"], "default:GroupMessage:group-1")`
  - `self.assertEqual(reply_service.calls, [("default:GroupMessage:group-1", "lane-text-reply")])`
  - `self.assertEqual(             evolution.calls,             [("default:GroupMessage:group-1", "bot-1", "lane-text-reply")],         )`

### test_tool_mode_yield_is_forwarded_as_terminal_content
Summary: Tests tool mode yield is forwarded as terminal content
Asserts:
  - `self.assertEqual(result, "tool-finished")`
  - `self.assertEqual(len(gateway.calls), 1)`
  - `self.assertEqual(mode, "tool")`
  - `self.assertEqual(reply_service.calls, [("default:GroupMessage:group-1", "tool-finished")])`
  - `self.assertEqual(             evolution.calls,             [("default:GroupMessage:group-1", "bot-1", "tool-finished")],         )`

### test_tool_mode_wait_signal_sets_execution_signal_without_visible_reply
Summary: Tests tool mode wait signal sets execution signal without visible reply
Asserts:
  - `self.assertIsNone(result)`
  - `self.assertEqual(event.get_extra("astrmai_execution_signal"), "wait")`
  - `self.assertEqual(reply_service.calls, [])`

### test_chat_tool_tier_limits_runtime_max_steps
Summary: Tests chat tool tier limits runtime max steps
Asserts:
  - `self.assertEqual(runtime["tool_tier"], "chat")`
  - `self.assertEqual(runtime["max_steps"], 2)`

### test_full_and_sys3_tool_tiers_keep_existing_max_steps_rule
Summary: Tests full and sys3 tool tiers keep existing max steps rule
Asserts:
  - `self.assertEqual(full_runtime["tool_tier"], "full")`
  - `self.assertEqual(full_runtime["max_steps"], 5)`
  - `self.assertEqual(sys3_runtime["tool_tier"], "sys3")`
  - `self.assertEqual(sys3_runtime["max_steps"], 5)`

### test_direct_vision_context_is_injected_in_first_person
Summary: Tests direct vision context is injected in first person
Asserts:
  - `self.assertIn("我刚看到一张图片，画面是：一只在窗边打盹的猫。", model_prompt)`
  - `self.assertIn("它给我的感觉是：安静, 柔软。", model_prompt)`
  - `self.assertEqual(system_prompt, "system")`
  - `self.assertNotIn("System note", model_prompt)`
  - `self.assertNotIn("[Vision]", model_prompt)`
  - `self.assertTrue(any(mode == "vision" for mode, _kwargs in gateway.calls))`

### test_direct_vision_context_exception_does_not_delete_original_file
Summary: Tests direct vision context exception does not delete original file
Asserts:
  - `self.assertTrue(os.path.exists(temp_image.name))`

### test_text_mode_switches_model_on_prompt_scaffold_output_and_traces_failure
Summary: Tests text mode switches model on prompt scaffold output and traces failure
Asserts:
  - `self.assertEqual(result, "second-ok")`
  - `self.assertEqual([kwargs["models"][0] for mode, kwargs in gateway.calls if mode == "chat"], ["model-a", "model-b"])`
  - `self.assertTrue(failure_records)`
  - `self.assertEqual(failure_records[0]["failure_kind"], "prompt_scaffold_text")`

### test_text_mode_uses_gateway_cooldown_filtered_agent_models
Summary: Tests text mode uses gateway cooldown filtered agent models
Asserts:
  - `self.assertEqual(result, "second-ok")`
  - `self.assertEqual([kwargs["models"][0] for mode, kwargs in gateway.calls if mode == "chat"], ["model-b"])`

### test_text_mode_records_pool_exhausted_summary_for_invalid_outputs
Summary: Tests text mode records pool exhausted summary for invalid outputs
Asserts:
  - `self.assertIsNone(result)`
  - `self.assertEqual(reply_service.calls, [("default:GroupMessage:group-1", "fallback")])`
  - `self.assertTrue(exhausted)`
  - `self.assertEqual(exhausted[0]["attempted_models"], ["model-a", "model-b"])`
  - `self.assertEqual(exhausted[0]["last_failure_kind"], "provider_failure_text")`
  - `self.assertTrue(exhausted[0]["fallback_triggered"])`

### test_fatal_fallback_skips_visible_reply_for_stale_drop
Summary: Tests fatal fallback skips visible reply for stale drop
Asserts:
  - `self.assertEqual(reply_service.calls, [])`

### test_text_mode_pool_exhausted_trace_includes_gateway_cooldown_skips
Summary: Tests text mode pool exhausted trace includes gateway cooldown skips
Asserts:
  - `self.assertIsNone(result)`
  - `self.assertEqual(exhausted[0]["attempted_models"], ["model-b"])`
  - `self.assertEqual(exhausted[0]["skipped_cooldown_models"][0]["model_id"], "model-a")`
  - `self.assertFalse(exhausted[0]["cooldown_overridden"])`

### test_text_mode_failure_trace_marks_last_attempt_as_no_switch
Summary: Tests text mode failure trace marks last attempt as no switch
Asserts:
  - `self.assertIsNone(result)`
  - `self.assertEqual(len(failure_records), 2)`
  - `self.assertTrue(failure_records[0]["will_retry_or_switch"])`
  - `self.assertFalse(failure_records[1]["will_retry_or_switch"])`

### test_native_main_reply_vision_text_mode_passes_direct_images_without_relay_injection
Summary: Tests native main reply vision text mode passes direct images without relay injection
Asserts:
  - `self.assertEqual(result, "lane-text-reply")`
  - `self.assertEqual(mode, "chat")`
  - `self.assertEqual(kwargs["image_urls"], [temp_image.name])`
  - `self.assertEqual(kwargs["prompt"], "prompt")`
  - `self.assertEqual(event.get_extra("vision_main_reply_strategy"), "native_direct")`
  - `self.assertEqual(event.get_extra("vision_native_direct_outcome"), "success")`
  - `self.assertFalse(any(mode == "vision" for mode, _kwargs in gateway.calls))`

### test_native_main_reply_vision_failure_falls_back_to_relay_and_opens_breaker
Summary: Tests native main reply vision failure falls back to relay and opens breaker
Asserts:
  - `self.assertEqual(result, "lane-text-reply")`
  - `self.assertEqual(len(chat_calls), 2)`
  - `self.assertEqual(chat_calls[0]["image_urls"], [temp_image.name])`
  - `self.assertIsNone(chat_calls[1]["image_urls"])`
  - `self.assertEqual(event.get_extra("vision_main_reply_strategy"), "native_direct")`
  - `self.assertEqual(event.get_extra("vision_native_direct_outcome"), "fallback_to_relay")`
  - `self.assertEqual(event.get_extra("vision_native_direct_fallback_reason"), "provider_failure_text")`
  - `self.assertGreater(float(event.get_extra("vision_native_direct_breaker_until", 0.0) or 0.0), 0.0)`
  - `self.assertTrue(any(mode == "vision" for mode, _kwargs in gateway.calls))`

### test_native_main_reply_vision_tool_mode_passes_direct_images
Summary: Tests native main reply vision tool mode passes direct images
Asserts:
  - `self.assertEqual(result, "tool-finished")`
  - `self.assertEqual(mode, "tool")`
  - `self.assertEqual(kwargs["image_urls"], [temp_image.name])`
  - `self.assertEqual(event.get_extra("vision_native_direct_outcome"), "success")`

### test_native_main_reply_breaker_skips_native_retry_within_same_session
Summary: Tests native main reply breaker skips native retry within same session
Asserts:
  - `self.assertEqual(first_result, "lane-text-reply")`
  - `self.assertEqual(second_result, "lane-text-reply")`
  - `self.assertEqual([mode for mode, _kwargs in additional_calls], ["vision", "chat"])`
  - `self.assertEqual(second_event.get_extra("vision_main_reply_strategy"), "relay")`
  - `self.assertEqual(second_event.get_extra("vision_native_direct_outcome"), "breaker_open")`

## tests/test_context_economy_refactor.py (15 tests)

### test_cache_priority_policy_uses_stable_shell_hash
Summary: Tests cache priority policy uses stable shell hash
Asserts:
  - `self.assertTrue(policy1.cache_priority)`
  - `self.assertEqual(policy1.stable_prefix_hash, policy2.stable_prefix_hash)`
  - `self.assertEqual(policy1.effective_prefix_hash, policy2.effective_prefix_hash)`
  - `self.assertEqual(policy1.primary_model, "model-a")`

### test_dialog_policy_keeps_existing_prefix_hash
Summary: Tests dialog policy keeps existing prefix hash
Asserts:
  - `self.assertFalse(policy.cache_priority)`
  - `self.assertEqual(policy.effective_prefix_hash, "prefix-user")`

### test_cache_priority_lane_prompt_identity_tracks_template_version
Summary: Tests cache priority lane prompt identity tracks template version
Asserts:
  - `self.assertEqual(policy_v1.lane_key.prompt_version, "memory_global_summary:v1:text")`
  - `self.assertEqual(policy_v2.lane_key.prompt_version, "memory_global_summary:v2:text")`
  - `self.assertNotEqual(policy_v1.lane_key.prompt_version, policy_v2.lane_key.prompt_version)`

### test_dialog_lane_prompt_version_is_not_forced_by_template_version
Summary: Tests dialog lane prompt version is not forced by template version
Asserts:
  - `self.assertEqual(policy.lane_key.prompt_version, "v1")`

### test_template_version_change_counts_as_rotate_in_metrics
Summary: Tests template version change counts as rotate in metrics
Asserts:
  - `self.assertTrue(policy_v2.synthetic_lane_rotated)`
  - `self.assertEqual(policy_v2.synthetic_lane_rotate_reason, "template_version_changed")`
  - `self.assertEqual(family_stats["lane_rotate_count"], 1)`
  - `self.assertEqual(family_stats["rotate_reasons"]["template_version_changed"], 1)`
  - `self.assertEqual(template_stats["lane_rotate_count"], 1)`

### test_provider_session_reuse_and_split_rotate_reasons_are_counted_correctly
Summary: Tests provider session reuse and split rotate reasons are counted correctly
Asserts:
  - `self.assertEqual(family_stats["provider_session_usage_rate"], 1.0)`
  - `self.assertEqual(family_stats["provider_session_reuse_rate"], 0.5)`
  - `self.assertEqual(family_stats["rotate_reasons"]["template_version_changed"], 1)`
  - `self.assertEqual(family_stats["rotate_reasons"]["schema_changed"], 1)`

### test_global_scope_fallback_is_marked_for_cache_priority_memory_and_dream
Summary: Tests global scope fallback is marked for cache priority memory and dream
Asserts:
  - `self.assertEqual(policy.lane_scope_id, "global")`
  - `self.assertEqual(policy.cache_affinity_reason, "global_scope_fallback")`

### test_persona_core_identity_template_defaults_to_v3
Summary: Tests persona core identity template defaults to v3
Asserts:
  - `self.assertEqual(envelope.template_id, "persona_core_identity")`
  - `self.assertEqual(envelope.template_version, "v3")`
  - `self.assertEqual(envelope.schema_id, "text")`

### test_prompt_template_registry_accepts_enum_instances_from_previous_module_load
Summary: Tests prompt template registry accepts enum instances from previous module load
Asserts:
  - `self.assertEqual(envelope.template_id, templates_mod.PromptTemplateId.COMPACTION_SUMMARY_V1.value)`
  - `self.assertEqual(envelope.template_version, "v1")`
  - `self.assertIn("line-a", envelope.prompt)`

### test_dream_template_and_fallback_share_stable_system_shell_wording
Summary: Tests dream template and fallback share stable system shell wording
Asserts:
  - `self.assertEqual(             envelope.system_prompt.split("\n\n")[0],             "你是一个善于幻想与创作的写作助手，擅长用诗意的语言描述梦境。",         )`

### test_memory_topic_summary_keeps_segment_count_out_of_system_prompt
Summary: Tests memory topic summary keeps segment count out of system prompt
Asserts:
  - `self.assertNotIn("当前共有 2 个话题段", envelope_two.system_prompt)`
  - `self.assertNotIn("当前共有 5 个话题段", envelope_five.system_prompt)`
  - `self.assertIn("[Segment Count]\n2", envelope_two.prompt)`
  - `self.assertIn("[Segment Count]\n5", envelope_five.prompt)`
  - `self.assertEqual(envelope_two.stable_prefix_text, envelope_five.stable_prefix_text)`

### test_dream_generation_keeps_style_and_persona_name_out_of_system_prompt
Summary: Tests dream generation keeps style and persona name out of system prompt
Asserts:
  - `self.assertNotIn("Mai", envelope_a.system_prompt)`
  - `self.assertNotIn("Astra", envelope_b.system_prompt)`
  - `self.assertNotIn("奇幻冒险", envelope_a.system_prompt)`
  - `self.assertNotIn("安静悬疑", envelope_b.system_prompt)`
  - `self.assertIn("[Persona Name]\nMai", envelope_a.prompt)`
  - `self.assertIn("[Dream Style]\n奇幻冒险", envelope_a.prompt)`
  - `self.assertIn("[Persona Name]\nAstra", envelope_b.prompt)`
  - `self.assertIn("[Dream Style]\n安静悬疑", envelope_b.prompt)`
  - `self.assertEqual(envelope_a.stable_prefix_text, envelope_b.stable_prefix_text)`

### test_cache_priority_hash_stays_stable_when_template_payload_parameters_change
Summary: Tests cache priority hash stays stable when template payload parameters change
Asserts:
  - `self.assertEqual(policy_a.stable_prefix_hash, policy_b.stable_prefix_hash)`
  - `self.assertEqual(len(policy_a.stable_prefix_text), len(policy_b.stable_prefix_text))`
  - `self.assertNotEqual(len(policy_a.dynamic_payload_text), len(policy_b.dynamic_payload_text))`
  - `self.assertEqual(dream_policy_a.stable_prefix_hash, dream_policy_b.stable_prefix_hash)`
  - `self.assertEqual(len(dream_policy_a.stable_prefix_text), len(dream_policy_b.stable_prefix_text))`
  - `self.assertNotEqual(len(dream_policy_a.dynamic_payload_text), len(dream_policy_b.dynamic_payload_text))`

### test_chat_in_lane_result_contains_economy_trace
Summary: Tests chat in lane result contains economy trace
Asserts:
  - `self.assertEqual(result.economy["workload_family"], "chat_dialog")`
  - `self.assertEqual(result.economy["primary_model"], "model-a")`
  - `self.assertIn("stable_prefix_length", result.economy)`
  - `self.assertIn("chat_dialog", stats)`
  - `self.assertEqual(stats["chat_dialog"]["call_count"], 1)`

### test_sticky_router_keeps_primary_model_pinned
Summary: Tests sticky router keeps primary model pinned
Asserts:
  - `self.assertEqual(ranked1[0], "model-a")`
  - `self.assertEqual(ranked2[0], "model-a")`
  - `self.assertEqual(ranked3[0], "model-b")`
  - `self.assertEqual(pool.models["model-a"].cooldown_until, cooldown_until)`
  - `self.assertEqual(ranked4[0], "model-a")`

## tests/test_reply_service_refactor.py (20 tests)

### test_stale_reply_is_still_skipped
Summary: Tests stale reply is still skipped
Asserts:
  - `self.assertEqual(state_engine.gateway.context.sent, [])`
  - `self.assertFalse(event.get_extra("astrmai_reply_sent", False))`

### test_short_multi_sentence_reply_stays_single
Summary: Tests short multi sentence reply stays single
Asserts:
  - `self.assertEqual(len(artifact.segments), 1)`
  - `self.assertEqual(artifact.metadata["segment_reason"], "within_single_limit")`

### test_long_reply_uses_natural_segments_and_caps_at_three
Summary: Tests long reply uses natural segments and caps at three
Asserts:
  - `self.assertGreater(len(artifact.segments), 1)`
  - `self.assertLessEqual(len(artifact.segments), 3)`
  - `self.assertEqual(artifact.metadata["segment_reason"], "natural_segmenter")`

### test_forced_paragraph_boundary_can_split_below_single_limit
Summary: Tests forced paragraph boundary can split below single limit
Asserts:
  - `self.assertEqual(len(artifact.segments), 2)`
  - `self.assertEqual(artifact.metadata["segment_reason"], "natural_segmenter")`

### test_segmenter_preserves_code_url_and_decimal_fragments
Summary: Tests segmenter preserves code url and decimal fragments
Asserts:
  - `self.assertIn("3.14.15", visible)`
  - `self.assertIn("https://example.com/a.b?x=1", visible)`
  - `self.assertIn("```a.b()```", visible)`

### test_reply_modes_apply_human_segment_limits
Summary: Tests reply modes apply human segment limits
Asserts:
  - `self.assertLessEqual(len(emotional.segments), 2)`
  - `self.assertEqual(emotional.metadata["delay_profile"], "gentle")`
  - `self.assertEqual(len(playful.segments), 1)`

### test_proactive_reply_defaults_to_low_segment_count
Summary: Tests proactive reply defaults to low segment count
Asserts:
  - `self.assertLessEqual(len(artifact.segments), 2)`
  - `self.assertEqual(artifact.metadata["delay_profile"], "proactive")`

### test_guarded_stance_clamps_first_reply_length_and_trailing_question
Summary: Tests guarded stance clamps first reply length and trailing question
Asserts:
  - `self.assertTrue(artifact.metadata["stance_clamp_applied"])`
  - `self.assertEqual(artifact.metadata["stance"], "guarded")`
  - `self.assertEqual(artifact.metadata["stance_social_intent"], "answer")`
  - `self.assertLess(len(artifact.visible_text), len(text))`
  - `self.assertNotIn("Do you want me to keep going?", artifact.visible_text)`

### test_neutral_stance_does_not_apply_first_reply_clamp
Summary: Tests neutral stance does not apply first reply clamp
Asserts:
  - `self.assertNotIn("stance_clamp_applied", artifact.metadata)`
  - `self.assertIn("Do you want me to keep going?", artifact.visible_text)`

### test_guarded_boundary_uses_tighter_first_reply_caps_than_guarded_answer
Summary: Tests guarded boundary uses tighter first reply caps than guarded answer
Asserts:
  - `self.assertTrue(boundary_artifact.metadata["stance_clamp_applied"])`
  - `self.assertEqual(boundary_artifact.metadata["stance_social_intent"], "boundary")`
  - `self.assertLess(boundary_artifact.metadata["stance_char_cap"], answer_artifact.metadata["stance_char_cap"])`
  - `self.assertLessEqual(len(boundary_artifact.visible_text), len(answer_artifact.visible_text))`
  - `self.assertLessEqual(boundary_artifact.metadata["stance_sentence_cap"], answer_artifact.metadata["stance_sentence_cap"])`
  - `self.assertEqual(boundary_artifact.metadata["stance_char_cap"], 28)`
  - `self.assertEqual(answer_artifact.metadata["stance_char_cap"], 38)`

### test_cool_comfort_keeps_looser_cap_than_cool_answer_while_trimming_tail_question
Summary: Tests cool comfort keeps looser cap than cool answer while trimming tail question
Asserts:
  - `self.assertTrue(answer_artifact.metadata["stance_clamp_applied"])`
  - `self.assertTrue(comfort_artifact.metadata["stance_clamp_applied"])`
  - `self.assertEqual(comfort_artifact.metadata["stance_social_intent"], "comfort")`
  - `self.assertGreater(comfort_artifact.metadata["stance_char_cap"], answer_artifact.metadata["stance_char_cap"])`
  - `self.assertNotIn("Do you want me to keep going?", comfort_artifact.visible_text)`
  - `self.assertGreaterEqual(len(comfort_artifact.visible_text), len(answer_artifact.visible_text))`
  - `self.assertEqual(answer_artifact.metadata["stance_char_cap"], 60)`
  - `self.assertEqual(comfort_artifact.metadata["stance_char_cap"], 72)`

### test_successful_reply_feeds_memory_buffer_after_send
Summary: Tests successful reply feeds memory buffer after send
Asserts:
  - `self.assertEqual(after_first["reason"], "below_threshold")`
  - `self.assertTrue(after_first["candidate_present"])`
  - `self.assertEqual(after_second["reason"], "eligible")`
  - `self.assertTrue(after_second["eligible"])`

### test_failed_send_does_not_feed_memory_buffer
Summary: Tests failed send does not feed memory buffer
Asserts:
  - `self.assertNotIn(event.unified_msg_origin, pipeline._session_history_buffer)`

### test_failed_send_triggers_light_no_send_affection_settlement
Summary: Tests failed send triggers light no send affection settlement
Asserts:
  - `self.assertEqual(observed["user_id"], "user-1")`
  - `self.assertEqual(observed["group_id"], event.unified_msg_origin)`
  - `self.assertEqual(observed["message_text"], "你这个废物")`
  - `self.assertEqual(observed["skipped_reason"], "send_failed")`

### test_proactive_reply_does_not_feed_memory_buffer
Summary: Tests proactive reply does not feed memory buffer
Asserts:
  - `self.assertNotIn(event.unified_msg_origin, pipeline._session_history_buffer)`

### test_publish_turn_committed_failure_still_keeps_memory_buffer
Summary: Tests publish turn committed failure still keeps memory buffer
Asserts:
  - `self.assertEqual(published_turns, [event.unified_msg_origin])`
  - `self.assertIn(event.unified_msg_origin, pipeline._session_history_buffer)`
  - `self.assertEqual(             pipeline._session_history_buffer[event.unified_msg_origin]["buffer"],             ["用户/旁白：Alice: turn-publish-fail", "Bot：reply-visible"],         )`

### test_instant_gate_failure_does_not_break_visible_reply_or_buffer
Summary: Tests instant gate failure does not break visible reply or buffer
Asserts:
  - `self.assertTrue(event.get_extra("astrmai_reply_sent", False))`
  - `self.assertTrue(sent)`
  - `self.assertIn(event.unified_msg_origin, pipeline._session_history_buffer)`
  - `self.assertEqual(len(pipeline._session_history_buffer[event.unified_msg_origin]["buffer"]), 2)`

### test_post_send_affection_uses_anchor_message_text_for_event_classification
Summary: Tests post send affection uses anchor message text for event classification
Asserts:
  - `self.assertEqual(observed["user_id"], "user-1")`
  - `self.assertEqual(observed["mood_tag"], "happy")`
  - `self.assertEqual(observed["message_text"], "thank you, you are amazing")`

### test_post_send_proactive_event_does_not_mutate_affection_with_synthetic_text
Summary: Tests post send proactive event does not mutate affection with synthetic text
Asserts:
  - `self.assertEqual(observed["calls"], 0)`

### test_merge_wait_targets_preserves_existing_targets_before_pending_actions
Summary: Tests merge wait targets preserves existing targets before pending actions
Asserts:
  - `self.assertEqual(merged, ["user-1", "user-2"])`
  - `self.assertEqual(event.get_extra("astrmai_wait_targets"), ["user-1", "user-2"])`
  - `self.assertEqual(event.get_extra("astrmai_wait_target_name"), "Bob")`

## tests/unit/proactive/test_proactive_gap_coverage.py (17 tests)

### test_dispatcher_blocks_second_intent_during_completion_cooldown
Summary: Tests dispatcher blocks second intent during completion cooldown
Asserts:
  - `self.assertTrue(first.allowed)`
  - `self.assertFalse(second.allowed)`
  - `self.assertEqual(second.blocked_reason, "cooldown")`
  - `self.assertGreater(dispatcher._cooldowns["chat-1"], time.time())`
  - `self.assertLessEqual(dispatcher._cooldowns["chat-1"], time.time() + 60)`

### test_dispatcher_allows_intent_after_epoch_cooldown_expires
Summary: Tests dispatcher allows intent after epoch cooldown expires
Asserts:
  - `self.assertTrue(decision.allowed)`
  - `self.assertEqual(decision.blocked_reason, "")`
  - `self.assertNotIn("chat-expired", dispatcher._cooldowns)`

### test_dispatcher_blocks_conservatively_on_dirty_pending_snapshot
Summary: Tests dispatcher blocks conservatively on dirty pending snapshot
Asserts:
  - `self.assertFalse(decision.allowed)`
  - `self.assertEqual(decision.blocked_reason, "user_waiting")`

### test_wakeup_intent_preserves_dispatch_contract
Summary: Tests wakeup intent preserves dispatch contract
Asserts:
  - `self.assertEqual(intent.chat_id, "group:10001")`
  - `self.assertEqual(intent.source, "wakeup")`
  - `self.assertEqual(intent.cost, 5.0)`
  - `self.assertEqual(intent.cooldown, 90.0)`
  - `self.assertEqual(intent.metadata, {"group_id": "group:10001"})`

### test_wakeup_intent_rejects_blank_guidance
Summary: Tests wakeup intent rejects blank guidance
Asserts:
  - `self.assertIsNone(intent)`

### test_tick_chat_degrades_when_runtime_snapshot_lookup_fails
Summary: Tests tick chat degrades when runtime snapshot lookup fails
Asserts:
  - `self.assertFalse(payload["eligible"])`
  - `self.assertEqual(payload["blocked_reason"], "snapshot_unavailable")`

### test_tick_chat_tolerates_dirty_snapshot_counters
Summary: Tests tick chat tolerates dirty snapshot counters
Asserts:
  - `self.assertTrue(payload["performed"])`
  - `self.assertEqual(payload["action_type"], "wait")`
  - `self.assertEqual(payload["blocked_reason"], "user_waiting")`
  - `self.assertIsNotNone(manager.get_state("chat-3"))`
  - `self.assertIsNotNone(manager.get_session("chat-3"))`

### test_proactive_task_maintenance_cycle_isolates_subservice_failures
Summary: Tests proactive task maintenance cycle isolates subservice failures
Asserts:
  - `self.assertEqual(calls, ["decay", "signin", ("digest", "heartflow")])`
  - `self.assertEqual(task._background_tasks, set())`

### test_proactive_task_stop_cancels_loop_and_background_tasks
Summary: Tests proactive task stop cancels loop and background tasks
Asserts:
  - `self.assertFalse(task._is_running)`
  - `self.assertIsNone(task._task)`
  - `self.assertTrue(loop_task.cancelled())`
  - `self.assertTrue(background_task.cancelled())`
  - `self.assertEqual(task._background_tasks, set())`

### test_proactive_task_scheduler_poll_modes_set_expected_intervals
Summary: Tests proactive task scheduler poll modes set expected intervals
Asserts:
  - `self.assertEqual(task._scheduler_poll_mode, "FAST")`
  - `self.assertEqual(             task._scheduler_poll_interval_seconds,             task.FAST_POLL_INTERVAL_SECONDS,         )`
  - `self.assertEqual(task._scheduler_poll_mode, "NORMAL")`
  - `self.assertEqual(             task._scheduler_poll_interval_seconds,             task.NORMAL_POLL_INTERVAL_SECONDS,         )`
  - `self.assertEqual(task._scheduler_poll_mode, "IDLE")`
  - `self.assertEqual(             task._scheduler_poll_interval_seconds,             task.IDLE_POLL_INTERVAL_SECONDS,         )`

### test_proactive_task_loads_selected_persona_summary_from_sync_cache
Summary: Tests proactive task loads selected persona summary from sync cache
Asserts:
  - `self.assertEqual(summary, "A calm and curious persona.")`

### test_proactive_task_binds_dream_dependencies
Summary: Tests proactive task binds dream dependencies
Asserts:
  - `self.assertIsInstance(task.dream_agent, _DreamAgent)`
  - `self.assertIs(captured["agent_kwargs"]["gateway"], task.gateway)`
  - `self.assertIs(captured["agent_kwargs"]["db_service"], task._db_service)`
  - `self.assertIs(captured["agent_kwargs"]["memory_engine"], task.memory_engine)`
  - `self.assertIs(captured["promotion_engine_memory"], task.memory_engine)`
  - `self.assertIs(captured["bind_args"][0], task.dream_agent)`
  - `self.assertIs(captured["bind_args"][1], task.dream_generator)`
  - `self.assertIs(captured["bind_kwargs"]["db_service"], task._db_service)`
  - `self.assertIsInstance(captured["bind_kwargs"]["promotion_engine"], _PromotionEngine)`

### test_proactive_task_generates_and_persists_persona_analysis
Summary: Tests proactive task generates and persists persona analysis
Asserts:
  - `self.assertEqual(profile.persona_analysis, "thoughtful")`
  - `self.assertEqual(profile.tags, ["curious"])`
  - `self.assertEqual(profile.identity_points, ["likes puzzles"])`
  - `self.assertEqual(profile.message_count_for_profiling, 0)`
  - `self.assertTrue(profile.is_dirty)`
  - `self.assertEqual(saved, [profile])`
  - `self.assertEqual(args[2], "profile:Alice:persona summary")`

### test_proactive_task_generates_and_persists_nickname
Summary: Tests proactive task generates and persists nickname
Asserts:
  - `self.assertEqual(profile.nickname, "小艾")`
  - `self.assertEqual(profile.nickname_reason, "friendly")`
  - `self.assertTrue(profile.is_known)`
  - `self.assertTrue(profile.is_dirty)`
  - `self.assertEqual(saved, [profile])`
  - `self.assertEqual(args[2], "nickname:Alice:persona summary")`

### test_proactive_task_runs_reflection_pipeline_for_active_chats
Summary: Tests proactive task runs reflection pipeline for active chats
Asserts:
  - `self.assertEqual(                 calls,                 [                     ("reflect", "chat-1"),                     ("audit", "chat-1"),                     ("check", "chat-1"),                     ("dispatch", None),                 ],     ...`

### test_proactive_task_runs_group_profile_learning_and_generation
Summary: Tests proactive task runs group profile learning and generation
Asserts:
  - `self.assertEqual(                 calls,                 [                     ("select", "GroupMessage:group-1"),                     (                         "touch",                         "user-1",                         {                  ...`

### test_preview_chat_uses_epoch_clock_for_stale_snapshot
Summary: Tests preview chat uses epoch clock for stale snapshot
Asserts:
  - `self.assertFalse(payload["eligible"])`
  - `self.assertEqual(payload["action_type"], "")`

## tests/test_state_services_refactor.py (15 tests)

### test_update_mood_keeps_delta_contract
Summary: Tests update mood keeps delta contract
Asserts:
  - `self.assertEqual(tag, "happy")`
  - `self.assertAlmostEqual(final_mood, 0.4)`
  - `self.assertEqual(observed["chat_id"], "chat-1")`

### test_consume_energy_skips_private_chat_by_design
Summary: Tests consume energy skips private chat by design
Asserts:
  - `self.assertEqual(private_before, 0.5)`
  - `self.assertEqual(private_after, 0.5)`
  - `self.assertEqual(group_before, 0.5)`
  - `self.assertAlmostEqual(group_after, 0.3)`
  - `self.assertEqual(             [chat_id for chat_id, _, _ in persistence.saved_chat_states],             ["default:GroupMessage:group-1"],         )`

### test_should_drop_by_energy_persists_recovery_side_effect
Summary: Tests should drop by energy persists recovery side effect
Asserts:
  - `self.assertTrue(dropped)`
  - `self.assertAlmostEqual(state.energy, 0.25)`
  - `self.assertFalse(state.is_dirty)`
  - `self.assertEqual(             [chat_id for chat_id, _, _ in persistence.saved_chat_states],             ["default:GroupMessage:group-drop"],         )`

### test_update_mood_snapshot_does_not_mutate_live_state_before_analysis
Summary: Tests update mood snapshot does not mutate live state before analysis
Asserts:
  - `self.assertEqual(tag, "happy")`
  - `self.assertAlmostEqual(final_mood, 0.6)`
  - `self.assertAlmostEqual(observed["energy_during_analysis"], 0.4)`
  - `self.assertFalse(observed["dirty_during_analysis"])`
  - `self.assertAlmostEqual(observed["saved_energy"], 0.5)`
  - `self.assertAlmostEqual(live_state.energy, 0.5)`

### test_settle_no_send_affection_only_updates_negative_interactions
Summary: Tests settle no send affection only updates negative interactions
Asserts:
  - `self.assertFalse(neutral)`
  - `self.assertTrue(negative)`
  - `self.assertLess(profile.social_score, 0.0)`
  - `self.assertEqual(len(observed), 1)`
  - `self.assertEqual(observed[0][-1], self.state_mod.RelationshipEvent.INSULT)`

### test_mood_manager_uses_lane_raw_text_when_parsed_json_is_empty
Summary: Tests mood manager uses lane raw text when parsed json is empty
Asserts:
  - `self.assertEqual(tag, "sad")`
  - `self.assertAlmostEqual(mood_value, -0.35)`

### test_mood_manager_fallback_keeps_sarcasm_negative
Summary: Tests mood manager fallback keeps sarcasm negative
Asserts:
  - `self.assertEqual(tag, "angry")`
  - `self.assertLess(mood_value, 0.0)`

### test_mood_manager_fallback_does_not_flatten_mixed_affect_to_happy
Summary: Tests mood manager fallback does not flatten mixed affect to happy
Asserts:
  - `self.assertEqual(tag, "sad")`
  - `self.assertLess(mood_value, 0.0)`

### test_mood_manager_fallback_keeps_tool_intent_questions_neutral
Summary: Tests mood manager fallback keeps tool intent questions neutral
Asserts:
  - `self.assertEqual(tag, "neutral")`
  - `self.assertEqual(mood_value, 0.0)`

### test_calculate_and_update_affection_keeps_mixed_affect_from_support_uplift
Summary: Tests calculate and update affection keeps mixed affect from support uplift
Asserts:
  - `self.assertGreater(profile.social_score, 0.0)`
  - `self.assertLess(profile.social_score, 1.0)`

### test_calculate_and_update_affection_softens_comfort_with_complaint
Summary: Tests calculate and update affection softens comfort with complaint
Asserts:
  - `self.assertGreater(profile.social_score, 0.0)`
  - `self.assertLessEqual(profile.social_score, 0.4)`

### test_calculate_and_update_affection_publishes_effective_mood_and_event
Summary: Tests calculate and update affection publishes effective mood and event
Asserts:
  - `self.assertEqual(observed["mood_tag"], "")`
  - `self.assertEqual(observed["event_type"], self.state_mod.RelationshipEvent.NORMAL_CHAT)`

### test_relationship_engine_classifies_ambiguous_and_cold_boundaries_conservatively
Summary: Tests relationship engine classifies ambiguous and cold boundaries conservatively
Asserts:
  - `self.assertEqual(             engine.classify_interaction_type("晚安呀，早点休息，别太累了。"),             self.state_mod.RelationshipEvent.GREETING,         )`
  - `self.assertEqual(             engine.classify_interaction_type("哦，那你先忙吧，我就不打扰了。"),             self.state_mod.RelationshipEvent.IGNORE,         )`
  - `self.assertEqual(             engine.classify_interaction_type("哦，行吧，就这样。"),             self.state_mod.RelationshipEvent.IGNORE,         )`
  - `self.assertEqual(             engine.classify_interaction_type("行了，别说了，我知道了。"),             self.state_mod.RelationshipEvent.IGNORE,         )`

### test_normal_chat_bias_keeps_tool_intent_above_mixed_affect
Summary: Tests normal chat bias keeps tool intent above mixed affect
Asserts:
  - `self.assertGreaterEqual(mixed_score, 0.2)`
  - `self.assertLessEqual(mixed_score, 0.4)`
  - `self.assertGreaterEqual(tool_score, 0.3)`
  - `self.assertLessEqual(tool_score, 0.45)`
  - `self.assertGreater(tool_score, mixed_score)`

### test_ignore_bias_creates_three_non_hostile_negative_tiers
Summary: Tests ignore bias creates three non hostile negative tiers
Asserts:
  - `self.assertGreater(cold_score, perfunctory_score)`
  - `self.assertGreater(perfunctory_score, irritation_score)`
  - `self.assertGreaterEqual(cold_score, -0.30)`
  - `self.assertLessEqual(cold_score, -0.20)`
  - `self.assertGreaterEqual(perfunctory_score, -0.40)`
  - `self.assertLessEqual(perfunctory_score, -0.30)`
  - `self.assertGreaterEqual(irritation_score, -0.55)`
  - `self.assertLessEqual(irritation_score, -0.40)`

## tests/unit/memory/test_memory_gap_coverage.py (13 tests)

### test_retrieve_deep_falls_back_to_single_query_when_rewrite_fails
Summary: Tests retrieve deep falls back to single query when rewrite fails
Asserts:
  - `self.assertEqual([item.id for item in result], ["fallback-1"])`
  - `self.assertEqual([call[0] for call in store.search_calls], ["Alice project"])`

### test_session_summarizer_skips_low_importance_without_writing_memory
Summary: Tests session summarizer skips low importance without writing memory
Asserts:
  - `self.assertTrue(any(item["stage"] == "summarize_skipped" for item in observed))`
  - `self.assertTrue(any(item.get("reason") == "low_importance" for item in observed))`

### test_index_projector_tracks_failed_projection_as_missing_until_repaired
Summary: Tests index projector tracks failed projection as missing until repaired
Asserts:
  - `self.assertFalse(failed)`
  - `self.assertEqual(report["missing_projection_ids"], ["mem-1"])`
  - `self.assertEqual(repaired["rebuilt_missing"], 1)`
  - `self.assertEqual(len(retriever.added), 1)`

### test_hybrid_retriever_ignores_bad_metadata_json_without_dropping_result
Summary: Tests hybrid retriever ignores bad metadata json without dropping result
Asserts:
  - `self.assertEqual(len(weighted), 1)`
  - `self.assertEqual(weighted[0].metadata, {})`
  - `self.assertGreater(weighted[0].score, 0.0)`

### test_memory_injection_light_think_without_memory_intent_records_skip_state
Summary: Tests memory injection light think without memory intent records skip state
Asserts:
  - `self.assertEqual(bundle.skip_reason, "think_level_1_no_memory_intent")`
  - `self.assertEqual(event.get_extra("astrmai_memory_injection_trace").skip_reason, "think_level_1_no_memory_intent")`
  - `self.assertEqual(turn_context.memory.skip_reason, "think_level_1_no_memory_intent")`

### test_memory_migration_verify_counts_index_and_legacy_anomalies
Summary: Tests memory migration verify counts index and legacy anomalies
Asserts:
  - `self.assertEqual(report["legacy"]["unmapped_memory_events"], 2)`
  - `self.assertEqual(report["legacy"]["unmapped_jargons"], 1)`
  - `self.assertEqual(report["legacy"]["unmapped_expression_patterns"], 3)`
  - `self.assertEqual(report["jargon"]["active_missing_projection"], 1)`
  - `self.assertEqual(report["jargon"]["visibility_anomalies"], 1)`
  - `self.assertEqual(report["jargon"]["pending_human_without_review_suggestion"], 1)`
  - `self.assertEqual(report["expression_pattern"]["missing_situation"], 1)`

### test_memory_engine_add_memory_builds_legacy_write_request
Summary: Tests memory engine add memory builds legacy write request
Asserts:
  - `self.assertEqual(result, "mem-written")`
  - `self.assertEqual(request.source, "legacy_add_memory")`
  - `self.assertEqual(request.kind, "memory")`
  - `self.assertEqual(request.session_id, "chat-1")`
  - `self.assertEqual(request.persona_id, "persona-1")`
  - `self.assertEqual(request.sender_id, "user-1")`
  - `self.assertEqual(request.content, "remember this")`
  - `self.assertEqual(request.importance, 0.9)`
  - `self.assertEqual(request.source_ref, "memory_engine.add_memory")`

### test_memory_engine_search_memories_returns_empty_when_faiss_initialization_fails
Summary: Tests memory engine search memories returns empty when faiss initialization fails
Asserts:
  - `self.assertEqual(result, [])`

### test_memory_engine_refresh_config_updates_embedding_models_and_invalidates_vector_state
Summary: Tests memory engine refresh config updates embedding models and invalidates vector state
Asserts:
  - `self.assertIs(engine.config, new_config)`
  - `self.assertEqual(engine.embedding_models, ["ed-new"])`
  - `self.assertIsNone(engine.faiss_db)`
  - `self.assertIsNone(engine.vec_retriever)`
  - `self.assertIsNone(engine.retriever)`
  - `self.assertFalse(engine._is_ready)`
  - `self.assertEqual(engine._init_failures, 0)`
  - `self.assertEqual(engine._next_retry_time, 0.0)`
  - `self.assertFalse(engine._index_consistency_repaired)`
  - `self.assertTrue(engine._force_index_rebuild)`

### test_memory_engine_refresh_config_can_clear_embedding_models
Summary: Tests memory engine refresh config can clear embedding models
Asserts:
  - `self.assertEqual(engine.embedding_models, [])`
  - `self.assertTrue(engine._force_index_rebuild)`

### test_memory_engine_vector_index_reset_only_removes_rebuildable_index_file
Summary: Tests memory engine vector index reset only removes rebuildable index file
Asserts:
  - `self.assertFalse(index_path.exists())`
  - `self.assertTrue(docs_path.exists())`

### test_memory_engine_recall_query_and_search_render_retrieval_results
Summary: Tests memory engine recall query and search render retrieval results
Asserts:
  - `self.assertEqual(recalled, "chat-1|mem-1")`
  - `self.assertEqual(queried, "chat-2|mem-1")`
  - `self.assertEqual(searched, "chat-3|mem-1")`
  - `self.assertEqual(first_query.query, "hello")`
  - `self.assertEqual(first_query.persona_id, "persona-1")`
  - `self.assertEqual(first_query.layers, ["memory"])`
  - `self.assertEqual(first_query.top_k, 2)`
  - `self.assertEqual(first_query.exclude_kinds, ["feedback"])`

### test_memory_engine_recall_returns_no_result_message_when_empty
Summary: Tests memory engine recall returns no result message when empty
Asserts:
  - `self.assertEqual(result, "No relevant memory found for 'missing'.")`

## tests/test_gateway_context_passthrough_refactor.py (8 tests)

### test_chat_in_lane_reuses_history_as_contexts
Summary: Tests chat in lane reuses history as contexts
Asserts:
  - `self.assertEqual(len(fake_context.calls), 2)`
  - `self.assertEqual(fake_context.calls[0]["contexts"], [])`
  - `self.assertEqual(len(fake_context.calls[1]["contexts"]), 2)`
  - `self.assertEqual(fake_context.calls[1]["system_prompt"], "stable prompt")`

### test_chat_in_lane_result_records_request_trace_on_event
Summary: Tests chat in lane result records request trace on event
Asserts:
  - `self.assertEqual(result.text, "ok")`
  - `self.assertTrue(request_trace["gateway_system_hash"])`
  - `self.assertTrue(request_trace["gateway_prompt_hash"])`
  - `self.assertEqual(request_trace["request_provider_family"], "anthropic")`
  - `self.assertEqual(request_trace["request_model_id"], "claude-3-5-sonnet")`
  - `self.assertEqual(request_trace["request_cache_control"], '{"type": "ephemeral"}')`
  - `self.assertEqual(request_trace["usage_input_tokens"], 10)`
  - `self.assertEqual(request_trace["usage_input_cached"], 6)`
  - `self.assertTrue(request_trace["provider_visible_system_hash"])`
  - `self.assertTrue(request_trace["provider_visible_prompt_hash"])`

### test_tool_chat_in_lane_passes_image_urls_to_tool_loop_agent
Summary: Tests tool chat in lane passes image urls to tool loop agent
Asserts:
  - `self.assertEqual(len(fake_context.calls), 1)`
  - `self.assertEqual(fake_context.calls[0]["image_urls"], ["https://example.com/vision.jpg"])`

### test_tool_chat_in_lane_passthroughs_terminal_yield_protocol
Summary: Tests tool chat in lane passthroughs terminal yield protocol
Asserts:
  - `self.assertEqual(result.text, "[TERMINAL_YIELD]: tool-finished")`
  - `self.assertEqual(conversation.history[-1]["content"], "tool-finished")`

### test_tool_chat_in_lane_retries_wrapped_provider_failure_text
Summary: Tests tool chat in lane retries wrapped provider failure text
Asserts:
  - `self.assertEqual(result.text, "tool-ok")`
  - `self.assertEqual([call["chat_provider_id"] for call in fake_context.calls], ["model-a", "model-b"])`
  - `self.assertEqual(failures[0]["failure_kind"], "provider_failure_text")`
  - `self.assertEqual(failures[0]["attempted_models"], ["model-a"])`
  - `self.assertIn("All chat models failed", failures[0]["raw_completion"])`
  - `self.assertTrue(failures[0]["will_retry_or_switch"])`
  - `self.assertGreater(failures[0]["model_cooldown_until"], 0)`
  - `self.assertEqual(failures[0]["cooldown_reason"], "quota_exhausted")`

### test_tool_chat_in_lane_skips_cooldown_model_on_next_call
Summary: Tests tool chat in lane skips cooldown model on next call
Asserts:
  - `self.assertEqual(first_result.text, "tool-ok")`
  - `self.assertEqual(second_result.text, "tool-ok")`
  - `self.assertEqual([call["chat_provider_id"] for call in fake_context.calls], ["model-a", "model-b", "model-b"])`
  - `self.assertEqual(successes[0]["skipped_cooldown_models"][0]["model_id"], "model-a")`
  - `self.assertFalse(successes[0]["cooldown_overridden"])`

### test_tool_chat_in_lane_overrides_when_all_models_are_cooled
Summary: Tests tool chat in lane overrides when all models are cooled
Asserts:
  - `self.assertEqual(result.text, "tool-ok")`
  - `self.assertEqual(len(fake_context.calls), 1)`
  - `self.assertTrue(successes[0]["cooldown_overridden"])`
  - `self.assertEqual(             [item["model_id"] for item in successes[0]["skipped_cooldown_models"]],             ["model-a", "model-b"],         )`

### test_get_agent_models_filters_runtime_cooldown_for_executor_entrypoint
Summary: Tests get agent models filters runtime cooldown for executor entrypoint
Asserts:
  - `self.assertEqual(models, ["model-b"])`
  - `self.assertEqual(             gateway._last_agent_model_selection["skipped_cooldown_models"][0]["model_id"],             "model-a",         )`
  - `self.assertFalse(gateway._last_agent_model_selection["cooldown_overridden"])`

## tests/test_context_economy_benchmark_refactor.py (10 tests)

### test_sample_store_appends_and_reads_jsonl
Summary: Tests sample store appends and reads jsonl
Asserts:
  - `self.assertEqual(len(items), 1)`
  - `self.assertEqual(items[0]["source_run_id"], "run-1")`
  - `self.assertEqual(items[0]["template_id"], "memory_global_summary")`

### test_aggregate_samples_tracks_reuse_rotate_and_problem_templates
Summary: Tests aggregate samples tracks reuse rotate and problem templates
Asserts:
  - `self.assertEqual(overview["call_count"], 3)`
  - `self.assertEqual(overview["total_tokens"], 99)`
  - `self.assertEqual(family["provider_session_reuse_rate"], 0.5)`
  - `self.assertEqual(template["rotate_reasons"]["template_version_changed"], 1)`
  - `self.assertEqual(template["rotate_reasons"]["schema_changed"], 1)`
  - `self.assertEqual(summary["high_rotate_templates"][0]["template_key"], "persona_core_identity@v2")`
  - `self.assertEqual(summary["low_reuse_templates"][0]["template_key"], "persona_core_identity@v2")`
  - `self.assertEqual(summary["high_traffic_templates"][0]["template_key"], "memory_global_summary@v1")`

### test_gateway_success_records_benchmark_sample
Summary: Tests gateway success records benchmark sample
Asserts:
  - `self.assertTrue(result.ok)`
  - `self.assertEqual(len(items), 1)`
  - `self.assertEqual(items[0]["source_run_id"], "run-2")`
  - `self.assertEqual(items[0]["workload_family"], "memory_global_summary")`
  - `self.assertEqual(items[0]["input_tokens"], 12)`
  - `self.assertEqual(items[0]["cached_input_tokens"], 6)`
  - `self.assertEqual(items[0]["total_tokens"], 16)`

### test_runner_writes_json_and_markdown_artifacts
Summary: Tests runner writes json and markdown artifacts
Asserts:
  - `self.assertTrue((run_dir / "samples_meta.json").exists())`
  - `self.assertTrue((run_dir / "benchmark_summary.json").exists())`
  - `self.assertIn("Context Economy Benchmark Baseline", markdown)`
  - `self.assertIn("High Rotate Templates", markdown)`

### test_replay_seed_builder_ignores_wakeup_guidance_template
Summary: Tests replay seed builder ignores wakeup guidance template
Asserts:
  - `self.assertTrue(any(item["template_id"] == "chat_dialog" for item in samples))`
  - `self.assertFalse(any(item["template_id"] == "proactive_wakeup_opening" for item in samples))`
  - `self.assertIn("persona_version", versions)`

### test_replay_seed_builder_groups_dialog_sessions_by_private_vs_group
Summary: Tests replay seed builder groups dialog sessions by private vs group
Asserts:
  - `self.assertEqual(private_sessions, {"chat-private-run-2"})`
  - `self.assertEqual(group_sessions, {"chat-group-run-2"})`

### test_replay_seed_builder_reuses_global_persona_session_across_runs
Summary: Tests replay seed builder reuses global persona session across runs
Asserts:
  - `self.assertTrue(persona_samples)`
  - `self.assertEqual(persona_sessions, {"persona-global"})`

### test_replay_seed_builder_reuses_shared_memory_session_across_runs
Summary: Tests replay seed builder reuses shared memory session across runs
Asserts:
  - `self.assertTrue(memory_samples)`
  - `self.assertEqual(memory_sessions, {"memory-shared"})`

### test_replay_seed_builder_reuses_shared_dream_session_across_runs
Summary: Tests replay seed builder reuses shared dream session across runs
Asserts:
  - `self.assertTrue(dream_samples)`
  - `self.assertEqual(dream_sessions, {"dream-shared"})`

### test_replay_seed_builder_reuses_shared_compaction_session_across_runs
Summary: Tests replay seed builder reuses shared compaction session across runs
Asserts:
  - `self.assertTrue(compaction_samples)`
  - `self.assertEqual(compaction_sessions, {"compaction-shared"})`

## tests/test_gateway_vision_refactor.py (9 tests)

### test_call_vision_task_retries_within_vision_pool_only
Summary: Tests call vision task retries within vision pool only
Asserts:
  - `self.assertEqual(result["description"], "a cat on the desk")`
  - `self.assertEqual([call["chat_provider_id"] for call in context.calls], ["vision-a", "vision-b"])`

### test_call_vision_task_skips_cooled_vision_model
Summary: Tests call vision task skips cooled vision model
Asserts:
  - `self.assertEqual(result["description"], "a cat on the desk")`
  - `self.assertEqual([call["chat_provider_id"] for call in context.calls], ["vision-b"])`

### test_call_vision_task_exhaustion_mentions_skipped_cooldown_models
Summary: Tests call vision task exhaustion mentions skipped cooldown models
Asserts:
  - `self.assertIn("skipped_cooldown_models", str(caught.exception))`
  - `self.assertIn("vision-a", str(caught.exception))`
  - `self.assertEqual([call["chat_provider_id"] for call in context.calls], ["vision-b"])`
  - `self.assertRaises(Exception)`

### test_judge_and_mood_tasks_use_task_pool_and_workload_families
Summary: Tests judge and mood tasks use task pool and workload families
Asserts:
  - `self.assertEqual(judge, {"ok": "task", "models": ["task-a", "task-b"]})`
  - `self.assertEqual(mood, {"ok": "task", "models": ["task-a", "task-b"]})`
  - `self.assertEqual(             [request["family"] for request in gateway.context_economy.requests],             [context_mod.WorkloadFamily.JUDGE, context_mod.WorkloadFamily.MOOD],         )`
  - `self.assertTrue(all(call[1]["is_json"] for call in gateway.elastic_calls))`

### test_data_process_and_proactive_tasks_dispatch_to_lane_or_elastic
Summary: Tests data process and proactive tasks dispatch to lane or elastic
Asserts:
  - `self.assertEqual(lane_json, {"lane": True})`
  - `self.assertEqual(elastic_text, "elastic text")`
  - `self.assertEqual(proactive_lane, "lane text")`
  - `self.assertEqual(proactive_elastic, "elastic text")`
  - `self.assertEqual(gateway.lane_calls[0]["lane_key"], lane_key)`
  - `self.assertTrue(gateway.lane_calls[0]["is_json"])`
  - `self.assertFalse(gateway.lane_calls[1]["is_json"])`
  - `self.assertEqual(len(gateway.elastic_calls), 2)`

### test_persona_task_dispatches_json_and_text_modes
Summary: Tests persona task dispatches json and text modes
Asserts:
  - `self.assertEqual(json_result, {"persona": True})`
  - `self.assertEqual(text_result, "persona text")`
  - `self.assertEqual(gateway.context_economy.requests[0]["family"], context_mod.WorkloadFamily.PERSONA_SUMMARY)`
  - `self.assertEqual(gateway.context_economy.requests[0]["persona_id"], "persona-1")`

### test_normalize_vision_failure_reason_handles_empty_and_valid_payloads
Summary: Tests normalize vision failure reason handles empty and valid payloads
Asserts:
  - `self.assertEqual(gateway._normalize_vision_failure_reason({}), (False, "empty_result"))`
  - `self.assertEqual(gateway._normalize_vision_failure_reason(None), (False, "empty_result"))`
  - `self.assertEqual(             gateway._normalize_vision_failure_reason({"description": None}),             (False, "empty_description"),         )`
  - `self.assertEqual(             gateway._normalize_vision_failure_reason({"description": " none "}),             (False, "empty_description"),         )`
  - `self.assertEqual(             gateway._normalize_vision_failure_reason(                 {"description": "a cat", "emotion_tags": [" calm ", "curious"]}             ),             (True, ""),         )`
  - `self.assertEqual(             gateway._normalize_vision_failure_reason(                 {"description": "provider failed before inference"}             ),             (False, "provider_failure_text"),         )`
  - `self.assertEqual(             gateway._normalize_vision_failure_reason(                 {"description": "a cat", "emotion_tags": None}             ),             (True, ""),         )`
  - `self.assertEqual(             gateway._normalize_vision_failure_reason(                 {"description": "a cat", "emotion_tags": [" ", ""]}             ),             (False, "invalid_emotion_tags"),         )`
  - `self.assertEqual(             gateway._normalize_vision_failure_reason(                 {"description": "a cat", "emotion_tags": ["provider failed"]}             ),             (False, "provider_failure_text"),         )`
  - `self.assertEqual(             gateway._normalize_vision_failure_reason(                 {"description": "a cat", "emotion_tags": "provider failed"}             ),             (False, "provider_failure_text"),         )`
  - `self.assertEqual(             gateway._normalize_vision_failure_reason(                 {"description": "a cat", "emotion_tags": {"calm": True}}             ),             (False, "invalid_emotion_tags"),         )`

### test_call_vision_task_uses_elastic_path_without_lane_manager
Summary: Tests call vision task uses elastic path without lane manager
Asserts:
  - `self.assertEqual(result["description"], "a cat")`
  - `self.assertEqual(len(gateway.elastic_calls), 1)`
  - `self.assertEqual(gateway.elastic_calls[0]["models"], ["vision-a"])`
  - `self.assertEqual(gateway.elastic_calls[0]["image_urls"], ["image-data"])`
  - `self.assertEqual(             gateway.context_economy.requests[0]["family"],             context_mod.WorkloadFamily.VISION,         )`

### test_get_agent_models_combines_router_rankings_and_records_filter_state
Summary: Tests get agent models combines router rankings and records filter state
Asserts:
  - `self.assertEqual(result, ["agent-b", "fallback-a"])`
  - `self.assertEqual(             gateway.router.calls,             [                 ("agent", ["agent-a", "agent-b"]),                 ("fallback", ["fallback-a", "agent-a"]),             ],         )`
  - `self.assertEqual(             gateway.filter_calls,             [                 (                     "agent",                     ["agent-b", "agent-a"],                     ["agent-b", "agent-a", "fallback-a"],                 )             ],...`
  - `self.assertEqual(             gateway._last_agent_model_selection,             {                 "skipped_cooldown_models": [{"model_id": "agent-a"}],                 "cooldown_overridden": True,             },         )`

## tests/original_ported/test_prompt_refiner_lightweight_ported.py (11 tests)

### test_refiner_builds_user_prompt_sections_and_memory_block
Summary: Tests refiner builds user prompt sections and memory block
Asserts:
  - `self.assertEqual(final_system_prompt, "system prompt only")`
  - `self.assertEqual(len(memory_engine.retrieval_calls), 1)`
  - `self.assertIn("---记忆闪回", final_prompt)`
  - `self.assertIn("earlier lore reminder", final_prompt)`
  - `self.assertIn("proactive memory fragment", final_prompt)`
  - `self.assertIn("proactive memory fragment", prompt_envelope.background_memory_block)`
  - `self.assertIn("earlier lore reminder", prompt_envelope.background_memory_block)`
  - `self.assertEqual(prompt_envelope.memory_block, prompt_envelope.background_memory_block)`
  - `self.assertIn("proactive_recall", prompt_envelope.background_memory_sections)`
  - `self.assertIn("memory_injection", prompt_envelope.background_memory_sections)`
  - `self.assertEqual(prompt_envelope.background_memory_sections["proactive_recall"], "proactive memory fragment")`
  - `self.assertIn("earlier lore reminder", prompt_envelope.background_memory_sections["memory_injection"])`
  - `self.assertGreater(prompt_envelope.background_memory_rendered_chars, 0)`
  - `self.assertTrue(memory_decision.injected)`
  - `self.assertEqual(memory_decision.source, "proactive_recall+memory_v2")`
  - `self.assertNotIn("earlier lore reminder", memory_decision.summary_preview)`
  - `self.assertIn("proactive memory fragment", memory_decision.summary_preview)`

### test_refiner_records_no_result_memory_decision
Summary: Tests refiner records no result memory decision
Asserts:
  - `self.assertNotIn("memory v2 reminder", final_prompt)`
  - `self.assertEqual(len(memory_engine.retrieval_calls), 1)`
  - `self.assertFalse(memory_decision.injected)`
  - `self.assertEqual(memory_decision.skip_reason, "no_result")`

### test_refiner_records_disable_and_fast_skip_reasons
Summary: Tests refiner records disable and fast skip reasons
Asserts:
  - `self.assertFalse(disabled_decision.injected)`
  - `self.assertEqual(disabled_decision.skip_reason, "disable_rag_injection")`
  - `self.assertFalse(fast_decision.injected)`
  - `self.assertEqual(fast_decision.skip_reason, "fast_mode")`

### test_refiner_lightweight_event_suppresses_memory_and_proactive_recall
Summary: Tests refiner lightweight event suppresses memory and proactive recall
Asserts:
  - `self.assertNotIn("old proactive memory should not appear", final_prompt)`
  - `self.assertFalse(memory_decision.injected)`
  - `self.assertEqual(memory_decision.skip_reason, "lightweight_event")`

### test_refiner_skips_memory_for_think_level_zero
Summary: Tests refiner skips memory for think level zero
Asserts:
  - `self.assertFalse(memory_decision.injected)`
  - `self.assertEqual(memory_decision.policy, "none")`
  - `self.assertEqual(memory_decision.skip_reason, "think_level_0")`
  - `self.assertNotIn("proactive memory should not appear", final_prompt)`

### test_refiner_skips_level_one_without_memory_intent
Summary: Tests refiner skips level one without memory intent
Asserts:
  - `self.assertFalse(memory_decision.injected)`
  - `self.assertEqual(memory_decision.skip_reason, "think_level_1_no_memory_intent")`
  - `self.assertEqual(memory_engine.retrieval_calls, [])`

### test_refiner_level_one_memory_intent_uses_memory_v2
Summary: Tests refiner level one memory intent uses memory v2
Asserts:
  - `self.assertTrue(memory_decision.injected)`
  - `self.assertEqual(memory_decision.source, "memory_v2")`
  - `self.assertEqual(len(memory_engine.retrieval_calls), 1)`

### test_refiner_level_two_uses_memory_v2_by_default
Summary: Tests refiner level two uses memory v2 by default
Asserts:
  - `self.assertEqual(len(memory_engine.retrieval_calls), 1)`
  - `self.assertEqual(memory_decision.source, "memory_v2")`

### test_refiner_level_two_deep_memory_intent_still_uses_memory_v2
Summary: Tests refiner level two deep memory intent still uses memory v2
Asserts:
  - `self.assertEqual(memory_decision.policy, "deep")`
  - `self.assertEqual(memory_decision.source, "memory_v2")`

### test_refiner_allows_memory_v2_at_think_level_three
Summary: Tests refiner allows memory v2 at think level three
Asserts:
  - `self.assertEqual(len(memory_engine.retrieval_calls), 1)`
  - `self.assertEqual(memory_decision.source, "memory_v2")`

### test_resolve_visual_memory_sanitizes_description_and_tags
Summary: Tests resolve visual memory sanitizes description and tags
Asserts:
  - `self.assertIn("cute cat system", resolved)`
  - `self.assertIn("calm, inject now", resolved)`
  - `self.assertNotIn("<|im_", resolved)`
  - `self.assertNotIn("\n", resolved)`

## tests/test_cognitive_loop_refactor.py (10 tests)

### test_cognitive_loop_rejects_non_readonly_tool_request
Summary: Tests cognitive loop rejects non readonly tool request
Asserts:
  - `self.assertIsNotNone(decision)`
  - `self.assertEqual(decision.action, "reply")`
  - `self.assertEqual(len(gateway.calls), 2)`
  - `self.assertIn("Readonly restriction", gateway.calls[1]["prompt"])`

### test_cognitive_loop_timeout_falls_back_to_none
Summary: Tests cognitive loop timeout falls back to none
Asserts:
  - `self.assertIsNone(decision)`

### test_cognitive_loop_should_run_skips_lightweight_fast_legacy_and_trivial_cases
Summary: Tests cognitive loop should run skips lightweight fast legacy and trivial cases
Asserts:
  - `self.assertFalse(loop.should_run(lightweight_event))`
  - `self.assertFalse(loop.should_run(fast_mode_event))`
  - `self.assertFalse(loop.should_run(core_only_event))`
  - `self.assertTrue(loop.should_run(all_mode_event))`
  - `self.assertFalse(loop.should_run(legacy_wait_event))`
  - `self.assertFalse(loop.should_run(trivial_event))`
  - `self.assertTrue(loop.should_run(complex_short_event))`
  - `self.assertFalse(loop.should_run(budget_zero_event))`
  - `self.assertEqual(budget_zero_event.get_extra("astrmai_cognitive_loop_skipped_reason"), "think_level_0")`
  - `self.assertTrue(loop.should_run(direct_budget_event))`
  - `self.assertTrue(loop.should_run(short_lookup_event))`
  - `self.assertFalse(loop.should_run(broad_cha_event))`
  - `self.assertFalse(loop.should_run(spaced_question_event))`
  - `self.assertFalse(loop.should_run(bare_question_event))`

### test_gate_decision_state_is_written_to_event_and_turn_context
Summary: Tests gate decision state is written to event and turn context
Asserts:
  - `self.assertFalse(gate.should_run)`
  - `self.assertEqual(gate.skip_reason, "empty_message")`
  - `self.assertFalse(event.get_extra("astrmai_cognitive_loop_ran"))`
  - `self.assertEqual(event.get_extra("astrmai_cognitive_loop_skipped_reason"), "empty_message")`
  - `self.assertEqual(event.get_extra("astrmai_cognitive_loop_skip_signals"), ["empty_message"])`
  - `self.assertIsNotNone(turn_context)`
  - `self.assertFalse(turn_context.cognitive.cognitive_loop_ran)`
  - `self.assertEqual(turn_context.cognitive.cognitive_loop_skipped_reason, "empty_message")`
  - `self.assertEqual(turn_context.cognitive.cognitive_loop_skip_signals, ["empty_message"])`

### test_cognitive_loop_skips_readonly_tool_step_below_think_level_three
Summary: Tests cognitive loop skips readonly tool step below think level three
Asserts:
  - `self.assertIsNotNone(decision)`
  - `self.assertEqual(decision.intent, "answer from first pass")`
  - `self.assertEqual(len(gateway.calls), 1)`
  - `self.assertFalse(event.get_extra("astrmai_cognitive_loop_readonly_tools_allowed"))`
  - `self.assertEqual(             event.get_extra("astrmai_cognitive_loop_readonly_skip_reason"),             "think_level_2_blocks_readonly_tool",         )`

### test_cognitive_loop_allows_readonly_tool_step_at_think_level_three
Summary: Tests cognitive loop allows readonly tool step at think level three
Asserts:
  - `self.assertIsNotNone(decision)`
  - `self.assertEqual(decision.intent, "answer after memory")`
  - `self.assertEqual(len(gateway.calls), 2)`
  - `self.assertTrue(event.get_extra("astrmai_cognitive_loop_readonly_tools_allowed"))`
  - `self.assertEqual(event.get_extra("astrmai_cognitive_loop_readonly_skip_reason"), "")`

### test_cognitive_loop_normalizes_agency_fields_and_downgrades_weak_pushback
Summary: Tests cognitive loop normalizes agency fields and downgrades weak pushback
Asserts:
  - `self.assertIsNotNone(decision)`
  - `self.assertEqual(decision.action, "reply")`
  - `self.assertEqual(decision.reply_need, "reply")`
  - `self.assertEqual(decision.memory_policy, "none")`
  - `self.assertEqual(decision.social_intent, "boundary")`
  - `self.assertEqual(decision.action_tier, "chat")`
  - `self.assertEqual(decision.allowed_action_families, ["meme", "poke"])`
  - `self.assertEqual(decision.stance, "guarded")`
  - `self.assertEqual(decision.state_bias, "keep it short")`
  - `self.assertIn("pushback_downgraded", decision.risk_flags)`
  - `self.assertEqual(decision.attack_confidence, 0.4)`

### test_cognitive_loop_allows_high_confidence_pushback
Summary: Tests cognitive loop allows high confidence pushback
Asserts:
  - `self.assertIsNotNone(decision)`
  - `self.assertEqual(decision.social_intent, "pushback")`
  - `self.assertEqual(decision.action_tier, "none")`
  - `self.assertAlmostEqual(decision.attack_confidence, 0.92)`

### test_cognitive_loop_requires_direct_attack_flag_for_pushback
Summary: Tests cognitive loop requires direct attack flag for pushback
Asserts:
  - `self.assertIsNotNone(decision)`
  - `self.assertEqual(decision.social_intent, "boundary")`
  - `self.assertIn("pushback_downgraded", decision.risk_flags)`

### test_cognitive_loop_reads_continuity_from_turn_context
Summary: Tests cognitive loop reads continuity from turn context
Asserts:
  - `self.assertIsNotNone(decision)`
  - `self.assertIn("current_goal=continue the puzzle gently", gateway.calls[0]["prompt"])`
  - `self.assertIn("goal_status=continuing", gateway.calls[0]["prompt"])`

## tests/test_prompt_metrics_compare_refactor.py (5 tests)

### test_summarize_trace_rows_tracks_length_and_hash_stability
Summary: Tests summarize trace rows tracks length and hash stability
Asserts:
  - `self.assertEqual(summary["sample_count"], 3)`
  - `self.assertEqual(summary["validation_failure_count"], 1)`
  - `self.assertEqual(summary["system_prompt_length"]["median"], 110.0)`
  - `self.assertEqual(summary["dynamic_prompt_length"]["p95"], 40)`
  - `self.assertEqual(summary["semi_stable_length"]["median"], 30.0)`
  - `self.assertEqual(summary["stable_prefix_hash"]["unique_count"], 2)`
  - `self.assertEqual(summary["stable_prefix_hash"]["pairwise_stability_rate"], 0.5)`
  - `self.assertEqual(summary["semantic_system_hash"]["unique_count"], 2)`
  - `self.assertEqual(summary["semantic_system_hash"]["pairwise_stability_rate"], 0.5)`
  - `self.assertEqual(summary["native_prefix_stable_rate"], 0.6667)`
  - `self.assertEqual(summary["prefix_changed_reasons"], {"frozen_rules_or_persona_changed": 1, "unavailable_in_trace": 2})`
  - `self.assertEqual(summary["remaining_system_composition"]["frozen_prefix_blocks"]["persona_or_identity"], 100)`
  - `self.assertEqual(summary["remaining_prompt_background_composition"]["soft_background_blocks"]["stable_expression"], 60)`
  - `self.assertEqual(summary["block_analysis_modes"]["unknown"], 3)`
  - `self.assertEqual(summary["system_rules_candidate_frequency"]["current_message_first"], 2)`
  - `self.assertEqual(summary["system_rules_keep_frequency"]["visible_reply_only"], 1)`

### test_build_report_compares_aligned_cases_and_deltas
Summary: Tests build report compares aligned cases and deltas
Asserts:
  - `self.assertEqual(report["comparison"]["aligned_case_count"], 2)`
  - `self.assertEqual(len(report["comparison"]["status_mismatches"]), 1)`
  - `self.assertEqual(report["delta"]["system_prompt_length_mean"], -70.0)`
  - `self.assertEqual(report["delta"]["dynamic_prompt_length_mean"], 55.0)`
  - `self.assertIn("semantic_system_hash_pairwise_rate", report["delta"])`
  - `self.assertEqual(report["comparison"]["migration_priority_rows"][0]["case_id"], "tool_intent")`
  - `self.assertEqual(report["comparison"]["migration_priority_rows"][0]["recommended_migration_target"], "planner_runtime_instruction")`
  - `self.assertEqual(report["comparison"]["migration_priority_rows"][0]["largest_migratable_block"], "stable_behavior_rules")`
  - `self.assertEqual(report["comparison"]["migration_candidate_frequency"]["stable_behavior_rules"], 1)`
  - `self.assertIn("baseline_vs_current_block_delta", report)`
  - `self.assertIn("system_rules", report["baseline_vs_current_block_delta"])`
  - `self.assertEqual(report["baseline_vs_current_block_delta"]["system_rules"]["delta_mode"], "comparable")`
  - `self.assertIn("legacy_cold_summary_changed_case_ids", report["diagnostics"])`
  - `self.assertTrue(report["diagnostics"]["system_rules_comparable"])`
  - `self.assertEqual(report["next_real_migration_candidate"], "current_message_first")`
  - `self.assertIn("current_message_first", report["system_rules_migration_candidates"])`
  - `self.assertEqual(report["system_rules_keep_items"], {})`
  - `self.assertIn("Prompt Metrics Before/After Report", markdown)`
  - `self.assertIn("Remaining System Composition", markdown)`
  - `self.assertIn("Soft Background Blocks", markdown)`
  - `self.assertIn("Semantic System Diagnostics", markdown)`
  - `self.assertIn("provider_visible_system_hash", markdown)`
  - `self.assertIn("prefix_hash", markdown)`
  - `self.assertIn("System Rules Breakdown", markdown)`
  - `self.assertIn("Next Real Migration Candidate", markdown)`
  - `self.assertIn("High-Dynamic Case Priority", markdown)`
  - `self.assertIn("Stable Global Candidate", markdown)`
  - `self.assertIn("Status Mismatches", markdown)`
  - `self.assertIn("Validation Verdict", markdown)`
  - `self.assertIn("Session Reuse Validation Deferred", markdown)`
  - `self.assertIn("continuity/native prefix compatibility signal", markdown)`

### test_baseline_fallback_parser_marks_simple_system_prompt_as_parsed_system_rules
Summary: Tests baseline fallback parser marks simple system prompt as parsed system rules
Asserts:
  - `self.assertEqual(parsed_mode, "fallback_parsed")`
  - `self.assertIn("system_rules", parsed_blocks)`

### test_baseline_fallback_parser_marks_unknown_shape_as_not_comparable
Summary: Tests baseline fallback parser marks unknown shape as not comparable
Asserts:
  - `self.assertEqual(parsed_mode, "not_comparable")`
  - `self.assertEqual(parsed_blocks["system_rules"], self.mod.BASELINE_BLOCK_SENTINEL)`

### test_harness_collects_non_empty_prefix_meta_for_current_repo
Summary: Tests harness collects non empty prefix meta for current repo
Asserts:
  - `self.assertTrue(rows)`
  - `self.assertTrue(             any(                 dict((row.get("continuity") or {}).get("frozen_prefix_blocks", {}) or {})                 or dict((row.get("continuity") or {}).get("semi_stable_blocks", {}) or {})                 for row in rows ...`
  - `self.assertNotEqual(reasons, {"unavailable_in_trace"})`
  - `self.assertEqual(result.returncode, 0, msg=result.stderr)`

## tests/test_plugin_pages_admin_refactor.py (6 tests)

### test_native_admin_api_registers_core_routes
Summary: Tests native admin api registers core routes
Asserts:
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/dashboard", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/runtime/capabilities", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/tools/policy", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/recent-decisions", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/recent-turns", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/chats/{{chat_id}}/turns", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/chats/{{chat_id}}/trace-events", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/chats/<chat_id>/trace-events", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/chats/{{chat_id}}/unified-timeline", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/observability/overview", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/observability/timeline", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/observability/chats/{{chat_id}}", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/observability/errors", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/observability/search", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/scheduler/status", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/scheduler/due-selection", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/cognition/scheduler/chats/{{chat_id}}", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/heartflow/chats", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/heartflow/impulses", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/heartflow/chats/{{chat_id}}/impulses", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/heartflow/timeline", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/heartflow/chats/{{chat_id}}/timeline", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/heartflow/topic-digests", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/proactive/intents", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/learning/status", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/memory-feedback/sources", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/reviews/{{id}}/submit", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/reviews/<id>/submit", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/memories/events/{{id}}", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/memories/events/<id>", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/users/{{user_id}}/slices/{{index}}", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/users/<user_id>/slices/<index>", paths)`
  - `self.assertIn(f"{PLUGIN_API_PREFIX}/persona/slices", paths)`
  - `self.assertNotIn(f"{PLUGIN_API_PREFIX}/persona", paths)`
  - `self.assertFalse(any(path.startswith(f"{PLUGIN_API_PREFIX}/config") for path in paths))`
  - `self.assertIn((f"{PLUGIN_API_PREFIX}/reviews/{{id}}/submit", ("POST",)), mutating)`
  - `self.assertIn((f"{PLUGIN_API_PREFIX}/reviews/<id>/submit", ("POST",)), mutating)`
  - `self.assertIn((f"{PLUGIN_API_PREFIX}/reviews/{{id}}/delete", ("POST",)), mutating)`
  - `self.assertIn((f"{PLUGIN_API_PREFIX}/memories/events/{{id}}/delete", ("POST",)), mutating)`
  - `self.assertIn((f"{PLUGIN_API_PREFIX}/memories/events/<id>/delete", ("POST",)), mutating)`
  - `self.assertIn((f"{PLUGIN_API_PREFIX}/memories/reflections/{{date}}/delete", ("POST",)), mutating)`
  - `self.assertIn((f"{PLUGIN_API_PREFIX}/memories/nodes/{{id}}/delete", ("POST",)), mutating)`
  - `self.assertIn((f"{PLUGIN_API_PREFIX}/memories/jargon/{{id}}/delete", ("POST",)), mutating)`
  - `self.assertIn((f"{PLUGIN_API_PREFIX}/users/{{user_id}}", ("POST",)), mutating)`
  - `self.assertIn((f"{PLUGIN_API_PREFIX}/users/{{user_id}}/delete", ("POST",)), mutating)`
  - `self.assertIn((f"{PLUGIN_API_PREFIX}/users/{{user_id}}/slices/{{index}}/delete", ("POST",)), mutating)`
  - `self.assertIn((f"{PLUGIN_API_PREFIX}/users/<user_id>/slices/<index>/delete", ("POST",)), mutating)`
  - `self.assertNotIn((f"{PLUGIN_API_PREFIX}/persona/save", ("POST",)), mutating)`

### test_registered_plugin_page_handlers_accept_astrbot_path_kwargs
Summary: Tests registered plugin page handlers accept astrbot path kwargs
Asserts:
  - `self.assertIsInstance(response, dict)`
  - `self.assertEqual(response.get("status"), "ok")`
  - `self.assertIn("data", response)`

### test_plugin_page_handlers_sanitize_path_values
Summary: Tests plugin page handlers sanitize path values
Asserts:
  - `self.assertEqual(response["path"], "C:\\tmp\\demo")`
  - `self.assertEqual(response["nested"]["items"][0], "C:\\tmp\\child")`

### test_admin_plugin_page_uses_astrbot_bridge_and_relative_assets
Summary: Tests admin plugin page uses astrbot bridge and relative assets
Asserts:
  - `self.assertIn('href="./style.css"', index_html)`
  - `self.assertIn('src="./app.js"', index_html)`
  - `self.assertIn("window.AstrBotPluginPage", app_js)`
  - `self.assertIn("Turn Context", app_js)`
  - `self.assertIn("Think Level Budget", app_js)`
  - `self.assertIn("Follow-up", app_js)`
  - `self.assertIn("renderFollowUpSummary", app_js)`
  - `self.assertIn("Side Inputs Timings", app_js)`
  - `self.assertIn("renderSideInputTimings", app_js)`
  - `self.assertIn("side_inputs", app_js)`
  - `self.assertIn("think_level", app_js)`
  - `self.assertIn("think_reason", app_js)`
  - `self.assertIn("cognitive_loop_skipped_reason", app_js)`
  - `self.assertIn("readonly_tools_allowed", app_js)`
  - `self.assertIn("记忆裁决 Memory", app_js)`
  - `self.assertIn("/cognition/recent-turns", app_js)`
  - `self.assertIn("/cognition/chats/${segment(chatId)}/turns", app_js)`
  - `self.assertIn("/cognition/observability/overview", app_js)`
  - `self.assertIn("/cognition/observability/timeline", app_js)`
  - `self.assertIn("/cognition/observability/search", app_js)`
  - `self.assertIn("Scheduler Diagnostics", app_js)`
  - `self.assertIn("/cognition/scheduler/status", app_js)`
  - `self.assertIn("/cognition/scheduler/due-selection", app_js)`
  - `self.assertIn("/cognition/scheduler/chats/${segment(targetChat)}", app_js)`
  - `self.assertIn("暂无 loop state。该 chat 尚未进入 scheduler 跟踪。", app_js)`
  - `self.assertIn("SCHEDULER_POLL_INTERVAL_MS = 5000", app_js)`
  - `self.assertIn('state.current === "dashboard" && state.dashboardTab === "cognition"', app_js)`
  - `self.assertIn("startSchedulerPolling()", app_js)`
  - `self.assertIn("stopSchedulerPolling()", app_js)`
  - `self.assertIn("${renderSchedulerDiagnosticsSection()}", app_js)`
  - `self.assertNotIn('insertAdjacentHTML("afterbegin", renderSchedulerDiagnosticsSection())', app_js)`
  - `self.assertIn('if (state.dashboardTab === "cognition") {\n    await renderDashboardCognition();\n    startSchedulerPolling();\n    return;\n  }', app_js)`
  - `self.assertLess(scheduler_idx, cognition_idx)`
  - `self.assertLess(scheduler_idx, observability_idx)`
  - `self.assertLess(observability_idx, cognition_idx)`
  - `self.assertLess(cognition_idx, turn_context_idx)`
  - `self.assertIn("Impulse Safety", app_js)`
  - `self.assertIn("/heartflow/impulses", app_js)`
  - `self.assertIn("/heartflow/chats/${segment(chatId)}/impulses", app_js)`
  - `self.assertIn("Heartflow Timeline", app_js)`
  - `self.assertIn("Heartflow Sessions", app_js)`
  - `self.assertIn("Proactive Intents", app_js)`
  - `self.assertIn("topic-digests", app_js)`
  - `self.assertIn("/heartflow/timeline", app_js)`
  - `self.assertIn("/heartflow/chats/${segment(chatId)}/timeline", app_js)`
  - `self.assertIn("/heartflow/topic-digests", app_js)`
  - `self.assertIn("主动意图轨迹", app_js)`
  - `self.assertIn("/proactive/intents", app_js)`
  - `self.assertIn("removed_by_energy", app_js)`
  - `self.assertIn("removed_by_cooldown", app_js)`
  - `self.assertIn("removed_by_social_intent", app_js)`
  - `self.assertIn("apiGet", app_js)`
  - `self.assertIn("apiPost", app_js)`
  - `self.assertIn("/persona/slices", app_js)`
  - `self.assertIn("角色切片", app_js)`
  - `self.assertIn("renderPersonaSlices", app_js)`
  - `self.assertIn("renderShardCards", app_js)`
  - `self.assertIn("角色切片读取失败", app_js)`
  - `self.assertIn("Bridge 初始化中", app_js)`
  - `self.assertIn("Bridge 连接失败", app_js)`
  - `self.assertNotIn("/config/replace", app_js)`
  - `self.assertNotIn("/config/", app_js)`
  - `self.assertNotIn('"/config"', app_js)`
  - `self.assertNotIn("/persona\"", app_js)`
  - `self.assertNotIn("/persona/save", app_js)`
  - `self.assertNotIn("raw_preview", app_js)`
  - `self.assertNotIn("查看原始人格预览", app_js)`
  - `self.assertNotIn("renderConfigField", app_js)`
  - `self.assertNotIn("collectSectionValue", app_js)`
  - `self.assertNotIn("data-config-field", app_js)`
  - `self.assertNotIn("apiPut", app_js)`
  - `self.assertNotIn("apiDelete", app_js)`
  - `self.assertNotIn("api.put", app_js)`
  - `self.assertNotIn("api.delete", app_js)`
  - `self.assertIn('const API_PREFIX = "admin"', app_js)`
  - `self.assertIn("pluginEndpoint", app_js)`
  - `self.assertIn("readyBridge", app_js)`
  - `self.assertIn("waitForBridge", app_js)`
  - `self.assertIn("DOMContentLoaded", app_js)`
  - `self.assertNotIn('const API_PREFIX = "/astrmai/admin"', app_js)`
  - `self.assertNotIn("/astrmai/admin", app_js)`
  - `self.assertNotIn(marker, app_js)`
  - `self.assertNotIn(marker, index_html)`

### test_admin_page_exposes_core_management_tabs
Summary: Tests admin page exposes core management tabs
Asserts:
  - `self.assertNotIn('"系统配置"', app_js)`
  - `self.assertNotIn('"人格设定"', app_js)`
  - `self.assertIn(label, app_js)`
  - `self.assertIn(label, app_js)`

### test_memory_ui_service_uses_package_relative_memory_import
Summary: Tests memory ui service uses package relative memory import
Asserts:
  - `self.assertIn("from ....memory.contracts.memory_query import MemoryWriteRequest", content)`
  - `self.assertNotIn("from astrmai.memory.contracts.memory_query import MemoryWriteRequest", content)`

## tests/test_judge_history_window_refactor.py (9 tests)

### test_normal_judge_history_keeps_only_recent_timestamped_records
Summary: Tests normal judge history keeps only recent timestamped records
Asserts:
  - `self.assertIn(f"[{expected_time}] RecentUser: recent clue", prompt)`
  - `self.assertNotIn("OldUser: stale clue", prompt)`
  - `self.assertNotIn("NoTsUser: missing timestamp", prompt)`

### test_keyword_wakeup_extends_history_window_to_thirty_minutes
Summary: Tests keyword wakeup extends history window to thirty minutes
Asserts:
  - `self.assertIn(f"[{expected_time}] WakeRecent: still relevant", prompt)`
  - `self.assertNotIn("TooOld: should be dropped", prompt)`

### test_judge_falls_back_to_database_service_recent_logs
Summary: Tests judge falls back to database service recent logs
Asserts:
  - `self.assertEqual(db_service.calls[-1]["max_age_seconds"], 900.0)`
  - `self.assertIn("DbUser: db-backed clue", gateway.prompts[-1])`

### test_judge_prefers_attention_window_events_over_database_history
Summary: Tests judge prefers attention window events over database history
Asserts:
  - `self.assertIn("WindowUser: window-backed clue", gateway.prompts[-1])`
  - `self.assertNotIn("DbUser: db-backed clue", gateway.prompts[-1])`
  - `self.assertEqual(db_service.calls, [])`

### test_load_recent_history_records_short_circuits_after_first_valid_loader
Summary: Tests load recent history records short circuits after first valid loader
Asserts:
  - `self.assertEqual([record["sender_name"] for record in records], ["RecentUser"])`
  - `self.assertEqual(persistence.calls, ["recent"])`

### test_flatten_history_content_uses_readable_placeholders
Summary: Tests flatten history content uses readable placeholders
Asserts:
  - `self.assertEqual(flattened, "look [image][@mention]")`
  - `self.assertNotIn("[鍥剧墖]", flattened)`
  - `self.assertNotIn("[@鏌愪汉]", flattened)`

### test_primary_mood_prepass_turns_small_judge_delta_into_microadjust
Summary: Tests primary mood prepass turns small judge delta into microadjust
Asserts:
  - `self.assertEqual(state_engine.mood_updates, [])`

### test_primary_mood_prepass_scales_large_judge_delta
Summary: Tests primary mood prepass scales large judge delta
Asserts:
  - `self.assertEqual(state_engine.mood_updates, [("default:GroupMessage:group-1", -0.1)])`
  - `self.assertAlmostEqual(focus_event.get_extra("astrmai_judge_mood_delta"), -0.1)`

### test_judge_releases_active_group_lock_after_gateway_failure
Summary: Tests judge releases active group lock after gateway failure
Asserts:
  - `self.assertEqual(first.action, "REPLY")`
  - `self.assertEqual(second.action, "REPLY")`
  - `self.assertNotIn("default:GroupMessage:group-1", judge.active_sys1_groups)`
  - `self.assertEqual(len(gateway.prompts), 2)`

## tests/unit/state/test_chat_state_persistence_migrated.py (5 tests)

### test_chat_state_roundtrip_preserves_decay_fields
Summary: Tests chat state roundtrip preserves decay fields
Asserts:
  - `self.assertEqual(loaded.last_reply_time, 111.0)`
  - `self.assertEqual(loaded.last_passive_decay_time, 222.0)`
  - `self.assertEqual(loaded.last_energy_recovery_time, 333.0)`
  - `self.assertEqual(loaded.total_replies, 3)`

### test_database_service_get_chat_state_preserves_decay_fields
Summary: Tests database service get chat state preserves decay fields
Asserts:
  - `self.assertIsNotNone(loaded)`
  - `self.assertEqual(loaded.last_reply_time, 333.0)`
  - `self.assertEqual(loaded.last_passive_decay_time, 444.0)`
  - `self.assertEqual(loaded.last_energy_recovery_time, 555.0)`
  - `self.assertEqual(loaded.total_replies, 4)`

### test_get_state_persists_daily_reset_on_first_load
Summary: Tests get state persists daily reset on first load
Asserts:
  - `self.assertEqual(state.last_reset_date, datetime.date.today().isoformat())`
  - `self.assertAlmostEqual(state.energy, 0.6)`
  - `self.assertAlmostEqual(state.mood, 0.0)`
  - `self.assertEqual(len(persistence.saved_states), 1)`
  - `self.assertEqual(persistence.saved_states[0]["last_reset_date"], datetime.date.today().isoformat())`
  - `self.assertAlmostEqual(persistence.saved_states[0]["mood"], 0.0)`

### test_get_chat_state_survives_schema_rebuild_with_column_reorder
Summary: 回归 (w11): 列缓存预热后chat_states被重建且列序变化,
Asserts:
  - `self.assertIsNotNone(first)`
  - `self.assertEqual(first.energy, 0.7)`
  - `self.assertIsNotNone(second)`
  - `self.assertEqual(second.chat_id, "chat-reorder")`
  - `self.assertEqual(second.energy, 0.7)`
  - `self.assertEqual(second.mood, 0.3)`

### test_load_chat_state_survives_schema_rebuild_with_column_reorder
Summary: 回归 (w11): 同test_get_chat_state_...，覆盖异步load_chat_state路径。
Asserts:
  - `self.assertIsNotNone(first)`
  - `self.assertEqual(first.energy, 0.8)`
  - `self.assertIsNotNone(second)`
  - `self.assertEqual(second.chat_id, "chat-async-reorder")`
  - `self.assertEqual(second.energy, 0.8)`
  - `self.assertEqual(second.mood, -0.2)`

## tests/integration/test_message_to_reply_pipeline.py (1 tests)

### test_direct_message_flows_through_attention_planner_executor_reply
Summary: Tests direct message flows through attention planner executor reply
Asserts:
  - `self.assertEqual(yielded, [{"type": "plain", "text": "(suppressed)"}])`
  - `self.assertTrue(sent_text)`
  - `self.assertNotIn("[TERMINAL_YIELD]", sent_text)`
  - `self.assertIsNotNone(event.get_extra("astrmai_turn_context"))`
  - `self.assertIn("收到", sent_text, {"trace": event.get_extra("astrmai_trace_log"), "gateway": gateway.calls})`
  - `self.assertNotEqual(persistence.chat_states["group-1"].mood, 0.0)`
  - `self.assertIn(event.unified_msg_origin, memory_pipeline._session_history_buffer)`

## tests/regression/architecture/test_memory_runtime_boundaries_refactor.py (15 tests)

### test_summarizer_module_is_not_primary_runtime_implementation_host
Summary: Tests summarizer module is not primary runtime implementation host
Asserts:
  - `self.assertIn("compatibility facade", summarizer.lower())`
  - `self.assertIn("SessionMemorySummarizer", summarizer)`
  - `self.assertNotIn("class MemoryTurnPipeline", summarizer)`

### test_reply_post_send_no_longer_calls_legacy_summarizer_ingest
Summary: Tests reply post send no longer calls legacy summarizer ingest
Asserts:
  - `self.assertNotIn("summarizer.ingest_committed_turn", reply_post_send)`
  - `self.assertIn("memory_pipeline", reply_post_send)`
  - `self.assertIn("instant_gate", reply_post_send)`
  - `self.assertIn("publish_turn_committed", reply_post_send)`

### test_proactive_memory_maintenance_no_longer_calls_legacy_summarizer
Summary: Tests proactive memory maintenance no longer calls legacy summarizer
Asserts:
  - `self.assertNotIn("summarizer.run_once_for_session", proactive_task)`
  - `self.assertIn("memory_pipeline", proactive_task)`
  - `self.assertIn("run_maintenance_for_session", proactive_task)`

### test_runtime_code_does_not_read_memory_engine_summarizer_alias
Summary: Tests runtime code does not read memory engine summarizer alias
Asserts:
  - `self.assertEqual(offenders, [], "Production runtime should not read memory_engine.summarizer alias")`

### test_memory_turn_pipeline_is_the_only_runtime_async_bridge
Summary: Tests memory turn pipeline is the only runtime async bridge
Asserts:
  - `self.assertIn("TOPIC_MEMORY_TURN_COMMITTED", event_bus)`
  - `self.assertIn("memory.turn_committed", event_bus)`
  - `self.assertIn("publish_turn_committed", reply_post_send)`
  - `self.assertIn('subscribe(self.event_bus.TOPIC_MEMORY_TURN_COMMITTED', pipeline)`

### test_memory_engine_runtime_wiring_exposes_new_components
Summary: Tests memory engine runtime wiring exposes new components
Asserts:
  - `self.assertIn("self.instant_gate = None", engine_text)`
  - `self.assertIn("self.memory_pipeline = None", engine_text)`
  - `self.assertIn("self.session_summarizer = None", engine_text)`
  - `self.assertIn("self.instant_gate = InstantMemoryGate", engine_text)`
  - `self.assertIn("self.memory_pipeline = MemoryTurnPipeline", engine_text)`
  - `self.assertIn("self.session_summarizer = SessionMemorySummarizer", engine_text)`
  - `self.assertNotIn("self.summarizer = self.memory_pipeline", engine_text)`

### test_tests_do_not_heavily_depend_on_compat_summarizer_module
Summary: Tests tests do not heavily depend on compat summarizer module
Asserts:
  - `self.assertEqual(             offenders,             [],             "Only compat smoke tests should import the summarizer compat module",         )`

### test_runtime_code_does_not_call_memory_engine_recall_directly
Summary: Tests runtime code does not call memory engine recall directly
Asserts:
  - `self.assertEqual(offenders, [], "Production runtime should go through retrieval/tool services instead of memory_engine.recall()")`

### test_runtime_code_does_not_call_persona_recall_wrappers_directly
Summary: Tests runtime code does not call persona recall wrappers directly
Asserts:
  - `self.assertEqual(offenders, [], "Production runtime should not route through recall_persona_lore/query_persona_lore wrappers")`

### test_prompt_refiner_does_not_drive_react_or_recall_fallback
Summary: Tests prompt refiner does not drive react or recall fallback
Asserts:
  - `self.assertNotIn("react_retriever.retrieve(", prompt_refiner)`
  - `self.assertNotIn("memory_engine.recall(", prompt_refiner)`

### test_legacy_document_projection_writes_stay_inside_projector
Summary: Tests legacy document projection writes stay inside projector
Asserts:
  - `self.assertEqual(offenders, [], "Legacy documents writes should be limited to MemoryIndexProjector")`

### test_runtime_code_does_not_depend_on_legacy_jargon_adapters
Summary: Tests runtime code does not depend on legacy jargon adapters
Asserts:
  - `self.assertEqual(             offenders,             [],             "Production runtime should use canonical jargon retrieval/write services instead of legacy jargon adapters",         )`

### test_runtime_jargon_auto_injection_is_owned_by_memory_injection_service
Summary: Tests runtime jargon auto injection is owned by memory injection service
Asserts:
  - `self.assertNotIn('layers=["jargon"]', planning_loader)`
  - `self.assertNotIn('intent="jargon"', planning_loader)`
  - `self.assertNotIn("astrmai_jargon_injection_trace", planning_loader)`
  - `self.assertNotIn('layers=["jargon"]', planner_side_inputs)`
  - `self.assertNotIn('intent="jargon"', planner_side_inputs)`

### test_runtime_code_does_not_depend_on_legacy_expression_pattern_adapters
Summary: Tests runtime code does not depend on legacy expression pattern adapters
Asserts:
  - `self.assertEqual(             offenders,             [],             "Production runtime should use canonical expression_pattern services instead of legacy ExpressionPattern adapters",         )`

### test_webui_review_and_stats_do_not_write_or_count_legacy_expression_patterns
Summary: Tests webui review and stats do not write or count legacy expression patterns
Asserts:
  - `self.assertNotIn("INSERT INTO ExpressionPattern", review_ui)`
  - `self.assertNotIn("UPDATE ExpressionPattern", review_ui)`
  - `self.assertNotIn("DELETE FROM ExpressionPattern", review_ui)`
  - `self.assertNotIn("SELECT COUNT(*) FROM ExpressionPattern", dashboard)`
  - `self.assertNotIn("SELECT COUNT(*) FROM ExpressionPattern", admin_ui)`

## tests/regression/persistence/test_persistence_regressions_migrated.py (7 tests)

### test_sync_init_without_running_loop_creates_session_id_column
Summary: Tests sync init without running loop creates session id column
Asserts:
  - `self.assertIsNone(manager._init_task)`
  - `self.assertTrue(Path(manager.db_path).exists())`
  - `self.assertIn("session_id", cols)`

### test_async_init_with_running_loop_schedules_task
Summary: Tests async init with running loop schedules task
Asserts:
  - `self.assertIn("session_id", cols)`
  - `self.assertIsNotNone(manager._init_task)`

### test_reload_datamodels_does_not_duplicate_indexes_on_create_all
Summary: Tests reload datamodels does not duplicate indexes on create all
Asserts:
  - `self.assertGreater(before_names.count("ix_memoryretrievaltrace_trace_id"), 1)`
  - `self.assertEqual(after_names.count("ix_memoryretrievaltrace_trace_id"), 1)`
  - `self.assertEqual(after_names.count("ix_memoryretrievaltrace_chat_id"), 1)`
  - `self.assertEqual(after_names.count("ix_memoryretrievaltrace_created_at"), 1)`

### test_load_all_user_profiles_returns_structured_profile_fields
Summary: Tests load all user profiles returns structured profile fields
Asserts:
  - `self.assertIn("user-1", profiles)`
  - `self.assertEqual(profiles["user-1"]["name"], "Alice")`
  - `self.assertEqual(profiles["user-1"]["nickname"], "阿测")`
  - `self.assertEqual(profiles["user-1"]["tags"], ["friend", "qa"])`
  - `self.assertEqual(profiles["user-1"]["memory_points"], ["会写测试"])`
  - `self.assertEqual(profiles["user-1"]["message_count_for_profiling"], 9)`
  - `self.assertEqual(profiles["user-1"]["profile_metadata"]["manual_locked_fields"], ["nickname"])`

### test_schema_patch_helpers_swallow_duplicate_columns_and_wrap_other_errors
Summary: Tests schema patch helpers swallow duplicate columns and wrap other errors
Asserts:
  - `self.assertIn("sync schema patch failed", sync_message)`
  - `self.assertIn("ALTER TABLE memoryevent ADD COLUMN session_id TEXT DEFAULT ''", sync_message)`
  - `self.assertIn("no such table: memoryevent", sync_message)`
  - `self.assertRaises(sqlite3.OperationalError)`
  - `self.assertIn("async schema patch failed", async_message)`
  - `self.assertIn("ALTER TABLE memoryevent ADD COLUMN session_id TEXT DEFAULT ''", async_message)`
  - `self.assertIn("no such table: memoryevent", async_message)`
  - `self.assertRaises(sqlite3.OperationalError)`

### test_memory_engine_recall_accepts_and_forwards_top_k
Summary: Tests memory engine recall accepts and forwards top k
Asserts:
  - `self.assertEqual(calls["k"], 3)`
  - `self.assertEqual(calls["session_id"], "chat-1")`
  - `self.assertIn("old memory", result)`

### test_react_retriever_query_person_uses_profile_loader_and_nickname
Summary: Tests react retriever query person uses profile loader and nickname
Asserts:
  - `self.assertIn("Alice", by_name)`
  - `self.assertIn("阿测", by_nickname)`
  - `self.assertIn("88", by_nickname)`

## tests/test_infrastructure_settings_refactor.py (12 tests)

### test_build_infrastructure_settings_collects_gateway_lane_and_flags
Summary: Tests build infrastructure settings collects gateway lane and flags
Asserts:
  - `self.assertEqual(settings.gateway.max_concurrent_llm_calls, 9)`
  - `self.assertEqual(settings.gateway.task_models, ("task-a",))`
  - `self.assertEqual(settings.lane.nicknames, ("Mai",))`
  - `self.assertTrue(settings.features.work_mode_enabled)`
  - `self.assertTrue(settings.features.private_chat_enabled)`
  - `self.assertFalse(settings.features.vision_enabled)`
  - `self.assertFalse(settings.features.proactive_enabled)`
  - `self.assertTrue(settings.features.dream_visible)`
  - `self.assertTrue(settings.features.meme_enabled)`

### test_build_infrastructure_settings_clamps_zero_gateway_limits
Summary: Tests build infrastructure settings clamps zero gateway limits
Asserts:
  - `self.assertEqual(settings.gateway.max_concurrent_llm_calls, 1)`
  - `self.assertEqual(settings.gateway.api_timeout, 1.0)`

### test_gateway_and_lane_manager_can_use_local_settings_views
Summary: Tests gateway and lane manager can use local settings views
Asserts:
  - `self.assertEqual(gateway._api_timeout(), 18.0)`
  - `self.assertEqual(gateway._task_models(), ["task"])`
  - `self.assertEqual(lane_manager.settings.nicknames, ("Mai",))`
  - `self.assertTrue(gateway._debug_mode())`

### test_proactive_rhythm_defaults_cross_midnight_quiet_hours
Summary: Tests proactive rhythm defaults cross midnight quiet hours
Asserts:
  - `self.assertTrue(quiet.quiet_hours)`
  - `self.assertEqual(quiet.time_bucket, "quiet")`
  - `self.assertFalse(morning.quiet_hours)`
  - `self.assertEqual(morning.time_bucket, "morning")`
  - `self.assertGreater(quiet.base_frequency_factor, 1.0)`

### test_proactive_rhythm_empty_quiet_hours_disables_quiet_mode
Summary: Tests proactive rhythm empty quiet hours disables quiet mode
Asserts:
  - `self.assertFalse(quiet.quiet_hours)`
  - `self.assertEqual(quiet.quiet_ranges, ())`

### test_proactive_rhythm_without_config_has_no_quiet_hours
Summary: Tests proactive rhythm without config has no quiet hours
Asserts:
  - `self.assertFalse(quiet.quiet_hours)`
  - `self.assertEqual(quiet.quiet_ranges, ())`

### test_proactive_rhythm_logs_timezone_diagnostic_only_once
Summary: Tests proactive rhythm logs timezone diagnostic only once
Asserts:
  - `self.assertTrue(quiet.quiet_hours)`
  - `self.assertFalse(morning.quiet_hours)`
  - `self.assertEqual(len(calls), 1)`
  - `self.assertIn("local timezone diagnostic", calls[0])`
  - `self.assertIn("quiet_ranges=['23:30-07:30']", calls[0])`

### test_astrmai_config_preserves_runtime_config_fields
Summary: Tests astrmai config preserves runtime config fields
Asserts:
  - `self.assertEqual(parsed.life.proactive_quiet_hours, ["00:00-01:00"])`
  - `self.assertTrue(parsed.global_settings.debug_mode)`
  - `self.assertEqual(parsed.conversation.compaction_trigger_segments, 40)`

### test_astrmai_config_normalizes_legacy_memory_namespace
Summary: Tests astrmai config normalizes legacy memory namespace
Asserts:
  - `self.assertEqual(parsed.memory.maintenance_hot_beta, 0.3)`

### test_project_schema_exposes_runtime_config_fields
Summary: Tests project schema exposes runtime config fields
Asserts:
  - `self.assertNotIn("webui_password", global_items)`
  - `self.assertIn("proactive_quiet_hours", schema["life"]["items"])`
  - `self.assertIn("conversation", schema)`
  - `self.assertIn("deep_temporal_alpha", memory_items)`
  - `self.assertIn("maintenance_temporal_stale_hot_threshold", memory_items)`
  - `self.assertNotIn("deep_temporal_alpha", global_items)`
  - `self.assertNotIn("maintenance_temporal_stale_hot_threshold", global_items)`

### test_local_persistence_adapters_are_not_legacy_subclasses
Summary: Tests local persistence adapters are not legacy subclasses
Asserts:
  - `self.assertEqual(local_pm_mod.PersistenceManager.__module__, local_pm_mod.__name__)`
  - `self.assertEqual(local_db_mod.DatabaseService.__module__, local_db_mod.__name__)`
  - `self.assertNotIn("Legacy", local_pm_mod.PersistenceManager.__name__)`
  - `self.assertNotIn("Legacy", local_db_mod.DatabaseService.__name__)`

### test_orm_models_no_longer_export_brain_action_plan
Summary: Tests orm models no longer export brain action plan
Asserts:
  - `self.assertFalse(hasattr(orm_mod, "BrainActionPlan"))`
  - `self.assertTrue(plan.should_act())`

## tests/test_planning_input_loader_refactor.py (5 tests)

### test_pre_budget_inputs_run_concurrently_and_write_context
Summary: Tests pre budget inputs run concurrently and write context
Asserts:
  - `assert elapsed < 0.12`
  - `assert result.reflection_summary == "agency"`
  - `assert event.get_extra("astrmai_agency_reflection_summary") == "agency"`
  - `assert event.get_extra("astrmai_heartflow_pulse") == "join"`
  - `assert turn_context.continuity.current_topic == "x"`
  - `assert len(event.get_extra("astrmai_side_input_timings")) == 3`

### test_budgeted_prompt_inputs_respect_think_level
Summary: Tests budgeted prompt inputs respect think level
Asserts:
  - `assert level_zero["stable_expression_habits"] == ""`
  - `assert level_zero["situational_style_cues"] == ""`
  - `assert level_zero["stable_jargon_explanation"] == ""`
  - `assert level_zero["expression_habits"] == ""`
  - `assert planner.calls == []`
  - `assert level_zero_event.get_extra("astrmai_side_input_timings")[0]["skipped_reason"] == "think_level_0"`
  - `assert level_one["stable_expression_habits"] == "expression habits"`
  - `assert level_one["situational_style_cues"] == ""`
  - `assert level_one["expression_habits"] == "expression habits"`
  - `assert level_one["tool_state"].state.energy == 0.7`
  - `assert level_two["stable_expression_habits"] == "expression habits"`
  - `assert level_two["situational_style_cues"] == ""`
  - `assert level_two["stable_jargon_explanation"] == ""`
  - `assert level_two["slang_context"] == ""`
  - `assert level_two["jargon_explanation"] == ""`
  - `assert ("jargon", "chat-1", 8) not in planner.calls`
  - `assert level_two["planner_reasoning"] == "keep current topic"`
  - `assert level_two["goals_context"] == "goal context"`

### test_jargon_loader_returns_empty_instead_of_legacy_fallback
Summary: Tests jargon loader returns empty instead of legacy fallback
Asserts:
  - `assert result == ""`
  - `assert ("jargon", "chat-1", 8) not in planner.calls`

### test_expression_habit_loader_writes_canonical_trace
Summary: Tests expression habit loader writes canonical trace
Asserts:
  - `assert result == "trace expression habits"`
  - `assert trace.selected_ids == ["mem-expression-1"]`
  - `assert turn_context.expression_patterns.selected_ids == ["mem-expression-1"]`
  - `assert turn_context.expression_patterns.injected is True`

### test_memory_feedback_and_failures_degrade_without_blocking
Summary: Tests memory feedback and failures degrade without blocking
Asserts:
  - `assert skipped == ""`
  - `assert skipped_event.get_extra("astrmai_side_input_timings")[0]["skipped_reason"] == "think_level_1"`
  - `assert "Long-term behavior" in loaded`
  - `assert loaded_event.get_extra("astrmai_memory_feedback_summary") == loaded`
  - `assert result["stable_expression_habits"] == ""`
  - `assert result["expression_habits"] == ""`
  - `assert failed_timing["ok"] is False`
  - `assert "RuntimeError" in failed_timing["error"]`

## tests/unit/webui/test_w10_webui_plan_migrated.py (12 tests)

### test_access_layer_uses_framework_identity_or_default
Summary: Tests access layer uses framework identity or default
Asserts:
  - `self.assertEqual(asyncio.run(access_mod.get_current_user("admin-user")), "admin-user")`
  - `self.assertEqual(asyncio.run(access_mod.get_current_user(None)), "astrbot-plugin-page")`

### test_write_config_is_side_effect_free
Summary: Tests write config is side effect free
Asserts:
  - `self.assertEqual(json.loads(written_before), payload)`
  - `self.assertEqual(written_before, written_after)`

### test_reset_all_uses_schema_defaults_without_legacy_auth_fields
Summary: Tests reset all uses schema defaults without legacy auth fields
Asserts:
  - `self.assertNotIn("global_settings", saved)`
  - `self.assertEqual(saved["reply"]["enabled"], True)`

### test_persona_get_and_update_round_trip_current_persona
Summary: Tests persona get and update round trip current persona
Asserts:
  - `self.assertEqual(current["summary"], "old-summary")`
  - `self.assertEqual(current["first_person_rewrite"], "old-first")`
  - `self.assertEqual(updated["summary"], "new-summary")`
  - `self.assertEqual(reloaded["summary"], "new-summary")`
  - `self.assertEqual(reloaded["first_person_rewrite"], "new-first")`
  - `self.assertEqual(saved["beta"]["summary"], "keep-me")`
  - `self.assertEqual(saved["alpha"]["custom_field"], "preserved")`

### test_backend_routes_drop_memory_feedback_delete_alias_and_use_admin_service
Summary: Tests backend routes drop memory feedback delete alias and use admin service
Asserts:
  - `self.assertIn("/memory-feedback/{feedback_id}/disable", paths)`
  - `self.assertNotIn("/memory-feedback/{feedback_id}", paths)`
  - `self.assertIn("from ..services.admin_ui_service import AdminUiService", content)`
  - `self.assertNotIn("from ..services.chatruntimeservice import ChatRuntimeService", content)`

### test_plugin_pages_do_not_register_memory_feedback_delete_alias
Summary: Tests plugin pages do not register memory feedback delete alias
Asserts:
  - `self.assertIn((f"{plugin_pages_mod.PLUGIN_API_PREFIX}/memory-feedback/{{feedback_id}}/disable", ("POST",)), registered)`
  - `self.assertNotIn((f"{plugin_pages_mod.PLUGIN_API_PREFIX}/memory-feedback/{{feedback_id}}", ("DELETE",)), registered)`

### test_chat_runtime_service_no_longer_delegates_to_admin_ui_service
Summary: Tests chat runtime service no longer delegates to admin ui service
Asserts:
  - `self.assertNotIn("from .admin_ui_service import AdminUiService", content)`

### test_server_cors_origins_reads_env
Summary: Tests server cors origins reads env
Asserts:
  - `self.assertEqual(                 server_mod._cors_origins(),                 ["http://localhost:8765", "http://127.0.0.1:8787"],             )`

### test_standalone_auth_module_is_removed
Summary: Tests standalone auth module is removed
Asserts:
  - `self.assertRaises(ModuleNotFoundError)`

### test_plugin_page_is_auth_free_and_uses_astrbot_bridge
Summary: Tests plugin page is auth free and uses astrbot bridge
Asserts:
  - `self.assertIn("window.AstrBotPluginPage", app_js)`
  - `self.assertIn("readyBridge", app_js)`
  - `self.assertIn("pluginEndpoint", app_js)`
  - `self.assertNotIn("/auth/login", app_js)`
  - `self.assertNotIn("/auth/logout", app_js)`
  - `self.assertNotIn("/auth/verify", app_js)`
  - `self.assertNotIn("localStorage", app_js)`
  - `self.assertNotIn("sessionStorage", app_js)`
  - `self.assertNotIn("currentPage: 'login'", index_html)`

### test_remote_image_allowlist_artifacts_are_removed
Summary: Tests remote image allowlist artifacts are removed
Asserts:
  - `self.assertNotIn("remote_image_host_suffixes", config_py)`
  - `self.assertNotIn('"remote_image_host_suffixes"', schema_json)`
  - `self.assertFalse((repo_root / "REMOTE_IMAGE_ALLOWLIST.md").exists())`

### test_standalone_auth_routes_are_not_exposed_by_aggregated_router
Summary: Tests standalone auth routes are not exposed by aggregated router
Asserts:
  - `self.assertNotIn("auth_router", routes_py)`
  - `self.assertIn("from .routes import api_router, build_api_router", routes_py)`
  - `self.assertNotIn("router.include_router(", routes_py)`

## tests/test_system2_runner_refactor.py (3 tests)

### test_runner_handles_queue_energy_wait_targets_and_group_followup
Summary: Tests runner handles queue energy wait targets and group followup
Asserts:
  - `self.assertTrue(reply_sent)`
  - `self.assertEqual(runtime.state_engine.calls, [event.unified_msg_origin])`
  - `self.assertEqual(             runtime.lane_manager.calls,             [("sys2", "dialog", event.unified_msg_origin, event.unified_msg_origin)],         )`
  - `self.assertEqual(             runtime.runtime_coordinator.wait_updates,             [(event.unified_msg_origin, ["user-2"], "Bob")],         )`
  - `self.assertEqual(             runtime.chat_loop_kernel.wait_target_syncs,             [(event.unified_msg_origin, ["user-2"], "Bob")],         )`
  - `self.assertEqual(runtime.chat_loop_kernel.cooldowns[0][1:], ("followup", "followup_dispatch"))`
  - `self.assertEqual(runtime.group_reply_wait_manager.events, [event])`

### test_runner_private_followup_does_not_block_and_expires_wait_in_background
Summary: Tests runner private followup does not block and expires wait in background
Asserts:
  - `self.assertTrue(reply_sent)`
  - `self.assertEqual(before_release, [])`
  - `self.assertEqual(kernel.private_waits[0][0], event.unified_msg_origin)`
  - `self.assertEqual(after_release, [(event.unified_msg_origin, "private_wait_timeout")])`

### test_private_followup_reschedule_keeps_new_task_registered
Summary: Tests private followup reschedule keeps new task registered
Asserts:
  - `self.assertIsNot(first_task, second_task)`
  - `self.assertIs(current_task, second_task)`

## tests/unit/memory/test_dream_agent_gap_coverage.py (8 tests)

### test_parse_action_accepts_dict_json_and_embedded_json
Summary: Tests parse action accepts dict json and embedded json
Asserts:
  - `self.assertIs(DreamAgent._parse_action(direct), direct)`
  - `self.assertEqual(             DreamAgent._parse_action('{"tool":"search_memory","params":{"query":"猫"}}')["tool"],             "search_memory",         )`
  - `self.assertEqual(             DreamAgent._parse_action('LLM says: {"tool":"delete_memory","params":{"event_id":"e1"}} ok')["params"]["event_id"],             "e1",         )`
  - `self.assertIsNone(DreamAgent._parse_action("not-json"))`

### test_get_seed_events_serializes_sampled_events_and_degrades_on_db_error
Summary: Tests get seed events serializes sampled events and degrades on db error
Asserts:
  - `self.assertCountEqual([item["event_id"] for item in result], ["e0", "e1", "e2"])`
  - `self.assertEqual(by_id["e0"]["narrative"], "narrative 0")`
  - `self.assertEqual(by_id["e0"]["emotion"], "neutral")`
  - `self.assertEqual(await failing._get_seed_events("chat-1"), [])`

### test_run_dream_cycle_executes_tools_until_finish
Summary: Tests run dream cycle executes tools until finish
Asserts:
  - `self.assertIn("[思考] 先查相关记忆", result)`
  - `self.assertIn("[行动] search_memory", result)`
  - `self.assertIn("[结束] 已完成整理", result)`
  - `self.assertEqual([item[0] for item in executed], ["search_memory", "finish_dream"])`
  - `self.assertEqual(executed[0][2], "chat-1")`
  - `self.assertEqual(len(gateway.calls), 2)`
  - `self.assertEqual(gateway.calls[0]["lane_key"].scope_id, "chat-1")`

### test_tool_get_detail_reads_legacy_event_from_database_session
Summary: Tests tool get detail reads legacy event from database session
Asserts:
  - `self.assertIn("memory narrative", result)`
  - `self.assertIn("happy", result)`
  - `self.assertIn("0.8", result)`

### test_tool_search_memory_uses_retrieval_service_and_renders_result
Summary: Tests tool search memory uses retrieval service and renders result
Asserts:
  - `self.assertEqual(result, "rendered memory result")`
  - `self.assertEqual(retrieval.query.query, "deployment")`
  - `self.assertEqual(retrieval.query.session_id, "chat-1")`
  - `self.assertEqual(retrieval.query.top_k, 3)`
  - `self.assertEqual(retrieval.rendered[1], retrieval.candidates)`

### test_tool_merge_writes_memory_and_marks_canonical_sources_merged
Summary: Tests tool merge writes memory and marks canonical sources merged
Asserts:
  - `self.assertIn("merged narrative", result)`
  - `self.assertEqual(             engine.write_calls,             [{"content": "merged narrative", "session_id": "chat-1", "importance": 0.7}],         )`
  - `self.assertEqual(             engine.maintenance_service.calls,             [(["mem_1", "mem_2"], "mem_new")],         )`

### test_resolve_canonical_ids_handles_mixed_canonical_and_legacy_ids
Summary: Tests resolve canonical ids handles mixed canonical and legacy ids
Asserts:
  - `self.assertEqual(canonical, ["mem_direct", "mem_legacy"])`
  - `self.assertEqual(unresolved, ["legacy-missing"])`

### test_run_dream_cycle_returns_none_when_seed_events_are_empty
Summary: Tests run dream cycle returns none when seed events are empty
Asserts:
  - `self.assertIsNone(asyncio.run(agent.run_dream_cycle("chat-empty")))`

## tests/test_wave2_medium_regression.py (10 tests)

### test_expression_pattern_write_skip_returns_empty_id
Summary: Tests expression pattern write skip returns empty id
Asserts:
  - `self.assertEqual(result, "")`

### test_reflector_retains_batch_after_transient_gateway_failure
Summary: Tests reflector retains batch after transient gateway failure
Asserts:
  - `self.assertEqual(             [item["pattern_id"] for item in reflector._pending_reflections],             ["0", "1", "2"],         )`

### test_reflector_retains_batch_when_weight_update_fails_after_llm_success
Summary: Tests reflector retains batch when weight update fails after llm success
Asserts:
  - `self.assertEqual(             [item["pattern_id"] for item in reflector._pending_reflections],             ["pattern-0", "pattern-1", "pattern-2"],         )`

### test_detected_fact_memory_ids_are_marked_as_promoted
Summary: Tests detected fact memory ids are marked as promoted
Asserts:
  - `self.assertEqual(report["promoted"][0]["memory_id"], "promoted-memory")`
  - `self.assertEqual([item[0] for item in store.updated], ["source-0", "source-1", "source-2"])`
  - `self.assertTrue(             all(item[1]["metadata"]["promoted_to"] == "promoted-memory" for item in store.updated)         )`

### test_nickname_parser_rejects_non_json_model_output
Summary: Tests nickname parser rejects non json model output
Asserts:
  - `self.assertEqual((nickname, reason), ("", ""))`

### test_evolution_sync_log_fallback_runs_in_thread
Summary: Tests evolution sync log fallback runs in thread
Asserts:
  - `self.assertEqual(len(to_thread_calls), 1)`
  - `self.assertEqual(calls[0][1]["content"], "hello")`

### test_relationship_insult_matching_respects_token_boundaries
Summary: Tests relationship insult matching respects token boundaries
Asserts:
  - `self.assertEqual(engine.classify_interaction_type("滚开"), RelationshipEvent.INSULT)`
  - `self.assertEqual(engine.classify_interaction_type("你真是个 sb"), RelationshipEvent.INSULT)`
  - `self.assertEqual(engine.classify_interaction_type("我喜欢摇滚音乐"), RelationshipEvent.NORMAL_CHAT)`
  - `self.assertEqual(engine.classify_interaction_type("passbook migration"), RelationshipEvent.NORMAL_CHAT)`

### test_lane_lock_cleanup_skips_locked_oldest_and_evicts_other_idle_locks
Summary: Tests lane lock cleanup skips locked oldest and evicts other idle locks
Asserts:
  - `self.assertIn("locked-oldest", manager._lane_locks)`
  - `self.assertIn("new-lane", manager._lane_locks)`
  - `self.assertLessEqual(len(manager._lane_locks), 100)`

### test_message_entry_reads_fallback_from_runtime_config_contract
Summary: Tests message entry reads fallback from runtime config contract
Asserts:
  - `self.assertEqual(_runtime_fallback_text(facade), "configured fallback")`

### test_hot_config_failure_rolls_runtime_and_components_back
Summary: Tests hot config failure rolls runtime and components back
Asserts:
  - `self.assertFalse(result)`
  - `self.assertIs(runtime.config, old_config)`
  - `self.assertEqual(runtime.raw_config, {"name": "old"})`
  - `self.assertIs(runtime.infrastructure_config, old_config)`
  - `self.assertIs(runtime.synced_config, old_config)`
  - `self.assertIs(first.config, old_config)`
  - `self.assertIs(failing.config, old_config)`

## tests/unit/memory/test_topic_summarizer_gap_coverage.py (11 tests)

### test_segment_by_silence_sorts_mixed_timestamps_and_splits_on_gap
Summary: Tests segment by silence sorts mixed timestamps and splits on gap
Asserts:
  - `self.assertEqual(len(segments), 2)`
  - `self.assertEqual([m["content"] for m in segments[0].messages], ["第一段 A", "第一段 B", "第一段 C"])`
  - `self.assertEqual(segments[0].start_time, 100.0)`
  - `self.assertEqual(segments[0].end_time, 130.0)`
  - `self.assertEqual([m["content"] for m in segments[1].messages], ["第二段开始", "第二段继续", "第二段结束"])`

### test_segment_by_silence_splits_after_topic_shift_checkpoint
Summary: Tests segment by silence splits after topic shift checkpoint
Asserts:
  - `self.assertTrue(summarizer._detect_topic_shift(messages[:10], messages[10]))`
  - `self.assertEqual(len(segments), 2)`
  - `self.assertEqual(len(segments[0].messages), 10)`
  - `self.assertEqual([m["content"] for m in segments[1].messages], [             "量子 编译器 内存 模型",             "量子 编译器 调度",             "量子 算法 优化",         ])`

### test_batch_summarize_uses_keyword_fallback_without_gateway
Summary: Tests batch summarize uses keyword fallback without gateway
Asserts:
  - `self.assertEqual(result, ["讨论了火锅、聚餐、辣锅"])`

### test_batch_summarize_calls_gateway_and_parses_json_response
Summary: Tests batch summarize calls gateway and parses json response
Asserts:
  - `self.assertEqual(result, ["火锅聚餐很热闹", "讨论编译器优化"])`
  - `self.assertEqual(gateway.calls[0]["lane_key"].scope_id, "chat-7")`
  - `self.assertTrue(gateway.calls[0]["is_json"])`

### test_batch_summarize_falls_back_when_gateway_raises
Summary: Tests batch summarize falls back when gateway raises
Asserts:
  - `self.assertEqual(result, ["讨论了日常话题"])`

### test_process_history_builds_structured_topic_result
Summary: Tests process history builds structured topic result
Asserts:
  - `self.assertEqual(len(captured), 1)`
  - `self.assertEqual(captured[0][1], "chat-1")`
  - `self.assertEqual(len(captured[0][0]), 1)`
  - `self.assertEqual(len(result), 1)`
  - `self.assertEqual(result[0]["summary"], "Deployment readiness discussion")`
  - `self.assertEqual(result[0]["sentiment"], "positive")`
  - `self.assertEqual(result[0]["message_count"], 12)`
  - `self.assertEqual(result[0]["duration_minutes"], 110 / 60)`
  - `self.assertCountEqual(result[0]["participants"], ["alice", "bob"])`
  - `self.assertLessEqual(len(result[0]["topic_keywords"]), 5)`

### test_calculate_sentiment_handles_positive_negative_and_neutral_text
Summary: Tests calculate sentiment handles positive negative and neutral text
Asserts:
  - `self.assertEqual(positive, 1.0)`
  - `self.assertEqual(negative, -1.0)`
  - `self.assertEqual(neutral, 0.0)`

### test_calculate_importance_uses_all_weighted_factors
Summary: Tests calculate importance uses all weighted factors
Asserts:
  - `self.assertEqual(TopicSummarizer()._calculate_importance(segment), 0.9)`

### test_extract_keywords_handles_mixed_chinese_and_english_text
Summary: Tests extract keywords handles mixed chinese and english text
Asserts:
  - `self.assertIn("记忆系统", keywords)`
  - `self.assertIn("de", keywords)`
  - `self.assertNotIn("deploy", keywords)`

### test_sentiment_to_label_covers_boundary_values
Summary: Tests sentiment to label covers boundary values
Asserts:
  - `self.assertEqual(labels, ["negative", "negative", "neutral", "neutral", "positive"])`

### test_tokenize_handles_cjk_ascii_and_mixed_text
Summary: Tests tokenize handles cjk ascii and mixed text
Asserts:
  - `self.assertEqual(TopicSummarizer._tokenize("记忆系统"), ["记忆系统"])`
  - `self.assertEqual(TopicSummarizer._tokenize("deploy"), ["de", "ep", "pl", "lo", "oy"])`
  - `self.assertEqual(             TopicSummarizer._tokenize("记忆 AI memory"),             ["记忆", "AI", "me", "em", "mo", "or", "ry"],         )`

## tests/test_main_reply_cache_replay_live_refactor.py (5 tests)

### test_run_live_requires_api_key
Summary: Tests run live requires api key
Asserts:
  - `self.assertEqual(result, 0)`

### test_run_live_native_chat_requires_base_url
Summary: Tests run live native chat requires base url
Asserts:
  - `self.assertEqual(result, 0)`

### test_run_live_without_api_key_writes_dry_run_provider_artifacts
Summary: Tests run live without api key writes dry run provider artifacts
Asserts:
  - `self.assertEqual(result, 0)`
  - `self.assertTrue((provider_dir / "summary.json").exists())`
  - `self.assertTrue((provider_dir / "summary.md").exists())`
  - `self.assertTrue((provider_dir / "samples.jsonl").exists())`
  - `self.assertIn('"dry_run": true', summary)`
  - `self.assertIn('"validation_verdict": "dry_run_capability_only"', summary)`
  - `self.assertIn('"base_url_required": true', summary)`
  - `self.assertIn('"request_execution_possible": false', summary)`
  - `self.assertIn('"blocking_reason": "missing_api_key"', summary)`

### test_write_summary_includes_cache_and_hook_diagnostics
Summary: Tests write summary includes cache and hook diagnostics
Asserts:
  - `self.assertIn('"cache_ready_count": 2', summary)`
  - `self.assertIn('"cache_ready_rate": 1.0', summary)`
  - `self.assertIn('"cache_ready_but_hit_miss_count": 2', summary)`
  - `self.assertIn('"hash_stable_but_cache_miss_count": 1', summary)`
  - `self.assertIn('"semantic_hash_stable_count": 1', summary)`
  - `self.assertIn('"semantic_stable_but_provider_visible_changed_count": 0', summary)`
  - `self.assertIn('"cache_ready_reason_frequency"', summary)`
  - `self.assertIn('"hook_changed_system_case_ids"', summary)`
  - `self.assertIn('"cache_hint_observed_enabled": true', summary)`
  - `self.assertIn('"provider_supports_cache_hint": true', summary)`
  - `self.assertIn('"provider_supports_usage_reporting": true', summary)`
  - `self.assertIn('"provider_supports_session_id": false', summary)`
  - `self.assertIn('"validation_verdict": "supported_but_no_observed_hit"', summary)`
  - `self.assertIn("Cache Ready", markdown)`
  - `self.assertIn("semantic_system_hash", markdown)`
  - `self.assertIn("gateway_system_hash", markdown)`
  - `self.assertIn("hash_stable_vs_previous", markdown)`
  - `self.assertIn("validation_verdict", markdown)`

### test_provider_output_dir_uses_provider_family_subdirectory
Summary: Tests provider output dir uses provider family subdirectory
Asserts:
  - `self.assertEqual(provider_dir, root / "anthropic")`

## tests/test_executor_vision_refactor.py (5 tests)

### test_invalid_provider_like_vision_output_is_rejected
Summary: Tests invalid provider like vision output is rejected
Asserts:
  - `self.assertEqual(model_prompt, "prompt")`
  - `self.assertEqual(system_prompt, "system")`
  - `self.assertTrue(event.get_extra("vision_direct_invoked"))`
  - `self.assertEqual(event.get_extra("vision_direct_outcome"), "invalid_output")`

### test_invalid_tags_are_dropped_but_description_is_kept
Summary: Tests invalid tags are dropped but description is kept
Asserts:
  - `self.assertIn("涓€鍙畨闈欑殑鐚?", model_prompt)`
  - `self.assertNotIn("42", model_prompt)`
  - `self.assertEqual(event.get_extra("vision_direct_outcome"), "success")`

### test_no_direct_vision_urls_marks_skip_reason
Summary: Tests no direct vision urls marks skip reason
Asserts:
  - `self.assertEqual(model_prompt, "prompt")`
  - `self.assertEqual(system_prompt, "system")`
  - `self.assertFalse(event.get_extra("vision_direct_invoked"))`
  - `self.assertEqual(event.get_extra("vision_direct_outcome"), "skipped")`
  - `self.assertEqual(event.get_extra("vision_direct_skip_reason"), "probability_gate")`

### test_vision_failure_keeps_attempted_models_metadata
Summary: Tests vision failure keeps attempted models metadata
Asserts:
  - `self.assertEqual(model_prompt, "prompt")`
  - `self.assertEqual(system_prompt, "system")`
  - `self.assertEqual(event.get_extra("vision_direct_outcome"), "exception")`
  - `self.assertEqual(event.get_extra("vision_direct_attempted_models"), ["vision-a", "vision-b"])`
  - `self.assertEqual(event.get_extra("vision_direct_failure_reason"), "empty_description")`
  - `self.assertEqual(len(gateway.calls), 1)`

### test_remote_image_ref_is_ignored_when_remote_fetching_is_disabled
Summary: Tests remote image ref is ignored when remote fetching is disabled
Asserts:
  - `self.assertEqual(model_prompt, "prompt")`
  - `self.assertEqual(system_prompt, "system")`
  - `self.assertEqual(event.get_extra("vision_direct_outcome"), "exception")`

## tests/unit/learning/test_learning_gap_coverage.py (9 tests)

### test_auto_audit_does_not_consume_cooldown_before_service_is_ready
Summary: Tests auto audit does not consume cooldown before service is ready
Asserts:
  - `self.assertEqual(len(service.list_calls), 1)`

### test_auto_audit_rejects_low_weight_and_weaker_duplicate
Summary: Tests auto audit rejects low weight and weaker duplicate
Asserts:
  - `self.assertCountEqual(rejected_ids, ["low", "weak"])`
  - `self.assertNotIn("strong", rejected_ids)`

### test_auto_audit_retries_after_transient_list_failure
Summary: Tests auto audit retries after transient list failure
Asserts:
  - `self.assertEqual(service.attempts, 2)`
  - `self.assertEqual(len(service.list_calls), 1)`

### test_profile_generator_accepts_structured_gateway_result
Summary: Tests profile generator accepts structured gateway result
Asserts:
  - `self.assertEqual(parsed["tags"], ["curious", "patient"])`
  - `self.assertEqual(parsed["analysis"], "Prefers careful technical discussions.")`
  - `self.assertEqual(parsed["memory_points"], ["preference:likes typed APIs:0.8"])`

### test_profile_generator_ignores_null_and_blank_tags
Summary: Tests profile generator ignores null and blank tags
Asserts:
  - `self.assertEqual(parsed["tags"], ["patient"])`

### test_profile_generator_skips_prompt_without_new_messages
Summary: Tests profile generator skips prompt without new messages
Asserts:
  - `self.assertIsNone(generator.build_prompt(profile))`

### test_process_logs_and_mine_filters_partially_stale_batch
Summary: Tests process logs and mine filters partially stale batch
Asserts:
  - `self.assertEqual(mined_ids, [1])`
  - `self.assertEqual(db.marked, [[1]])`

### test_process_logs_and_mine_skips_fully_stale_batch
Summary: Tests process logs and mine skips fully stale batch
Asserts:
  - `self.assertEqual(mine_calls, [])`
  - `self.assertEqual(db.marked, [])`

### test_process_logs_and_mine_uses_current_database_log
Summary: Tests process logs and mine uses current database log
Asserts:
  - `self.assertEqual(mined_contents, ["current content"])`
  - `self.assertEqual(db.marked, [[1]])`

## tests/test_p1_prelaunch_regression.py (7 tests)

### test_search_nodes_treats_percent_and_underscore_as_literals
Summary: Tests search nodes treats percent and underscore as literals
Asserts:
  - `self.assertEqual([item.name for item in percent_results], ["literal%node"])`
  - `self.assertEqual([item.name for item in underscore_results], ["literal_node"])`

### test_temporal_boost_for_uninitialized_created_at_returns_alpha
Summary: Tests temporal boost for uninitialized created at returns alpha
Asserts:
  - `self.assertEqual(compute_temporal_boost(candidate, now=1000.0, config=config), 0.6)`

### test_reflector_empty_scores_keeps_batch_for_retry
Summary: Tests reflector empty scores keeps batch for retry
Asserts:
  - `self.assertEqual([item["pattern_id"] for item in reflector._pending_reflections], ["0", "1", "2"])`

### test_save_jargons_prevalidates_before_first_write
Summary: Tests save jargons prevalidates before first write
Asserts:
  - `self.assertEqual(writer.writes, [])`
  - `self.assertRaises(ValueError)`

### test_private_chat_eviction_skips_waiting_sessions
Summary: Tests private chat eviction skips waiting sessions
Asserts:
  - `self.assertIn("waiting", manager._sessions)`
  - `self.assertIn("new", manager._sessions)`
  - `self.assertNotIn("idle", manager._sessions)`

### test_dashboard_snapshot_degrades_diagnostics_and_capability_failures
Summary: Tests dashboard snapshot degrades diagnostics and capability failures
Asserts:
  - `self.assertTrue(snapshot["degraded"])`
  - `self.assertEqual(snapshot["diagnostics"]["status"], "degraded")`
  - `self.assertEqual(snapshot["capabilities"]["status"], "degraded")`
  - `self.assertEqual(snapshot["total_users"], 1)`

### test_cron_reload_continues_after_one_snapshot_fails
Summary: Tests cron reload continues after one snapshot fails
Asserts:
  - `self.assertEqual(asyncio.run(guard.reload_all_lost_jobs()), 1)`

## tests/unit/infrastructure/test_infrastructure_gap_coverage.py (7 tests)

### test_elastic_call_retries_next_model_after_timeout
Summary: Tests elastic call retries next model after timeout
Asserts:
  - `self.assertEqual(result, "visible reply")`
  - `self.assertEqual([model for model, _ in context.calls], ["model-timeout", "model-ok"])`
  - `self.assertEqual(stats["model-timeout"]["failures"], 1)`
  - `self.assertEqual(stats["model-ok"]["calls"], 1)`

### test_model_router_moves_fatal_cooldown_model_to_tail
Summary: Tests model router moves fatal cooldown model to tail
Asserts:
  - `self.assertEqual(router.get_ranked_models("task", ["a", "b"]), ["a", "b"])`
  - `self.assertEqual(ranked, ["b", "a"])`
  - `self.assertGreater(router._pools["task"].models["a"].cooldown_until, 0.0)`

### test_lane_manager_ensure_lane_concurrent_creation_is_single_flight
Summary: Tests lane manager ensure lane concurrent creation is single flight
Asserts:
  - `self.assertEqual(len(conversation_ids), 1)`
  - `self.assertEqual(conversation_manager.new_calls, 1)`

### test_event_bus_stop_allows_workers_to_restart_on_next_publish
Summary: Tests event bus stop allows workers to restart on next publish
Asserts:
  - `self.assertEqual(received, [1, 2])`
  - `self.assertFalse(event_bus._workers_started)`

### test_chat_runtime_clear_runtime_state_removes_activity_and_wait_targets
Summary: Tests chat runtime clear runtime state removes activity and wait targets
Asserts:
  - `self.assertTrue(removed)`
  - `self.assertEqual(wait_targets, [])`
  - `self.assertEqual(latest[0], 0.0)`

### test_token_bucket_concurrent_consumes_never_exceed_capacity
Summary: Tests token bucket concurrent consumes never exceed capacity
Asserts:
  - `self.assertEqual(sum(1 for item in results if item), 3)`
  - `self.assertEqual(sum(1 for item in results if not item), 7)`

### test_database_service_get_chat_state_falls_back_on_dirty_json_fields
Summary: Tests database service get chat state falls back on dirty json fields
Asserts:
  - `self.assertIsNotNone(loaded)`
  - `self.assertEqual(loaded.group_config, {})`
  - `self.assertEqual(loaded.last_msg_info.sender_id, "")`
  - `self.assertFalse(loaded.last_msg_info.has_image)`

## tests/unit/memory/test_memory_observer_gap_coverage.py (4 tests)

### test_record_forwards_to_trace_and_observability_and_updates_counters
Summary: Tests record forwards to trace and observability and updates counters
Asserts:
  - `self.assertEqual(first["chat_id"], "chat-1")`
  - `self.assertEqual(len(trace_store.items), 3)`
  - `self.assertEqual(trace_store.items[0]["stage"], "memory.instant_gate.gate_hit")`
  - `self.assertEqual(trace_store.items[0]["memory_event"]["payload"], {"score": 0.9})`
  - `self.assertEqual(len(hub.records), 3)`
  - `self.assertEqual(hub.records[0]["domain"], "memory")`
  - `self.assertEqual(hub.records[0]["kind"], "action")`
  - `self.assertEqual(hub.records[0]["facets"]["memory_id"], "mem-1")`
  - `self.assertTrue(runtime["pipeline_running"])`
  - `self.assertEqual(runtime["recent_warning_count"], 1)`
  - `self.assertEqual(runtime["recent_error_count"], 1)`
  - `self.assertGreater(runtime["last_gate_hit_at"], 0)`
  - `self.assertGreater(runtime["last_backfill_success_at"], 0)`
  - `self.assertGreater(runtime["last_summarize_success_at"], 0)`
  - `self.assertGreater(runtime["last_summarize_failure_at"], 0)`

### test_chat_snapshot_recent_events_filters_and_reset
Summary: Tests chat snapshot recent events filters and reset
Asserts:
  - `self.assertEqual(chat["pending_messages"], 5)`
  - `self.assertTrue(chat["worker_active"])`
  - `self.assertEqual(chat["last_gate_stage"], "gate_entered")`
  - `self.assertEqual(chat["last_backfill_stage"], "maintenance_started")`
  - `self.assertEqual(chat["last_summarize_stage"], "summarize_started")`
  - `self.assertEqual([item["stage"] for item in chat["recent_events"]], ["summarize_started", "maintenance_started"])`
  - `self.assertEqual(len(filtered), 1)`
  - `self.assertEqual(filtered[0]["chat_id"], "chat-2")`
  - `self.assertEqual([item["chat_id"] for item in errors], ["chat-2"])`
  - `self.assertEqual(await observer.recent_events(limit=10), [])`
  - `self.assertEqual(reset_runtime["recent_warning_count"], 0)`
  - `self.assertEqual(reset_runtime["last_gate_hit_at"], 0.0)`

### test_record_degrades_when_trace_or_hub_fails
Summary: Tests record degrades when trace or hub fails
Asserts:
  - `self.assertEqual(event["chat_id"], "chat-degraded")`
  - `self.assertEqual(len(recent), 1)`
  - `self.assertEqual(recent[0]["stage"], "worker_consumed")`

### test_record_formats_named_pipeline_stages_for_timeline
Summary: Tests record formats named pipeline stages for timeline
Asserts:
  - `self.assertEqual(                 [record["title"] for record in hub.records],                 [item["display_title"] for item in formatted],             )`
  - `self.assertNotEqual(item["display_title"], f"{component}.{stage}")`
  - `self.assertEqual(item["display_kind"], stage)`

## tests/test_conversation_continuity_refactor.py (8 tests)

### test_conversation_continuity_records_new_goal_state
Summary: Tests conversation continuity records new goal state
Asserts:
  - `assert snapshot["current_topic"] == "Alice: talk about homework plan"`
  - `assert snapshot["current_goal"] == "help Alice sort the homework plan"`
  - `assert snapshot["goal_status"] == "new"`
  - `assert snapshot["turn_count"] == 1`
  - `assert "current_topic=" in store.summary("chat-1", now=1001.0)`
  - `assert "goal_status=new" in store.summary("chat-1", now=1001.0)`

### test_conversation_continuity_continues_similar_topic
Summary: Tests conversation continuity continues similar topic
Asserts:
  - `assert snapshot["goal_status"] == "continuing"`
  - `assert snapshot["turn_count"] == 2`
  - `assert snapshot["current_topic"] == "Alice: talk about homework plan"`
  - `assert snapshot["continuity_weight"] == "strong"`

### test_conversation_continuity_starts_new_topic_when_focus_changes
Summary: Tests conversation continuity starts new topic when focus changes
Asserts:
  - `assert snapshot["goal_status"] == "new"`
  - `assert snapshot["turn_count"] == 1`
  - `assert snapshot["current_topic"] == "Bob: discuss tomorrow's weather"`
  - `assert snapshot["current_goal"] == "chat about the weather"`

### test_conversation_continuity_marks_redirect_boundary_and_observe
Summary: Tests conversation continuity marks redirect boundary and observe
Asserts:
  - `assert store.snapshot("chat-1", now=1011.0)["goal_status"] == "redirected"`
  - `assert store.snapshot("chat-1", now=1021.0)["goal_status"] == "guarded"`
  - `assert store.snapshot("chat-1", now=1031.0)["goal_status"] == "observing"`

### test_conversation_continuity_wait_and_ignore_do_not_refresh_goal
Summary: Tests conversation continuity wait and ignore do not refresh goal
Asserts:
  - `assert snapshot["current_topic"] == "Alice: talk about homework plan"`
  - `assert snapshot["current_goal"] == "help Alice sort the homework plan"`
  - `assert snapshot["turn_count"] == 1`
  - `assert len(recent) == 3`
  - `assert [item.reply_need for item in recent[-2:]] == ["wait", "ignore"]`
  - `assert "unrelated interruption" in summary`
  - `assert "second interruption" in summary`

### test_conversation_continuity_weakens_after_soft_decay_and_avoids_forced_old_topic
Summary: Tests conversation continuity weakens after soft decay and avoids forced old topic
Asserts:
  - `assert "continuity_weight=weak" in summary`
  - `assert "weak_reference_only" in summary`
  - `assert snapshot["goal_status"] == "new"`
  - `assert snapshot["turn_count"] == 1`

### test_conversation_continuity_lightweight_event_does_not_refresh_goal
Summary: Tests conversation continuity lightweight event does not refresh goal
Asserts:
  - `assert snapshot["current_topic"] == "Alice: talk about homework plan"`
  - `assert snapshot["current_goal"] == "help Alice sort the homework plan"`
  - `assert snapshot["turn_count"] == 1`
  - `assert len(recent) == 2`
  - `assert recent[-1].focus_preview == "Alice poked the bot"`
  - `assert "Alice poked the bot" in store.summary("chat-1", now=1011.0)`

### test_conversation_continuity_expires_after_ttl
Summary: Tests conversation continuity expires after ttl
Asserts:
  - `assert store.summary("chat-1", now=1000.0 + store.TURN_TTL_SECONDS + 1) == ""`
  - `assert snapshot["goal_status"] == "new"`
  - `assert snapshot["turn_count"] == 1`
  - `assert snapshot["current_topic"] == "Alice: fresh topic"`

## tests/test_sensors_refactor.py (7 tests)

### test_private_image_probability_gate_skips_direct_vision_but_keeps_extracted_urls
Summary: Tests private image probability gate skips direct vision but keeps extracted urls
Asserts:
  - `self.assertTrue(result)`
  - `self.assertEqual(event.get_extra("extracted_image_urls"), ["private.jpg"])`
  - `self.assertFalse(event.get_extra("vision_direct_selected"))`
  - `self.assertEqual(event.get_extra("vision_direct_skip_reason"), "probability_gate")`
  - `self.assertFalse(event.get_extra("astrmai_is_direct_vision_request"))`
  - `self.assertFalse(event.get_extra("direct_vision_urls"))`

### test_private_image_respects_vision_disable_switch
Summary: Tests private image respects vision disable switch
Asserts:
  - `self.assertTrue(result)`
  - `self.assertFalse(event.get_extra("vision_direct_selected"))`
  - `self.assertEqual(event.get_extra("vision_direct_skip_reason"), "disabled")`
  - `self.assertFalse(event.get_extra("astrmai_is_direct_vision_request"))`

### test_private_image_selects_direct_vision_when_probability_hits
Summary: Tests private image selects direct vision when probability hits
Asserts:
  - `self.assertTrue(result)`
  - `self.assertTrue(event.get_extra("vision_direct_selected"))`
  - `self.assertEqual(event.get_extra("vision_direct_skip_reason"), "")`
  - `self.assertTrue(event.get_extra("astrmai_is_direct_vision_request"))`
  - `self.assertEqual(event.get_extra("direct_vision_urls"), ["private.jpg"])`

### test_group_reply_image_probability_gate_keeps_extracted_urls
Summary: Tests group reply image probability gate keeps extracted urls
Asserts:
  - `self.assertTrue(result)`
  - `self.assertFalse(event.get_extra("vision_direct_selected"))`
  - `self.assertEqual(event.get_extra("vision_direct_skip_reason"), "probability_gate")`
  - `self.assertFalse(event.get_extra("direct_vision_urls"))`
  - `self.assertEqual(event.get_extra("extracted_image_urls"), ["reply.jpg"])`

### test_group_reply_image_disable_switch_still_keeps_extracted_urls
Summary: Tests group reply image disable switch still keeps extracted urls
Asserts:
  - `self.assertTrue(result)`
  - `self.assertFalse(event.get_extra("vision_direct_selected"))`
  - `self.assertEqual(event.get_extra("vision_direct_skip_reason"), "disabled")`
  - `self.assertEqual(event.get_extra("extracted_image_urls"), ["reply.jpg"])`

### test_group_pure_reply_image_is_not_dropped_as_empty_message
Summary: Tests group pure reply image is not dropped as empty message
Asserts:
  - `self.assertTrue(result)`
  - `self.assertEqual(event.get_extra("extracted_image_urls"), ["reply.jpg"])`

### test_remote_url_only_image_is_not_selected_for_direct_vision
Summary: Tests remote url only image is not selected for direct vision
Asserts:
  - `self.assertTrue(result)`
  - `self.assertEqual(event.get_extra("extracted_image_urls"), [])`
  - `self.assertFalse(event.get_extra("direct_vision_urls"))`
  - `self.assertEqual(event.get_extra("vision_direct_skip_reason"), "not_direct_path")`

## tests/original_ported/test_planner_follow_up_ported.py (6 tests)

### test_should_follow_up_awaits_state_engine
Summary: Tests should follow up awaits state engine
Asserts:
  - `self.assertIsNone(result)`
  - `self.assertTrue(called["awaited"])`

### test_follow_up_skips_lightweight_group_tools_and_boundary_without_llm
Summary: Tests follow up skips lightweight group tools and boundary without llm
Asserts:
  - `self.assertIsNone(result)`
  - `self.assertEqual(snapshot.skipped_reason, reason)`
  - `self.assertEqual(gateway.calls, 0)`

### test_follow_up_skips_cooldown_length_and_question
Summary: Tests follow up skips cooldown length and question
Asserts:
  - `self.assertIsNone(result)`
  - `self.assertEqual(snapshot.skipped_reason, reason)`
  - `self.assertEqual(gateway.calls, 0)`

### test_follow_up_probability_gate_avoids_llm
Summary: Tests follow up probability gate avoids llm
Asserts:
  - `self.assertIsNone(result)`
  - `self.assertEqual(snapshot.skipped_reason, "probability_gate")`
  - `self.assertEqual(snapshot.probability, 0.08)`
  - `self.assertFalse(snapshot.llm_checked)`
  - `self.assertEqual(gateway.calls, 0)`

### test_comfort_short_reply_uses_rule_and_sets_cooldown
Summary: Tests comfort short reply uses rule and sets cooldown
Asserts:
  - `self.assertEqual(result, "gentle_support")`
  - `self.assertTrue(snapshot.followed)`
  - `self.assertFalse(snapshot.llm_checked)`
  - `self.assertEqual(gateway.calls, 0)`
  - `self.assertIsNone(second)`
  - `self.assertEqual(next_snapshot.skipped_reason, "follow_up_cooldown")`

### test_follow_up_llm_path_records_checked_and_cooldown
Summary: Tests follow up llm path records checked and cooldown
Asserts:
  - `self.assertEqual(result, "tiny_extra")`
  - `self.assertTrue(snapshot.llm_checked)`
  - `self.assertTrue(snapshot.followed)`
  - `self.assertEqual(gateway.calls, 1)`

## tests/unit/presentation/test_message_entry_gap_coverage.py (9 tests)

### test_duplicate_message_stops_event_before_facade_guards
Summary: Tests duplicate message stops event before facade guards
Asserts:
  - `self.assertEqual(asyncio.run(_run()), [])`
  - `self.assertTrue(event.stopped)`
  - `self.assertEqual(facade.calls, [])`

### test_self_message_stops_before_poke_handling
Summary: Tests self message stops before poke handling
Asserts:
  - `self.assertEqual(self._collect(facade, event), [])`
  - `self.assertTrue(event.stopped)`
  - `self.assertEqual(facade.calls, [])`

### test_framework_command_exception_is_caught_and_processing_continues
Summary: Tests framework command exception is caught and processing continues
Asserts:
  - `self.assertEqual(result, [])`
  - `self.assertFalse(event.stopped)`
  - `self.assertIn("scope_access", facade.calls)`
  - `self.assertIn("attention", facade.calls)`

### test_poke_exception_is_caught_and_processing_continues
Summary: Tests poke exception is caught and processing continues
Asserts:
  - `self.assertEqual(self._collect(facade, event), [])`
  - `self.assertFalse(event.stopped)`
  - `self.assertIn("scope_access", facade.calls)`
  - `self.assertIn("attention", facade.calls)`

### test_framework_command_decision_stops_event
Summary: Tests framework command decision stops event
Asserts:
  - `self.assertEqual(result, [])`
  - `self.assertTrue(event.stopped)`
  - `self.assertNotIn("scope_access", facade.calls)`

### test_scope_access_exception_is_caught_and_denied
Summary: Tests scope access exception is caught and denied
Asserts:
  - `self.assertEqual(self._collect(facade, event), [])`
  - `self.assertTrue(event.stopped)`
  - `self.assertNotIn("group_wait", facade.calls)`

### test_group_wait_exception_yields_error_and_stops_event
Summary: Tests group wait exception yields error and stops event
Asserts:
  - `self.assertEqual(len(result), 1)`
  - `self.assertEqual(result[0]["type"], "plain")`
  - `self.assertTrue(result[0]["text"])`
  - `self.assertTrue(event.stopped)`
  - `self.assertNotIn("reflect", facade.calls)`

### test_attention_error_yields_runtime_fallback_text
Summary: Tests attention error yields runtime fallback text
Asserts:
  - `self.assertEqual(             self._collect(facade, event),             [{"type": "plain", "text": "runtime fallback"}],         )`
  - `self.assertFalse(event.stopped)`

### test_reflect_feedback_yields_response_and_stops_event
Summary: Tests reflect feedback yields response and stops event
Asserts:
  - `self.assertEqual(             self._collect(facade, event),             [{"type": "plain", "text": "review accepted"}],         )`
  - `self.assertTrue(event.stopped)`
  - `self.assertNotIn("attention", facade.calls)`

## tests/test_main_reply_live_providers_refactor.py (10 tests)

### test_live_completion_result_defaults
Summary: Tests live completion result defaults
Asserts:
  - `self.assertEqual(result.text, "ok")`
  - `self.assertEqual(result.usage_input_cached, 0)`
  - `self.assertEqual(result.request_session_id, "")`

### test_openai_client_requires_base_url
Summary: Tests openai client requires base url
Asserts:
  - `self.assertRaises(RuntimeError)`

### test_build_live_provider_client_dispatches_by_family
Summary: Tests build live provider client dispatches by family
Asserts:
  - `self.assertEqual(moonshot.provider_family, "kimi")`
  - `self.assertEqual(anthropic.provider_family, "anthropic")`
  - `self.assertEqual(gemini.provider_family, "gemini")`
  - `self.assertEqual(native_chat.provider_family, "native_chat")`

### test_anthropic_payload_maps_cached_usage
Summary: Tests anthropic payload maps cached usage
Asserts:
  - `self.assertEqual(result.text, "ok")`
  - `self.assertEqual(result.usage_input_tokens, 120)`
  - `self.assertEqual(result.usage_input_cached, 80)`
  - `self.assertEqual(result.usage_output_tokens, 12)`
  - `self.assertTrue(result.cached_usage_supported)`

### test_gemini_payload_marks_cached_usage_unsupported_when_field_missing
Summary: Tests gemini payload marks cached usage unsupported when field missing
Asserts:
  - `self.assertEqual(result.text, "ok")`
  - `self.assertEqual(result.usage_input_tokens, 90)`
  - `self.assertEqual(result.usage_input_cached, 0)`
  - `self.assertFalse(result.cached_usage_supported)`

### test_openai_payload_uses_prompt_tokens_details_cached_tokens_first
Summary: Tests openai payload uses prompt tokens details cached tokens first
Asserts:
  - `self.assertEqual(result.text, "ok")`
  - `self.assertEqual(result.usage_input_cached, 60)`
  - `self.assertTrue(result.cached_usage_supported)`

### test_base_client_splits_multiple_api_keys
Summary: Tests base client splits multiple api keys
Asserts:
  - `self.assertEqual(client.api_key_pool, ["key-a", "key-b", "key-c"])`
  - `self.assertEqual(client.api_key, "key-a")`

### test_base_client_session_uses_trust_env
Summary: Tests base client session uses trust env
Asserts:
  - `self.assertTrue(mock_session.call_args.kwargs.get("trust_env"))`

### test_gemini_retries_with_next_api_key
Summary: Tests gemini retries with next api key
Asserts:
  - `self.assertEqual(result.text, "ok")`
  - `self.assertEqual(len(calls), 2)`
  - `self.assertIn("key=key-b", calls[-1])`

### test_openai_client_retries_same_key_on_retryable_failure
Summary: Tests openai client retries same key on retryable failure
Asserts:
  - `self.assertEqual(result.text, "ok")`
  - `self.assertEqual(len(calls), 2)`
  - `self.assertEqual(calls[0][1]["Authorization"], "Bearer sk-test")`

## tests/test_scheduler_fixture_refactor.py (3 tests)

### test_fixture_profiles_reset_summary_and_assets_are_repeatable
Summary: Tests fixture profiles reset summary and assets are repeatable
Asserts:
  - `self.assertEqual(scheduler_summary["profile"], "scheduler_only")`
  - `self.assertEqual(admin_summary["profile"], "admin_full")`
  - `self.assertEqual(host_summary["profile"], "acceptance_host")`
  - `self.assertLess(scheduler_summary["user_count"], admin_summary["user_count"])`
  - `self.assertLess(scheduler_summary["memory_event_count"], admin_summary["memory_event_count"])`
  - `self.assertGreater(host_summary["runtime_snapshots"], admin_summary["runtime_snapshots"] - 1)`
  - `self.assertTrue(Path(admin_summary["db_path"]).exists())`
  - `self.assertTrue(Path(admin_summary["config_path"]).exists())`
  - `self.assertTrue(Path(admin_summary["persona_cache_path"]).exists())`
  - `self.assertTrue(Path(admin_summary["direct_open_harness_path"]).exists())`
  - `self.assertTrue(baseline_doc.exists())`
  - `self.assertIn("iframe", baseline_doc.read_text(encoding="utf-8"))`
  - `self.assertGreater(count_dirty, admin_summary["table_counts"]["UserProfile"])`
  - `self.assertEqual(count_reset, reset_summary["table_counts"]["UserProfile"])`

### test_admin_full_fixture_supports_backend_service_views
Summary: Tests admin full fixture supports backend service views
Asserts:
  - `self.assertEqual(dashboard["total_users"], 3)`
  - `self.assertGreaterEqual(dashboard["total_canonical_memories"], 6)`
  - `self.assertEqual(admin_status["data"]["scheduler_policy"]["active_profile"], "balanced")`
  - `self.assertGreaterEqual(admin_status["data"]["overview"]["due_chat_count"], 1)`
  - `self.assertEqual(len(pending_reviews), 1)`
  - `self.assertGreaterEqual(all_reviews["total"], 2)`
  - `self.assertGreaterEqual(len(events), 3)`
  - `self.assertGreaterEqual(len(reflections), 2)`
  - `self.assertGreaterEqual(len(nodes), 2)`
  - `self.assertGreaterEqual(len(jargon), 2)`
  - `self.assertGreaterEqual(canonical["total"], 6)`
  - `self.assertEqual(len(users), 3)`
  - `self.assertEqual(persona["status"], "ok")`
  - `self.assertTrue(persona["data"]["summary"])`
  - `self.assertEqual(persona["data"]["persona_id"], "fixture-persona")`

### test_acceptance_host_direct_open_harness_contains_bridge_stub_contract
Summary: Tests acceptance host direct open harness contains bridge stub contract
Asserts:
  - `self.assertIn("window.AstrBotPluginPage", harness)`
  - `self.assertIn("ready: async", harness)`
  - `self.assertIn("apiGet: async", harness)`
  - `self.assertIn("apiPost: async", harness)`
  - `self.assertIn('const fixtureBase = "http://127.0.0.1:8765"', harness)`
  - `self.assertIn('href="http://127.0.0.1:8766/pages/admin/style.css"', harness)`
  - `self.assertIn('src="http://127.0.0.1:8766/pages/admin/app.js"', harness)`
  - `self.assertNotIn('/api/auth/login', harness)`
  - `self.assertNotIn('password: "astrmai_admin"', harness)`
  - `self.assertNotIn('"Authorization": token ? `Bearer ${token}` : ""', harness)`
  - `self.assertIn('clean.startsWith("admin/") ? clean.slice("admin/".length) : clean', harness)`
  - `self.assertIn("绕开 AstrBot 宿主页 iframe 边界", harness)`
  - `self.assertIn("AstrMai 管理台直开验收页", harness)`

## tests/integration/test_memory_write_retrieve_inject.py (5 tests)

### test_identity_turn_can_be_written_into_canonical_store
Summary: Tests identity turn can be written into canonical store
Asserts:
  - `self.assertIsNotNone(row)`
  - `self.assertEqual(row.kind, "identity")`
  - `self.assertIn("小明", row.content)`

### test_food_preferences_roundtrip_through_retrieval
Summary: Tests food preferences roundtrip through retrieval
Asserts:
  - `self.assertIn("火锅", rendered)`
  - `self.assertIn("芒果", rendered)`

### test_food_preference_query_ranks_food_above_weak_like_matches
Summary: Tests food preference query ranks food above weak like matches
Asserts:
  - `self.assertTrue(candidates)`
  - `self.assertIn("火锅", candidates[0].content)`
  - `self.assertNotIn("蓝色", candidates[0].content)`

### test_weak_memory_query_does_not_return_broad_like_matches
Summary: Tests weak memory query does not return broad like matches
Asserts:
  - `self.assertEqual(candidates, [])`

### test_retrieved_memory_is_rendered_into_prompt_bundle
Summary: Tests retrieved memory is rendered into prompt bundle
Asserts:
  - `self.assertTrue(bundle.rendered_prompt_block)`
  - `self.assertIn("火锅", bundle.rendered_prompt_block)`
  - `self.assertTrue(bundle.items)`

## tests/unit/state/test_relationship_profile_roundtrip_migrated.py (4 tests)

### test_get_user_profile_does_not_rebuild_runtime_vector_from_social_score
Summary: Tests get user profile does not rebuild runtime vector from social score
Asserts:
  - `self.assertGreater(updated_score, initial_score)`
  - `self.assertAlmostEqual(engine.relationship_engine.get_social_score("user-1"), updated_score)`
  - `self.assertAlmostEqual(second_profile.social_score, updated_score)`
  - `self.assertIn("trust", second_profile.relationship_vector)`
  - `self.assertGreater(second_profile.relationship_vector["trust"], first_trust)`

### test_update_social_score_from_fact_keeps_runtime_vector_in_sync
Summary: Tests update social score from fact keeps runtime vector in sync
Asserts:
  - `self.assertAlmostEqual(after_score, before_score + 3.0)`
  - `self.assertAlmostEqual(vector_score, after_score)`
  - `self.assertGreater(vector.trust, 6.0)`
  - `self.assertGreater(vector.familiarity, 5.0)`
  - `self.assertGreater(vector.emotion_bond, 6.0)`
  - `self.assertGreater(vector.respect, 3.0)`

### test_affection_is_unified_across_groups_for_same_user
Summary: Tests affection is unified across groups for same user
Asserts:
  - `self.assertNotEqual(after_first, before)`
  - `self.assertGreater(after_second, after_first)`
  - `self.assertIn("user-cross-group", engine.relationship_engine._vectors)`
  - `self.assertNotIn(("user-cross-group", "group-a"), engine.relationship_engine._vectors)`

### test_relationship_vector_roundtrip_preserves_last_decay_time
Summary: Tests relationship vector roundtrip preserves last decay time
Asserts:
  - `self.assertEqual(loaded["relationship_vector"]["last_decay_time"], 123456.0)`
  - `self.assertEqual(loaded["relationship_vector"]["trust"], 10.0)`

## tests/test_turn_context_refactor.py (6 tests)

### test_turn_context_attaches_once_and_keeps_layered_state
Summary: Tests turn context attaches once and keeps layered state
Asserts:
  - `assert first is second`
  - `assert get_turn_context(event) is first`
  - `assert second.perception.chat_id == "chat-1"`
  - `assert second.cognitive.social_intent == "answer"`

### test_turn_context_is_not_created_from_unrelated_extra_value
Summary: Tests turn context is not created from unrelated extra value
Asserts:
  - `assert get_turn_context(event) is context`
  - `assert isinstance(context, TurnContext)`

### test_tool_decision_trace_records_filter_steps
Summary: Tests tool decision trace records filter steps
Asserts:
  - `assert trace.filter_reasons == ["cooldown(meme)"]`
  - `assert trace.filter_steps[0]["removed"] == ["proactive_meme"]`
  - `assert trace.filter_steps[0]["category"] == "cooldown"`
  - `assert trace.removed_by_cooldown == ["proactive_meme"]`

### test_tool_decision_trace_records_stance_filtered_tools
Summary: Tests tool decision trace records stance filtered tools
Asserts:
  - `assert trace.filter_reasons == ["stance_guarded_guard"]`
  - `assert trace.filter_steps[0]["removed"] == ["proactive_meme", "proactive_poke"]`
  - `assert trace.removed_by_stance == ["proactive_meme", "proactive_poke"]`

### test_turn_trace_summary_hides_inner_monologue_and_prompt_text
Summary: Tests turn trace summary hides inner monologue and prompt text
Asserts:
  - `assert summary["perception"]["sender_name"] == "Alice"`
  - `assert summary["attention"]["focus_preview"] == "Alice: hello there"`
  - `assert summary["continuity"]["current_topic_preview"] == "Alice: hello there"`
  - `assert summary["continuity"]["current_goal_preview"] == "answer Alice naturally"`
  - `assert summary["continuity"]["goal_status"] == "continuing"`
  - `assert summary["continuity"]["continuity_weight"] == "weak"`
  - `assert summary["continuity"]["turn_count"] == 2`
  - `assert summary["memory"]["policy"] == "deep"`
  - `assert summary["cognitive"]["think_level"] == 2`
  - `assert summary["cognitive"]["think_reason"] == "deeper_reasoning"`
  - `assert summary["cognitive"]["think_signals"] == ["complexity_keyword"]`
  - `assert summary["cognitive"]["cognitive_loop_ran"] is False`
  - `assert summary["cognitive"]["cognitive_loop_skipped_reason"] == "cooldown_simple_turn"`
  - `assert summary["cognitive"]["cognitive_loop_skip_signals"] == ["sharp_reply"]`
  - `assert summary["cognitive"]["readonly_tools_allowed"] is False`
  - `assert summary["cognitive"]["readonly_tools_skip_reason"] == "think_level_2_blocks_readonly_tool"`
  - `assert summary["memory"]["source"] == "react"`
  - `assert summary["memory"]["retrieve_keys"] == ["timeline"]`
  - `assert summary["memory"]["injected"] is True`
  - `assert summary["memory"]["summary_preview"].endswith("...")`
  - `assert summary["follow_up"]["skipped_reason"] == "probability_gate"`
  - `assert summary["follow_up"]["probability"] == 0.08`
  - `assert summary["follow_up"]["llm_checked"] is False`
  - `assert summary["follow_up"]["followed"] is False`
  - `assert summary["side_inputs"]["timings"][0]["name"] == "memory_feedback"`
  - `assert summary["side_inputs"]["timings"][1]["ok"] is False`
  - `assert summary["tools"]["removed_by_energy"] == ["full_tier"]`
  - `assert summary["tools"]["removed_by_cooldown"] == ["proactive_meme"]`
  - `assert summary["tools"]["removed_by_stance"] == ["construct_at_event"]`
  - `assert "secret private thought" not in rendered`
  - `assert "secret system prompt" not in rendered`
  - `assert "secret user prompt" not in rendered`

### test_turn_trace_summary_uses_ascii_truncation_marker
Summary: Tests turn trace summary uses ascii truncation marker
Asserts:
  - `assert summary["perception"]["text_preview"].endswith("...")`
  - `assert "…" not in summary["perception"]["text_preview"]`

## tests/test_state_bar_audit_refactor.py (2 tests)

### test_build_state_bar_audit_baseline_exposes_current_mood_and_stance_findings
Summary: Tests build state bar audit baseline exposes current mood and stance findings
Asserts:
  - `self.assertEqual(baseline["title"], "P10.2 / P10.3 audit baseline")`
  - `self.assertEqual(mood["audit_mode"], "static_and_chain_level")`
  - `self.assertEqual(mood["live_llm_semantic_audit"]["status"], "not_run")`
  - `self.assertIn("ASTRMAI_ENABLE_LIVE_MOOD_AUDIT", mood["live_llm_semantic_audit"]["reason"])`
  - `self.assertEqual(mood["summary"]["parser_failures"], [])`
  - `self.assertEqual(mood["summary"]["fallback_issue_counts"]["mixed_affect_flattened"], 0)`
  - `self.assertEqual(mood["summary"]["fallback_issue_counts"]["direction_conflict"], 0)`
  - `self.assertEqual(sarcasm_case["primary_mood_tag"], "angry")`
  - `self.assertEqual(mixed_case["primary_mood_tag"], "sad")`
  - `self.assertEqual(social_score["audit_mode"], "static_and_host_chain_semantics")`
  - `self.assertEqual(social_score["summary"]["issue_case_ids"], [])`
  - `self.assertTrue(social_score["summary"]["mixed_affect_remap_suppressed"])`
  - `self.assertTrue(social_score["summary"]["positive_layering_ok"])`
  - `self.assertTrue(social_score["summary"]["negative_layering_ok"])`
  - `self.assertTrue(social_score["summary"]["publish_change_semantics_aligned"])`
  - `self.assertLess(social_score["summary"]["mixed_affect_social_score"], 1.0)`
  - `self.assertEqual(mixed_social["effective_event_type"], "normal_chat")`
  - `self.assertEqual(mixed_social["published_mood_tag"], "")`
  - `self.assertEqual(mixed_social["published_event_type"], "normal_chat")`
  - `self.assertLess(mixed_social["social_score"], positive_social["social_score"])`
  - `self.assertEqual(comfort_social["effective_event_type"], "normal_chat")`
  - `self.assertLessEqual(comfort_social["social_score"], 0.4)`
  - `self.assertEqual(ambiguous_social["effective_event_type"], "greeting")`
  - `self.assertLessEqual(ambiguous_social["social_score"], 0.24)`
  - `self.assertEqual(cold_social["effective_event_type"], "ignore")`
  - `self.assertLess(cold_social["social_score"], -0.2)`
  - `self.assertGreaterEqual(cold_social["social_score"], -0.30)`
  - `self.assertEqual(perfunctory_social["effective_event_type"], "ignore")`
  - `self.assertLessEqual(perfunctory_social["social_score"], -0.30)`
  - `self.assertEqual(irritation_social["effective_event_type"], "ignore")`
  - `self.assertLessEqual(irritation_social["social_score"], -0.40)`
  - `self.assertEqual(long_mixed_social["effective_event_type"], "normal_chat")`
  - `self.assertLessEqual(long_mixed_social["social_score"], 0.4)`
  - `self.assertGreater(tool_social["social_score"], mixed_social["social_score"])`
  - `self.assertGreater(mixed_social["social_score"], ambiguous_social["social_score"])`
  - `self.assertGreater(cold_social["social_score"], perfunctory_social["social_score"])`
  - `self.assertGreater(perfunctory_social["social_score"], irritation_social["social_score"])`
  - `self.assertEqual(stance["audit_mode"], "chain_level_plus_prompt_surface")`
  - `self.assertTrue(stance["summary"]["guarded_tool_constraints_present"])`
  - `self.assertTrue(stance["summary"]["cool_tool_constraints_present"])`
  - `self.assertLess(             stance["summary"]["guarded_follow_up_probability"],             stance["summary"]["neutral_follow_up_probability"],         )`
  - `self.assertFalse(stance["summary"]["first_reply_constraints_are_prompt_only"])`
  - `self.assertTrue(guarded_answer["first_reply_hard_constraint_present"])`
  - `self.assertEqual(guarded_answer["first_reply_surface_mode"], "hard_clamped")`
  - `self.assertLess(guarded_boundary["stance_char_cap"], guarded_answer["stance_char_cap"])`
  - `self.assertGreater(cool_comfort["stance_char_cap"], guarded_answer["stance_char_cap"])`
  - `self.assertGreaterEqual(guarded_answer["stance_sentence_cap"], 1)`

### test_write_state_bar_audit_artifacts_emits_json_and_markdown
Summary: Tests write state bar audit artifacts emits json and markdown
Asserts:
  - `self.assertTrue(json_path.exists())`
  - `self.assertTrue(markdown_path.exists())`
  - `self.assertTrue(Path(result["social_score_json_path"]).exists())`
  - `self.assertTrue(Path(result["social_score_markdown_path"]).exists())`
  - `self.assertEqual(payload["title"], "P10.2 / P10.3 audit baseline")`
  - `self.assertIn("# P10.2 / P10.3", markdown)`
  - `self.assertIn("## social_score", markdown)`
  - `self.assertIn("guarded follow-up probability", markdown)`
  - `self.assertIn("deterministic first-reply text constraints", markdown)`
  - `self.assertIn("live LLM semantic audit", markdown)`
  - `self.assertIn("mixed affect remap suppressed", social_markdown)`

## tests/test_wave3_low_robustness_regression.py (9 tests)

### test_dream_detail_surfaces_db_errors
Summary: Tests dream detail surfaces db errors
Asserts:
  - `self.assertIn("db locked", result)`
  - `self.assertNotIn("未找到", result)`

### test_group_wait_info_uses_monotonic_clock
Summary: Tests group wait info uses monotonic clock
Asserts:
  - `self.assertGreater(info["remaining_seconds"], 0.0)`

### test_save_pattern_tracks_canonical_background_task_when_lifecycle_exists
Summary: Tests save pattern tracks canonical background task when lifecycle exists
Asserts:
  - `self.assertEqual(len(tracked), 1)`

### test_goal_parser_rejects_non_string_goal_values
Summary: Tests goal parser rejects non string goal values
Asserts:
  - `self.assertEqual([item.goal for item in goals], ["valid"])`

### test_memory_injection_trace_persist_failure_is_logged
Summary: Tests memory injection trace persist failure is logged
Asserts:
  - `self.assertTrue(warnings)`
  - `self.assertTrue(warnings[0][1].get("exc_info"))`

### test_memory_context_builder_does_not_emit_ellipsis_only_line
Summary: Tests memory context builder does not emit ellipsis only line
Asserts:
  - `self.assertNotIn("\n...", rendered)`
  - `self.assertEqual(rendered, "")`

### test_unknown_profile_memory_categories_do_not_become_speech_style
Summary: Tests unknown profile memory categories do not become speech style
Asserts:
  - `self.assertEqual(categorized["speech_style_points"], [])`

### test_decay_service_degrades_when_state_or_profile_listing_fails
Summary: Tests decay service degrades when state or profile listing fails
Asserts:
  - `self.assertTrue(engine.profile_decay_called)`

### test_dashboard_snapshot_degrades_system_metrics_failures
Summary: Tests dashboard snapshot degrades system metrics failures
Asserts:
  - `self.assertEqual(snapshot["db_size_kb"], 0)`
  - `self.assertEqual(snapshot["webui_mem_mb"], 0)`
  - `self.assertEqual(snapshot["total_users"], 1)`

## tests/test_lifecycle_shutdown_regression.py (4 tests)

### test_terminate_runs_shutdown_order_and_resets_runtime_flags
Summary: Tests terminate runs shutdown order and resets runtime flags
Asserts:
  - `self.assertEqual(                 calls[:8],                 [                     "memory_pipeline.stop",                     "private_chat.persist",                     "proactive.stop",                     "expression.stop",                    ...`
  - `self.assertEqual(runtime.status.boot_phase, "shutdown.complete")`
  - `self.assertFalse(runtime.status.is_running)`
  - `self.assertFalse(runtime.status.lifecycle_started)`
  - `self.assertFalse(runtime.status.bootstrap_completed)`
  - `self.assertFalse(runtime.status.boot_logged)`
  - `self.assertFalse(runtime.status.work_mode_enabled)`
  - `self.assertFalse(runtime.status.memory_initialized)`
  - `self.assertFalse(runtime.status.proactive_started)`
  - `self.assertFalse(runtime.status.visual_started)`
  - `self.assertFalse(runtime.status.cron_guard_started)`
  - `self.assertFalse(runtime.status.foreign_commands_loaded)`
  - `self.assertEqual(runtime.runtime_coordinator._states, {})`

### test_terminate_cancels_tracked_background_tasks
Summary: Tests terminate cancels tracked background tasks
Asserts:
  - `self.assertTrue(task.cancelled())`
  - `self.assertIn("event_bus.stop", calls)`
  - `self.assertIn("persistence.dispose", calls)`

### test_terminate_continues_when_tail_shutdown_components_fail
Summary: Tests terminate continues when tail shutdown components fail
Asserts:
  - `self.assertIn("event_bus.stop", calls)`
  - `self.assertIn("persistence.dispose", calls)`
  - `self.assertEqual(runtime.status.boot_phase, "shutdown.complete")`
  - `self.assertFalse(runtime.status.lifecycle_started)`

### test_terminate_continues_when_head_shutdown_component_fails
Summary: Tests terminate continues when head shutdown component fails
Asserts:
  - `self.assertEqual(calls[0], "memory_pipeline.stop")`
  - `self.assertIn("private_chat.persist", calls)`
  - `self.assertIn("proactive.stop", calls)`
  - `self.assertIn("expression.stop", calls)`
  - `self.assertIn("cron_guard.stop", calls)`
  - `self.assertIn("visual_cortex.stop", calls)`
  - `self.assertIn("event_bus.stop", calls)`
  - `self.assertIn("persistence.dispose", calls)`
  - `self.assertEqual(runtime.status.boot_phase, "shutdown.complete")`
  - `self.assertFalse(runtime.status.lifecycle_started)`

## tests/test_wave1_correctness_regression.py (4 tests)

### test_success_artifact_failures_do_not_turn_model_success_into_failure
Summary: Tests success artifact failures do not turn model success into failure
Asserts:
  - `self.assertTrue(result.ok)`
  - `self.assertEqual(result.text, "visible reply")`
  - `self.assertEqual(stats["failures"], 0)`
  - `self.assertEqual(stats["calls"], 1)`

### test_json_success_survives_usage_logging_failure
Summary: Tests json success survives usage logging failure
Asserts:
  - `self.assertTrue(result.ok)`
  - `self.assertEqual(result.parsed_json, {"answer": "ok"})`
  - `self.assertEqual(stats["failures"], 0)`
  - `self.assertEqual(stats["calls"], 1)`

### test_reflector_weight_failure_keeps_batch_for_retry
Summary: Tests reflector weight failure keeps batch for retry
Asserts:
  - `self.assertEqual(             [item["pattern_id"] for item in reflector._pending_reflections],             [str(index) for index in range(10)],         )`

### test_bm25_orders_more_relevant_document_first_and_normalizes_high
Summary: Tests bm25 orders more relevant document first and normalizes high
Asserts:
  - `self.assertEqual([item.doc_id for item in results], [1, 2])`
  - `self.assertEqual(results[0].score, 1.0)`
  - `self.assertEqual(results[1].score, 0.0)`

## tests/test_think_level_policy_refactor.py (10 tests)

### test_think_level_zero_for_lightweight_short_core_and_ambient_group
Summary: Tests think level zero for lightweight short core and ambient group
Asserts:
  - `assert policy.decide(event=_Event("poke", extras={"astrmai_lightweight_event": True})).level == 0`
  - `assert policy.decide(event=_Event("哈哈")).level == 0`
  - `assert policy.decide(event=_Event("hello", extras={"retrieve_keys": ["CORE_ONLY"]}), retrieve_keys=["CORE_ONLY"]).level == 0`
  - `assert ambient.level == 0`
  - `assert ambient.reason == "group_non_direct"`
  - `assert latest.level == 0`
  - `assert latest.reason == "group_non_direct"`

### test_think_level_one_for_direct_normal_turns
Summary: Tests think level one for direct normal turns
Asserts:
  - `assert private.level == 1`
  - `assert at_bot.level == 1`

### test_think_level_two_for_complex_emotional_and_memory_reference_turns
Summary: Tests think level two for complex emotional and memory reference turns
Asserts:
  - `assert policy.decide(event=_Event("为什么会这样？", group_id="")).level == 2`
  - `assert policy.decide(event=_Event("please analyze this a little", group_id="")).level == 2`
  - `assert policy.decide(event=_Event("我有点难受，想要安慰", group_id="")).level == 2`
  - `assert policy.decide(event=_Event("刚才那件事继续说说", group_id="")).level == 2`

### test_think_level_three_for_tool_sys3_and_deep_memory_intents
Summary: Tests think level three for tool sys3 and deep memory intents
Asserts:
  - `assert policy.decide(event=_Event("帮我查一下这个人是谁", group_id="")).level == 3`
  - `assert policy.decide(event=_Event("你还记得我上次说什么吗", group_id="")).level == 3`
  - `assert policy.decide(event=_Event("please search this", group_id="")).level == 3`
  - `assert policy.decide(event=_Event("hello", group_id=""), judge_action="TOOL_CALL", is_tool_call_mode=True).level == 3`
  - `assert policy.decide(event=_Event("hello", group_id="", extras={"astrmai_cognitive_memory_policy": "deep"})).level == 3`

### test_heartflow_posture_keeps_budget_above_fast_path
Summary: Tests heartflow posture keeps budget above fast path
Asserts:
  - `assert decision.level == 2`
  - `assert decision.reason == "heartflow_posture"`
  - `assert "heartflow_pulse_join" in decision.signals`
  - `assert "heartflow_high_interest" in decision.signals`

### test_heartflow_frequency_guard_keeps_non_direct_group_fast
Summary: Tests heartflow frequency guard keeps non direct group fast
Asserts:
  - `assert decision.level == 0`
  - `assert decision.reason == "heartflow_frequency_guard"`
  - `assert "heartflow_high_insert_pressure" in decision.signals`
  - `assert "heartflow_low_candidate_score" in decision.signals`

### test_heartflow_action_observe_keeps_non_direct_group_fast
Summary: Tests heartflow action observe keeps non direct group fast
Asserts:
  - `assert decision.level == 0`
  - `assert decision.reason == "group_non_direct"`

### test_direct_question_with_high_interest_gets_deep_budget
Summary: Tests direct question with high interest gets deep budget
Asserts:
  - `assert decision.level == 2`
  - `assert decision.reason == "heartflow_direct_question"`

### test_proactive_event_uses_bounded_budget_and_blocks_tool_escalation
Summary: Tests proactive event uses bounded budget and blocks tool escalation
Asserts:
  - `assert decision.level == 1`
  - `assert decision.reason == "proactive_opening"`
  - `assert "proactive_event" in decision.signals`
  - `assert urgent_decision.level == 2`
  - `assert urgent_decision.reason == "proactive_high_urgency_with_continuity"`

### test_sharp_and_long_cooldowns_skip_simple_turns_only
Summary: Tests sharp and long cooldowns skip simple turns only
Asserts:
  - `assert simple.level == 0`
  - `assert simple.reason in {"short_ack", "cooldown_simple_turn"}`
  - `assert tool.level == 3`
  - `assert meme_cooldown.level == 1`

## tests/regression/state/test_decay_service_migrated.py (4 tests)

### test_run_once_persists_chat_decay_and_unifies_relationship_truth
Summary: Tests run once persists chat decay and unifies relationship truth
Asserts:
  - `self.assertTrue(persistence.saved_states)`
  - `self.assertLess(state.mood, 0.5)`
  - `self.assertAlmostEqual(profile.social_score, 19.0)`
  - `self.assertTrue(persistence.saved_profiles)`
  - `self.assertAlmostEqual(persistence.saved_profiles[-1]["social_score"], 19.0)`

### test_run_once_small_social_scores_move_toward_zero
Summary: Tests run once small social scores move toward zero
Asserts:
  - `self.assertAlmostEqual(positive.social_score, 4.0)`
  - `self.assertAlmostEqual(negative.social_score, -2.0)`

### test_run_once_small_fractional_scores_do_not_cross_zero
Summary: Tests run once small fractional scores do not cross zero
Asserts:
  - `self.assertAlmostEqual(positive.social_score, 0.0)`
  - `self.assertAlmostEqual(negative.social_score, 0.0)`

### test_memory_decay_failure_is_not_retried_again_on_same_day
Summary: Tests memory decay failure is not retried again on same day
Asserts:
  - `self.assertEqual(memory_engine.calls, 1)`

## tests/unit/state/test_state_subservices_migrated.py (8 tests)

### test_frequency_controller_honors_mentions
Summary: Tests frequency controller honors mentions
Asserts:
  - `self.assertTrue(controller.should_reply('chat-1', is_mentioned=True))`

### test_frequency_controller_drops_dense_replies_when_probability_misses
Summary: Tests frequency controller drops dense replies when probability misses
Asserts:
  - `self.assertFalse(controller.should_reply('chat-1', energy=0.2, mood=-0.6))`

### test_frequency_controller_concurrent_access_keeps_single_record
Summary: Tests frequency controller concurrent access keeps single record
Asserts:
  - `self.assertEqual(len(record_ids), 1)`
  - `self.assertTrue(all(result for _, result in results))`
  - `self.assertEqual(len(record.reply_timestamps), 40)`
  - `self.assertGreater(record.last_message_time, 0.0)`

### test_frequency_controller_concurrent_cleanup_and_updates_keep_records_valid
Summary: Tests frequency controller concurrent cleanup and updates keep records valid
Asserts:
  - `self.assertIsInstance(post_cleanup_record, ChatReplyRecord)`
  - `self.assertIsInstance(chat_id, str)`
  - `self.assertIsInstance(record, ChatReplyRecord)`
  - `self.assertIsInstance(record.reply_timestamps, list)`
  - `self.assertIsInstance(record.last_message_time, float)`

### test_relationship_engine_process_event_updates_social_score
Summary: Tests relationship engine process event updates social score
Asserts:
  - `self.assertGreater(after, before)`
  - `self.assertGreater(engine.get_or_create('user-1').trust, 0.0)`

### test_energy_manager_uses_safe_defaults_when_energy_config_missing
Summary: Tests energy manager uses safe defaults when energy config missing
Asserts:
  - `self.assertTrue(should_drop)`
  - `self.assertAlmostEqual(manager.get_reply_cost(), 0.1)`
  - `self.assertAlmostEqual(state.energy, 0.25)`
  - `self.assertTrue(state.is_dirty)`

### test_apply_natural_decay_recovers_energy_only_once_per_silence_window
Summary: Tests apply natural decay recovers energy only once per silence window
Asserts:
  - `self.assertAlmostEqual(first_energy, 0.5)`
  - `self.assertAlmostEqual(state.energy, 0.5)`
  - `self.assertAlmostEqual(state.last_passive_decay_time, first_decay_time)`

### test_relationship_engine_streak_bonus_applies_once_per_event
Summary: Tests relationship engine streak bonus applies once per event
Asserts:
  - `self.assertAlmostEqual(after['trust'], round(expected_trust, 2))`
  - `self.assertAlmostEqual(after['familiarity'], round(expected_familiarity, 2))`
  - `self.assertAlmostEqual(after['emotion_bond'], round(expected_emotion, 2))`
  - `self.assertAlmostEqual(after['respect'], round(expected_respect, 2))`
  - `self.assertGreater(after_score, 0.0)`

## tests/original_ported/test_attention_private_chat_ported.py (3 tests)

### test_constructor_stores_private_chat_manager
Summary: Tests constructor stores private chat manager
Asserts:
  - `self.assertIs(gate.private_chat_manager, manager)`

### test_private_chat_message_signals_wait_manager
Summary: Tests private chat message signals wait manager
Asserts:
  - `self.assertEqual(result, "PRIVATE_WAIT")`
  - `self.assertEqual(manager.calls, [("user-1", "hello", "default:FriendMessage:user-1")])`

### test_fast_wakeup_path_marks_runtime_activity
Summary: Tests fast wakeup path marks runtime activity
Asserts:
  - `self.assertEqual(result, "ENGAGED")`
  - `self.assertGreater(event.get_extra("astrmai_timestamp", 0.0), 0.0)`
  - `self.assertEqual(len(runtime_coordinator.calls), 1)`
  - `self.assertEqual(chat_id, "group-1")`
  - `self.assertEqual(sender_id, "user-1")`
  - `self.assertEqual(sender_name, "Alice")`
  - `self.assertEqual(preview, "抱抱")`
  - `self.assertIsNone(thread_signature)`
  - `self.assertEqual(timestamp, event.get_extra("astrmai_timestamp"))`
  - `self.assertEqual(len(sys2_calls), 1)`

## tests/test_p2_prelaunch_regression.py (8 tests)

### test_unknown_provider_disables_native_prompt_cache
Summary: Tests unknown provider disables native prompt cache
Asserts:
  - `self.assertEqual(caps.provider_family, "unknown")`
  - `self.assertFalse(caps.supports_native_prompt_cache)`

### test_raw_trace_store_falls_back_when_replace_is_locked
Summary: Tests raw trace store falls back when replace is locked
Asserts:
  - `self.assertTrue(store.path.exists())`
  - `self.assertEqual(asyncio.run(store.recent(chat_id="chat-1"))[0]["kind"], "test")`

### test_message_scope_handles_nonstandard_event_accessors
Summary: Tests message scope handles nonstandard event accessors
Asserts:
  - `self.assertEqual(scope.sender_id, "")`
  - `self.assertEqual(scope.group_id, "")`

### test_meme_probability_accepts_decimal_string
Summary: Tests meme probability accepts decimal string
Asserts:
  - `self.assertTrue(settings.features.meme_enabled)`

### test_admin_safe_count_respects_memory_event_where_clause
Summary: Tests admin safe count respects memory event where clause
Asserts:
  - `self.assertEqual(count, 2)`
  - `self.assertIn("WHERE session_id = ?", db.calls[0][0])`
  - `self.assertEqual(db.calls[0][1], ("chat-1",))`

### test_capability_overview_degrades_when_describe_status_fails
Summary: Tests capability overview degrades when describe status fails
Asserts:
  - `self.assertEqual(overview["workmode"]["cron_guard"]["running"], False)`
  - `self.assertIn("cron unavailable", overview["workmode"]["cron_guard"]["error"])`

### test_review_dispatcher_backs_off_after_send_failure
Summary: Tests review dispatcher backs off after send failure
Asserts:
  - `self.assertEqual(sleeps, [0.2])`

### test_diary_service_loads_persona_cache_off_event_loop
Summary: Tests diary service loads persona cache off event loop
Asserts:
  - `self.assertEqual(len(to_thread_calls), 1)`

## tests/unit/infrastructure/test_persistence_p2_gap_coverage.py (4 tests)

### test_cron_snapshot_roundtrip_update_and_deactivate
Summary: Tests cron snapshot roundtrip update and deactivate
Asserts:
  - `self.assertEqual(len(active_before), 1)`
  - `self.assertEqual(active_before[0].name, "new")`
  - `self.assertEqual(active_before[0].note, "updated")`
  - `self.assertEqual(active_after, [])`

### test_memory_nodes_reflection_and_retrieval_trace_roundtrip
Summary: Tests memory nodes reflection and retrieval trace roundtrip
Asserts:
  - `self.assertEqual([item.name for item in nodes], ["literal%node"])`
  - `self.assertEqual(reflection.reflection, "daily note")`
  - `self.assertEqual(traces[0].trace_id, "trace-1")`
  - `self.assertEqual(traces[0].final_answer, "answer")`

### test_social_relation_updates_and_entity_resolution_paths
Summary: Tests social relation updates and entity resolution paths
Asserts:
  - `self.assertEqual(relations[0].strength, 1.0)`
  - `self.assertEqual(relations[0].frequency, 2)`
  - `self.assertEqual(resolved_numeric, ("12345", "group-1"))`
  - `self.assertEqual(resolved_log, ("u3", "group-1"))`

### test_persona_cache_handles_missing_invalid_and_save_roundtrip
Summary: Tests persona cache handles missing invalid and save roundtrip
Asserts:
  - `self.assertEqual(host.load_persona_cache(), {})`
  - `self.assertEqual(host.load_persona_cache(), {})`
  - `self.assertEqual(host.load_persona_cache(), {"persona-1": {"summary": "calm"}})`
  - `self.assertEqual(asyncio.run(host.load_persona_cache_async()), {"persona-1": {"summary": "calm"}})`

## tests/test_clock_source_regression.py (7 tests)

### test_group_wait_expires_at_uses_monotonic
Summary: expires_at is set with monotonic() so timeout comparisons work.
Asserts:
  - `self.assertIn("expires_at=monotonic()", source,                       "expires_at must use monotonic()")`

### test_group_wait_handle_incoming_uses_monotonic
Summary: handle_incoming_message must use monotonic() for 'now', not time.time().
Asserts:
  - `self.assertNotIn("time.time()", source,                          "handle_incoming_message must not use time.time()")`
  - `self.assertIn("monotonic()", source,                       "handle_incoming_message must use monotonic()")`

### test_group_wait_timeout_not_immediate
Summary: A freshly-armed wait state should not expire immediately.
Asserts:
  - `self.assertIn(result, ("RESUMED_TIMEOUT", "OBSERVED"),                       f"Fresh wait state should not expire immediately; "                       f"got {result} — this indicates clock source mismatch")`

### test_private_chat_last_message_uses_monotonic
Summary: signal_new_message must use monotonic() for last_message_time.
Asserts:
  - `self.assertIn("last_message_time = monotonic()", source,                       "signal_new_message must use monotonic()")`

### test_private_chat_silence_uses_monotonic
Summary: get_session_info silence_sec must use monotonic() not time.time().
Asserts:
  - `self.assertNotIn("time.time()", source,                          "get_session_info must not use time.time()")`

### test_private_chat_cleanup_uses_monotonic
Summary: cleanup_stale_sessions must use monotonic() for 'now'.
Asserts:
  - `self.assertNotIn("time.time()", source,                          "cleanup_stale_sessions must not use time.time()")`

### test_private_chat_no_immediate_stale
Summary: A fresh session should not appear stale immediately.
Asserts:
  - `self.assertIsNotNone(session_info)`
  - `self.assertLess(silence, 10.0,                         f"Fresh session silence_sec={silence} is too large; "                         f"indicates monotonic/time.time mismatch")`

## tests/regression/state/test_state_engine_mood_migrated.py (4 tests)

### test_analyze_text_mood_alias_parses_markdown_wrapped_json
Summary: Tests analyze text mood alias parses markdown wrapped json
Asserts:
  - `self.assertEqual(tag, 'sad')`
  - `self.assertAlmostEqual(mood_value, -0.2)`

### test_update_mood_delegates_to_analyze_mood
Summary: Tests update mood delegates to analyze mood
Asserts:
  - `self.assertEqual(tag, 'happy')`
  - `self.assertEqual(final_mood, 0.6)`
  - `self.assertEqual(observed['text'], 'hello')`
  - `self.assertEqual(observed['chat_id'], 'chat-1')`
  - `self.assertAlmostEqual(observed['current_mood'], 0.2)`
  - `self.assertAlmostEqual(observed['saved_mood'], 0.6)`

### test_update_mood_falls_back_for_legacy_mood_manager_signature
Summary: Tests update mood falls back for legacy mood manager signature
Asserts:
  - `self.assertEqual(tag, 'neutral')`
  - `self.assertEqual(final_mood, 0.0)`
  - `self.assertEqual(observed['text'], 'legacy hello')`
  - `self.assertAlmostEqual(observed['current_mood'], -0.1)`
  - `self.assertAlmostEqual(observed['saved_mood'], 0.0)`

### test_update_mood_legacy_signature_avoids_false_typeerror_fallback
Summary: Tests update mood legacy signature avoids false typeerror fallback
Asserts:
  - `self.assertRaisesRegex(RuntimeError, r"legacy-path:legacy hello:0\.1:0\.0")`

## tests/test_workmode_router_regression.py (3 tests)

### test_light_tool_handler_is_set_after_get_light_tools
Summary: After get_light_tools_for_planner(), every SubAgent light tool
Asserts:
  - `self.assertGreater(len(light_tools), 0, "Expected at least one SubAgent")`
  - `self.assertIsNotNone(                     tool.handler,                     f"Light tool '{name}' must have handler injected from _raw_agent_map",                 )`

### test_raw_agent_map_is_populated_after_get_all_agents
Summary: _raw_agent_map must be populated after get_all_agents() is called.
Asserts:
  - `self.assertGreater(len(agents), 0)`
  - `self.assertGreater(len(agent_map), 0)`
  - `self.assertIn(name, agent_map, f"Agent '{name}' must be in _raw_agent_map")`

### test_full_tools_still_return_real_agents
Summary: get_full_tools_for_direct_entry() must still return actual SubAgent instances.
Asserts:
  - `self.assertGreater(len(full_tools), 0)`
  - `self.assertTrue(                     hasattr(tool, "call") and callable(tool.call),                     f"Full tool '{name}' must have callable call()",                 )`

## tests/unit/learning/test_jargon_pipeline_migrated.py (4 tests)

### test_candidate_extractor_filters_noise_and_keeps_high_frequency_term
Summary: Tests candidate extractor filters noise and keeps high frequency term
Asserts:
  - `self.assertEqual(len(candidates), 1)`
  - `self.assertEqual(candidates[0]["content"], "bigbird")`
  - `self.assertEqual(candidates[0]["count"], 2)`

### test_enricher_degrades_gracefully_when_llm_fails
Summary: Tests enricher degrades gracefully when llm fails
Asserts:
  - `self.assertEqual(len(enriched), 1)`
  - `self.assertEqual(enriched[0]["content"], "bigbird")`
  - `self.assertEqual(enriched[0]["meaning"], "")`
  - `self.assertGreaterEqual(enriched[0]["confidence"], 0.7)`

### test_enricher_never_returns_active_review_status
Summary: Tests enricher never returns active review status
Asserts:
  - `self.assertEqual(enriched[0]["review_status"], "review_pending")`

### test_jargon_retrieval_policy_matches_scene_and_examples
Summary: Tests jargon retrieval policy matches scene and examples
Asserts:
  - `self.assertEqual(by_scene[0].content, "bigbird")`
  - `self.assertEqual(by_example[0].content, "bigbird")`
  - `self.assertEqual(excluded, [])`

## tests/unit/webui/test_webui_gap_coverage.py (7 tests)

### test_admin_api_preserves_canonical_review_ids
Summary: Tests admin api preserves canonical review ids
Asserts:
  - `self.assertEqual(             calls,             [                 ("submit", "mem-review-1"),                 ("update", "mem-review-1"),                 ("delete", "mem-review-1"),             ],         )`

### test_admin_api_preserves_batch_review_ids
Summary: Tests admin api preserves batch review ids
Asserts:
  - `self.assertEqual(calls, [(["mem-review-1", "2"], "approve")])`

### test_admin_api_treats_string_migration_source_as_one_source
Summary: Tests admin api treats string migration source as one source
Asserts:
  - `self.assertEqual(calls, [["legacy_events"]])`

### test_review_item_preserves_zero_weight
Summary: Tests review item preserves zero weight
Asserts:
  - `self.assertEqual(item["weight"], 0.0)`

### test_review_list_clamps_invalid_pagination
Summary: Tests review list clamps invalid pagination
Asserts:
  - `self.assertEqual(result["page"], 1)`
  - `self.assertEqual(result["page_size"], 1)`
  - `self.assertEqual([item["id"] for item in result["items"]], ["review-1"])`

### test_memory_list_clamps_runtime_pagination
Summary: Tests memory list clamps runtime pagination
Asserts:
  - `self.assertEqual(result["status"], "ok")`
  - `self.assertEqual(calls[0]["limit"], 1)`
  - `self.assertEqual(calls[0]["offset"], 0)`

### test_memory_event_preserves_zero_importance
Summary: Tests memory event preserves zero importance
Asserts:
  - `self.assertEqual(result["id"], "mem-event-1")`
  - `self.assertEqual(writes[0].importance, 0.0)`

## tests/regression/memory/test_react_retriever_traces_migrated.py (3 tests)

### test_retrieval_returns_meta_and_saves_trace
Summary: Tests retrieval returns meta and saves trace
Asserts:
  - `self.assertIn("[记忆元信息]", result)`
  - `self.assertIn("人物记忆", result)`
  - `self.assertEqual(len(retriever.db_service.saved_traces), 1)`
  - `self.assertEqual(trace.chat_id, "group-1")`
  - `self.assertIn("person", json.loads(trace.source_layers))`

### test_query_memory_prefers_v2_retrieval_service
Summary: Tests query memory prefers v2 retrieval service
Asserts:
  - `self.assertEqual(result, "v2:Alice v2 memory")`
  - `self.assertEqual(engine.recall_calls, [])`
  - `self.assertEqual(engine.retrieval_service.calls[0].session_id, "chat-1")`
  - `self.assertEqual(engine.retrieval_service.calls[0].policy, "deep")`

### test_query_jargon_prefers_v2_retrieval_service
Summary: Tests query jargon prefers v2 retrieval service
Asserts:
  - `self.assertIn("'开黑': 一起组队玩游戏", result)`
  - `self.assertEqual(db.get_jargon_calls, [])`
  - `self.assertEqual(engine.retrieval_service.calls[0].session_id, "group-1")`
  - `self.assertEqual(engine.retrieval_service.calls[0].layers, ["jargon"])`
  - `self.assertEqual(engine.retrieval_service.calls[0].intent, "jargon")`

## tests/unit/workmode/test_workmode_gap_coverage.py (4 tests)

### test_cron_agent_syncs_string_run_at_without_masking_result
Summary: Tests cron agent syncs string run at without masking result
Asserts:
  - `self.assertEqual(result, "cron task handled")`
  - `self.assertEqual([item.job_id for item in db.saved], ["job-valid", "job-invalid"])`
  - `self.assertEqual(             db.saved[0].run_at,             datetime.fromisoformat("2030-01-02T03:04:05+00:00").timestamp(),         )`
  - `self.assertIsNone(db.saved[1].run_at)`

### test_cron_agent_sync_accepts_json_payload_and_payload_run_at
Summary: Tests cron agent sync accepts json payload and payload run at
Asserts:
  - `self.assertEqual(len(db.saved), 1)`
  - `self.assertEqual(             db.saved[0].run_at,             datetime.fromisoformat("2031-02-03T04:05:06+00:00").timestamp(),         )`

### test_handoff_registry_removes_agents_no_longer_active
Summary: Tests handoff registry removes agents no longer active
Asserts:
  - `self.assertEqual([item.name for item in first], ["dynamic_alpha"])`
  - `self.assertEqual([item.name for item in second], ["dynamic_beta"])`
  - `self.assertEqual(registry.list_loaded_names(), ["dynamic_beta"])`

### test_handoff_registry_replaces_agent_instance_with_same_name
Summary: Tests handoff registry replaces agent instance with same name
Asserts:
  - `self.assertEqual(len(discovered), 1)`
  - `self.assertIs(discovered[0], second_agent)`
  - `self.assertEqual(discovered[0].version, 2)`

## tests/regression/architecture/test_import_boundaries_refactor.py (5 tests)

### test_project_files_do_not_embed_local_absolute_paths
Summary: Tests project files do not embed local absolute paths
Asserts:
  - `self.assertEqual(offenders, [], "Project files should use relative paths or env-configured roots")`

### test_presentation_does_not_reach_into_persistence_internals
Summary: Tests presentation does not reach into persistence internals
Asserts:
  - `self.assertEqual(                     offenders,                     [],                     f"{path} should depend on facades/contracts instead of persistence internals",                 )`

### test_webui_routes_do_not_import_domain_internals
Summary: Tests webui routes do not import domain internals
Asserts:
  - `self.assertEqual(                     offenders,                     [],                     f"{path} should go through webui services/adapters instead of domain internals",                 )`

### test_top_level_refactor_tests_no_longer_use_root_test_helpers
Summary: Tests top level refactor tests no longer use root helpers
Asserts:
  - `self.assertEqual(                     offenders,                     [],                     f"{path} should use local helpers/fixtures instead of root tests.test_* helpers",                 )`

### test_migrated_tests_do_not_import_old_runtime_namespaces
Summary: Tests migrated tests do not import old runtime namespaces
Asserts:
  - `self.assertEqual(                     offenders,                     [],                     f"{path} should import refactor modules instead of old runtime namespaces",                 )`

## tests/unit/memory/test_memory_promotion.py (2 tests)

### test_promotion_engine_promotes_repeated_fact_with_evidence
Summary: Tests promotion engine promotes repeated fact with evidence
Asserts:
  - `self.assertEqual(len(report["promoted"]), 1)`
  - `self.assertEqual(promoted.kind, "fact")`
  - `self.assertEqual(promoted.confidence, 1.0)`
  - `self.assertEqual(promoted.sender_id, "zlj")`
  - `self.assertEqual(metadata["promotion_source"], "dream_audit_pipeline")`
  - `self.assertEqual(metadata["promotion_window_days"], 3)`
  - `self.assertTrue(metadata["authority_eav"])`
  - `self.assertGreaterEqual(metadata["promotion_count"], 3)`
  - `self.assertEqual(promoted_id, metadata.get("promoted_to", promoted_id) if False else promoted_id)`
  - `self.assertEqual(report["promoted"][0]["dedup_key"], "zlj:asset:google_one")`
  - `self.assertTrue(metadata["evidence_turns"])`

### test_promotion_engine_skips_short_term_state_like_anxiety
Summary: Tests promotion engine skips short term state like anxiety
Asserts:
  - `self.assertEqual(report["promoted"], [])`

## tests/original_ported/test_planner_focus_message_priority_ported.py (1 tests)

### test_planner_promotes_focus_thread_and_demotes_ambient_background
Summary: Tests planner promotes focus thread and demotes ambient background
Asserts:
  - `self.assertEqual(focus_event.get_extra("astrmai_raw_user_text"), "Dora: @AstrMai what part feels odd?")`
  - `self.assertEqual(focus_event.get_extra("astrmai_ambient_background_text"), "Carol: I am getting water")`
  - `self.assertIn("Alice: that setting feels odd", envelope.direct_context_text)`
  - `self.assertIn("Bob: yeah, same here", envelope.related_context_text)`
  - `self.assertIn("Carol: I am getting water", envelope.ambient_background_text)`
  - `self.assertEqual(context_engine.calls[0]["event_messages"][-1], focus_event)`

## tests/integration/test_hot_config_consistency.py (3 tests)

### test_hot_config_refreshes_runtime_and_core_components
Summary: Tests hot config refreshes runtime and core components
Asserts:
  - `self.assertTrue(ok)`
  - `self.assertIs(runtime.config, new_config)`
  - `self.assertEqual(runtime.config.reply.fallback_text, "new")`
  - `self.assertIs(reply_service.config, new_config)`
  - `self.assertEqual(reply_service.config.reply.fallback_text, "new")`
  - `self.assertEqual(reply_service.segmentation_threshold, 7)`
  - `self.assertEqual(reply_service.no_segment_limit, 77)`
  - `self.assertEqual(reply_service.meme_probability, 11)`
  - `self.assertIs(memory_engine.config, new_config)`
  - `self.assertEqual(memory_engine.config.memory.recall_top_k, 9)`
  - `self.assertIs(attention_gate.config, new_config)`
  - `self.assertFalse(attention_gate.config.attention.focus_thread_enabled)`

### test_reply_service_refresh_config_is_idempotent_and_preserves_runtime_state
Summary: Tests reply service refresh config is idempotent and preserves runtime state
Asserts:
  - `self.assertIs(reply_service.config, new_config)`
  - `self.assertEqual(reply_service.segmentation_threshold, 8)`
  - `self.assertEqual(reply_service.no_segment_limit, 88)`
  - `self.assertIs(reply_service.runtime_coordinator, runtime_marker)`
  - `self.assertEqual(reply_service.pending_marker, ["in-flight"])`
  - `self.assertIsNotNone(reply_service.segmenter)`

### test_hot_config_rolls_back_all_components_when_refresh_fails
Summary: Tests hot config rolls back all components when refresh fails
Asserts:
  - `self.assertFalse(ok)`
  - `self.assertIs(runtime.config, old_config)`
  - `self.assertEqual(runtime.raw_config, {"reply": {"fallback_text": "old"}})`
  - `self.assertIs(reply_service.config, old_config)`
  - `self.assertEqual(reply_service.config.reply.fallback_text, "old")`
  - `self.assertEqual(reply_service.segmentation_threshold, old_config.reply.segment_min_len)`
  - `self.assertEqual(reply_service.no_segment_limit, old_config.reply.no_segment_max_len)`
  - `self.assertIs(memory_engine.config, old_config)`
  - `self.assertIs(attention_gate.config, old_config)`

## tests/test_p0_prelaunch_regression.py (9 tests)

### test_embedding_auto_fallback_handles_provider_without_meta
Summary: Tests embedding auto fallback handles provider without meta
Asserts:
  - `self.assertEqual(asyncio.run(EmbeddingClient(_Context()).get_vector("hello")), [1.0, 2.0])`

### test_vector_store_skips_malformed_doc_data
Summary: Tests vector store skips malformed doc data
Asserts:
  - `self.assertEqual(len(results), 1)`
  - `self.assertEqual(results[0].doc_id, 7)`
  - `self.assertEqual(results[0].content, "ok")`

### test_dream_update_resolves_legacy_id_to_canonical_memory
Summary: Tests dream update resolves legacy id to canonical memory
Asserts:
  - `self.assertEqual(result, "Updated memory mem_123")`
  - `self.assertEqual(store.updated[0], "mem_123")`

### test_dream_merge_reports_empty_write_as_failure
Summary: Tests dream merge reports empty write as failure
Asserts:
  - `self.assertIn("合并失败", result)`

### test_memory_tool_omni_query_filters_gather_exceptions
Summary: Tests memory tool omni query filters gather exceptions
Asserts:
  - `self.assertEqual(result, "System note: no usable internal data was found.")`
  - `self.assertNotIn("CancelledError", result)`

### test_topic_summarizer_sorts_mixed_timestamp_types
Summary: Tests topic summarizer sorts mixed timestamp types
Asserts:
  - `self.assertEqual([item["content"] for item in segments[0].messages], ["bad", "first", "second"])`

### test_relationship_process_event_normalizes_bad_intensity
Summary: Tests relationship process event normalizes bad intensity
Asserts:
  - `self.assertLess(negative_score, 0)`
  - `self.assertGreater(string_score, 0)`

### test_prompt_builder_defaults_missing_freshness_budget
Summary: Tests prompt builder defaults missing freshness budget
Asserts:
  - `self.assertEqual(envelope.freshness_state, FreshnessState.FRESH)`

### test_prepare_image_returns_none_for_bad_payload
Summary: Tests prepare image returns none for bad payload
Asserts:
  - `self.assertIsNone(ImagePipeline.prepare_image("not base64"))`
  - `self.assertIsNone(ImagePipeline.prepare_image(base64.b64encode(b"not an image").decode("ascii")))`

## tests/unit/state/test_user_profile_service_migrated.py (5 tests)

### test_record_profile_learning_touch_updates_count_and_know_times
Summary: Tests record profile learning touch updates count and know times
Asserts:
  - `self.assertEqual(profile.name, "Alice")`
  - `self.assertEqual(profile.message_count_for_profiling, 1)`
  - `self.assertEqual(profile.know_times, 1)`
  - `self.assertEqual(footprint["private_touch_count"], 1)`

### test_observe_activity_collects_recent_messages_without_incrementing_learning_counter
Summary: Tests observe activity collects recent messages without incrementing learning counter
Asserts:
  - `self.assertEqual(profile.message_count_for_profiling, 0)`
  - `self.assertEqual(profile.group_footprints["group-1"]["message_count"], 1)`
  - `self.assertEqual(profile.group_footprints["group-1"]["recent_messages"][-1]["text"], "今天去看电影了")`

### test_refresh_profile_from_generation_merges_points_and_respects_manual_locks
Summary: Tests refresh profile from generation merges points and respects manual locks
Asserts:
  - `self.assertEqual(refreshed.persona_analysis, "旧画像保持不动")`
  - `self.assertIn("夜猫子", refreshed.tags)`
  - `self.assertIn("老朋友", refreshed.tags)`
  - `self.assertIn("爱好:电影:0.80", refreshed.memory_points)`
  - `self.assertEqual(refreshed.identity_points, ["身份:手工设定"])`
  - `self.assertEqual(refreshed.profile_metadata["last_refresh_source"], "test")`

### test_apply_profile_name_rejects_placeholder_and_respects_manual_lock
Summary: Tests apply profile name rejects placeholder and respects manual lock
Asserts:
  - `self.assertFalse(locked_changed)`
  - `self.assertFalse(unlocked_placeholder)`
  - `self.assertEqual(profile.name, "Alice")`

### test_flush_message_counters_keeps_dirty_when_save_fails_then_clears_on_success
Summary: Tests flush message counters keeps dirty when save fails then clears on success
Asserts:
  - `self.assertTrue(dirty_after_failure)`
  - `self.assertFalse(profile.is_dirty)`
  - `self.assertIn("user-1", persistence.saved)`
  - `self.assertRaisesRegex(RuntimeError, "save failed")`

## tests/original_ported/test_reverse_session_marker_ported.py (3 tests)

### test_append_marker_is_idempotent
Summary: Tests append marker is idempotent
Asserts:
  - `self.assertEqual(marker_once, marker_twice)`
  - `self.assertEqual(parsed["session_id"], "lane-1")`
  - `self.assertEqual(parsed["parent_session_id"], "parent-1")`
  - `self.assertEqual(parsed["source"], "astrmai")`

### test_provider_detection_only_matches_explicit_reverse_markers
Summary: Tests provider detection only matches explicit reverse markers
Asserts:
  - `self.assertTrue(self.reverse_mod.provider_is_gemini_reverse(local_reverse))`
  - `self.assertFalse(self.reverse_mod.provider_is_gemini_reverse(local_unmarked))`
  - `self.assertFalse(self.reverse_mod.provider_is_gemini_reverse(builtin_gemini))`

### test_maybe_attach_marker_only_for_gemini_reverse_provider
Summary: Tests maybe attach marker only for gemini reverse provider
Asserts:
  - `self.assertIn("<astrbot_reverse_session>", reverse_prompt)`
  - `self.assertIn("session_id=default:GroupMessage:group-1@@astrmai:sys2:dialog:v1", reverse_prompt)`
  - `self.assertNotIn("<astrbot_reverse_session>", regular_prompt)`

## tests/test_pfc_tools_chat_extensions_refactor.py (7 tests)

### test_message_emoji_like_uses_builtin_pool_and_current_message
Summary: Tests message emoji like uses builtin pool and current message
Asserts:
  - `self.assertIn("QQ", result)`
  - `self.assertEqual(             api.calls,             [("set_msg_emoji_like", {"message_id": "msg-1", "emoji_id": self.mod.QQ_MESSAGE_EMOJI_OPTIONS["approve"][0]})],         )`

### test_group_sign_tool_blocks_private_chat
Summary: Tests group sign tool blocks private chat
Asserts:
  - `self.assertIn("当前不是群聊", result)`
  - `self.assertEqual(api.calls, [])`

### test_construct_at_event_ignores_dirty_none_target_when_deduping
Summary: Tests construct at event ignores dirty none target when deduping
Asserts:
  - `self.assertEqual(len(actions), 2)`
  - `self.assertEqual(actions[-1]["target_id"], "user-2")`

### test_group_sign_tool_calls_current_group_only
Summary: Tests group sign tool calls current group only
Asserts:
  - `self.assertIn("群签到", result)`
  - `self.assertEqual(api.calls, [("set_group_sign", {"group_id": "67890"})])`

### test_custom_face_catalog_query_returns_preview
Summary: Tests custom face catalog query returns preview
Asserts:
  - `self.assertIn("cat_smile", result)`
  - `self.assertIn("dog_wave", result)`
  - `self.assertEqual(api.calls, [("fetch_custom_face", {"count": 2})])`

### test_proactive_poke_rejects_cross_group_target
Summary: Tests proactive poke rejects cross group target
Asserts:
  - `self.assertIn("不在当前群聊", result)`
  - `self.assertEqual(api.calls, [])`

### test_proactive_poke_rejects_private_arbitrary_numeric_target
Summary: Tests proactive poke rejects private arbitrary numeric target
Asserts:
  - `self.assertIn("不在当前私聊", result)`
  - `self.assertEqual(api.calls, [])`

## tests/original_ported/test_dialog_focus_continuity_regression_ported.py (1 tests)

### test_window_with_old_messages_centers_focus_thread
Summary: Tests window with old messages centers focus thread
Asserts:
  - `self.assertEqual(focus_event.get_extra("astrmai_raw_user_text"), "Carol: why not?")`
  - `self.assertEqual(envelope.focus_message_text, "Carol: why not?")`
  - `self.assertIn("Alice: do not do that", envelope.direct_context_text)`
  - `self.assertIn("Bob: I am talking about something else", envelope.ambient_background_text)`
  - `self.assertEqual(context_engine.calls[0]["event_messages"][-1], focus_event)`

## tests/original_ported/test_database_adapters_ported.py (4 tests)

### test_jargon_adapter_methods
Summary: Tests jargon adapter methods
Asserts:
  - `self.assertEqual(self.db.get_jargon("group-1", "spark"), "first meaning")`
  - `self.assertEqual(len(search_results), 1)`
  - `self.assertEqual(search_results[0].content, "spark")`
  - `self.assertEqual(jargon_list, [{"text": "spark", "meaning": "first meaning", "situation": ""}])`

### test_legacy_save_jargon_redirects_to_canonical_memory
Summary: Tests legacy save jargon redirects to canonical memory
Asserts:
  - `self.assertIsNotNone(canonical)`
  - `self.assertEqual(canonical[0], "jargon")`
  - `self.assertEqual(canonical[3], "reborn strategy")`
  - `self.assertEqual(canonical[4], "active")`
  - `self.assertEqual(canonical[5], "auto_and_tool")`
  - `self.assertEqual(legacy_count, 0)`
  - `self.assertEqual(saved.content, "phoenix")`
  - `self.assertEqual(saved.meaning, "reborn strategy")`

### test_profile_lookup_by_name_and_nickname
Summary: Tests profile lookup by name and nickname
Asserts:
  - `self.assertIsNotNone(by_name)`
  - `self.assertEqual(by_name.user_id, "user-1")`
  - `self.assertIsNotNone(by_nickname)`
  - `self.assertEqual(by_nickname.name, "Alice")`

### test_pattern_adapter_methods
Summary: Tests pattern adapter methods
Asserts:
  - `self.assertAlmostEqual(pattern.weight, 0.6)`
  - `self.assertAlmostEqual(pattern.weight, 2.0)`
  - `self.assertEqual(remaining, [])`

## tests/original_ported/test_gateway_lane_request_kwargs_ported.py (3 tests)

### test_claude_lane_request_adds_cache_control
Summary: Tests claude lane request adds cache control
Asserts:
  - `self.assertEqual(fake_context.calls[0]["cache_control"], {"type": "ephemeral"})`

### test_runner_lane_request_adds_remote_session_id
Summary: Tests runner lane request adds remote session id
Asserts:
  - `self.assertIn("session_id", fake_context.calls[0])`
  - `self.assertIn("@@astrmai:bg:memory:", session_id)`
  - `self.assertTrue(session_id.endswith("memory_global_summary:v1:text"))`

### test_fallback_model_recomputes_request_kwargs_from_actual_provider
Summary: Tests fallback model recomputes request kwargs from actual provider
Asserts:
  - `self.assertEqual(fake_context.calls[0]["chat_provider_id"], "claude-3-5-sonnet")`
  - `self.assertEqual(fake_context.calls[0]["cache_control"], {"type": "ephemeral"})`
  - `self.assertEqual(fake_context.calls[1]["chat_provider_id"], "dify-agent")`
  - `self.assertIn("session_id", fake_context.calls[1])`
  - `self.assertNotIn("cache_control", fake_context.calls[1])`

## tests/test_learning_event_collaboration_refactor.py (2 tests)

### test_learning_events_publish_to_state_and_memory_topics
Summary: Tests learning events publish to state and memory topics
Asserts:
  - `self.assertEqual(state_engine.received[0]["sender_id"], "user-1")`
  - `self.assertEqual(memory_engine.received[0][0], "bot")`
  - `self.assertEqual(memory_engine.received[1][0], "mining")`
  - `self.assertEqual(memory_engine.writes[0].kind, "jargon")`
  - `self.assertEqual(memory_engine.writes[0].status, "review_pending")`

### test_trigger_knowledge_update_publishes_topic_without_clearing_signal
Summary: Tests trigger knowledge update publishes topic without clearing signal
Asserts:
  - `self.assertTrue(event_bus.knowledge_updated.is_set())`
  - `self.assertEqual(received, [{}])`

## tests/unit/memory/test_memory_contracts_migrated.py (3 tests)

### test_extract_and_summarize_history_passes_structured_messages
Summary: Tests extract and summarize history passes structured messages
Asserts:
  - `self.assertEqual(captured["session_id"], "chat-1")`
  - `self.assertEqual(len(captured["messages"]), 2)`
  - `self.assertEqual(captured["messages"][0]["sender"], "Alice")`
  - `self.assertEqual(captured["messages"][1]["content"], "world")`
  - `self.assertFalse(asyncio.iscoroutine(captured["messages"]))`

### test_summarize_session_uses_structured_messages_contract
Summary: Tests summarize session uses structured messages contract
Asserts:
  - `self.assertEqual(recorded["session_id"], "chat-1")`
  - `self.assertEqual(recorded["messages"], messages)`

### test_compat_summarizer_module_still_reexports_chat_history_summarizer
Summary: Tests compat summarizer module still reexports chat history summarizer
Asserts:
  - `self.assertIsNotNone(instance)`

## tests/regression/conversation/test_dialog_focus_thread_continuity_regression_migrated.py (1 tests)

### test_reply_to_bot_thread_beats_later_plain_message
Summary: Tests reply to bot thread beats later plain message
Asserts:
  - `self.assertEqual(envelope.focus_message_text, "Bob: 为什么不可以")`
  - `self.assertIn("Alice: 不可以和妹妹结婚呀？", envelope.direct_context_text)`
  - `self.assertIn("Carol: 我去吃饭", envelope.ambient_background_text)`

## tests/original_ported/test_lane_manager_conversation_binding_ported.py (2 tests)

### test_same_lane_reuses_same_conversation
Summary: Tests same lane reuses same conversation
Asserts:
  - `self.assertEqual(lane_umo1, lane_umo2)`
  - `self.assertEqual(cid1, cid2)`
  - `self.assertEqual(history1, [])`
  - `self.assertEqual(len(history2), 2)`
  - `self.assertEqual(history2[0]["role"], "user")`
  - `self.assertEqual(history2[1]["content"], "world")`

### test_same_lane_allows_conversation_io_outside_lane_lock
Summary: Tests same lane allows conversation io outside lane lock
Asserts:
  - `self.assertTrue(overlap_seen)`

## tests/test_main_reverse_session_hook_refactor.py (1 tests)

### test_hook_injects_reverse_session_block_for_gemini_reverse_provider
Summary: Tests hook injects reverse session block for gemini reverse provider
Asserts:
  - `self.assertIn("base prompt", request.system_prompt)`
  - `self.assertIn("astrbot_reverse_session", request.system_prompt)`
  - `self.assertIn("session_id=session-1", request.system_prompt)`
  - `self.assertIn("session_scope=platform:FriendMessage:user-1", request.system_prompt)`
  - `self.assertEqual(trace["request_session_id"], "session-1")`
  - `self.assertEqual(trace["post_hook_system_hash"], event.get_extra("astrmai_post_hook_system_hash"))`
  - `self.assertEqual(trace["provider_visible_system_hash"], event.get_extra("astrmai_post_hook_system_hash"))`

## tests/unit/state/test_state_gap_coverage.py (6 tests)

### test_relationship_process_event_negative_event_resets_positive_streak
Summary: Tests relationship process event negative event resets positive streak
Asserts:
  - `self.assertLess(after_score, before_score)`
  - `self.assertEqual(vec.positive_streak, 0)`
  - `self.assertEqual(vec.negative_streak, 1)`
  - `self.assertEqual(vec.total_interactions, 1)`
  - `self.assertLess(vec.trust, 60.0)`
  - `self.assertLess(vec.emotion_bond, 40.0)`

### test_affection_router_hostile_trigger_can_override_context_window
Summary: Tests affection router hostile trigger can override context window
Asserts:
  - `self.assertEqual(winner, "user-b")`

### test_affection_router_hostile_trigger_does_not_override_stronger_context
Summary: Tests affection router hostile trigger does not override stronger context
Asserts:
  - `self.assertEqual(winner, "user-a")`

### test_affection_router_non_hostile_trigger_keeps_normal_scoring
Summary: Tests affection router non hostile trigger keeps normal scoring
Asserts:
  - `self.assertEqual(winner, "user-a")`

### test_mood_manager_lane_result_normalizes_unknown_tag_and_clamps_value
Summary: Tests mood manager lane result normalizes unknown tag and clamps value
Asserts:
  - `self.assertEqual(tag, "neutral")`
  - `self.assertAlmostEqual(mood_value, 1.0)`
  - `self.assertEqual(observed["base_origin"], "chat-1")`
  - `self.assertTrue(observed["is_json"])`
  - `self.assertFalse(observed["use_fallback"])`

### test_apply_natural_decay_epoch0_does_not_catastrophically_decay_mood
Summary: Tests apply natural decay epoch0 does not catastrophically decay mood
Asserts:
  - `self.assertAlmostEqual(state.mood, 0.7)`
  - `self.assertAlmostEqual(state.last_passive_decay_time, 1_000_000.0)`
  - `self.assertFalse(state.is_dirty)`

## tests/test_group_signin_service_refactor.py (5 tests)

### test_run_once_signs_active_group_and_dispatches_followup
Summary: Tests run once signs active group and dispatches followup
Asserts:
  - `self.assertEqual(api.calls, [("set_group_sign", {"group_id": "12345"})])`
  - `self.assertEqual(len(dispatcher.intents), 1)`
  - `self.assertEqual(dispatcher.intents[0].source, "group_signin")`
  - `self.assertEqual(dispatcher.intents[0].chat_id, "default:GroupMessage:12345")`
  - `self.assertEqual(state.group_config["group_signin"]["last_date"], "2026-01-18")`
  - `self.assertTrue(state.is_dirty)`
  - `self.assertEqual(len(persistence.saved), 1)`

### test_run_once_skips_duplicate_same_day
Summary: Tests run once skips duplicate same day
Asserts:
  - `self.assertEqual(api.calls, [])`
  - `self.assertEqual(dispatcher.intents, [])`

### test_run_once_sign_failure_does_not_dispatch
Summary: Tests run once sign failure does not dispatch
Asserts:
  - `self.assertEqual(len(api.calls), 1)`
  - `self.assertEqual(dispatcher.intents, [])`
  - `self.assertEqual(persistence.saved, [])`

### test_run_once_before_window_skips_all_groups
Summary: Tests run once before window skips all groups
Asserts:
  - `self.assertEqual(api.calls, [])`
  - `self.assertEqual(dispatcher.intents, [])`

### test_run_once_after_sign_hour_does_not_late_patch_sign
Summary: Tests run once after sign hour does not late patch sign
Asserts:
  - `self.assertEqual(api.calls, [])`
  - `self.assertEqual(dispatcher.intents, [])`

## tests/unit/webui/test_chat_runtime_service_gap_coverage.py (3 tests)

### test_status_endpoints_degrade_without_bound_runtime
Summary: Tests status endpoints degrade without bound runtime
Asserts:
  - `self.assertEqual(asyncio.run(service.proactive_status())["data"], {"running": False})`
  - `self.assertFalse(asyncio.run(service.dream_status())["runtime_bound"])`
  - `self.assertFalse(asyncio.run(service.diary_status())["data"]["available"])`
  - `self.assertEqual(asyncio.run(service.run_dream_once())["status"], "error")`
  - `self.assertEqual(asyncio.run(service.run_diary_once())["status"], "error")`

### test_chat_runtime_clear_removes_coordinator_and_heartflow_state
Summary: Tests chat runtime clear removes coordinator and heartflow state
Asserts:
  - `self.assertEqual(runtime["data"]["wait_target_name"], "Alice")`
  - `self.assertTrue(cleared["changed"])`
  - `self.assertEqual(coordinator.cleared, "chat-1")`
  - `self.assertEqual(heartflow._pulses_by_chat, {})`
  - `self.assertEqual(heartflow.state.cooldown_tags, [])`

### test_memory_feedback_lists_filters_sources_and_disables
Summary: Tests memory feedback lists filters sources and disables
Asserts:
  - `self.assertEqual(filtered["total"], 1)`
  - `self.assertEqual({item["source"] for item in all_sources["items"]}, {"planner", "judge"})`
  - `self.assertTrue(disabled["changed"])`
  - `self.assertIs(engine.disabled, signal_a)`

## tests/test_cron_guard_refactor.py (2 tests)

### test_guard_revives_missing_jobs_from_snapshots
Summary: Tests guard revives missing jobs from snapshots
Asserts:
  - `self.assertEqual(result, 1)`
  - `self.assertIn(("add", "n"), revived)`

### test_guard_replaces_snapshot_when_framework_returns_new_job_id
Summary: Tests guard replaces snapshot when framework returns new job id
Asserts:
  - `self.assertTrue(result)`
  - `self.assertEqual(cron_mgr.added["payload"]["session"], "umo")`
  - `self.assertEqual(cron_mgr.added["run_at"].isoformat(), "2026-07-03T08:30:00+08:00")`
  - `self.assertEqual(deactivated, ["old-job"])`
  - `self.assertEqual(saved[0].job_id, "new-job")`
  - `self.assertEqual(json.loads(saved[0].payload)["session"], "umo")`

## tests/original_ported/test_gateway_failure_normalization_ported.py (2 tests)

### test_provider_failure_payload_raises_cascade_failure
Summary: Tests provider failure payload raises cascade failure
Asserts:
  - `self.assertEqual(exc.__class__.__name__, "LLMCascadeFailureException")`
  - `self.assertEqual(exc.last_failure_kind, "provider_failure_text")`
  - `self.assertEqual(exc.attempted_models, ["model-a"])`
  - `self.assertIn("request_id", exc.raw_completion.lower().replace(" ", "_"))`

### test_lane_history_persists_sanitized_assistant_text
Summary: Tests lane history persists sanitized assistant text
Asserts:
  - `self.assertEqual(reply_text, "呜……\n不要难过，亚托莉抱抱你！")`
  - `self.assertEqual(conversation.history[-1]["content"], "呜……\n不要难过，亚托莉抱抱你！")`

## tests/test_host_mood_chain_audit_refactor.py (2 tests)

### test_host_message_entry_matches_direct_mood_result
Summary: Tests host message entry matches direct mood result
Asserts:
  - `self.assertEqual(payload["status"], "passed")`
  - `self.assertTrue(payload["all_matched"])`
  - `self.assertEqual(case["host_source"], "attention_ingress")`
  - `self.assertTrue(case["matched"])`
  - `self.assertEqual(case["host_results"], [{"type": "plain", "text": "(ghost)"}])`

### test_host_reply_post_send_matches_expected_mood_and_social_score
Summary: Tests host reply post send matches expected mood and social score
Asserts:
  - `self.assertEqual(payload["status"], "passed")`
  - `self.assertTrue(payload["all_matched"])`
  - `self.assertTrue(payload["publish_change_semantics_aligned"])`
  - `self.assertTrue(mixed_case["mood_tag_remap_suppressed"])`
  - `self.assertEqual(mixed_case["effective_event_type"], "normal_chat")`
  - `self.assertEqual(mixed_case["published_mood_tag"], "")`
  - `self.assertEqual(mixed_case["published_event_type"], "normal_chat")`
  - `self.assertLess(mixed_case["actual_social_score"], 1.0)`
  - `self.assertEqual(comfort_case["effective_event_type"], "normal_chat")`
  - `self.assertLessEqual(comfort_case["actual_social_score"], 0.4)`
  - `self.assertEqual(ambiguous_case["effective_event_type"], "greeting")`
  - `self.assertLessEqual(ambiguous_case["actual_social_score"], 0.24)`
  - `self.assertEqual(cold_case["effective_event_type"], "ignore")`
  - `self.assertLess(cold_case["actual_social_score"], -0.2)`
  - `self.assertGreaterEqual(cold_case["actual_social_score"], -0.30)`
  - `self.assertEqual(perfunctory_case["effective_event_type"], "ignore")`
  - `self.assertLessEqual(perfunctory_case["actual_social_score"], -0.30)`
  - `self.assertEqual(irritation_case["effective_event_type"], "ignore")`
  - `self.assertLessEqual(irritation_case["actual_social_score"], -0.40)`
  - `self.assertGreater(tool_case["actual_social_score"], mixed_case["actual_social_score"])`
  - `self.assertGreater(mixed_case["actual_social_score"], ambiguous_case["actual_social_score"])`
  - `self.assertGreater(cold_case["actual_social_score"], perfunctory_case["actual_social_score"])`
  - `self.assertGreater(perfunctory_case["actual_social_score"], irritation_case["actual_social_score"])`
  - `self.assertTrue(case["mood_matched"])`
  - `self.assertTrue(case["social_score_matched"])`
  - `self.assertEqual(case["social_score_amplitude_issue"], "")`
  - `self.assertEqual(case["host_results"], [{"type": "plain", "text": "(ghost)"}])`

## tests/manual/risk_audit/test_risk_legacy_compat_disconnect.py (4 tests)

### test_all_legacy_attrs_have_corresponding_property
Summary: Every name in LEGACY_RUNTIME_ATTRS must have a matching @property.
Asserts:
  - `self.assertEqual(missing, [],                          f"These LEGACY_RUNTIME_ATTRS have no @property on PluginRuntimeContext: {missing}. "                          f"They will always be None and never exported.")`

### test_none_service_not_exported
Summary: Services that are None are omitted from export_legacy_attrs.
Asserts:
  - `self.assertNotIn(name, attrs,                              f"None service '{name}' should NOT be in exported attrs")`

### test_stale_attribute_not_cleaned_on_none
Summary: When a previously-set service becomes None, host plugin keeps old value.
Asserts:
  - `self.assertTrue(hasattr(host, "gateway"), "host.gateway should be set")`
  - `self.assertIs(host.gateway, old_gw)`
  - `self.assertIs(host.gateway, old_gw,                       "CRITICAL: host.gateway still points to old gateway after degradation! "                       "Code using host.gateway will call a stale/dead service.")`

### test_export_legacy_attrs_coverage
Summary: Check how many of the 32 LEGACY_RUNTIME_ATTRS would be exported.
Asserts:
  - `self.assertLessEqual(len(exported_legacy), 32,                              f"Fresh runtime exports {len(exported_legacy)}/32 attrs. "                              f"Missing: {len(missing_legacy)}")`
  - `self.assertIn(key, attrs, f"'{key}' should always be in exported attrs")`

## tests/unit/learning/test_mining_helpers_migrated.py (5 tests)

### test_expression_candidate_extractor_uses_deterministic_frequency_and_dedup
Summary: Tests expression candidate extractor uses deterministic frequency and dedup
Asserts:
  - `self.assertEqual(len(result), 1)`
  - `self.assertEqual(result[0]["expression"], "ship it softly")`
  - `self.assertEqual(result[0]["count"], 2)`

### test_expression_pattern_enricher_degrades_to_candidate_payload
Summary: Tests expression pattern enricher degrades to candidate payload
Asserts:
  - `self.assertEqual(result[0]["summary"], "ship it softly")`
  - `self.assertEqual(result[0]["review_status"], "pending")`
  - `self.assertAlmostEqual(result[0]["confidence"], 0.72)`

### test_jargon_miner_filters_blank_messages_before_delegating
Summary: Tests jargon miner filters blank messages before delegating
Asserts:
  - `self.assertEqual(result, ["ok"])`
  - `self.assertEqual([msg.content for msg in extractor.calls[0][1]], ["hello", "world"])`

### test_jargon_miner_reads_jargon_min_count_from_real_config
Summary: Tests jargon miner reads jargon min count from real config
Asserts:
  - `self.assertEqual(jargon_miner.candidate_extractor.min_count, 1)`

### test_social_relation_miner_normalizes_score_and_ignores_empty_input
Summary: Tests social relation miner normalizes score and ignores empty input
Asserts:
  - `self.assertEqual(state_engine.calls, [("user-1", 1.5)])`

## tests/regression/memory/test_memory_v2_tool_injection.py (3 tests)

### test_omni_tool_uses_memory_tool_service_before_legacy_recall
Summary: Tests omni tool uses memory tool service before legacy recall
Asserts:
  - `self.assertEqual(result, "v2 result")`
  - `self.assertEqual(engine.recall_calls, [])`
  - `self.assertEqual(service.calls[0]["query"], "blue notebook")`
  - `self.assertEqual(service.calls[0]["chat_id"], "chat-1")`
  - `self.assertIs(service.calls[0]["event"], event)`

### test_omni_tool_without_v2_service_returns_offline_note
Summary: Tests omni tool without v2 service returns offline note
Asserts:
  - `self.assertIn("系统提示", result)`
  - `self.assertEqual(engine.calls, [])`

### test_self_lore_tool_without_v2_service_returns_offline_note
Summary: Tests self lore tool without v2 service returns offline note
Asserts:
  - `self.assertIn("系统提示", result)`
  - `self.assertEqual(engine.calls, [])`

## tests/manual/risk_audit/test_risk_config_hot_apply.py (6 tests)

### test_infrastructure_settings_is_frozen_dataclass
Summary: InfrastructureSettings is @dataclass(frozen=True).
Asserts:
  - `self.assertIn("frozen=True", class_source,                       "InfrastructureSettings is frozen — any rebuild creates a NEW instance. "                       "Old references remain pointing to the original.")`

### test_rebuild_creates_new_instance
Summary: rebuild_infrastructure_settings() replaces the attribute completely.
Asserts:
  - `self.assertIsNot(old_settings, new_settings,                          "Each build_infrastructure_settings() call creates a NEW instance. "                          "Any code holding a reference to the old instance sees stale data.")`

### test_gateway_holds_its_own_settings_copy
Summary: GlobalModelGateway receives a settings snapshot at construction.
Asserts:
  - `self.assertIn("self.settings", source,                       "Gateway stores its own settings reference at construction. "                       "When apply_hot_config() rebuilds infrastructure_settings, "                       "gateway.settings s...`

### test_lane_manager_holds_its_own_settings_copy
Summary: LaneManager stores a settings snapshot at construction.
Asserts:
  - `self.assertIn("self.settings", source or "settings",                           "LaneManager stores its own settings snapshot. "                           "Stale after hot-apply.")`
  - `self.assertTrue(True, "LaneManager may store settings — verify manually")`

### test_semaphore_not_recreated_on_hot_apply
Summary: _global_semaphore uses the initial max_concurrent_llm_calls forever.
Asserts:
  - `self.assertIn("max_concurrent_llm_calls", source,                       "Semaphore is created once at construction with initial limit. "                       "Hot-apply changes to max_concurrent_llm_calls do NOT update "                       "th...`

### test_feature_flags_property_always_fresh
Summary: feature_flags property reads live infrastructure_settings.
Asserts:
  - `self.assertIn("infrastructure_settings", source,                       "feature_flags ALWAYS reads from the live infrastructure_settings. "                       "This is the correct pattern — other consumers should follow it.")`

## tests/original_ported/test_lane_history_sanitization_ported.py (2 tests)

### test_recent_transcript_filters_dirty_turns
Summary: Tests recent transcript filters dirty turns
Asserts:
  - `self.assertNotIn("[RollingSummary]", transcript)`
  - `self.assertNotIn("JSON 响应", transcript)`
  - `self.assertNotIn("wait_and_listen", transcript)`
  - `self.assertNotIn("assistant:", transcript)`
  - `self.assertIn("呜……", transcript)`
  - `self.assertIn("[萤] 说: 呜呜呜", transcript)`

### test_recent_transcript_honors_age_window_and_ignores_untimed_history
Summary: Tests recent transcript honors age window and ignores untimed history
Asserts:
  - `self.assertIn("fresh reply", transcript)`
  - `self.assertIn("fresh message", transcript)`
  - `self.assertNotIn("old reply", transcript)`
  - `self.assertNotIn("old poke", transcript)`

## tests/manual/risk_audit/test_risk_judge_prompt_injection.py (7 tests)

### test_message_field_no_length_limit
Summary: The user message is injected verbatim into the LLM prompt.
Asserts:
  - `self.assertIn("{message}", source,                       "User message is injected verbatim via f-string. "                       "No length limit or truncation. A 10K+ char message "                       "is injected directly into the LLM contex...`

### test_persona_summary_no_length_limit
Summary: The persona_summary is also injected verbatim.
Asserts:
  - `self.assertIn("persona_summary", source,                       "persona_summary is injected without length limit. "                       "Combined with unbounded message, the prompt can grow "                       "arbitrarily large.")`

### test_history_truncation_exists
Summary: History records are truncated to 60 chars — but message is not.
Asserts:
  - `self.assertIn("[:60]", source,                       "History records ARE truncated to 60 chars. "                       "But the LIVE message field (not from history) has no such guard.")`

### test_build_dynamic_actions_is_bounded
Summary: Dynamic actions output is bounded (~400 chars max).
Asserts:
  - `self.assertLessEqual(action_count, 5,                              f"Max {action_count} dynamic actions — bounded output")`

### test_calculate_max_prompt_size
Summary: Estimate maximum LLM prompt size for Judge.evaluate().
Asserts:
  - `self.assertIn("message", source,                       "The {message} field in the prompt IS the live user input. "                       "A malicious 50K-character message results in a 50K+ prompt. "                       "This is a prompt inject...`

### test_flatten_history_content_has_truncation
Summary: _flatten_history_content exists and should handle large inputs.
Asserts:
  - `self.assertTrue(hasattr(Judge, "_flatten_history_content"),                         "_flatten_history_content exists for parsing message components")`

### test_evaluate_has_no_input_validation
Summary: evaluate() has no input size validation before prompt construction.
Asserts:
  - `self.assertNotIn("len(message)", source,                          "evaluate() does NOT check message length. "                          "No input validation before prompt injection. "                          f"Found validation keywords (not appli...`

## tests/original_ported/test_planner_includes_last_assistant_turn_ported.py (1 tests)

### test_planner_carries_last_assistant_turn_into_prompt_envelope
Summary: Tests planner carries last assistant turn into prompt envelope
Asserts:
  - `self.assertIsNotNone(envelope)`
  - `self.assertEqual(envelope.last_assistant_reply, "no, that is not allowed")`
  - `self.assertEqual(envelope.focus_message_text, "Alice: why not?")`
  - `self.assertEqual(event.get_extra("astrmai_raw_user_text"), "Alice: why not?")`
  - `self.assertEqual(             event.get_extra("astrmai_recent_transcript"),             "AstrMai: no, that is not allowed\nUser: why not?",         )`

## tests/test_visual_cortex_refactor.py (3 tests)

### test_visual_cortex_processes_and_persists_result
Summary: Tests visual cortex processes and persists result
Asserts:
  - `self.assertIn("chat-1:pic-1", stored)`
  - `self.assertEqual(stored["chat-1:pic-1"].description, "test")`

### test_worker_marks_queue_item_done_when_processing_raises
Summary: Tests worker marks queue item done when processing raises

### test_worker_marks_queue_item_done_when_cancelled_during_processing
Summary: Tests worker marks queue item done when cancelled during processing

## tests/manual/risk_audit/test_risk_memory_v2_migration.py (5 tests)

### test_initialize_runs_sequential_migration_steps
Summary: initialize() calls multiple migration steps in sequence.
Asserts:
  - `self.assertEqual(len(found), len(migrations),                          f"All {len(migrations)} migration steps should exist. "                          f"Found: {found}")`

### test_migration_steps_have_internal_try_except
Summary: Steps 10-14 each catch exceptions internally — failure doesn't block next.
Asserts:
  - `self.assertIn("except", source,                       "import_legacy_memory_events has internal try/except — "                       "failure is recorded but doesn't halt")`
  - `self.assertIn("except", source,                       "import_legacy_jargons has internal try/except")`
  - `self.assertIn("except", source,                       "import_legacy_expression_patterns has internal try/except")`

### test_v2_store_initialize_has_no_outer_try_except
Summary: v2_store.initialize() is NOT wrapped in try/except.
Asserts:
  - `self.assertFalse(in_try,                          "memory_engine.initialize() has NO outer try/except. "                          "If v2_store.initialize() fails, the entire chain halts "                          "and the error propagates to the c...`

### test_bm25_retriever_init_can_fail
Summary: BM25Retriever initialization has no try/except wrapper.
Asserts:
  - `self.assertIn("bm25_retriever", source,                       "BM25Retriever is initialized during migration chain")`
  - `self.assertTrue(True,                             "BM25Retriever init has NO try/except wrapper. "                             "If it fails, the memory engine is partially initialized.")`

### test_migration_failure_records_status
Summary: Failed migrations record 'failed' status in memory_v2_migrations table.
Asserts:
  - `self.assertIn("failed", source,                       "Failed migrations are recorded with status='failed'. "                       "WebUI should surface these to the admin.")`

## tests/manual/risk_audit/test_risk_chat_loop_lost_message.py (4 tests)

### test_save_before_dispatch_window_exists
Summary: Confirm tick() saves state BEFORE dispatching to the handler.
Asserts:
  - `self.assertGreater(save_pos, 0, "save() call must exist in tick()")`
  - `self.assertGreater(dispatch_pos, 0, "_dispatch() call must exist in tick()")`
  - `self.assertTrue(                 True,                 "CONFIRMED: save() occurs BEFORE _dispatch() in tick(). "                 "If process crashes between them, the state is persisted as "                 "'processed' but the message was never d...`

### test_crash_between_save_and_dispatch_loses_message
Summary: Simulate a crash between save and dispatch — message is lost.
Asserts:
  - `self.assertGreaterEqual(len(save_calls), 1,                                     "save() was called — state is persisted as 'processed'")`
  - `self.assertEqual(save_calls[0][0], "save",                              "State was saved but dispatch never completed. Message is LOST.")`

### test_max_loss_is_one_tick_per_chat
Summary: The loss window is bounded: at most 1 tick worth of messages per chat.
Asserts:
  - `self.assertTrue(             True,             "ARCHITECTURAL NOTE: The loss window is bounded to 1 tick per chat. "             "State persistence includes last_tick, so on restart, the chat "             "resumes from the persisted state. Only i...`

### test_no_redelivery_mechanism_exists
Summary: Confirm there is no dead-letter queue or re-delivery logic.
Asserts:
  - `self.assertNotIn(keyword, source.lower(),                              f"No '{keyword}' mechanism found — lost messages are truly lost.")`

## tests/original_ported/test_attention_focus_root_resolution_ported.py (2 tests)

### test_reply_to_bot_uses_replied_event_as_thread_root
Summary: Tests reply to bot uses replied event as thread root
Asserts:
  - `self.assertIsNotNone(root_candidate)`
  - `self.assertIs(root_candidate.event, bot_event)`
  - `self.assertEqual(reason, "explicit_reply_target")`

### test_same_sender_followup_uses_previous_message_as_root
Summary: Tests same sender followup uses previous message as root
Asserts:
  - `self.assertIsNotNone(root_candidate)`
  - `self.assertIs(root_candidate.event, first)`
  - `self.assertEqual(reason, "same_sender_chain")`

## tests/original_ported/test_reply_engine_output_guard_ported.py (2 tests)

### test_provider_failure_payload_is_replaced_with_fallback
Summary: Tests provider failure payload is replaced with fallback
Asserts:
  - `self.assertEqual(state_engine.gateway.context.sent[0][1], "fallback")`

### test_prompt_scaffold_lines_are_filtered_but_natural_reply_is_preserved
Summary: Tests prompt scaffold lines are filtered but natural reply is preserved
Asserts:
  - `self.assertNotIn("user:", sent_text)`
  - `self.assertNotIn("assistant:", sent_text)`
  - `self.assertNotIn("[RollingSummary]", sent_text)`
  - `self.assertIn("hmm", sent_text)`
  - `self.assertIn("Do not be sad", sent_text)`

## tests/unit/webui/test_user_ui_service_migrated.py (1 tests)

### test_update_user_and_slice_mutations_mark_manual_locks
Summary: Tests update user and slice mutations mark manual locks
Asserts:
  - `self.assertEqual(record["name"], "Alice Manual")`
  - `self.assertEqual(record["tags"], ["夜猫子", "熟人"])`
  - `self.assertTrue({"name", "tags", "persona_analysis", "identity_points"}.issubset(locks))`

## tests/original_ported/test_attention_image_gating_ported.py (2 tests)

### test_passive_group_image_share_is_ignored
Summary: Tests passive group image share is ignored
Asserts:
  - `self.assertEqual(result, "IGNORED_IMAGE")`

### test_direct_vision_request_is_not_treated_as_passive_share
Summary: Tests direct vision request is not treated as passive share
Asserts:
  - `self.assertEqual(result, "ENGAGED")`

## tests/test_config_standalone_refactor.py (7 tests)

### test_astrmai_config_instantiates_with_expected_defaults
Summary: Tests astrmai config instantiates with expected defaults
Asserts:
  - `self.assertEqual(config.agent.max_steps, 5)`
  - `self.assertTrue(hasattr(config.provider, "embedding_models"))`
  - `self.assertTrue(hasattr(config, "performance"))`
  - `self.assertEqual(config.performance.summary_threshold, 300)`
  - `self.assertTrue(hasattr(config.global_settings, "enable_private_chat"))`
  - `self.assertTrue(hasattr(config.sys3, "enable_work_mode"))`
  - `self.assertTrue(hasattr(config.vision, "use_native_main_reply_vision"))`
  - `self.assertTrue(hasattr(config.vision, "native_main_reply_failure_cooldown_sec"))`
  - `self.assertTrue(hasattr(config, "conversation"))`
  - `self.assertEqual(config.conversation.compaction_trigger_segments, 40)`
  - `self.assertEqual(config.conversation.compaction_keep_recent_segments, 16)`
  - `self.assertEqual(config.evolution.jargon_min_count, 2)`

### test_astrmai_config_accepts_conversation_and_memory_namespace_fields
Summary: Tests astrmai config accepts conversation and memory namespace fields
Asserts:
  - `self.assertEqual(config.conversation.compaction_trigger_segments, 64)`
  - `self.assertEqual(config.conversation.compaction_keep_recent_segments, 20)`
  - `self.assertEqual(config.memory.deep_temporal_alpha, 0.5)`
  - `self.assertEqual(config.memory.maintenance_hot_beta, 0.25)`

### test_astrmai_config_migrates_legacy_global_memory_fields
Summary: Tests astrmai config migrates legacy global memory fields
Asserts:
  - `self.assertTrue(config.global_settings.debug_mode)`
  - `self.assertEqual(config.memory.maintenance_hot_beta, 0.3)`
  - `self.assertEqual(config.memory.deep_temporal_alpha, 0.4)`

### test_memory_namespace_overrides_legacy_global_fields
Summary: Tests memory namespace overrides legacy global fields
Asserts:
  - `self.assertEqual(config.memory.maintenance_hot_beta, 0.6)`

### test_memory_config_instance_is_preserved
Summary: Tests memory config instance is preserved
Asserts:
  - `self.assertEqual(config.memory.deep_temporal_alpha, 0.9)`
  - `self.assertEqual(config.memory.maintenance_hot_beta, 0.1)`

### test_astrmai_config_accepts_performance_and_evolution_fields
Summary: Tests astrmai config accepts performance and evolution fields
Asserts:
  - `self.assertEqual(config.performance.summary_threshold, 512)`
  - `self.assertEqual(config.evolution.jargon_min_count, 5)`

### test_schema_json_is_parseable_and_contains_runtime_config_fields
Summary: Tests schema json is parseable and contains runtime config fields
Asserts:
  - `self.assertIn("summary_threshold", performance_items)`
  - `self.assertIn("use_native_main_reply_vision", vision_items)`
  - `self.assertIn("native_main_reply_failure_cooldown_sec", vision_items)`
  - `self.assertIn("jargon_min_count", evolution_items)`
  - `self.assertEqual(performance_items["summary_threshold"]["default"], 300)`
  - `self.assertEqual(evolution_items["jargon_min_count"]["default"], 2)`
  - `self.assertEqual(schema["conversation"]["items"]["compaction_trigger_segments"]["default"], 40)`
  - `self.assertEqual(schema["conversation"]["items"]["compaction_keep_recent_segments"]["default"], 16)`

## tests/original_ported/test_lane_stores_raw_dialogue_ported.py (1 tests)

### test_lane_history_uses_raw_user_text_instead_of_wrapped_prompt
Summary: Tests lane history uses raw user text instead of wrapped prompt
Asserts:
  - `self.assertEqual(len(history), 2)`
  - `self.assertEqual(history[0]["role"], "user")`
  - `self.assertEqual(history[0]["content"], "[Alice] 说: 为什么不可以")`
  - `self.assertNotIn("导演旁白", history[0]["content"])`

## tests/original_ported/test_reply_engine_focus_anchor_ported.py (1 tests)

### test_reply_prefers_thread_root_over_focus_and_legacy_anchor
Summary: Tests reply prefers thread root over focus and legacy anchor
Asserts:
  - `self.assertEqual(captured["anchor_text"], "thread root message")`
  - `self.assertIs(captured["anchor_event"], thread_root_event)`

## tests/integration/gateway/test_gateway_context_passthrough_migrated.py (1 tests)

### test_chat_in_lane_reuses_history_as_contexts
Summary: Tests chat in lane reuses history as contexts
Asserts:
  - `self.assertEqual(len(fake_context.calls), 2)`
  - `self.assertEqual(fake_context.calls[0]["contexts"], [])`
  - `self.assertEqual(len(fake_context.calls[1]["contexts"]), 2)`
  - `self.assertEqual(fake_context.calls[1]["system_prompt"], "stable prompt")`

## tests/test_learning_refactor.py (4 tests)

### test_process_bot_reply_skips_polluted_reply
Summary: Tests process bot reply skips polluted reply
Asserts:
  - `self.assertEqual(manager.db.logged, [])`

### test_get_active_patterns_canonical_async_works_inside_running_loop
Summary: Tests get active patterns canonical async works inside running loop
Asserts:
  - `self.assertEqual(asyncio.run(_run()), "active patterns from async")`
  - `self.assertEqual(service.calls, [("chat-async", 3)])`

### test_get_active_patterns_canonical_sync_rejects_running_loop
Summary: Tests get active patterns canonical sync rejects running loop
Asserts:
  - `self.assertEqual(service.calls, [])`
  - `self.assertRaisesRegex(RuntimeError, "sync-only")`

### test_get_active_patterns_canonical_sync_still_works_without_running_loop
Summary: Tests get active patterns canonical sync still works without running loop
Asserts:
  - `self.assertEqual(             manager.get_active_patterns_canonical("chat-sync", limit=4),             "active patterns from sync",         )`
  - `self.assertEqual(service.calls, [("chat-sync", 4)])`

## tests/manual/risk_audit/test_risk_service_bus_attribute_break.py (3 tests)

### test_gateway_replacement_not_reflected_in_host_plugin
Summary: When gateway is replaced, host_plugin.gateway still points to old.
Asserts:
  - `self.assertIs(host.gateway, old_gateway, "host.gateway should be gateway_v1 after sync")`
  - `self.assertIs(runtime.gateway, new_gateway, "runtime.gateway should be gateway_v2 after replacement")`
  - `self.assertIs(host.gateway, old_gateway,                       "BUG: host.gateway still points to gateway_v1 after replacement! "                       "Any code using host.gateway directly runs on stale service.")`
  - `self.assertIs(host.gateway, new_gateway,                       "After explicit sync_host_compat_attrs(), host.gateway is updated.")`

### test_export_legacy_attrs_skips_none_services
Summary: Services that are None (degraded/optional) are omitted from export.
Asserts:
  - `self.assertNotIn("sys3_router", attrs, "None services should be omitted from legacy attrs")`
  - `self.assertNotIn("cron_guard", attrs, "None services should be omitted from legacy attrs")`

### test_weakref_does_not_prevent_gc
Summary: host_plugin_ref uses weakref — host can be GC'd even if runtime alive.
Asserts:
  - `self.assertIsNone(host_ref(),                           "host_plugin_ref is a weakref — host can be GC'd. "                           "This is BY DESIGN but callers must handle host_plugin_ref() returning None.")`

## tests/original_ported/test_mojibake_output_guard_ported.py (4 tests)

### test_prompt_and_state_lines_are_stripped_from_mixed_reply
Summary: Tests prompt and state lines are stripped from mixed reply
Asserts:
  - `self.assertNotIn("user:", cleaned)`
  - `self.assertNotIn("鍥剧墖", cleaned)`
  - `self.assertNotIn("当前心情", cleaned)`
  - `self.assertNotIn("你的想法", cleaned)`
  - `self.assertIn("嘿嘿，让高性能的亚托莉看看！", cleaned)`
  - `self.assertIn("哇！这是什么呀？", cleaned)`

### test_bot_name_prefix_is_stripped_without_touching_inline_mentions
Summary: Tests bot name prefix is stripped without touching inline mentions
Asserts:
  - `self.assertEqual(cleaned, "快来让亚托莉也蹭欧气～")`
  - `self.assertEqual(inline, "我看到你写了 ATRI:xxx，先别急。")`

### test_chinese_bot_name_prefix_is_stripped_only_at_line_start
Summary: Tests chinese bot name prefix is stripped only at line start
Asserts:
  - `self.assertEqual(cleaned, "\u4f60\u597d\u5440")`
  - `self.assertEqual(             inline,             "\u6211\u770b\u5230\u4f60\u5199\u4e86\u4e9a\u6258\u8389\uff1a\u4f60\u597d\u5440\uff0c\u6240\u4ee5\u6211\u6765\u56de\u5e94\u3002",         )`

### test_memory_prompt_scaffold_is_stripped_without_blocking_natural_impression
Summary: Tests memory prompt scaffold is stripped without blocking natural impression
Asserts:
  - `self.assertNotIn("---记忆闪回---", cleaned)`
  - `self.assertNotIn("内心浮现的印象", cleaned)`
  - `self.assertNotIn("---任意分区标题---", cleaned)`
  - `self.assertEqual(cleaned, "我印象里你挺在意天气的，先看看今天会不会下雨。")`
  - `self.assertEqual(leaked, "")`

## tests/manual/risk_audit/test_risk_chat_loop_state_machine.py (5 tests)

### test_derive_phase_has_7_paths
Summary: _derive_phase must handle all possible action types.
Asserts:
  - `self.assertGreaterEqual(explicit_returns, 4,                                 f"_derive_phase has {explicit_returns} return paths")`
  - `self.assertGreaterEqual(action_map_entries, 6,                                 f"All 6 phases should be mappable: {action_map_entries}")`

### test_decide_has_ingress_message_branch
Summary: INGRESS_MESSAGE path must exist in _decide().
Asserts:
  - `self.assertIn("INGRESS_MESSAGE", source,                       "INGRESS_MESSAGE handler must exist in _decide()")`
  - `self.assertIn("RESUME_WAIT", source,                       "RESUME_WAIT handler must exist in _decide()")`
  - `self.assertIn("INTERRUPT_WAIT", source,                       "INTERRUPT_WAIT handler must exist in _decide()")`

### test_dream_maintenance_path_exists
Summary: DREAM_MAINTENANCE dispatch path must exist but may lack explicit test.
Asserts:
  - `self.assertIn("DREAM_MAINTENANCE", source,                       "DREAM_MAINTENANCE path exists in _decide(). "                       "Verify it has a dedicated test — currently may be untested.")`

### test_state_machine_lines_of_code
Summary: ChatLoopKernel is 2272 lines — verify expected complexity scope.
Asserts:
  - `self.assertGreaterEqual(line_count, 2000,                                 f"ChatLoopKernel is {line_count} lines — extreme complexity. "                                 f"Any state transition bug could silently misroute messages.")`

### test_scheduler_policy_profile_coverage
Summary: All scheduler profiles should be referenced and have required structure.
Asserts:
  - `self.assertGreaterEqual(len(profile_attrs), 1,                                 f"Should have at least 1 scheduler/profile attribute. Found: {profile_attrs}")`

## tests/original_ported/test_attention_focus_selection_ported.py (2 tests)

### test_reply_to_bot_has_highest_focus_priority
Summary: Tests reply to bot has highest focus priority
Asserts:
  - `self.assertIs(focus_event, reply_to_bot)`
  - `self.assertEqual(reason, "reply_to_bot")`
  - `self.assertEqual(background_events, [older_plain, later_plain])`

### test_direct_wakeup_beats_later_plain_message
Summary: Tests direct wakeup beats later plain message
Asserts:
  - `self.assertIs(focus_event, wakeup_event)`
  - `self.assertEqual(reason, "direct_wakeup")`
  - `self.assertEqual(background_events, [earlier_plain, later_plain])`

## tests/original_ported/test_expression_selector_reviewed_ported.py (3 tests)

### test_selector_passes_review_filters_and_scope
Summary: Tests selector passes review filters and scope
Asserts:
  - `self.assertIn("solid indeed", result)`
  - `self.assertTrue(selector.db.calls)`
  - `self.assertEqual(first_call["shared_scope"], "group-1")`
  - `self.assertEqual(first_call["review_status"], "approved")`
  - `self.assertTrue(first_call["only_checked"])`

### test_selector_cools_down_recent_patterns_and_filters_short_repeats
Summary: Tests selector cools down recent patterns and filters short repeats
Asserts:
  - `self.assertIn("phrase-0", first)`
  - `self.assertNotIn("phrase-0", second)`
  - `self.assertIn("phrase-5", second)`
  - `self.assertNotIn("咻——！", third)`

### test_fast_select_blocks_conflicting_contextual_situation
Summary: Tests fast select blocks conflicting contextual situation
Asserts:
  - `self.assertNotIn("thanks for saying that", result)`
  - `self.assertIn("generic fallback", result)`

## tests/unit/state/test_group_reply_wait_manager_concurrency_migrated.py (2 tests)

### test_concurrent_reregistration_keeps_latest_wait_state
Summary: Tests concurrent reregistration keeps lawait state
Asserts:
  - `self.assertTrue(entered.wait(timeout=2), "first registration did not block on old timeout cancellation")`
  - `self.assertFalse(second_thread.is_alive(), "second registration should finish while first is blocked")`
  - `self.assertFalse(first_thread.is_alive(), "first registration should finish after release")`
  - `self.assertEqual(results, {"first": True, "second": True})`
  - `self.assertIsNotNone(info)`
  - `self.assertEqual(info["target_user_id"], "user-b")`
  - `self.assertEqual(info["target_name"], "Second")`

### test_reregistered_wait_survives_old_timeout_task
Summary: Tests reregistered wait survives old timeout task
Asserts:
  - `self.assertTrue(manager.register_from_reply_event(first))`
  - `self.assertTrue(manager.register_from_reply_event(second))`
  - `self.assertIsNotNone(info)`
  - `self.assertEqual(info["target_user_id"], "user-b")`
  - `self.assertEqual(info["target_name"], "Second")`
  - `self.assertTrue(manager.cancel_wait("default:GroupMessage:group-1", reason="cleanup"))`
  - `self.assertIsNone(manager.get_wait_info("default:GroupMessage:group-1"))`

## tests/test_scheduler_benchmark_refactor.py (3 tests)

### test_profile_matrix_covers_all_profiles_and_scenarios
Summary: Tests profile matrix covers all profiles and scenarios
Asserts:
  - `self.assertEqual(             matrix["profiles"],             ["dialogue_first", "balanced", "maintenance_friendly"],         )`
  - `self.assertIn("hot_dialogue_pressure", matrix["scenarios"])`
  - `self.assertIn("maintenance_backlog", matrix["scenarios"])`
  - `self.assertIn("busy_executor_pressure", matrix["scenarios"])`
  - `self.assertIn("retry_pressure_mix", matrix["scenarios"])`
  - `self.assertIn("forced_promotion_pressure", matrix["scenarios"])`

### test_profiles_produce_distinct_selection_metrics
Summary: Tests profiles produce distinct selection metrics
Asserts:
  - `self.assertGreater(             maintenance_profiles["maintenance_friendly"]["scheduler_batch_plan"]["maintenance_slots"],             maintenance_profiles["balanced"]["scheduler_batch_plan"]["maintenance_slots"],         )`
  - `self.assertGreaterEqual(             maintenance_profiles["balanced"]["scheduler_batch_plan"]["maintenance_slots"],             maintenance_profiles["dialogue_first"]["scheduler_batch_plan"]["maintenance_slots"],         )`
  - `self.assertGreater(             busy_profiles["maintenance_friendly"]["maintenance_selected_count"],             busy_profiles["balanced"]["maintenance_selected_count"],         )`
  - `self.assertGreater(             busy_profiles["balanced"]["quota_skip_counts"]["skipped_by_maintenance_quota"],             busy_profiles["maintenance_friendly"]["quota_skip_counts"]["skipped_by_maintenance_quota"],         )`
  - `self.assertGreater(             forced_profiles["balanced"]["forced_promotion_count"],             forced_profiles["dialogue_first"]["forced_promotion_count"],         )`

### test_artifact_writer_emits_matrix_assets
Summary: Tests artifact writer emits matrix assets
Asserts:
  - `self.assertTrue((run_dir / "samples_meta.json").exists())`
  - `self.assertTrue((run_dir / "matrix_results.json").exists())`
  - `self.assertTrue((run_dir / "benchmark_summary.json").exists())`
  - `self.assertIn("Scheduler Profile Matrix Benchmark", markdown)`
  - `self.assertIn("hot_dialogue_pressure", markdown)`

## tests/unit/learning/test_mining_and_nickname_p2_gap_coverage.py (3 tests)

### test_expression_miner_normalizes_messages_before_extracting
Summary: Tests expression miner normalizes messages before extracting
Asserts:
  - `self.assertEqual(result, [{"expression": "ok", "style": "soft"}])`
  - `self.assertEqual([item.sender_name for item in captured["messages"]], ["Bob", "Cici"])`

### test_expression_miner_loads_existing_patterns_and_degrades_on_failure
Summary: Tests expression miner loads existing patterns and degrades on failure
Asserts:
  - `self.assertEqual(asyncio.run(miner._existing_patterns("group-1")), {"hello"})`
  - `self.assertEqual(asyncio.run(miner._existing_patterns("group-1")), set())`

### test_nickname_generator_payload_parse_and_fallback_choice
Summary: Tests nickname generator payload parse and fallback choice
Asserts:
  - `self.assertEqual(payload["persona_summary"], "calm persona")`
  - `self.assertEqual(payload["tags_text"], "careful, 7")`
  - `self.assertEqual(len(payload["analysis"]), 200)`
  - `self.assertEqual((nickname, reason), ("小A", "亲切"))`
  - `self.assertEqual((bad_nickname, bad_reason), ("", ""))`
  - `self.assertEqual(generator.choose("Display", ""), "Display")`
  - `self.assertEqual(generator.choose("", ""), "未知用户")`

## tests/regression/attention/test_attention_focus_thread_selection_migrated.py (1 tests)

### test_reply_to_bot_builds_focus_thread_and_keeps_unrelated_message_ambient
Summary: Tests reply to bot builds focus thread and keeps unrelated message ambient
Asserts:
  - `self.assertEqual(focus_thread["core_events"], [bot_event, focus_event])`
  - `self.assertEqual(focus_thread["ambient_events"], [unrelated])`

## tests/original_ported/test_prompt_prefix_stability_ported.py (1 tests)

### test_same_inputs_produce_same_prefix_hash
Summary: Tests same inputs produce same prefix hash
Asserts:
  - `self.assertEqual(hash1, hash2)`
  - `self.assertTrue(hash1)`
  - `self.assertEqual(system_prompt1, system_prompt2)`
  - `self.assertEqual(style_variant1, style_variant2)`
  - `self.assertEqual(proactive_recall1, proactive_recall2)`
  - `self.assertNotIn("<CHAT_HISTORY>", system_prompt1)`
  - `self.assertNotIn("[Tools]", system_prompt1)`
  - `self.assertNotIn("fixed slang", system_prompt1)`

## tests/unit/memory/test_memory_conflict_resolution.py (4 tests)

### test_explicit_correction_extracts_correction_claim
Summary: Tests explicit correction extracts correction claim
Asserts:
  - `self.assertTrue(claims)`
  - `self.assertTrue(claims[0].is_correction)`
  - `self.assertEqual(claims[0].attribute, "server_count")`
  - `self.assertEqual(decision.action, "authority_override")`

### test_short_term_state_does_not_override_authority
Summary: Tests short term state does not override authority
Asserts:
  - `self.assertTrue(claims)`
  - `self.assertEqual(claims[0].fact_scope, "short_term")`
  - `self.assertEqual(decision.action, "volatile_state_write")`
  - `self.assertTrue(decision.metadata["volatile_state"])`

### test_uncertain_correction_degrades_to_plain_memory
Summary: Tests uncertain correction degrades to plain memory
Asserts:
  - `self.assertNotEqual(decision.action, "authority_override")`
  - `self.assertEqual(decision.action, "plain_memory_write")`

### test_llm_claim_extraction_failure_returns_empty_claims
Summary: Tests llm claim extraction failure returns empty claims
Asserts:
  - `self.assertEqual(asyncio.run(run()), [])`

## tests/test_executor_lock_regression.py (3 tests)

### test_cancelled_error_decrements_executor_pending
Summary: After CancelledError during try_acquire_executor, executor_pending
Asserts:
  - `self.assertIsNotNone(lock1, "First acquire must succeed")`
  - `self.assertIsNotNone(                 lock2,                 "Third acquire must succeed after cancellation — executor_pending was not decremented",             )`
  - `self.assertIsNotNone(state)`
  - `self.assertEqual(                 state.executor_pending, 0,                 f"executor_pending should be 0 after releases, got {state.executor_pending}",             )`

### test_normal_acquire_release_does_not_leak
Summary: Normal acquire/release cycle keeps executor_pending at 0.
Asserts:
  - `self.assertIsNotNone(lock)`
  - `self.assertEqual(state.executor_pending, 0)`

### test_cancelled_error_has_try_except_in_source
Summary: Verify the source code of try_acquire_executor contains
Asserts:
  - `self.assertIn(             "CancelledError",             source,             "try_acquire_executor source must contain CancelledError handler",         )`

## tests/original_ported/test_attention_focus_thread_followups_ported.py (1 tests)

### test_same_sender_quick_followups_enter_same_thread
Summary: Tests same sender quick followups enter same thread
Asserts:
  - `self.assertEqual(reason, "same_sender_chain")`
  - `self.assertEqual(focus_thread["core_events"], [first, focus])`
  - `self.assertEqual(focus_thread["ambient_events"], [ambient])`

## tests/manual/risk_audit/test_risk_attention_gate_pool_lock.py (3 tests)

### test_pool_lock_is_single_global_lock
Summary: _pool_lock is ONE asyncio.Lock for all chat_ids.
Asserts:
  - `self.assertIn("_pool_lock", source,                       "Single global _pool_lock protects focus_pools for ALL chats. "                       "Under high concurrency (many chats), this is a contention point.")`
  - `self.assertGreaterEqual(per_chat_lock_count, 1,                                 f"_pool_lock exists ({per_chat_lock_count} Lock creations total)")`

### test_critical_section_is_constant_time
Summary: The critical section under _pool_lock must be O(1) — no I/O.
Asserts:
  - `self.assertNotIn("await", lines_inside_lock,                          "CRITICAL: No await inside _pool_lock critical section. "                          "This means the lock is held for microseconds only — NOT a real bottleneck.")`

### test_measure_lock_contention_with_concurrent_callers
Summary: Simulate 50 concurrent callers accessing _get_or_create_session.
Asserts:
  - `self.assertEqual(len(gate.focus_pools), 50,                             "All 50 sessions should be created")`
  - `self.assertLess(elapsed, 2.0,                             f"50 concurrent callers completed in {elapsed:.3f}s. "                             f"Lock contention adds measurable but small overhead.")`

## tests/test_high_bugfix_regression.py (6 tests)

### test_hooks_replace_pass_with_logger_debug
Summary: R14: main.py hooks use logger.debug, not bare pass.
Asserts:
  - `self.assertIn('logger.debug("[AstrMai] on_llm_response hook failed"', src)`
  - `self.assertIn('logger.debug("[AstrMai] on_agent_begin hook failed"', src)`
  - `self.assertIn('logger.debug("[AstrMai] on_agent_done hook failed"', src)`

### test_on_llm_request_conditionally_modifies_system_prompt
Summary: R6: system_prompt only modified conditionally.
Asserts:
  - `self.assertIn("needs_reverse_block", src)`
  - `self.assertIn("<astrbot_reverse_session>", src)`

### test_gate_sensors_use_logger_exception
Summary: R13: gate.py sensors use logger.exception.
Asserts:
  - `self.assertIn('logger.exception(f"[AttentionGate] sensor is_command check failed', src)`
  - `self.assertIn('logger.exception("[AttentionGate] sensor should_process_message check failed', src)`

### test_bootstrap_warns_proactive_failure
Summary: R12: bootstrap.py warns when ProactiveTask creation fails.
Asserts:
  - `self.assertIn("主动发言、梦境整理等功能将不可用", src)`

### test_resolve_chat_key_exists
Summary: R11: GroupDialogueStore has _resolve_chat_key method.
Asserts:
  - `self.assertIn("def _resolve_chat_key", src)`
  - `self.assertIn('raise ValueError("chat_id must be a non-empty string', src)`
  - `self.assertNotIn("str(chat_id or \"\")", src)`
  - `self.assertIn("self._resolve_chat_key(chat_id)", src)`

### test_event_bus_fixes
Summary: R7+R8+R9: EventBus has worker_tasks, dropped_count, improved logging.
Asserts:
  - `self.assertIn("self._worker_tasks", src)`
  - `self.assertIn("def get_dropped_count", src)`
  - `self.assertIn("qsize", src)`
  - `self.assertIn("self._background_tasks.add(t)", src)`
  - `self.assertIn("self._worker_tasks", health_src)`

## tests/original_ported/test_prompt_envelope_rendering_ported.py (1 tests)

### test_refiner_prefers_prompt_envelope_when_available
Summary: Tests refiner prefers prompt envelope when available
Asserts:
  - `self.assertEqual(final_system_prompt, "system prompt only")`
  - `self.assertIn("Alice: why not?", final_prompt)`
  - `self.assertIn("Current focus", final_prompt)`
  - `self.assertIn("AstrMai: no, that is not allowed", final_prompt)`
  - `self.assertIn("Bob: stay on topic", final_prompt)`
  - `self.assertGreaterEqual(final_prompt.count("---"), 4)`
  - `self.assertNotIn("old prompt", final_prompt)`

## tests/manual/risk_audit/test_risk_faiss_silent_degradation.py (4 tests)

### test_search_memories_returns_empty_on_faiss_unavailable
Summary: When _ensure_faiss_initialized returns False, search returns [].
Asserts:
  - `self.assertEqual(results, [],                              "When FAISS is unavailable, search_memories() returns [] — "                              "callers see 'no results' instead of 'search degraded'.")`
  - `self.assertEqual(len(results), 0,                              "Zero results is ambiguous: it could mean 'no matching memories' "                              "or 'FAISS is down'. Callers have no way to tell.")`

### test_callers_silently_receive_empty_results
Summary: All known callers return [] without checking FAISS status.
Asserts:
  - `self.assertEqual(results, [], "search_memories → [] — silent degradation")`

### test_faiss_retry_backoff_caps_at_3600s
Summary: FAISS retry uses exponential backoff capped at 3600s.
Asserts:
  - `self.assertFalse(result)`
  - `self.assertGreater(engine._next_retry_time, 0,                                "Backoff is set — FAISS won't retry immediately")`

### test_hybrid_retriever_has_dummy_fallback
Summary: HybridRetriever gracefully handles vector=None with a dummy.
Asserts:
  - `self.assertTrue("vector" in source.lower() or True,                       "HybridRetriever creates a dummy vector when vector=None. "                       "The fallback exists and is graceful, but callers are not warned.")`

## tests/test_database_adapters_refactor.py (2 tests)

### test_repositories_are_mounted_on_database_service
Summary: Tests repositories are mounted on database service
Asserts:
  - `self.assertEqual(sorted(repositories.keys()), ["chat", "memory", "profile", "review"])`
  - `self.assertIs(repositories["chat"], self.db.chat_repository)`
  - `self.assertIs(repositories["profile"], self.db.profile_repository)`

### test_memory_and_profile_repositories_delegate_to_legacy_behavior
Summary: Tests memory and profile repositories delegate to legacy behavior
Asserts:
  - `self.assertEqual(self.db.memory_repository.get_jargon("group-1", "梗"), "第一条含义")`
  - `self.assertIsNotNone(by_nickname)`
  - `self.assertEqual(by_nickname.name, "Alice")`

## tests/original_ported/test_near_context_priority_ported.py (2 tests)

### test_context_engine_drops_far_context_blocks_when_near_context_priority
Summary: Tests context engine drops far context blocks when near context priority
Asserts:
  - `self.assertNotIn("SLANG_BLOCK_SHOULD_DROP", system_prompt)`
  - `self.assertNotIn("EXPRESSION_BLOCK_SHOULD_DROP", system_prompt)`
  - `self.assertNotIn("JARGON_BLOCK_SHOULD_DROP", system_prompt)`
  - `self.assertIsInstance(style_variant, str)`
  - `self.assertNotIn("SLANG_BLOCK_SHOULD_DROP", style_variant)`
  - `self.assertEqual(proactive_recall, "")`

### test_refiner_memory_injection_is_disabled_when_near_context_priority
Summary: Tests refiner memory injection is disabled when near context priority
Asserts:
  - `self.assertEqual(result, "")`

## tests/manual/risk_audit/test_risk_gateway_cooldown_perf.py (4 tests)

### test_cleanup_scans_all_entries
Summary: _cleanup_model_cooldowns iterates the entire dict every call.
Asserts:
  - `self.assertIn("list(cooldowns.items())", source,                       "_cleanup_model_cooldowns iterates all entries — O(n) per call.")`

### test_cooldown_cleanup_called_on_every_llm_request
Summary: _cleanup_model_cooldowns is called from _filter_cooldown_attempt_queue.
Asserts:
  - `self.assertIn("_cleanup_model_cooldowns", source,                       "_cleanup_model_cooldowns() is called on every "                       "_filter_cooldown_attempt_queue() — which runs on every LLM call.")`

### test_measure_cooldown_cleanup_latency
Summary: Measure cleanup latency with 500 expired cooldown entries.
Asserts:
  - `self.assertLess(avg_us, 100_000,                         f"Cleanup of 500 expired entries averaged {avg_us:.1f} µs/call. "                         f"Within acceptable range (<100ms).")`

### test_semaphore_serializes_all_llm_calls
Summary: The global semaphore means cleanup latency blocks all callers.
Asserts:
  - `self.assertIn("_global_semaphore", src,             "GlobalModelGateway uses _global_semaphore to serialize LLM calls. "             "Any latency in cooldown cleanup blocks ALL concurrent callers."         )`

## tests/manual/risk_audit/test_risk_conf_schema_garbled.py (2 tests)

### test_memory_section_has_garbled_text_detected
Summary: deep_temporal_* fields have garbled Chinese descriptions — CONFIRMED BUG.
Asserts:
  - `self.assertGreater(len(found_garbled), 0,                            f"CONFIRMED: {len(found_garbled)} fields in memory section have garbled text. "                            f"Fields: {[f[0] for f in found_garbled]}. "                           ...`

### test_count_garbled_fields
Summary: Count how many fields across the entire schema have garbled text.
Asserts:
  - `self.assertLessEqual(garbled_count, 20,                              f"Found {garbled_count} garbled fields. "                              f"Should be 0 or very few. Check encoding pipeline.")`
  - `self.assertGreater(garbled_count, 0,                            "If 0 garbled fields, the bug may be fixed — update this test.")`

## tests/regression/proactive/test_dream_maintenance_migrated.py (3 tests)

### test_dream_agent_supports_read_only_jargon_tools
Summary: Tests dream agent supports read only jargon tools
Asserts:
  - `self.assertIn("团建黑话", jargon_text)`
  - `self.assertIn("建议复核黑话", suggestion)`
  - `self.assertEqual(calls[0].layers, ["jargon"])`
  - `self.assertEqual(calls[0].intent, "jargon")`
  - `self.assertTrue(calls[0].allow_stale)`

### test_build_maintenance_result_keeps_jargon_suggestions
Summary: Tests build maintenance result keeps jargon suggestions
Asserts:
  - `self.assertIn("jargon_review", result["tags"])`
  - `self.assertTrue(result["jargon_suggestions"])`

### test_select_session_bucket_prefers_previous_session_when_still_available
Summary: Tests select session bucket prefers previous session when still available
Asserts:
  - `self.assertEqual(selected, "group-2")`

## tests/original_ported/test_group_wait_thread_signature_ported.py (2 tests)

### test_target_message_without_thread_resume_signal_keeps_waiting
Summary: Tests target message without thread resume signal keeps waiting
Asserts:
  - `self.assertTrue(manager.register_from_reply_event(reply_event))`
  - `self.assertEqual(result, "OBSERVED")`
  - `self.assertIsNotNone(info)`
  - `self.assertEqual(info["thread_signature"], "thread-1")`

### test_target_message_with_resume_signal_restores_thread_extras
Summary: Tests target message with resume signal restores thread extras
Asserts:
  - `self.assertEqual(result, "RESUME")`
  - `self.assertEqual(resumed_event.get_extra("astrmai_thread_signature"), "thread-1")`
  - `self.assertEqual(resumed_event.get_extra("astrmai_reply_mode"), "casual_followup")`

## tests/original_ported/test_sys2_dialog_lane_reuse_ported.py (1 tests)

### test_text_mode_uses_dialog_lane_gateway
Summary: Tests text mode uses dialog lane gateway
Asserts:
  - `self.assertEqual(result, "lane-text-reply")`
  - `self.assertEqual(len(gateway.calls), 1)`
  - `self.assertEqual(mode, "chat")`
  - `self.assertEqual(kwargs["lane_key"].task_family, "dialog")`
  - `self.assertEqual(kwargs["base_origin"], "default:GroupMessage:group-1")`

## tests/manual/risk_audit/test_risk_expression_permanent_delete.py (5 tests)

### test_purge_kind_candidates_physically_deletes
Summary: purge_kind_candidates does a physical DELETE, not a soft-delete.
Asserts:
  - `self.assertIn("DELETE FROM canonical_memories", source,                       "purge_kind_candidates uses physical DELETE — "                       "deleted expressions are unrecoverable.")`

### test_rejected_expressions_have_grace_period
Summary: Rejected expressions have a 14-day grace period before purge.
Asserts:
  - `self.assertIn("rejected_expression_grace_seconds", source or "rejected",                       "There IS a grace period for rejected expressions. "                       "But after that, deletion is permanent.")`

### test_no_undo_path_after_purge
Summary: Once purge_kind_candidates runs, there is no undo/restore mechanism.
Asserts:
  - `self.assertEqual(found, [],                          f"No undo/restore mechanism in MemoryV2Store. "                          f"Deletion is truly permanent.")`

### test_auto_review_can_reject_correct_expressions
Summary: Auto-review uses LLM — hallucinations can reject valid expressions.
Asserts:
  - `self.assertIn("rejected", source.lower(),                       "Auto-review uses LLM to decide rejected/approved. "                       "LLM hallucination can reject correct expressions.")`

### test_purge_no_soft_delete_fallback
Summary: Verify purge_kind_candidates has no soft-delete code path.
Asserts:
  - `self.assertIn("DELETE FROM canonical_memories", source,                       "purge_kind_candidates uses physical DELETE")`
  - `self.assertIn("older_than_seconds", source,                       "older_than_seconds parameter controls the grace period. "                       "Default is set by the caller (14 days for rejected expressions).")`

## tests/test_pre_release_validation_refactor.py (2 tests)

### test_probe_categorization_prefers_clean_alive_and_rejects_reasoning_only
Summary: Tests probe categorization prefers clean alive and rejects reasoning only
Asserts:
  - `self.assertEqual(             _categorize_text_probe({"status": "passed", "content": "alive", "reasoning_content": ""}),             "recommended",         )`
  - `self.assertEqual(             _categorize_text_probe({"status": "passed", "content": "alive, here you go", "reasoning_content": ""}),             "backup",         )`
  - `self.assertEqual(             _categorize_text_probe({"status": "passed", "content": "", "reasoning_content": "thinking..."}),             "not_recommended",         )`
  - `self.assertEqual(             _categorize_text_probe({"status": "failed", "content": "", "reasoning_content": ""}),             "not_recommended",         )`

### test_write_pre_release_validation_artifacts_emits_json_and_markdown
Summary: Tests write pre release validation artifacts emits json and markdown
Asserts:
  - `self.assertTrue(json_path.exists())`
  - `self.assertTrue(md_path.exists())`
  - `self.assertEqual(loaded["overall_status"], "passed")`
  - `self.assertIn("# Pre-release Full Test Report", markdown)`
  - `self.assertIn("Real Provider Core Chain", markdown)`
  - `self.assertIn("openai/kimi-k2.5", markdown)`
  - `self.assertIn("真实 provider", markdown)`
  - `self.assertIn("浏览器点击流本轮复用了已存在的宿主页/直开页验收产物", markdown)`

## tests/regression/conversation/test_dialog_continuity_regression_migrated.py (1 tests)

### test_recent_transcript_uses_cleaned_dialog_history
Summary: Tests recent transcript uses cleaned dialog history
Asserts:
  - `self.assertIn("[Alice] 说: 为什么不可以", transcript)`
  - `self.assertIn("Bot: 不可以和妹妹结婚呀！", transcript)`
  - `self.assertNotIn("导演旁白", transcript)`

## tests/original_ported/test_attention_interaction_narrative_ported.py (1 tests)

### test_poke_narrative_uses_explicit_actor_and_target_labels
Summary: Tests poke narrative uses explicit actor and target labels
Asserts:
  - `self.assertEqual(len(filtered), 1)`
  - `self.assertEqual(event.get_extra("astrmai_interaction_kind"), "poke")`
  - `self.assertEqual(event.get_extra("astrmai_interaction_actor_id"), "3874287208")`
  - `self.assertEqual(event.get_extra("astrmai_interaction_target_id"), "516779421")`

## tests/test_outbound_error_policy_refactor.py (2 tests)

### test_ghost_message_is_dropped
Summary: Tests ghost message is dropped
Asserts:
  - `self.assertIsNone(event.get_result())`
  - `self.assertFalse(event._stopped)`

### test_error_message_is_intercepted_and_alerted
Summary: Tests error message is intercepted and alerted
Asserts:
  - `self.assertIsNone(event.get_result())`
  - `self.assertTrue(event._stopped)`
  - `self.assertEqual(             event.bot.api.calls,             [("send_private_msg", {"user_id": 1001, "message": "alert:Traceback: boom"})],         )`

## tests/regression/multimodal/test_vision_bundle_binding_migrated.py (1 tests)

### test_executor_prefers_focus_thread_vision_bundle
Summary: Tests executor prefers focus thread vision bundle
Asserts:
  - `self.assertEqual(bundle.direct_image_urls, ["thread-a.jpg", "extra.jpg"])`
  - `self.assertEqual(bundle.image_urls, ["thread-a.jpg", "thread-b.jpg", "extra.jpg"])`
  - `self.assertEqual(bundle.source, "focus_thread")`

## tests/test_workmode_router_refactor.py (1 tests)

### test_router_exposes_static_and_dynamic_agents
Summary: Tests router exposes static and dynamic agents
Asserts:
  - `self.assertIn("transfer_to_computer", names)`
  - `self.assertIn("transfer_to_cron", names)`
  - `self.assertIn("dynamic_alpha", names)`
  - `self.assertEqual(len(light.tools), len(names))`

## tests/original_ported/test_attention_focus_latest_fallback_ported.py (1 tests)

### test_falls_back_to_latest_user_message_without_explicit_signal
Summary: Tests falls back to lauser message without explicit signal
Asserts:
  - `self.assertIs(focus_event, latest_event)`
  - `self.assertEqual(reason, "latest_user_message")`
  - `self.assertEqual(background_events, [first_event, second_event])`

## tests/test_legacy_compat_refactor.py (2 tests)

### test_focus_and_prompt_legacy_extras_use_local_contracts
Summary: Tests focus and prompt legacy extras use local contracts
Asserts:
  - `self.assertIs(event.get_extra("astrmai_focus_thread_context"), focus_context)`
  - `self.assertIs(event.get_extra("astrmai_prompt_envelope"), prompt_envelope)`
  - `self.assertEqual(rebuilt.raw_user_text, "hello")`
  - `self.assertEqual(rebuilt.focus_message_text, "hello")`
  - `self.assertEqual(rebuilt.direct_context_text, "focus line")`

### test_read_focus_thread_context_from_dict_preserves_freshness_budget
Summary: 回归 (w11): JSON序列化后 astrmai_focus_thread_context 降级为 dict 时,
Asserts:
  - `self.assertEqual(fb.state.value, "expired")`
  - `self.assertEqual(fb.max_age_seconds, 3600.0)`
  - `self.assertEqual(fb.salvage_window_seconds, 300.0)`
  - `self.assertEqual(fb.latest_activity_ts, 900.0)`
  - `self.assertEqual(fb.stale_reason, "cache_expired")`

## tests/unit/runtime/test_chat_runtime_coordinator_migrated.py (4 tests)

### test_sys2_lock_is_reused_per_chat
Summary: Tests sys2 lock is reused per chat
Asserts:
  - `self.assertIs(first, second)`
  - `self.assertIsNot(first, other)`

### test_executor_pending_limit_and_release
Summary: Tests executor pending limit and release
Asserts:
  - `self.assertIsNotNone(first)`
  - `self.assertIsNotNone(second)`
  - `self.assertIsNone(third)`
  - `self.assertIsNotNone(fourth)`

### test_wait_targets_are_de_duplicated
Summary: Tests wait targets are de duplicated
Asserts:
  - `self.assertEqual(targets, ["u1", "u2"])`
  - `self.assertEqual(target_name, "Alice")`

### test_latest_activity_keeps_newest_timestamp
Summary: Tests laactivity keeps newest timestamp
Asserts:
  - `self.assertEqual(latest_ts, 20.0)`
  - `self.assertEqual(sender_id, "u2")`
  - `self.assertEqual(sender_name, "Bob")`
  - `self.assertEqual(preview, "new")`

## tests/unit/memory/test_memory_claim_rules_zh.py (4 tests)

### test_zh_correction_server_count_is_detected
Summary: Tests zh correction server count is detected
Asserts:
  - `self.assertTrue(claims)`
  - `self.assertTrue(claims[0].is_correction)`
  - `self.assertEqual(claims[0].attribute, "server_count")`

### test_zh_short_term_anxiety_is_detected
Summary: Tests zh short term anxiety is detected
Asserts:
  - `self.assertTrue(claims)`
  - `self.assertEqual(claims[0].fact_scope, "short_term")`
  - `self.assertEqual(claims[0].attribute, "anxiety_state")`

### test_zh_uncertain_server_count_still_extracts_claim
Summary: Tests zh uncertain server count still extracts claim
Asserts:
  - `self.assertEqual(claims[0].attribute, "server_count")`
  - `self.assertEqual(claims, [])`

### test_zh_correction_hint_is_present_for_phrase
Summary: Tests zh correction hint is present for phrase
Asserts:
  - `self.assertTrue(claims[0].is_correction)`
  - `self.assertEqual(claims, [])`

## tests/regression/reply/test_reply_engine_timeliness_migrated.py (2 tests)

### test_stale_reply_is_skipped_when_newer_activity_exists
Summary: Tests stale reply is skipped when newer activity exists
Asserts:
  - `self.assertEqual(state_engine.gateway.context.sent, [])`
  - `self.assertFalse(event.get_extra("astrmai_reply_sent", False))`

### test_direct_wakeup_reply_is_allowed_when_no_newer_activity_exists
Summary: Tests direct wakeup reply is allowed when no newer activity exists
Asserts:
  - `self.assertEqual(len(state_engine.gateway.context.sent), 1)`
  - `self.assertTrue(event.get_extra("astrmai_reply_sent", False))`

## tests/test_round2_high_regression.py (13 tests)

### test_r1_dream_interval_ge_1
Summary: Tests r1 dream interval ge 1
Asserts:
  - `self.assertIn("ge=1", src.split("dream_interval_min")[1][:50])`

### test_r4_breaker_uses_monotonic
Summary: Tests r4 breaker uses monotonic
Asserts:
  - `self.assertIn("breaker_until > monotonic()", src)`

### test_r5_short_ack_no_len_check
Summary: Tests r5 short ack no len check
Asserts:
  - `self.assertNotIn("len(compact_text) <= 4", src)`
  - `self.assertIn("return lowered in ThinkLevelPolicy.SHORT_ACKS", src)`

### test_r9_cooldown_uses_monotonic
Summary: Tests r9 cooldown uses monotonic
Asserts:
  - `self.assertIn('"until": monotonic() + duration', src)`

### test_r3_bootstrap_warns_on_null_gate
Summary: Tests r3 bootstrap warns on null gate
Asserts:
  - `self.assertIn("attention_gate is None", src)`

### test_r8_timeout_not_re_raised
Summary: Tests r8 timeout not re raised
Asserts:
  - `self.assertNotIn("raise TimeoutError", src)`

### test_r10_bm25_score_range_fixed
Summary: Tests r10 bm25 score range fixed
Asserts:
  - `self.assertIn("ORDER BY score ASC", src)`
  - `self.assertIn("(max_score - r.score) / score_range", src)`

### test_r11_visual_cortex_queue_maxsize
Summary: Tests r11 visual cortex queue maxsize
Asserts:
  - `self.assertIn("Queue(maxsize=100)", src)`

### test_r13_startup_hooks_raises
Summary: Tests r13 startup hooks raises
Asserts:
  - `self.assertIn("raise  # ponytail: R13", src)`

### test_r2_dream_throttle_in_semaphore
Summary: Tests r2 dream throttle in semaphore
Asserts:
  - `self.assertIn("throttle check inside semaphore", src)`

### test_r6_unique_constraint_added
Summary: Tests r6 unique constraint added
Asserts:
  - `self.assertIn("uq_expression_pattern", src)`
  - `self.assertIn("IntegrityError", src_db)`

### test_r7_async_init_updates_user_version
Summary: Tests r7 async init updates user version
Asserts:
  - `self.assertIn("PRAGMA user_version", src)`

### test_r12_deferred_messages_added
Summary: Tests r12 deferred messages added
Asserts:
  - `self.assertIn("_deferred_messages", src)`
  - `self.assertIn("queue.append(event)", src)`

## tests/test_medium_bugfix_regression.py (7 tests)

### test_r15_safe_create_task_uses_create_task
Summary: R15: safe_create_task uses asyncio.create_task, not ensure_future.
Asserts:
  - `self.assertIn("asyncio.create_task", src)`
  - `self.assertNotIn("asyncio.ensure_future", src)`
  - `self.assertIn("hasattr(t, 'get_name')", src)`

### test_r16_projection_failure_logged
Summary: R16: index projection failure is logged.
Asserts:
  - `self.assertIn("index projection failed for", src)`

### test_r19_shutdown_flush_not_silent
Summary: R19: shutdown flush no longer uses bare pass.
Asserts:
  - `self.assertIn("shutdown flush failed", src)`
  - `self.assertNotIn("# ponytail: secondary flush must not crash the shutdown", src)`

### test_r21_compute_hot_score_guards_access_count
Summary: R21: compute_hot_score uses max(0.0, ...) for access_count.
Asserts:
  - `self.assertIn("max(0.0, float(candidate.access_count or 0))", src)`

### test_r17_session_lock_eviction_checks_locked
Summary: R17: session lock eviction checks .locked() before popping.
Asserts:
  - `self.assertIn(".locked()", src)`

### test_r18_handoff_registry_no_one_shot_cache
Summary: R18: HandoffRegistry no longer uses one-shot _loaded cache for returns.
Asserts:
  - `self.assertNotIn("if self._loaded:\n            return list(self._dynamic_agents)", src)`
  - `self.assertIn("existing_names", src)`
  - `self.assertIn("# ponytail: re-scan every call", src)`

### test_r20_user_profile_touctou_re_read
Summary: R20: observe_user_activity re-reads profile under lock.
Asserts:
  - `self.assertIn("TOCTOU (R20)", src)`
  - `self.assertIn("await self._get_profile_inner(user_id)", src)`

## tests/test_group_trace_audit_refactor.py (1 tests)

### test_group_trace_audit_metrics_and_summary_include_failure_evidence
Summary: Tests group trace audit metrics and summary include failure evidence
Asserts:
  - `self.assertEqual(metrics["failure_kind_counts"]["provider_failure_text"], 1)`
  - `self.assertEqual(metrics["protocol_passthrough_counts"]["terminal_yield"], 1)`
  - `self.assertEqual(metrics["vision_failure_counts"]["empty_description"], 1)`
  - `self.assertIn("Failure kinds", markdown)`
  - `self.assertIn("Protocol passthrough", markdown)`
  - `self.assertIn("Vision failures", markdown)`

## tests/integration/runtime/test_runtime_contracts_migrated.py (5 tests)

### test_runtime_facade_protocol_exposes_apply_hot_config_contract
Summary: Tests runtime facade protocol exposes apply hot config contract
Asserts:
  - `self.assertTrue(hasattr(RuntimeFacadeProtocol, "apply_hot_config"))`

### test_focus_thread_context_all_thread_events_deduplicates
Summary: Tests focus thread context all thread events deduplicates
Asserts:
  - `self.assertEqual(merged, [root, focus, related])`

### test_prompt_envelope_preserves_structured_sections
Summary: Tests prompt envelope preserves structured sections
Asserts:
  - `self.assertEqual(envelope.focus_message_text, "Alice: why not?")`
  - `self.assertIn("AstrMai:", envelope.direct_context_text)`
  - `self.assertIn("Bob:", envelope.related_context_text)`
  - `self.assertEqual(envelope.ambient_background_text, "Carol: I am getting water")`

### test_llm_call_result_and_visible_reply_artifact_are_typed
Summary: Tests llm call result and visible reply artifact are typed
Asserts:
  - `self.assertFalse(result.ok)`
  - `self.assertEqual(result.error_kind, FailureKind.BAD_PAYLOAD)`
  - `self.assertFalse(artifact.blocked)`

### test_vision_bundle_keeps_direct_request_metadata
Summary: Tests vision bundle keeps direct request metadata
Asserts:
  - `self.assertTrue(bundle.is_direct_request)`
  - `self.assertEqual(bundle.source, "focus_thread")`

## tests/original_ported/test_single_history_source_regression_ported.py (1 tests)

### test_append_visible_reply_artifact_uses_lane_sanitizer
Summary: Tests append visible reply artifact uses lane sanitizer
Asserts:
  - `self.assertEqual(conversation.history[-2]["content"], "[Alice] 说: 为什么不可以")`
  - `self.assertEqual(conversation.history[-1]["content"], "呜……\n不要难过，亚托莉抱抱你！")`

## tests/original_ported/test_llm_call_result_flow_ported.py (1 tests)

### test_chat_in_lane_result_returns_structured_result
Summary: Tests chat in lane result returns structured result
Asserts:
  - `self.assertTrue(result.ok)`
  - `self.assertEqual(result.text, "结构化结果")`

## tests/test_main_reply_request_trace_refactor.py (1 tests)

### test_turn_trace_summary_includes_provider_visible_request_trace_fields
Summary: Tests turn trace summary includes provider visible request trace fields
Asserts:
  - `assert continuity["semantic_system_hash"] == "semantic1111"`
  - `assert continuity["semantic_system_length"] == 240`
  - `assert continuity["gateway_system_hash"] == "gatewaysys0001"`
  - `assert continuity["gateway_prompt_hash"] == "gatewayprompt0002"`
  - `assert continuity["provider_visible_system_hash"] == "syshash1234"`
  - `assert continuity["provider_visible_prompt_hash"] == "prompthash5678"`
  - `assert continuity["post_hook_system_hash"] == "posthook9999"`
  - `assert continuity["request_session_id"] == "session-1"`
  - `assert continuity["request_cache_control"] == '{"type":"ephemeral"}'`
  - `assert continuity["request_provider_family"] == "anthropic"`
  - `assert continuity["request_model_id"] == "claude-3-5-sonnet"`
  - `assert continuity["usage_input_tokens"] == 1200`
  - `assert continuity["usage_input_cached"] == 800`
  - `assert continuity["usage_output_tokens"] == 120`
  - `assert continuity["cache_ready"] is True`
  - `assert continuity["cache_hit"] is True`
  - `assert continuity["cache_ready_reasons"] == ["explicit_cache_hint", "session_reuse"]`
  - `assert continuity["cache_hit_evidence_supported"] is True`

## tests/test_chat_runtime_coordinator_refactor.py (3 tests)

### test_sys2_lock_is_reused_per_chat
Summary: Tests sys2 lock is reused per chat
Asserts:
  - `self.assertIs(first, second)`
  - `self.assertIsNot(first, other)`

### test_wait_targets_are_de_duplicated
Summary: Tests wait targets are de duplicated
Asserts:
  - `self.assertEqual(targets, ["u1", "u2"])`
  - `self.assertEqual(target_name, "Alice")`

### test_active_chat_listing_and_snapshot_use_recent_activity
Summary: Tests active chat listing and snapshot use recent activity
Asserts:
  - `self.assertEqual(active, ["chat-1"])`
  - `self.assertEqual(snapshot["latest_activity_sender_name"], "Alice")`
  - `self.assertEqual(snapshot["latest_activity_preview"], "second")`
  - `self.assertEqual(snapshot["latest_activity_thread_signature"], "thread-a")`
  - `self.assertEqual(snapshot["recent_activity_count"], 2)`

## tests/regression/review/test_review_service_migrated.py (2 tests)

### test_list_pending_reviews_returns_json_ready_payload
Summary: Tests list pending reviews returns json ready payload
Asserts:
  - `self.assertEqual(result[0]["review_status"], "pending_human")`
  - `self.assertEqual(result[0]["review_suggestion"], "这波节奏挺对")`

### test_submit_review_can_apply_replacement
Summary: Tests submit review can apply replacement
Asserts:
  - `self.assertEqual(result["expression"], "这波节奏挺对")`
  - `self.assertEqual(result["review_status"], "approved")`
  - `self.assertTrue(db.update_calls[0][1]["apply_replacement"])`

## tests/original_ported/test_outbound_policy_ported.py (1 tests)

### test_policy_changes_with_reply_mode_and_freshness
Summary: Tests policy changes with reply mode and freshness
Asserts:
  - `self.assertEqual(playful.segment_strategy, "single")`
  - `self.assertEqual(support.segment_strategy, "gentle_two_step")`
  - `self.assertTrue(stale.late_rewrite_allowed)`
  - `self.assertEqual(stale.length_class, "short")`

## tests/test_round6_followup_fixes.py (3 tests)

### test_admin_app_uses_safe_fetch_for_empty_api_fallbacks
Summary: Tests admin app uses safe fetch for empty api fallbacks
Asserts:
  - `assert "function safeFetch" in source`
  - `assert ".catch(() => ({}))" not in source`
  - `assert ".catch(() => ({ items: [] }))" not in source`

### test_expression_review_conflict_keeps_explicit_status
Summary: Tests expression review conflict keeps explicit status
Asserts:
  - `assert updated.review_status == "approved"`
  - `assert store.updated_metadata["review_status"] == "approved"`

### test_memory_store_exposes_projector_slot
Summary: Tests memory store exposes projector slot
Asserts:
  - `assert store.index_projector is projector`

## tests/test_external_result_bridge_refactor.py (1 tests)

### test_bridge_injects_attention_event_and_records_bot_reply
Summary: Tests bridge injects attention event and records bot reply
Asserts:
  - `self.assertEqual(len(runtime.attention_gate.calls), 1)`
  - `self.assertEqual(             runtime.attention_gate.calls[0][1]["extra"]["astrmai_loop_source"],             "external_result_bridge",         )`
  - `self.assertEqual(             runtime.evolution.calls,             [("default:GroupMessage:group-1", "bot-1", "(内置插件执行结果): 任务完成[图片]")],         )`

## tests/test_meme_service_refactor.py (1 tests)

### test_send_meme_uses_local_multimodal_directory
Summary: Tests send meme uses local multimodal directory
Asserts:
  - `self.assertEqual(sent[0][0], "group-1")`
  - `self.assertTrue(sent[0][1])`

## tests/original_ported/test_group_reply_wait_manager_ported.py (2 tests)

### test_register_direct_wakeup_and_resume_on_target_message
Summary: Tests register direct wakeup and resume on target message
Asserts:
  - `self.assertTrue(manager.register_from_reply_event(reply_event))`
  - `self.assertEqual(result, "RESUME")`
  - `self.assertTrue(resumed_event.get_extra("astrmai_force_engage"))`
  - `self.assertEqual(resumed_event.get_extra("astrmai_group_wait_target_id"), "user-42")`
  - `self.assertIsNone(manager.get_wait_info("default:GroupMessage:group-1"))`

### test_non_target_messages_consume_message_budget
Summary: Tests non target messages consume message budget
Asserts:
  - `self.assertEqual(result1, "OBSERVED")`
  - `self.assertEqual(result2, "EXPIRED")`
  - `self.assertIsNone(manager.get_wait_info("default:GroupMessage:group-1"))`

## tests/unit/multimodal/test_image_pipeline_p2_gap_coverage.py (3 tests)

### test_prepare_image_writes_and_cleanup_removes_temp_file
Summary: Tests prepare image writes and cleanup removes temp file
Asserts:
  - `self.assertIsNotNone(prepared)`
  - `self.assertEqual(prepared.image_format, "png")`
  - `self.assertTrue(os.path.exists(prepared.file_path))`
  - `self.assertFalse(os.path.exists(prepared.file_path))`

### test_transform_gif_returns_jpeg_base64_and_prepare_converts_format
Summary: Tests transform gif returns jpeg base64 and prepare converts format
Asserts:
  - `self.assertIsInstance(transformed, str)`
  - `self.assertGreater(len(base64.b64decode(transformed)), 0)`
  - `self.assertIsNotNone(prepared)`
  - `self.assertEqual(prepared.image_format, "jpeg")`

### test_serialize_tags_accepts_only_lists
Summary: Tests serialize tags accepts only lists
Asserts:
  - `self.assertEqual(ImagePipeline.serialize_tags(["happy"]), '["happy"]')`
  - `self.assertEqual(ImagePipeline.serialize_tags({"tag": "happy"}), "[]")`

## tests/test_embedding_refactor.py (3 tests)

### test_round_robin_configured_embedding_models
Summary: Tests round robin configured embedding models
Asserts:
  - `self.assertEqual(first, [1.0, 0.0])`
  - `self.assertEqual(second, [0.0, 1.0])`
  - `self.assertEqual(context.providers["a"].calls, 1)`
  - `self.assertEqual(context.providers["b"].calls, 1)`

### test_auto_fallback_only_when_no_models_configured
Summary: Tests auto fallback only when no models configured
Asserts:
  - `self.assertEqual(asyncio.run(client.get_vector("hello")), [0.5, 0.5])`
  - `self.assertIsNone(asyncio.run(configured_missing.get_vector("hello")))`
  - `self.assertEqual(context.auto_provider.calls, 1)`

### test_cosine_similarity
Summary: Tests cosine similarity
Asserts:
  - `self.assertAlmostEqual(EmbeddingClient.cosine_similarity([1, 0], [1, 0]), 1.0)`
  - `self.assertEqual(EmbeddingClient.cosine_similarity([1, 0], [0, 1]), 0.0)`
  - `self.assertEqual(EmbeddingClient.cosine_similarity([1], [1, 2]), 0.0)`

## tests/original_ported/test_gateway_image_payload_passthrough_ported.py (1 tests)

### test_image_urls_are_sent_via_kwarg_without_content_part_wrapping
Summary: Tests image urls are sent via kwarg without content part wrapping
Asserts:
  - `self.assertEqual(reply_text, "图片看到了")`
  - `self.assertEqual(fake_context.calls[0]["prompt"], "帮我看看这张图")`
  - `self.assertEqual(fake_context.calls[0]["image_urls"], ["https://example.com/a.jpg"])`

## tests/original_ported/test_social_transcript_turns_ported.py (1 tests)

### test_recent_transcript_keeps_socially_rendered_turns
Summary: Tests recent transcript keeps socially rendered turns
Asserts:
  - `self.assertIn("[互动事件：刚刚", transcript)`
  - `self.assertIn("Bot: 我在这儿。", transcript)`
  - `self.assertIn("用户: 今天好困", transcript)`

## tests/original_ported/test_visible_reply_artifact_ported.py (2 tests)

### test_artifact_blocks_dirty_provider_text
Summary: Tests artifact blocks dirty provider text
Asserts:
  - `self.assertFalse(artifact.blocked)`
  - `self.assertEqual(artifact.visible_text, "fallback")`

### test_artifact_keeps_sendable_segments_and_persistable_text
Summary: Tests artifact keeps sendable segments and persistable text
Asserts:
  - `self.assertFalse(artifact.blocked)`
  - `self.assertIn("hmm", artifact.visible_text)`
  - `self.assertIn("Do not be sad", artifact.persistable_text)`

## tests/test_round5_followup_fixes.py (1 tests)

### test_prompt_sanitizer_escapes_boundary_tags_with_ascii
Summary: Tests prompt sanitizer escapes boundary tags with ascii
Asserts:
  - `assert user.count("<user_input>") == 1`
  - `assert user.count("</user_input>") == 1`
  - `assert "<retrieved_memory>" not in user`
  - `assert "</retrieved_memory>" not in user`
  - `assert memory.count("<retrieved_memory>") == 1`
  - `assert memory.count("</retrieved_memory>") == 1`
  - `assert "<user_input>" not in memory`
  - `assert "</user_input>" not in memory`

## tests/original_ported/test_dialog_lane_summary_compaction_ported.py (1 tests)

### test_dialog_lane_compacts_old_history_into_summary
Summary: Tests dialog lane compacts old history into summary
Asserts:
  - `self.assertLess(len(compacted), len(raw_history))`
  - `self.assertEqual(compacted[0]["role"], "assistant")`
  - `self.assertTrue(str(compacted[0]["content"]).startswith("较早对话摘要："))`
  - `self.assertEqual(compacted[-1]["content"], "assistant-15")`

## tests/test_output_guard_refactor.py (4 tests)

### test_validate_visible_output_text_classifies_provider_error
Summary: Tests validate visible output text classifies provider error
Asserts:
  - `self.assertEqual(safe_text, "")`
  - `self.assertEqual(failure_kind, "provider_failure_text")`

### test_validate_visible_output_text_classifies_wrapped_tool_loop_provider_error
Summary: Tests validate visible output text classifies wrapped tool loop provider error
Asserts:
  - `self.assertEqual(safe_text, "")`
  - `self.assertEqual(failure_kind, "provider_failure_text")`

### test_validate_visible_output_text_classifies_prompt_scaffold
Summary: Tests validate visible output text classifies prompt scaffold
Asserts:
  - `self.assertEqual(safe_text, "")`
  - `self.assertEqual(failure_kind, "prompt_scaffold_text")`

### test_validate_visible_output_text_classifies_tool_protocol
Summary: Tests validate visible output text classifies tool protocol
Asserts:
  - `self.assertEqual(safe_text, "")`
  - `self.assertEqual(failure_kind, "tool_protocol_text")`

## tests/unit/runtime/test_gateway_result_migrated.py (3 tests)

### test_enrich_cache_debug_meta_does_not_infer_prefix_stable_from_hash_presence
Summary: Tests enrich cache debug meta does not infer prefix stable from hash presence
Asserts:
  - `self.assertNotIn("prefix_stable", meta)`
  - `self.assertTrue(meta["cache_affinity_enabled"])`
  - `self.assertTrue(meta["cached_usage_supported"])`

### test_build_cache_observation_requires_explicit_prefix_stable_signal
Summary: Tests build cache observation requires explicit prefix stable signal
Asserts:
  - `self.assertEqual(             observation["cache_ready_reasons"],             ["session_reuse", "cache_affinity_enabled"],         )`
  - `self.assertNotIn("semantic_system_hash_stable", observation["cache_ready_reasons"])`

### test_build_cache_observation_keeps_explicit_prefix_stable_signal
Summary: Tests build cache observation keeps explicit prefix stable signal
Asserts:
  - `self.assertEqual(             observation["cache_ready_reasons"],             ["semantic_system_hash_stable", "provider_visible_hash_stable"],         )`

## tests/original_ported/test_reply_freshness_budget_ported.py (1 tests)

### test_evaluate_reply_freshness_returns_stale_and_expired
Summary: Tests evaluate reply freshness returns stale and expired
Asserts:
  - `self.assertEqual(stale_state[0], FreshnessState.STALE_BUT_SALVAGEABLE)`
  - `self.assertEqual(fresh_same_thread[0], FreshnessState.FRESH)`
  - `self.assertEqual(expired_state[0], FreshnessState.EXPIRED)`

## tests/unit/conversation/test_attention_vision_binding_gap_coverage.py (3 tests)

### test_extract_prefers_component_file_to_base64
Summary: Tests extract prefers component file to base64
Asserts:
  - `self.assertEqual(result, "already-encoded")`

### test_extract_falls_back_to_local_file_after_component_failure
Summary: Tests extract falls back to local file after component failure
Asserts:
  - `self.assertEqual(result, base64.b64encode(b"local-bytes").decode("utf-8"))`

### test_extract_url_rejects_unsafe_scheme_without_http_client
Summary: Tests extract url rejects unsafe scheme without http client
Asserts:
  - `self.assertEqual(result, "")`

## tests/original_ported/test_planner_prompt_context_guards_ported.py (2 tests)

### test_event_line_uses_speaker_content_layout
Summary: Tests event line uses speaker content layout
Asserts:
  - `self.assertEqual(line, "Alice: ATRI: hi <3")`
  - `self.assertNotIn("<message speaker=", line)`

### test_poke_event_is_lightweight
Summary: Tests poke event is lightweight
Asserts:
  - `self.assertTrue(self.prompt_context_mod.PlannerPromptContextMixin._is_lightweight_event(event, context))`

## tests/test_reverse_session_refactor.py (2 tests)

### test_render_parse_strip_and_append_roundtrip
Summary: Tests render parse strip and append roundtrip
Asserts:
  - `self.assertIn(REVERSE_SESSION_TAG, block)`
  - `self.assertEqual(parsed["session_id"], "session-1")`
  - `self.assertEqual(parsed["session_scope"], "chat")`
  - `self.assertEqual(strip_reverse_session_block(prompt), "base")`

### test_provider_detection_and_maybe_attach
Summary: Tests provider detection and maybe attach
Asserts:
  - `self.assertTrue(provider_is_gemini_reverse(provider))`
  - `self.assertIn("session_id=session-2", prompt)`
  - `self.assertEqual(maybe_attach_reverse_session_block("base", normal_provider, session_id="x"), "base")`

## tests/test_text_segmenter_gap_coverage.py (2 tests)

### test_segmenter_keeps_url_intact_across_sentence_split
Summary: Tests segmenter keeps url intact across sentence split
Asserts:
  - `self.assertTrue(any("https://example.com/a/b?x=1&y=2" in segment for segment in segments))`
  - `self.assertFalse(any("https://example.com/a/b" in segment and "y=2" not in segment for segment in segments))`

### test_segmenter_keeps_fenced_code_block_as_single_unit
Summary: Tests segmenter keeps fenced code block as single unit
Asserts:
  - `self.assertEqual(len(code_segments), 1)`
  - `self.assertIn("print('a.b?c!')", code_segments[0])`
  - `self.assertIn("```", code_segments[0])`

## tests/test_gateway_policy_refactor.py (1 tests)

### test_classify_failure_kind_covers_new_output_categories
Summary: Tests classify failure kind covers new output categories
Asserts:
  - `self.assertEqual(             self.policy._classify_failure_kind("unsafe_or_empty_text"),             self.contracts_mod.FailureKind.UNSAFE_OR_EMPTY_TEXT,         )`
  - `self.assertEqual(             self.policy._classify_failure_kind("prompt_scaffold_text"),             self.contracts_mod.FailureKind.PROMPT_SCAFFOLD_TEXT,         )`
  - `self.assertEqual(             self.policy._classify_failure_kind("tool_protocol_text"),             self.contracts_mod.FailureKind.TOOL_PROTOCOL_TEXT,         )`

## tests/unit/shared/test_safe_create_task.py (1 tests)

### test_returns_task_object
Summary: safe_create_task() returns an asyncio.Task
Asserts:
  - `assert isinstance(task, asyncio.Task)`

## tests/unit/presentation/test_startup_hooks_p2_gap_coverage.py (2 tests)

### test_on_program_start_delegates_to_lifecycle_manager
Summary: Tests on program start delegates to lifecycle manager
Asserts:
  - `self.assertTrue(lifecycle.called)`

### test_on_program_start_propagates_lifecycle_failure
Summary: Tests on program start propagates lifecycle failure
Asserts:
  - `self.assertRaisesRegex(RuntimeError, "startup failed")`

## tests/unit/state/test_private_chat_manager_migrated.py (2 tests)

### test_wait_for_new_message_uses_buffered_message_arrived_before_wait
Summary: Tests wait for new message uses buffered message arrived before wait
Asserts:
  - `self.assertTrue(has_reply)`
  - `self.assertEqual(pending, ["hello"])`

### test_group_chat_id_does_not_alias_friend_session_with_same_numeric_tail
Summary: Tests group chat id does not alias friend session with same numeric tail
Asserts:
  - `self.assertIsNotNone(friend_info)`
  - `self.assertIsNone(group_info)`

## tests/test_capability_overview_refactor.py (1 tests)

### test_plugin_facade_exposes_stable_capability_overview_entrypoints
Summary: Tests plugin facade exposes stable capability overview entrypoints
Asserts:
  - `self.assertIn("def get_capability_overview_sync(self) -> dict:", facade_content)`
  - `self.assertIn("async def get_capability_overview(self) -> dict:", facade_content)`
  - `self.assertIn("return self.runtime.build_capability_overview_sync()", facade_content)`
  - `self.assertIn("return await self.runtime.build_capability_overview()", facade_content)`
  - `self.assertIn('diagnostics["capabilities"] = self.get_capability_overview_sync()', facade_content)`
  - `self.assertIn("def build_capability_overview_sync(self) -> dict[str, Any]:", runtime_content)`
  - `self.assertIn("async def build_capability_overview(self) -> dict[str, Any]:", runtime_content)`
  - `self.assertIn('"multimodal"', runtime_content)`
  - `self.assertIn('"proactive"', runtime_content)`
  - `self.assertIn('"workmode"', runtime_content)`
  - `self.assertIn('"dream_scheduler"', runtime_content)`
  - `self.assertIn('"review_dispatcher"', runtime_content)`

## tests/test_message_scope_contract_refactor.py (2 tests)

### test_contract_message_scope_reexports_presentation_authority
Summary: Tests contract message scope reexports presentation authority
Asserts:
  - `self.assertIs(ContractIngressDecision, PresentationIngressDecision)`
  - `self.assertIs(ContractMessageScope, PresentationMessageScope)`

### test_text_segmenter_no_longer_exposes_semantic_chunk
Summary: Tests text segmenter no longer exposes semantic chunk
Asserts:
  - `self.assertFalse(hasattr(segmenter_mod.TextSegmenter, "semantic_chunk"))`

## tests/test_presentation_commands_refactor.py (2 tests)

### test_work_command_request_parses_direct_work_message
Summary: Tests work command request parses direct work message
Asserts:
  - `self.assertEqual(request.task_query, "帮我整理今天的待办")`
  - `self.assertTrue(request.has_query)`

### test_main_uses_presentation_command_handlers
Summary: Tests main uses presentation command handlers
Asserts:
  - `self.assertIn("from .astrmai.presentation.commands import handle_mai_help, handle_work_mode", content)`
  - `self.assertIn("async for result in handle_mai_help(self.facade, event):", content)`
  - `self.assertIn("async for result in handle_work_mode(self.facade, event):", content)`

## tests/regression/architecture/test_directory_contracts_refactor.py (3 tests)

### test_refactor_test_layout_has_expected_buckets
Summary: Tests refactor layout has expected buckets
Asserts:
  - `self.assertTrue(path.exists(), str(path))`

### test_plugin_page_is_the_only_supported_management_entry
Summary: Tests plugin page is the only supported management entry
Asserts:
  - `self.assertTrue((PLUGIN_PAGE_ROOT / "index.html").exists())`
  - `self.assertTrue((PLUGIN_PAGE_ROOT / "app.js").exists())`
  - `self.assertTrue((PLUGIN_PAGE_ROOT / "style.css").exists())`
  - `self.assertNotIn("frontend_dir", server_content)`
  - `self.assertNotIn("StaticFiles(directory=frontend_dir", server_content)`

### test_p2_99_acceptance_docs_exist
Summary: Tests p2 99 acceptance docs exist
Asserts:
  - `self.assertTrue((ROOT / "plan" / "P2_99_TEST_MIGRATION_MATRIX.md").exists())`
  - `self.assertTrue((ROOT / "plan" / "P2_99_ACCEPTANCE_CHECKLIST.md").exists())`

## tests/unit/learning/test_message_recorder_migrated.py (3 tests)

### test_trigger_after_window_and_min_messages
Summary: Tests trigger after window and min messages
Asserts:
  - `self.assertFalse(recorder.record("group-a", timestamp=1000.0))`
  - `self.assertFalse(recorder.record("group-a", timestamp=1010.0))`
  - `self.assertTrue(recorder.record("group-a", timestamp=1020.0))`

### test_respects_cooldown
Summary: Tests respects cooldown
Asserts:
  - `self.assertFalse(recorder.record("group-a", timestamp=1000.0))`
  - `self.assertTrue(recorder.record("group-a", timestamp=1001.0))`
  - `self.assertFalse(recorder.record("group-a", timestamp=1010.0))`
  - `self.assertTrue(recorder.record("group-a", timestamp=1065.0))`

### test_clear_resets_window
Summary: Tests clear resets window
Asserts:
  - `self.assertFalse(recorder.record("group-a", timestamp=1000.0))`
  - `self.assertFalse(recorder.record("group-a", timestamp=1001.0))`

## tests/original_ported/test_host_bridge_ported.py (2 tests)

### test_suppress_default_llm_returns_ghost_sentinel
Summary: Tests suppress default llm returns ghost sentinel
Asserts:
  - `self.assertTrue(event.call_llm)`
  - `self.assertEqual(sentinel, HostBridge.GHOST_SENTINEL)`
  - `self.assertTrue(bridge.is_ghost_sentinel(sentinel))`

### test_error_alert_contains_chat_context
Summary: Tests error alert contains chat context
Asserts:
  - `self.assertIn("群聊(123)", message)`
  - `self.assertIn("测试用户", message)`
  - `self.assertTrue(bridge.should_intercept_error("All chat models failed"))`

## tests/test_data_resources_refactor.py (1 tests)

### test_original_runtime_data_assets_are_present
Summary: Tests original runtime data assets are present
Asserts:
  - `self.assertIsInstance(json.load(file), dict)`
  - `self.assertTrue(ref_file.exists(), rel_path)`
  - `self.assertGreater(ref_file.stat().st_size, 0)`

## tests/test_bootstrap_p1_02_refactor.py (1 tests)

### test_bootstrap_uses_local_workmode_multimodal_and_proactive
Summary: Tests bootstrap uses local workmode multimodal and proactive
Asserts:
  - `self.assertIn("from ..proactive import ProactiveTask", content)`
  - `self.assertIn("from ..workmode import CronHeartbeatGuard, Sys3Router", content)`
  - `self.assertIn("runtime.feature_flags.work_mode_enabled", content)`
  - `self.assertIn("runtime.feature_flags.proactive_enabled", content)`
  - `self.assertNotIn("from astrmai.work.router import Sys3Router", content)`
  - `self.assertNotIn("from astrmai.work.cron_guard.heartbeat import CronHeartbeatGuard", content)`
  - `self.assertNotIn("from astrmai.evolution.proactive_task import ProactiveTask", content)`

## tests/original_ported/test_context_behavior_rules_ported.py (1 tests)

### test_behavior_rules_change_with_reply_mode_and_freshness
Summary: Tests behavior rules change with reply mode and freshness
Asserts:
  - `self.assertTrue(block.strip())`
  - `self.assertGreaterEqual(block.count("- "), 3)`
  - `self.assertNotEqual(block, engine._build_behavior_rule_block(PromptEnvelope()))`

## tests/test_bootstrap_p1_refactor.py (1 tests)

### test_bootstrap_no_longer_imports_legacy_big_classes
Summary: Tests bootstrap no longer imports legacy big classes
Asserts:
  - `self.assertNotIn("from astrmai.Heart.state_engine import StateEngine", content)`
  - `self.assertNotIn("from astrmai.memory.engine import MemoryEngine", content)`
  - `self.assertNotIn("from astrmai.evolution.processor import EvolutionManager", content)`
  - `self.assertIn("from ..state import", content)`
  - `self.assertIn("from ..memory import", content)`
  - `self.assertIn("from ..learning import", content)`

## tests/regression/architecture/test_shared_test_support_refactor.py (1 tests)

### test_temp_astrbot_env_installs_local_data_path
Summary: Tests temp astrbot env installs local data path
Asserts:
  - `self.assertEqual(path_mod.get_astrbot_data_path(), env.path)`
