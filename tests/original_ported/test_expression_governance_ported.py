import asyncio
import unittest
from types import SimpleNamespace

from config import AstrMaiConfig
from astrmai.learning.review.expression_auto_check_task import ExpressionAutoCheckTask
from astrmai.learning.review.expression_governance_runner import ExpressionGovernanceRunner
from astrmai.learning.review.jargon_auto_check_task import JargonAutoCheckTask
from astrmai.learning.review.reflect_tracker import ReflectTracker
from astrmai.proactive.review_dispatcher import ReviewDispatcher


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


class FakeStore:
    def __init__(self, rows=None):
        self.rows = rows or []
        self.updated = []

    async def list_candidates(self, session_id="", kinds=None, statuses=None, limit=20, include_inactive=False):
        filtered = []
        for row in self.rows:
            if session_id and session_id not in {"GLOBAL", row.session_id or "GLOBAL"}:
                continue
            if kinds and row.kind not in kinds:
                continue
            if statuses and row.status not in statuses:
                continue
            filtered.append(row)
        return filtered[:limit]

    async def update_memory(self, memory_id, **kwargs):
        self.updated.append((memory_id, kwargs))
        for row in self.rows:
            if row.id != memory_id:
                continue
            if "summary" in kwargs:
                row.summary = kwargs["summary"]
            if "status" in kwargs:
                row.status = kwargs["status"]
            if "visibility" in kwargs:
                row.visibility = kwargs["visibility"]
            if "metadata" in kwargs:
                row.metadata = kwargs["metadata"]
            return 1
        return 0


