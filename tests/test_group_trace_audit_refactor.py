import importlib
import json
import unittest


class GroupTraceAuditRefactorTests(unittest.TestCase):
    def test_group_trace_audit_metrics_and_summary_include_failure_evidence(self):
        audit_mod = importlib.import_module("tests.manual.group_trace_audit")
        records = [
            {
                "scenario_id": "s1",
                "turn_index": 1,
                "self_check_passed": False,
                "compaction_state": "WAIT_NEXT_NODE",
                "evaluation_count": 100,
                "closure_score": 0.2,
                "tail_activity_score": 0.3,
                "topic_density_score": 0.4,
                "stability_score": 0.5,
                "benefit_score": 0.6,
                "safe_hook_block_reason": "reply_chain_active",
                "failure_kind": "provider_failure_text",
                "protocol_passthrough": True,
                "protocol_type": "terminal_yield",
                "vision_failure_kind": "empty_description",
            }
        ]
        summaries = [
            {
                "scenario_id": "s1",
                "title": "title",
                "description": "desc",
                "tags": [],
                "difficulty": "base",
                "total_turns": 1,
                "self_check_passed_turns": 0,
                "self_check_failed_turns": 1,
                "states_seen": ["WAIT_NEXT_NODE"],
                "state_counts": {"WAIT_NEXT_NODE": 1},
                "first_state_turns": {"WAIT_NEXT_NODE": 1},
                "block_reason_counts": {"reply_chain_active": 1},
                "failure_kind_counts": {"provider_failure_text": 1},
                "protocol_passthrough_counts": {"terminal_yield": 1},
                "vision_failure_counts": {"empty_description": 1},
                "last_reply_preview": "preview",
                "last_fail_reasons": ["bad"],
                "failure_cards": [],
                "forced_trajectory": [],
                "recovery_checks": [],
            }
        ]
        metrics = audit_mod.build_metrics(summaries, records)
        markdown = audit_mod.render_summary_markdown(summaries, audit_mod.SummaryOptions(), metrics)

        self.assertEqual(metrics["failure_kind_counts"]["provider_failure_text"], 1)
        self.assertEqual(metrics["protocol_passthrough_counts"]["terminal_yield"], 1)
        self.assertEqual(metrics["vision_failure_counts"]["empty_description"], 1)
        self.assertIn("Failure kinds", markdown)
        self.assertIn("Protocol passthrough", markdown)
        self.assertIn("Vision failures", markdown)
        json.dumps(metrics, ensure_ascii=False)


if __name__ == "__main__":
    unittest.main()
