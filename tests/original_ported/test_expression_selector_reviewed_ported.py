import asyncio
import unittest
from types import SimpleNamespace

from astrmai.conversation.planning.expression_policy import ExpressionSelector


class _FakePattern:
    def __init__(self, situation, expression):
        self.situation = situation
        self.expression = expression


class _FakeDB:
    def __init__(self):
        self.calls = []
        self.patterns = [_FakePattern("praise", "solid indeed"), _FakePattern("small talk", "lol")]

    def get_patterns(self, group_id, limit=10, only_checked=False, include_rejected=False, shared_scope=None, think_level=None, review_status=None):
        self.calls.append({
            "group_id": group_id,
            "limit": limit,
            "only_checked": only_checked,
            "include_rejected": include_rejected,
            "shared_scope": shared_scope,
            "think_level": think_level,
            "review_status": review_status,
        })
        return list(self.patterns)


class _FakeGateway:
    def __init__(self):
        self.config = SimpleNamespace()

    async def call_data_process_task(self, *args, **kwargs):
        return {"indexes": [0, 1]}


class ExpressionSelectorReviewedPortedTests(unittest.TestCase):
    def test_selector_passes_review_filters_and_scope(self):
        selector = ExpressionSelector(_FakeDB(), _FakeGateway())

        async def _run():
            return await selector.select(
                chat_id="group-1",
                context_text="someone just praised a travel guide",
                think_level=1,
                shared_scope="group-1",
            )

        result = asyncio.run(_run())

        self.assertIn("solid indeed", result)
        self.assertTrue(selector.db.calls)
        first_call = selector.db.calls[0]
        self.assertEqual(first_call["shared_scope"], "group-1")
        self.assertEqual(first_call["review_status"], "approved")
        self.assertTrue(first_call["only_checked"])

    def test_selector_cools_down_recent_patterns_and_filters_short_repeats(self):
        db = _FakeDB()
        db.patterns = [
            _FakePattern(f"situation-{index}", f"phrase-{index}")
            for index in range(7)
        ]
        db.patterns[6] = _FakePattern("catchphrase", "咻——！")
        selector = ExpressionSelector(db, _FakeGateway())

        async def _run():
            first = await selector.select(
                chat_id="group-1",
                context_text="normal chat",
                think_level=0,
                shared_scope="group-1",
            )
            second = await selector.select(
                chat_id="group-1",
                context_text="normal chat",
                think_level=0,
                shared_scope="group-1",
            )
            third = await selector.select(
                chat_id="group-1",
                context_text="刚刚已经说过咻——！",
                think_level=0,
                shared_scope="group-2",
            )
            return first, second, third

        first, second, third = asyncio.run(_run())

        self.assertIn("phrase-0", first)
        self.assertNotIn("phrase-0", second)
        self.assertIn("phrase-5", second)
        self.assertNotIn("咻——！", third)


if __name__ == "__main__":
    unittest.main()
