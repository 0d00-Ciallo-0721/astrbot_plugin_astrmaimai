import asyncio
import unittest
from types import SimpleNamespace

from astrmai.learning.review.expression_auto_check_task import ExpressionAutoCheckTask
from astrmai.learning.review.reflect_tracker import ReflectTracker


class FakeGateway:
    def __init__(self, result):
        self.result = result
        self.config = SimpleNamespace(global_settings=SimpleNamespace(admin_ids=["admin-1"]))

    async def call_data_process_task(self, *args, **kwargs):
        return self.result


class FakeDB:
    def __init__(self, patterns=None):
        self.patterns = patterns or []
        self.updated = []

    async def list_reviewable_patterns_async(self, group_id=None, limit=20):
        return self.patterns[:limit]

    async def update_pattern_review_async(self, pattern_id, **kwargs):
        self.updated.append((pattern_id, kwargs))
        for pattern in self.patterns:
            if pattern.id == pattern_id:
                for key, value in kwargs.items():
                    if value is not None and hasattr(pattern, key):
                        setattr(pattern, key, value)
                if kwargs.get("replacement_expression") and kwargs.get("apply_replacement"):
                    pattern.expression = kwargs["replacement_expression"]
                return pattern
        return None


class FakeEvent:
    def __init__(self, text, group_id="group-1", sender_id="admin-1"):
        self.message_str = text
        self.unified_msg_origin = group_id
        self._sender_id = sender_id

    def get_sender_id(self):
        return self._sender_id


class ExpressionGovernancePortedTests(unittest.IsolatedAsyncioTestCase):
    async def test_auto_check_marks_pattern_approved(self):
        pattern = SimpleNamespace(id=1, group_id="group-1", situation="praise", expression="indeed", count=3, style="", content_list="[]")
        db = FakeDB([pattern])
        tracker = ReflectTracker(db, FakeGateway({"decision": "approved"}))
        task = ExpressionAutoCheckTask(
            db_service=db,
            gateway=FakeGateway({"decision": "approved", "weight_delta": 0.1}),
            tracker=tracker,
            config=SimpleNamespace(evolution=SimpleNamespace(review_batch_size=10, review_min_count=2)),
        )

        processed = await task.run_once("group-1")
        self.assertEqual(processed, 1)
        self.assertEqual(db.updated[0][0], 1)
        self.assertEqual(db.updated[0][1]["review_status"], "approved")

    async def test_auto_check_revision_only_records_suggestion(self):
        pattern = SimpleNamespace(id=3, group_id="group-1", situation="comfort", expression="do not be sad", count=3, style="", content_list="[]")
        db = FakeDB([pattern])
        tracker = ReflectTracker(db, FakeGateway({"decision": "approved"}))
        task = ExpressionAutoCheckTask(
            db_service=db,
            gateway=FakeGateway({"decision": "revision_needed", "reason": "too templated", "replacement_expression": "I am here with you"}),
            tracker=tracker,
            config=SimpleNamespace(evolution=SimpleNamespace(review_batch_size=10, review_min_count=2)),
        )

        await task.run_once("group-1")

        self.assertEqual(pattern.expression, "do not be sad")
        self.assertEqual(db.updated[0][1]["review_status"], "pending_human")
        self.assertEqual(db.updated[0][1]["review_suggestion"], "I am here with you")

    async def test_tracker_consumes_admin_feedback(self):
        pattern = SimpleNamespace(id=2, group_id="group-1", situation="greeting", expression="here I am")
        db = FakeDB([pattern])
        tracker = ReflectTracker(db, FakeGateway({"decision": "approved"}), config=SimpleNamespace(global_settings=SimpleNamespace(admin_ids=["admin-1"])))
        tracker.queue_review_request(pattern, reason="needs human confirmation")

        message = await tracker.try_consume_feedback(FakeEvent("expression review #2 approve"))
        self.assertIn("#2", message)
        self.assertEqual(db.updated[0][1]["review_status"], "approved")


if __name__ == "__main__":
    unittest.main()
