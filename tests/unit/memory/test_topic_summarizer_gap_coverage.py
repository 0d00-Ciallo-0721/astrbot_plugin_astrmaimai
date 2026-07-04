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

    def test_process_history_builds_structured_topic_result(self):
        from astrmai.memory.services.topic_summarizer import TopicSummarizer

        summarizer = TopicSummarizer()
        captured = []

        async def _summarize(segments, session_id=""):
            captured.append((segments, session_id))
            return ["Deployment readiness discussion"]

        summarizer._batch_summarize = _summarize
        messages = [
            {
                "sender": "alice" if index % 2 == 0 else "bob",
                "content": f"nice deployment readiness update {index}",
                "timestamp": index * 10,
            }
            for index in range(12)
        ]

        result = asyncio.run(summarizer.process_history(messages, session_id="chat-1"))

        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0][1], "chat-1")
        self.assertEqual(len(captured[0][0]), 1)
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["summary"], "Deployment readiness discussion")
        self.assertEqual(result[0]["sentiment"], "positive")
        self.assertEqual(result[0]["message_count"], 12)
        self.assertEqual(result[0]["duration_minutes"], 110 / 60)
        self.assertCountEqual(result[0]["participants"], ["alice", "bob"])
        self.assertLessEqual(len(result[0]["topic_keywords"]), 5)

    def test_calculate_sentiment_handles_positive_negative_and_neutral_text(self):
        from astrmai.memory.services.topic_summarizer import TopicSegment, TopicSummarizer

        summarizer = TopicSummarizer()

        positive = summarizer._calculate_sentiment(
            TopicSegment(messages=[{"content": "nice work"}, {"content": "still nice"}])
        )
        negative = summarizer._calculate_sentiment(
            TopicSegment(messages=[{"content": "sb response"}, {"content": "plain"}])
        )
        neutral = summarizer._calculate_sentiment(
            TopicSegment(messages=[{"content": "deployment status update"}])
        )

        self.assertEqual(positive, 1.0)
        self.assertEqual(negative, -1.0)
        self.assertEqual(neutral, 0.0)

    def test_calculate_importance_uses_all_weighted_factors(self):
        from astrmai.memory.services.topic_summarizer import TopicSegment, TopicSummarizer

        segment = TopicSegment(
            participants=["a", "b", "c", "d", "e"],
            start_time=0,
            end_time=900,
            message_count=45,
            sentiment_score=-0.5,
        )

        self.assertEqual(TopicSummarizer()._calculate_importance(segment), 0.9)

    def test_extract_keywords_handles_mixed_chinese_and_english_text(self):
        from astrmai.memory.services.topic_summarizer import TopicSegment, TopicSummarizer

        segment = TopicSegment(
            messages=[
                {"content": "记忆系统 deploy deploy"},
                {"content": "记忆系统 memory"},
            ]
        )

        keywords = TopicSummarizer()._extract_keywords(segment)

        self.assertIn("记忆系统", keywords)
        self.assertIn("de", keywords)
        self.assertNotIn("deploy", keywords)

    def test_sentiment_to_label_covers_boundary_values(self):
        from astrmai.memory.services.topic_summarizer import TopicSummarizer

        labels = [
            TopicSummarizer._sentiment_to_label(score)
            for score in (-1.0, -0.31, 0.0, 0.29, 1.0)
        ]

        self.assertEqual(labels, ["negative", "negative", "neutral", "neutral", "positive"])

    def test_tokenize_handles_cjk_ascii_and_mixed_text(self):
        from astrmai.memory.services.topic_summarizer import TopicSummarizer

        self.assertEqual(TopicSummarizer._tokenize("记忆系统"), ["记忆系统"])
        self.assertEqual(TopicSummarizer._tokenize("deploy"), ["de", "ep", "pl", "lo", "oy"])
        self.assertEqual(
            TopicSummarizer._tokenize("记忆 AI memory"),
            ["记忆", "AI", "me", "em", "mo", "or", "ry"],
        )


if __name__ == "__main__":
    unittest.main()
