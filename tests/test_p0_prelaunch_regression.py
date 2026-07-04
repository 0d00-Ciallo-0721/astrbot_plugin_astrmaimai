import asyncio
import base64
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class P0PrelaunchRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_embedding_auto_fallback_handles_provider_without_meta(self):
        from astrmai.memory.retrieval.embedding import EmbeddingClient

        class _Provider:
            meta = None

            async def get_embedding(self, text):
                return [1.0, 2.0]

        class _Context:
            def get_all_embedding_providers(self):
                return [_Provider()]

        self.assertEqual(asyncio.run(EmbeddingClient(_Context()).get_vector("hello")), [1.0, 2.0])

    def test_vector_store_skips_malformed_doc_data(self):
        from astrmai.memory.retrieval.vector_store import VectorRetriever

        class _Faiss:
            async def retrieve(self, **kwargs):
                return [
                    SimpleNamespace(data={"text": "missing id"}, similarity=0.9),
                    SimpleNamespace(data={"id": 7, "text": "ok", "metadata": {"kind": "memory"}}, similarity=0.8),
                ]

        results = asyncio.run(VectorRetriever(_Faiss()).search("hello", k=2))

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].doc_id, 7)
        self.assertEqual(results[0].content, "ok")

    def test_dream_update_resolves_legacy_id_to_canonical_memory(self):
        from astrmai.memory.dream.dream_agent import DreamAgent

        class _Store:
            async def find_ids_by_source_ref(self, source_ref):
                return ["mem_123"] if source_ref == "MemoryEvent:legacy-1" else []

            async def update_content(self, memory_id, **kwargs):
                self.updated = (memory_id, kwargs)
                return 1

        store = _Store()
        engine = SimpleNamespace(v2_store=store, index_projector=SimpleNamespace(project=lambda memory_id: asyncio.sleep(0)))
        agent = DreamAgent(SimpleNamespace(config=SimpleNamespace()), SimpleNamespace(), memory_engine=engine)

        result = asyncio.run(agent._tool_update({"event_id": "legacy-1", "new_narrative": "new text"}))

        self.assertEqual(result, "Updated memory mem_123")
        self.assertEqual(store.updated[0], "mem_123")

    def test_dream_merge_reports_empty_write_as_failure(self):
        from astrmai.memory.dream.dream_agent import DreamAgent

        class _MemoryEngine:
            async def add_memory(self, **kwargs):
                return ""

        agent = DreamAgent(SimpleNamespace(config=SimpleNamespace()), SimpleNamespace(), memory_engine=_MemoryEngine())

        result = asyncio.run(
            agent._tool_merge({"event_ids": ["legacy-1"], "new_narrative": "merged"}, "chat-1")
        )

        self.assertIn("合并失败", result)

    def test_memory_tool_omni_query_filters_gather_exceptions(self):
        from astrmai.memory.services.memory_tool_service import MemoryToolService

        class _Service(MemoryToolService):
            async def search_memory(self, **kwargs):
                raise asyncio.CancelledError()

        result = asyncio.run(_Service(SimpleNamespace()).omni_query(query="hello"))

        self.assertEqual(result, "System note: no usable internal data was found.")
        self.assertNotIn("CancelledError", result)

    def test_topic_summarizer_sorts_mixed_timestamp_types(self):
        from astrmai.memory.services.topic_summarizer import TopicSummarizer

        summarizer = TopicSummarizer()
        segments = summarizer._segment_by_silence(
            [
                {"sender": "b", "content": "second", "timestamp": "2"},
                {"sender": "a", "content": "first", "timestamp": 1},
                {"sender": "c", "content": "bad", "timestamp": "not-a-time"},
            ]
        )

        self.assertEqual([item["content"] for item in segments[0].messages], ["bad", "first", "second"])

    def test_relationship_process_event_normalizes_bad_intensity(self):
        from astrmai.state.relationship.relationship_engine import RelationshipEngine, RelationshipEvent

        engine = RelationshipEngine()

        negative_score = engine.process_event("user-1", RelationshipEvent.INSULT, intensity=-10)
        string_score = engine.process_event("user-2", RelationshipEvent.NORMAL_CHAT, intensity="bad")

        self.assertLess(negative_score, 0)
        self.assertGreater(string_score, 0)

    def test_prompt_builder_defaults_missing_freshness_budget(self):
        from astrmai.conversation.contracts.focus_context import FreshnessState, ReplyMode
        from astrmai.conversation.planning.prompt_builder import build_prompt_envelope

        focus_context = SimpleNamespace(
            focus_reason="direct",
            root_reason="",
            reply_mode=ReplyMode.CASUAL_FOLLOWUP,
            social_state="",
            thread_signature="sig",
            freshness_budget=None,
        )
        planner = SimpleNamespace(_build_guidance_lines=lambda reply_mode: [])

        envelope = build_prompt_envelope(planner, focus_context, "hi", "", "", "", "", "", False)

        self.assertEqual(envelope.freshness_state, FreshnessState.FRESH)

    def test_prepare_image_returns_none_for_bad_payload(self):
        from astrmai.multimodal.image_pipeline import ImagePipeline

        self.assertIsNone(ImagePipeline.prepare_image("not base64"))
        self.assertIsNone(ImagePipeline.prepare_image(base64.b64encode(b"not an image").decode("ascii")))


if __name__ == "__main__":
    unittest.main()
