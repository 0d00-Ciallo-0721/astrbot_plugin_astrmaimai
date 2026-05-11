import unittest

PORTED_MODULES = [
    "tests.original_ported.test_dialog_lane_summary_compaction_ported",
    "tests.original_ported.test_gateway_failure_normalization_ported",
    "tests.original_ported.test_gateway_image_payload_passthrough_ported",
    "tests.original_ported.test_gateway_lane_request_kwargs_ported",
    "tests.original_ported.test_host_bridge_ported",
    "tests.original_ported.test_lane_history_sanitization_ported",
    "tests.original_ported.test_lane_manager_conversation_binding_ported",
    "tests.original_ported.test_lane_stores_raw_dialogue_ported",
    "tests.original_ported.test_llm_call_result_flow_ported",
    "tests.original_ported.test_mojibake_output_guard_ported",
    "tests.original_ported.test_reply_freshness_budget_ported",
    "tests.original_ported.test_reverse_session_marker_ported",
    "tests.original_ported.test_single_history_source_regression_ported",
    "tests.original_ported.test_social_transcript_turns_ported",
    "tests.original_ported.test_attention_focus_latest_fallback_ported",
    "tests.original_ported.test_attention_focus_root_resolution_ported",
    "tests.original_ported.test_attention_focus_selection_ported",
    "tests.original_ported.test_attention_focus_thread_followups_ported",
    "tests.original_ported.test_attention_image_gating_ported",
    "tests.original_ported.test_attention_private_chat_ported",
    "tests.original_ported.test_planner_follow_up_ported",
    "tests.original_ported.test_planner_focus_message_priority_ported",
    "tests.original_ported.test_planner_includes_last_assistant_turn_ported",
    "tests.original_ported.test_prompt_envelope_rendering_ported",
    "tests.original_ported.test_prompt_prefix_stability_ported",
    "tests.original_ported.test_prompt_refiner_focus_layout_ported",
    "tests.original_ported.test_prompt_refiner_lightweight_ported",
    "tests.original_ported.test_reply_engine_output_guard_ported",
    "tests.original_ported.test_expression_selector_reviewed_ported",
    "tests.original_ported.test_group_wait_thread_signature_ported",
    "tests.original_ported.test_sys2_dialog_lane_reuse_ported",
    "tests.original_ported.test_expression_governance_ported",
    "tests.original_ported.test_reply_engine_focus_anchor_ported",
    "tests.original_ported.test_visible_reply_artifact_ported",
    "tests.original_ported.test_outbound_policy_ported",
    "tests.original_ported.test_context_behavior_rules_ported",
    "tests.original_ported.test_database_adapters_ported",
    "tests.original_ported.test_dialog_focus_continuity_regression_ported",
    "tests.original_ported.test_group_reply_wait_manager_ported",
    "tests.original_ported.test_near_context_priority_ported",
]


def load_tests(loader, tests, pattern):
    suite = unittest.TestSuite()
    suite.addTests(loader.loadTestsFromNames(PORTED_MODULES))
    return suite
