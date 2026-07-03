import asyncio
import unittest
from types import SimpleNamespace


class WebUiGapCoverageTests(unittest.TestCase):
    def test_admin_api_preserves_canonical_review_ids(self):
        from astrmai.webui.plugin_pages import AstrMaiAdminPageApi

        calls = []

        class _Reviews:
            async def submit_review(self, review_id, action, replacement, weight, reason):
                calls.append(("submit", review_id))
                return {"status": "ok"}

            async def update_review_record(self, review_id, body):
                calls.append(("update", review_id))
                return {"status": "ok"}

            async def delete_review_record(self, review_id):
                calls.append(("delete", review_id))
                return {"status": "ok"}

        api = AstrMaiAdminPageApi(SimpleNamespace(runtime=None))
        api._reviews = lambda: _Reviews()
        request = SimpleNamespace(
            path_params={"id": "mem-review-1"},
            query_params={},
            json=lambda: {"action": "approve"},
        )

        async def _run():
            await api.submit_review(request)
            await api.update_review(request)
            await api.delete_review(request)

        asyncio.run(_run())

        self.assertEqual(
            calls,
            [
                ("submit", "mem-review-1"),
                ("update", "mem-review-1"),
                ("delete", "mem-review-1"),
            ],
        )

    def test_admin_api_preserves_batch_review_ids(self):
        from astrmai.webui.plugin_pages import AstrMaiAdminPageApi

        calls = []

        class _Reviews:
            async def batch_review(self, ids, action):
                calls.append((ids, action))
                return {"status": "ok"}

        api = AstrMaiAdminPageApi(SimpleNamespace(runtime=None))
        api._reviews = lambda: _Reviews()
        request = SimpleNamespace(
            path_params={},
            query_params={},
            json=lambda: {
                "ids": ["mem-review-1", 2, "", None],
                "action": "approve",
            },
        )

        asyncio.run(api.batch_review(request))

        self.assertEqual(calls, [(["mem-review-1", "2"], "approve")])

    def test_admin_api_treats_string_migration_source_as_one_source(self):
        from astrmai.webui.plugin_pages import AstrMaiAdminPageApi

        calls = []

        class _Memory:
            async def migration_dry_run(self, sources):
                calls.append(sources)
                return {"status": "ok"}

        api = AstrMaiAdminPageApi(SimpleNamespace(runtime=None))
        api._memory = lambda: _Memory()
        request = SimpleNamespace(
            path_params={},
            query_params={},
            json=lambda: {"import_sources": "legacy_events"},
        )

        asyncio.run(api.memory_migration_dry_run(request))

        self.assertEqual(calls, [["legacy_events"]])

    def test_review_item_preserves_zero_weight(self):
        from astrmai.webui.backend.services.review_ui_service import ReviewUiService

        item = ReviewUiService._canonical_to_review_item(
            {
                "id": "mem-review-1",
                "session_id": "chat-1",
                "content": "quiet reply",
                "status": "review_pending",
                "metadata": {"review_status": "pending", "weight": 0.0},
            }
        )

        self.assertEqual(item["weight"], 0.0)

    def test_review_list_clamps_invalid_pagination(self):
        from astrmai.webui.backend.services.review_ui_service import ReviewUiService

        class _PluginApi:
            def get_expression_pattern_service(self):
                return object()

            async def list_recent_reviews(self, group_id="", limit=20):
                return [
                    {
                        "id": "review-1",
                        "review_status": "pending",
                        "situation": "chat",
                        "expression": "hello",
                    }
                ]

        service = ReviewUiService(_PluginApi(), db_factory=None)

        result = asyncio.run(
            service.list_reviews(
                page=0,
                page_size=-5,
            )
        )

        self.assertEqual(result["page"], 1)
        self.assertEqual(result["page_size"], 1)
        self.assertEqual([item["id"] for item in result["items"]], ["review-1"])

    def test_memory_list_clamps_runtime_pagination(self):
        from astrmai.webui.backend.services.memory_ui_service import MemoryUiService

        calls = []

        class _Store:
            async def list_canonical(self, **kwargs):
                calls.append(kwargs)
                return {"items": [], "total": 0}

        class _PluginApi:
            def get_memory_engine(self):
                return object()

            def get_v2_store(self):
                return _Store()

        service = MemoryUiService(db_factory=None, plugin_api=_PluginApi())

        result = asyncio.run(service.list_canonical(limit=-8, offset=-4))

        self.assertEqual(result["status"], "ok")
        self.assertEqual(calls[0]["limit"], 1)
        self.assertEqual(calls[0]["offset"], 0)

    def test_memory_event_preserves_zero_importance(self):
        from astrmai.webui.backend.services.memory_ui_service import MemoryUiService

        writes = []

        class _Writer:
            async def write(self, request):
                writes.append(request)
                return "mem-event-1"

        class _PluginApi:
            def get_memory_engine(self):
                return object()

            def get_write_service(self):
                return _Writer()

        service = MemoryUiService(db_factory=None, plugin_api=_PluginApi())

        result = asyncio.run(
            service.create_event(
                {
                    "narrative": "neutral event",
                    "importance": 0.0,
                }
            )
        )

        self.assertEqual(result["id"], "mem-event-1")
        self.assertEqual(writes[0].importance, 0.0)


if __name__ == "__main__":
    unittest.main()