class FakeProjector:
    def __init__(self):
        self.projected = []
        self.cleaned = []

    async def project(self, memory_id):
        self.projected.append(memory_id)

    async def cleanup_deleted(self, memory_ids):
        self.cleaned.extend(memory_ids)
        return len(memory_ids)


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

    async def test_tracker_matches_full_umo_for_bare_group_pending_request(self):
        pattern = SimpleNamespace(id=22, group_id="123456", situation="greeting", expression="here I am")
        db = FakeDB([pattern])
        tracker = ReflectTracker(db, FakeGateway({"decision": "approved"}), config=SimpleNamespace(global_settings=SimpleNamespace(admin_ids=["admin-1"])))
        tracker.queue_review_request(pattern, reason="needs human confirmation")

        message = await tracker.try_consume_feedback(FakeEvent("expression review #22 approve", group_id="default:GroupMessage:123456"))

        self.assertIn("#22", message)
        self.assertEqual(db.updated[0][1]["review_status"], "approved")

    async def test_review_dispatcher_normalizes_bare_group_id_and_marks_sent_after_success(self):
        class _Context:
            def __init__(self):
                self.sent = []

            async def send_message(self, umo, chain):
                self.sent.append((umo, chain))

        pattern = SimpleNamespace(id=2, group_id="123456", situation="greeting", expression="here I am")
        tracker = ReflectTracker(FakeDB([pattern]), FakeGateway({"decision": "approved"}))
        tracker.queue_review_request(pattern, reason="needs human confirmation")
        context = _Context()

        await ReviewDispatcher(context, tracker).dispatch_pending()

        self.assertEqual(context.sent[0][0], "default:GroupMessage:123456")
        self.assertEqual(await tracker.get_unsent_requests(), [])

    async def test_review_dispatcher_keeps_pending_request_when_send_fails(self):
        class _Context:
            async def send_message(self, umo, chain):
                raise RuntimeError("send failed")

        pattern = SimpleNamespace(id=3, group_id="123456", situation="greeting", expression="here I am")
        tracker = ReflectTracker(FakeDB([pattern]), FakeGateway({"decision": "approved"}))
        tracker.queue_review_request(pattern, reason="needs human confirmation")

        await ReviewDispatcher(_Context(), tracker).dispatch_pending()

        requests = await tracker.get_unsent_requests()
        self.assertEqual([item["pattern_id"] for item in requests], ["3"])

    async def test_governance_runner_uses_canonical_backlog_not_active_states(self):
        calls = []

        class _Reflector:
            async def reflect_batch(self, chat_id):
                calls.append(("reflect", chat_id))

            async def auto_audit(self, chat_id):
                calls.append(("audit", chat_id))

            async def pending_scope_ids(self):
                return ["chat-pending"]

        class _AutoCheck:
            async def run_once(self, chat_id):
                calls.append(("check", chat_id))

        class _PatternService:
            async def list_governance_groups(self):
                return ["chat-review", "chat-pending"]

        class _Dispatcher:
            async def dispatch_pending(self):
                calls.append(("dispatch", None))

        runner = ExpressionGovernanceRunner(
            state_engine=SimpleNamespace(get_active_states=lambda: []),
            pattern_service=_PatternService(),
            reflector=_Reflector(),
            auto_check_task=_AutoCheck(),
            review_dispatcher=_Dispatcher(),
        )
        await runner.run_once()
        self.assertIn(("reflect", "chat-review"), calls)
        self.assertIn(("audit", "chat-review"), calls)
        self.assertIn(("check", "chat-review"), calls)
        self.assertIn(("reflect", "chat-pending"), calls)
        self.assertIn(("dispatch", None), calls)

    async def test_jargon_auto_check_promotes_or_rejects_candidates(self):
        projector = FakeProjector()
        store = FakeStore(
            [
                SimpleNamespace(
                    id="mem-jargon-1",
                    kind="jargon",
                    session_id="group-1",
                    content="bigbird",
                    summary="raid boss nickname",
                    confidence=0.7,
                    status="review_pending",
                    visibility="maintenance_only",
                    metadata={"meaning": "raid boss nickname", "count": 3, "review_status": "review_pending", "examples": ["bigbird is here"]},
                )
            ]
        )
        db = SimpleNamespace(memory_engine=SimpleNamespace(v2_store=store, index_projector=projector))
        task = JargonAutoCheckTask(
            db_service=db,
            gateway=FakeGateway({"decision": "approved", "reason": "group-specific and stable", "meaning": "raid boss nickname"}),
            config=AstrMaiConfig(
                evolution={
                    "review_batch_size": 10,
                    "review_runner_min_interval_sec": 15,
                    "jargon_min_count": 2,
                    "review_min_count": 2,
                }
            ),
        )

        processed = await task.run_once("group-1")

        self.assertEqual(processed, 1)
        self.assertEqual(store.updated[0][0], "mem-jargon-1")
        self.assertEqual(store.updated[0][1]["status"], "active")
        self.assertEqual(store.updated[0][1]["metadata"]["review_status"], "approved")
        self.assertEqual(projector.projected, ["mem-jargon-1"])

    async def test_jargon_auto_check_uses_configured_jargon_threshold_over_review_min_count(self):
        store = FakeStore(
            [
                SimpleNamespace(
                    id="mem-jargon-2",
                    kind="jargon",
                    session_id="group-1",
                    content="bigbird",
                    summary="raid boss nickname",
                    confidence=0.7,
                    status="review_pending",
                    visibility="maintenance_only",
                    metadata={"meaning": "raid boss nickname", "count": 2, "review_status": "review_pending", "examples": ["bigbird is here"]},
                )
            ]
        )
        db = SimpleNamespace(memory_engine=SimpleNamespace(v2_store=store, index_projector=FakeProjector()))
        task = JargonAutoCheckTask(
            db_service=db,
            gateway=FakeGateway({"decision": "approved"}),
            config=AstrMaiConfig(
                evolution={
                    "review_batch_size": 10,
                    "review_runner_min_interval_sec": 15,
                    "review_min_count": 1,
                    "jargon_min_count": 3,
                }
            ),
        )

        processed = await task.run_once("group-1")

        self.assertEqual(processed, 0)
        self.assertEqual(store.updated, [])

    async def test_governance_runner_processes_jargon_backlog_without_active_chat(self):
        calls = []

        class _JargonCheck:
            async def list_governance_groups(self):
                return ["group-jargon"]

            async def run_once(self, chat_id):
                calls.append(("jargon", chat_id))

        runner = ExpressionGovernanceRunner(
            state_engine=SimpleNamespace(get_active_states=lambda: []),
            pattern_service=None,
            reflector=None,
            auto_check_task=None,
            jargon_auto_check_task=_JargonCheck(),
            review_dispatcher=None,
        )
        await runner.run_once()
        self.assertEqual(calls, [("jargon", "group-jargon")])


if __name__ == "__main__":
    unittest.main()
