import asyncio
import tempfile
import unittest

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class TopicSummarizerGapCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_segment_by_silence_sorts_mixed_timestamps_and_splits_on_gap(self):
        from astrmai.memory.services.topic_summarizer import TopicSummarizer

        summarizer = TopicSummarizer()
        messages = [
            {"sender": "u2", "content": "第二段继续", "timestamp": "500"},
            {"sender": "u1", "content": "第一段 A", "timestamp": "100"},
            {"sender": "u1", "content": "第一段 B", "timestamp": 120},
            {"sender": "u2", "content": "第一段 C", "timestamp": "130"},
            {"sender": "u3", "content": "第二段开始", "timestamp": 490},
            {"sender": "u3", "content": "第二段结束", "timestamp": "510"},
        ]

        segments = summarizer._segment_by_silence(messages)

        self.assertEqual(len(segments), 2)
        self.assertEqual([m["content"] for m in segments[0].messages], ["第一段 A", "第一段 B", "第一段 C"])
        self.assertEqual(segments[0].start_time, 100.0)
        self.assertEqual(segments[0].end_time, 130.0)
        self.assertEqual([m["content"] for m in segments[1].messages], ["第二段开始", "第二段继续", "第二段结束"])

    def test_segment_by_silence_splits_after_topic_shift_checkpoint(self):
        from astrmai.memory.services.topic_summarizer import TopicSummarizer

        summarizer = TopicSummarizer()
        messages = [
            {"sender": "u1", "content": f"火锅 聚餐 辣锅 第{i}", "timestamp": i}
            for i in range(10)
        ]
        messages.extend(
            [
                {"sender": "u2", "content": "量子 编译器 内存 模型", "timestamp": 10},
                {"sender": "u2", "content": "量子 编译器 调度", "timestamp": 11},
                {"sender": "u3", "content": "量子 算法 优化", "timestamp": 12},
            ]
        )

        self.assertTrue(summarizer._detect_topic_shift(messages[:10], messages[10]))
        segments = summarizer._segment_by_silence(messages)

        self.assertEqual(len(segments), 2)
        self.assertEqual(len(segments[0].messages), 10)
        self.assertEqual([m["content"] for m in segments[1].messages], [
            "量子 编译器 内存 模型",
            "量子 编译器 调度",
            "量子 算法 优化",
        ])

    def test_batch_summarize_uses_keyword_fallback_without_gateway(self):
        async def _run():
            from astrmai.memory.services.topic_summarizer import TopicSegment, TopicSummarizer

            summarizer = TopicSummarizer()
            segment = TopicSegment(
                messages=[{"sender": "u1", "content": "火锅很好吃", "timestamp": 1}],
                keywords=["火锅", "聚餐", "辣锅"],
                message_count=1,
            )

            result = await summarizer._batch_summarize([segment], session_id="chat-1")

            self.assertEqual(result, ["讨论了火锅、聚餐、辣锅"])

        asyncio.run(_run())

    def test_batch_summarize_calls_gateway_and_parses_json_response(self):
        async def _run():
            from astrmai.memory.services.topic_summarizer import TopicSegment, TopicSummarizer

            class _Gateway:
                def __init__(self):
                    self.calls = []

                async def call_data_process_task(self, **kwargs):
                    self.calls.append(kwargs)
                    return '["火锅聚餐很热闹", "讨论编译器优化"]'

            gateway = _Gateway()
            summarizer = TopicSummarizer(gateway=gateway)
            segments = [
                TopicSegment(
                    messages=[{"sender": "u1", "content": "火锅 聚餐", "timestamp": 1}],
                    keywords=["火锅"],
                    message_count=1,
                ),
                TopicSegment(
                    messages=[{"sender": "u2", "content": "编译器 优化", "timestamp": 2}],
                    keywords=["编译器"],
                    message_count=1,
                ),
            ]

            result = await summarizer._batch_summarize(segments, session_id="chat-7")

            self.assertEqual(result, ["火锅聚餐很热闹", "讨论编译器优化"])
            self.assertEqual(gateway.calls[0]["lane_key"].scope_id, "chat-7")
            self.assertTrue(gateway.calls[0]["is_json"])

        asyncio.run(_run())

    def test_batch_summarize_falls_back_when_gateway_raises(self):
        async def _run():
            from astrmai.memory.services.topic_summarizer import TopicSegment, TopicSummarizer

            class _Gateway:
                async def call_data_process_task(self, **_kwargs):
                    raise RuntimeError("llm unavailable")

            summarizer = TopicSummarizer(gateway=_Gateway())
            segment = TopicSegment(
                messages=[{"sender": "u1", "content": "日常聊天", "timestamp": 1}],
                keywords=[],
                message_count=1,
            )

            result = await summarizer._batch_summarize([segment], session_id="chat-1")

            self.assertEqual(result, ["讨论了日常话题"])

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
