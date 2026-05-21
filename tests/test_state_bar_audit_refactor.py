import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers.state_bar_audit import (
    build_state_bar_audit_baseline,
    write_state_bar_audit_artifacts,
)


class StateBarAuditRefactorTests(unittest.TestCase):
    def test_build_state_bar_audit_baseline_exposes_current_mood_and_stance_findings(self):
        baseline = build_state_bar_audit_baseline()

        self.assertEqual(baseline["title"], "P10.2 / P10.3 audit baseline")

        mood = baseline["mood"]
        self.assertEqual(mood["audit_mode"], "static_and_chain_level")
        self.assertEqual(mood["live_llm_semantic_audit"]["status"], "not_run")
        self.assertIn("ASTRMAI_ENABLE_LIVE_MOOD_AUDIT", mood["live_llm_semantic_audit"]["reason"])
        self.assertEqual(mood["summary"]["parser_failures"], [])
        self.assertEqual(mood["summary"]["fallback_issue_counts"]["mixed_affect_flattened"], 0)
        self.assertEqual(mood["summary"]["fallback_issue_counts"]["direction_conflict"], 0)
        sarcasm_case = next(item for item in mood["fallback_cases"] if item["case_id"] == "fallback_sarcasm")
        self.assertEqual(sarcasm_case["primary_mood_tag"], "angry")
        mixed_case = next(item for item in mood["fallback_cases"] if item["case_id"] == "fallback_mixed")
        self.assertEqual(mixed_case["primary_mood_tag"], "sad")

        social_score = baseline["social_score"]
        self.assertEqual(social_score["audit_mode"], "static_and_host_chain_semantics")
        self.assertEqual(social_score["summary"]["issue_case_ids"], [])
        self.assertTrue(social_score["summary"]["mixed_affect_remap_suppressed"])
        self.assertTrue(social_score["summary"]["positive_layering_ok"])
        self.assertTrue(social_score["summary"]["negative_layering_ok"])
        self.assertTrue(social_score["summary"]["publish_change_semantics_aligned"])
        self.assertLess(social_score["summary"]["mixed_affect_social_score"], 1.0)
        mixed_social = next(item for item in social_score["cases"] if item["case_id"] == "mixed_affect")
        positive_social = next(item for item in social_score["cases"] if item["case_id"] == "positive_gratitude")
        self.assertEqual(mixed_social["effective_event_type"], "normal_chat")
        self.assertEqual(mixed_social["published_mood_tag"], "")
        self.assertEqual(mixed_social["published_event_type"], "normal_chat")
        self.assertLess(mixed_social["social_score"], positive_social["social_score"])
        comfort_social = next(item for item in social_score["cases"] if item["case_id"] == "comfort_with_complaint")
        self.assertEqual(comfort_social["effective_event_type"], "normal_chat")
        self.assertLessEqual(comfort_social["social_score"], 0.4)
        ambiguous_social = next(item for item in social_score["cases"] if item["case_id"] == "ambiguous_soft_affection")
        self.assertEqual(ambiguous_social["effective_event_type"], "greeting")
        self.assertLessEqual(ambiguous_social["social_score"], 0.24)
        cold_social = next(item for item in social_score["cases"] if item["case_id"] == "cold_distance")
        self.assertEqual(cold_social["effective_event_type"], "ignore")
        self.assertLess(cold_social["social_score"], -0.2)
        self.assertGreaterEqual(cold_social["social_score"], -0.30)
        perfunctory_social = next(item for item in social_score["cases"] if item["case_id"] == "perfunctory_brief")
        self.assertEqual(perfunctory_social["effective_event_type"], "ignore")
        self.assertLessEqual(perfunctory_social["social_score"], -0.30)
        irritation_social = next(item for item in social_score["cases"] if item["case_id"] == "mild_irritation")
        self.assertEqual(irritation_social["effective_event_type"], "ignore")
        self.assertLessEqual(irritation_social["social_score"], -0.40)
        long_mixed_social = next(item for item in social_score["cases"] if item["case_id"] == "long_mixed_balance")
        self.assertEqual(long_mixed_social["effective_event_type"], "normal_chat")
        self.assertLessEqual(long_mixed_social["social_score"], 0.4)
        tool_social = next(item for item in social_score["cases"] if item["case_id"] == "tool_intent")
        self.assertGreater(tool_social["social_score"], mixed_social["social_score"])
        self.assertGreater(mixed_social["social_score"], ambiguous_social["social_score"])
        self.assertGreater(cold_social["social_score"], perfunctory_social["social_score"])
        self.assertGreater(perfunctory_social["social_score"], irritation_social["social_score"])

        stance = baseline["stance"]
        self.assertEqual(stance["audit_mode"], "chain_level_plus_prompt_surface")
        self.assertTrue(stance["summary"]["guarded_tool_constraints_present"])
        self.assertTrue(stance["summary"]["cool_tool_constraints_present"])
        self.assertLess(
            stance["summary"]["guarded_follow_up_probability"],
            stance["summary"]["neutral_follow_up_probability"],
        )
        self.assertFalse(stance["summary"]["first_reply_constraints_are_prompt_only"])
        guarded_answer = next(item for item in stance["cases"] if item["stance"] == "guarded" and item["social_intent"] == "answer")
        self.assertTrue(guarded_answer["first_reply_hard_constraint_present"])
        self.assertEqual(guarded_answer["first_reply_surface_mode"], "hard_clamped")
        guarded_boundary = next(item for item in stance["cases"] if item["stance"] == "guarded" and item["social_intent"] == "boundary")
        cool_comfort = next(item for item in stance["cases"] if item["stance"] == "cool" and item["social_intent"] == "comfort")
        self.assertLess(guarded_boundary["stance_char_cap"], guarded_answer["stance_char_cap"])
        self.assertGreater(cool_comfort["stance_char_cap"], guarded_answer["stance_char_cap"])
        self.assertGreaterEqual(guarded_answer["stance_sentence_cap"], 1)

    def test_write_state_bar_audit_artifacts_emits_json_and_markdown(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            result = write_state_bar_audit_artifacts(temp_dir)
            json_path = Path(result["json_path"])
            markdown_path = Path(result["markdown_path"])

            self.assertTrue(json_path.exists())
            self.assertTrue(markdown_path.exists())
            self.assertTrue(Path(result["social_score_json_path"]).exists())
            self.assertTrue(Path(result["social_score_markdown_path"]).exists())

            payload = json.loads(json_path.read_text(encoding="utf-8"))
            markdown = markdown_path.read_text(encoding="utf-8")
            social_markdown = Path(result["social_score_markdown_path"]).read_text(encoding="utf-8")

            self.assertEqual(payload["title"], "P10.2 / P10.3 audit baseline")
            self.assertIn("# P10.2 / P10.3", markdown)
            self.assertIn("## social_score", markdown)
            self.assertIn("guarded follow-up probability", markdown)
            self.assertIn("deterministic first-reply text constraints", markdown)
            self.assertIn("live LLM semantic audit", markdown)
            self.assertIn("mixed affect remap suppressed", social_markdown)


if __name__ == "__main__":
    unittest.main()
