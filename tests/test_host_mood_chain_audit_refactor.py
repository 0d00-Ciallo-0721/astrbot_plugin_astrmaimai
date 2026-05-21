import unittest
from types import SimpleNamespace


class _StubGateway:
    def __init__(self):
        self.task_models = ["stub-model"]
        self.lane_manager = object()
        self.config = SimpleNamespace(
            reply=SimpleNamespace(emotion_mapping=[]),
            provider=SimpleNamespace(task_models=["stub-model"]),
        )

    async def chat_in_lane_result(self, **kwargs):
        prompt = str(kwargs.get("prompt", "") or "")
        if any(token in prompt for token in ("真烦", "讨厌", "烦死了", "搞砸")):
            payload = {"mood_tag": "angry", "mood_value": -0.7}
        elif any(token in prompt for token in ("难过", "受伤", "失望", "不舒服")):
            payload = {"mood_tag": "sad", "mood_value": -0.45}
        elif any(token in prompt for token in ("靠谱", "开心", "贴贴")):
            payload = {"mood_tag": "happy", "mood_value": 0.52}
        else:
            payload = {"mood_tag": "neutral", "mood_value": 0.0}
        return SimpleNamespace(parsed_json=payload, raw_completion="")


class HostMoodChainAuditRefactorTests(unittest.TestCase):
    def test_host_message_entry_matches_direct_mood_result(self):
        from tests.helpers.host_mood_chain_audit import build_host_mood_chain_baseline

        payload = build_host_mood_chain_baseline(gateway_factory=lambda: _StubGateway())
        self.assertEqual(payload["status"], "passed")
        self.assertTrue(payload["all_matched"])
        for case in payload["cases"]:
            self.assertEqual(case["host_source"], "attention_ingress")
            self.assertTrue(case["matched"])
            self.assertEqual(case["host_results"], [{"type": "plain", "text": "(ghost)"}])

    def test_host_reply_post_send_matches_expected_mood_and_social_score(self):
        from tests.helpers.host_mood_chain_audit import build_host_reply_post_send_baseline

        payload = build_host_reply_post_send_baseline(gateway_factory=lambda: _StubGateway())
        self.assertEqual(payload["status"], "passed")
        self.assertTrue(payload["all_matched"])
        self.assertTrue(payload["publish_change_semantics_aligned"])
        for case in payload["cases"]:
            self.assertTrue(case["mood_matched"])
            self.assertTrue(case["social_score_matched"])
            self.assertEqual(case["social_score_amplitude_issue"], "")
            self.assertEqual(case["host_results"], [{"type": "plain", "text": "(ghost)"}])
        mixed_case = next(item for item in payload["cases"] if item["case_id"] == "mixed_short")
        self.assertTrue(mixed_case["mood_tag_remap_suppressed"])
        self.assertEqual(mixed_case["effective_event_type"], "normal_chat")
        self.assertEqual(mixed_case["published_mood_tag"], "")
        self.assertEqual(mixed_case["published_event_type"], "normal_chat")
        self.assertLess(mixed_case["actual_social_score"], 1.0)
        comfort_case = next(item for item in payload["cases"] if item["case_id"] == "comfort_complaint_short")
        self.assertEqual(comfort_case["effective_event_type"], "normal_chat")
        self.assertLessEqual(comfort_case["actual_social_score"], 0.4)
        ambiguous_case = next(item for item in payload["cases"] if item["case_id"] == "ambiguous_soft_affection_short")
        self.assertEqual(ambiguous_case["effective_event_type"], "greeting")
        self.assertLessEqual(ambiguous_case["actual_social_score"], 0.24)
        cold_case = next(item for item in payload["cases"] if item["case_id"] == "cold_distance_short")
        self.assertEqual(cold_case["effective_event_type"], "ignore")
        self.assertLess(cold_case["actual_social_score"], -0.2)
        self.assertGreaterEqual(cold_case["actual_social_score"], -0.30)
        perfunctory_case = next(item for item in payload["cases"] if item["case_id"] == "perfunctory_brief_short")
        self.assertEqual(perfunctory_case["effective_event_type"], "ignore")
        self.assertLessEqual(perfunctory_case["actual_social_score"], -0.30)
        irritation_case = next(item for item in payload["cases"] if item["case_id"] == "mild_irritation_short")
        self.assertEqual(irritation_case["effective_event_type"], "ignore")
        self.assertLessEqual(irritation_case["actual_social_score"], -0.40)
        mixed_case = next(item for item in payload["cases"] if item["case_id"] == "mixed_short")
        tool_case = next(item for item in payload["cases"] if item["case_id"] == "tool_intent_short")
        self.assertGreater(tool_case["actual_social_score"], mixed_case["actual_social_score"])
        self.assertGreater(mixed_case["actual_social_score"], ambiguous_case["actual_social_score"])
        self.assertGreater(cold_case["actual_social_score"], perfunctory_case["actual_social_score"])
        self.assertGreater(perfunctory_case["actual_social_score"], irritation_case["actual_social_score"])
