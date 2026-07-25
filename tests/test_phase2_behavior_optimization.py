import asyncio
import unittest
from types import SimpleNamespace


class _PolicyEvent:
    def __init__(self, text):
        self.message_str = text
        self._extras = {}

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value


class QueryRewriteFastFallbackTests(unittest.TestCase):
    def _service(self, gateway):
        from astrmai.memory.services.memory_retrieval_service import MemoryRetrievalService

        config = SimpleNamespace(timing=SimpleNamespace(query_rewrite_timeout_sec=3.5))
        engine = SimpleNamespace(gateway=gateway, config=config)
        return MemoryRetrievalService(store=object(), engine=engine)

    def test_query_rewrite_uses_one_model_without_retry_and_keeps_original(self):
        from astrmai.memory.contracts.memory_query import MemoryQuery

        class Gateway:
            def __init__(self):
                self.kwargs = None

            async def call_data_process_task(self, **kwargs):
                self.kwargs = kwargs
                return {"queries": ["喜欢的食物", "食物偏好", "喜欢的食物"]}

        gateway = Gateway()
        service = self._service(gateway)
        query = MemoryQuery(query="我喜欢吃什么")

        result = asyncio.run(service._rewrite_queries(query))

        self.assertEqual(result, ["我喜欢吃什么", "喜欢的食物", "食物偏好"])
        self.assertEqual(gateway.kwargs["timeout_override"], 3.5)
        self.assertEqual(gateway.kwargs["max_retries_override"], 0)
        self.assertEqual(gateway.kwargs["max_models_override"], 1)
        self.assertFalse(gateway.kwargs["use_fallback"])
        self.assertEqual(query.metadata["query_rewrite_trace"]["status"], "success")
        self.assertFalse(query.metadata["query_rewrite_trace"]["original_query_fallback"])

    def test_query_rewrite_timeout_immediately_falls_back_to_original(self):
        from astrmai.memory.contracts.memory_query import MemoryQuery

        class Gateway:
            async def call_data_process_task(self, **_kwargs):
                raise asyncio.TimeoutError("rewrite timed out")

        service = self._service(Gateway())
        query = MemoryQuery(query="还记得那个吗")

        result = asyncio.run(service._rewrite_queries(query))

        self.assertEqual(result, ["还记得那个吗"])
        self.assertEqual(query.metadata["query_rewrite_trace"]["status"], "timeout")
        self.assertTrue(query.metadata["query_rewrite_trace"]["original_query_fallback"])
        self.assertEqual(query.metadata["query_rewrite_trace"]["rewrite_count"], 0)

    def test_query_rewrite_hard_timeout_does_not_wait_for_cancel_cleanup(self):
        from astrmai.memory.contracts.memory_query import MemoryQuery

        class Gateway:
            async def call_data_process_task(self, **_kwargs):
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    await asyncio.sleep(0.3)

        service = self._service(Gateway())
        service.engine.config.timing.query_rewrite_timeout_sec = 0.05
        query = MemoryQuery(query="还记得那个吗")

        async def run():
            started = asyncio.get_running_loop().time()
            result = await service._rewrite_queries(query)
            return result, asyncio.get_running_loop().time() - started

        result, elapsed = asyncio.run(run())

        self.assertEqual(result, ["还记得那个吗"])
        self.assertLess(elapsed, 0.5)
        self.assertEqual(query.metadata["query_rewrite_trace"]["status"], "timeout")
        self.assertTrue(query.metadata["query_rewrite_trace"]["cancellation_requested"])


class ReplyShapePolicyTests(unittest.TestCase):
    def _config(self, **overrides):
        values = {
            "humanlike_short_reply_enabled": True,
            "short_reply_max_chars": 80,
            "short_reply_max_sentences": 2,
            "short_reply_allow_followup_question": False,
        }
        values.update(overrides)
        return SimpleNamespace(**values)

    def test_known_low_information_utterance_uses_micro_policy(self):
        from astrmai.conversation.reply_shape_policy import resolve_reply_shape_policy

        event = _PolicyEvent("哼哼哼")

        policy = resolve_reply_shape_policy(event, event.message_str, self._config())

        self.assertEqual(policy["mode"], "micro")
        self.assertEqual(policy["max_sentences"], 2)
        self.assertFalse(policy["allow_followup_question"])

    def test_question_image_and_private_batch_do_not_use_micro_policy(self):
        from astrmai.conversation.reply_shape_policy import resolve_reply_shape_policy

        question = _PolicyEvent("我叫什么")
        image = _PolicyEvent("嗯")
        image.set_extra("extracted_image_refs", ["image.jpg"])
        batch = _PolicyEvent("好")
        batch.set_extra("astrmai_private_batch_message_count", 3)

        self.assertEqual(resolve_reply_shape_policy(question, question.message_str, self._config())["mode"], "default")
        self.assertEqual(resolve_reply_shape_policy(image, image.message_str, self._config())["reason"], "non_chat_payload")
        self.assertEqual(resolve_reply_shape_policy(batch, batch.message_str, self._config())["reason"], "non_chat_payload")

    def test_disabled_policy_keeps_legacy_mode(self):
        from astrmai.conversation.reply_shape_policy import resolve_reply_shape_policy

        event = _PolicyEvent("行")

        policy = resolve_reply_shape_policy(
            event,
            event.message_str,
            self._config(humanlike_short_reply_enabled=False),
        )

        self.assertEqual(policy["mode"], "default")
        self.assertEqual(policy["reason"], "disabled")


if __name__ == "__main__":
    unittest.main()
