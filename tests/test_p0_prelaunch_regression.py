import asyncio
import base64
import tempfile
import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

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

    def test_vector_store_normalizes_framework_metadata_formats(self):
        from astrmai.memory.retrieval.vector_store import VectorRetriever

        class _Faiss:
            async def retrieve(self, **kwargs):
                return [
                    SimpleNamespace(data={"id": 1, "text": "dict", "metadata": {"kind": "fact"}}, similarity=0.9),
                    SimpleNamespace(data={"id": 2, "text": "json", "metadata": '{"kind": "preference"}'}, similarity=0.8),
                    SimpleNamespace(data={"id": 3, "text": "bad", "metadata": "{bad-json"}, similarity=0.7),
                    SimpleNamespace(data={"id": 4, "text": "none", "metadata": None}, similarity=0.6),
                ]

        results = asyncio.run(VectorRetriever(_Faiss()).search("hello", k=4))

        self.assertEqual([item.metadata for item in results], [
            {"kind": "fact"},
            {"kind": "preference"},
            {},
            {},
        ])

    def test_vector_store_timeout_returns_lexical_fallback_and_opens_circuit(self):
        from astrmai.memory.retrieval import vector_store
        from astrmai.memory.retrieval.vector_store import VectorRetriever

        class _Faiss:
            def __init__(self):
                self.calls = 0

            async def retrieve(self, **kwargs):
                self.calls += 1
                return []

        config = SimpleNamespace(
            timing=SimpleNamespace(
                faiss_timeout_sec=4.0,
                faiss_failure_threshold=1,
                faiss_circuit_breaker_cooldown_sec=30.0,
            )
        )
        faiss = _Faiss()
        retriever = VectorRetriever(faiss, config=config)
        observation = {}

        async def _timeout(awaitable, *args, **kwargs):
            awaitable.close()
            raise asyncio.TimeoutError

        with patch.object(vector_store.asyncio, "wait_for", new=_timeout):
            results = asyncio.run(retriever.search("hello", observation=observation))

        self.assertEqual(results, [])
        self.assertEqual(retriever._failure_count, 1)
        self.assertTrue(retriever._circuit_open())
        self.assertEqual(observation["status"], "timeout")
        self.assertEqual(observation["retrieve_stage"], "faiss_db.retrieve")
        self.assertEqual(observation["timeout_origin"], "faiss_db.retrieve")
        self.assertGreaterEqual(observation["elapsed_ms"], 0.0)
        self.assertTrue(observation["circuit_open"])
        self.assertGreater(observation["cooldown_remaining_sec"], 0.0)
        self.assertEqual(observation["configured_timeout_sec"], 4.0)
        self.assertEqual(observation["effective_timeout_sec"], 4.0)
        self.assertFalse(observation["timeout_budget_clamped"])
        self.assertEqual(observation["failure_threshold"], 1)
        self.assertEqual(observation["cooldown_sec"], 30.0)

        second_observation = {}
        second_results = asyncio.run(
            retriever.search("hello again", observation=second_observation)
        )
        self.assertEqual(second_results, [])
        self.assertEqual(faiss.calls, 0)
        self.assertEqual(second_observation["status"], "circuit_open")

    def test_vector_store_default_timeout_is_twenty_seconds(self):
        from astrmai.memory.retrieval import vector_store
        from astrmai.memory.retrieval.vector_store import VectorRetriever

        class _Faiss:
            async def retrieve(self, **_kwargs):
                return []

        observation = {}

        async def _timeout(awaitable, *args, **kwargs):
            awaitable.close()
            raise asyncio.TimeoutError

        with patch.object(vector_store.asyncio, "wait_for", new=_timeout):
            results = asyncio.run(VectorRetriever(_Faiss()).search("hello", observation=observation))

        self.assertEqual(results, [])
        self.assertEqual(observation["status"], "timeout")
        self.assertEqual(observation["retrieve_stage"], "faiss_db.retrieve")
        self.assertEqual(observation["timeout_origin"], "faiss_db.retrieve")
        self.assertEqual(observation["timeout_sec"], 20.0)
        self.assertEqual(observation["configured_timeout_sec"], 20.0)
        self.assertEqual(observation["effective_timeout_sec"], 20.0)
        self.assertFalse(observation["timeout_budget_clamped"])

    def test_vector_store_timeout_is_clamped_to_shared_turn_budget(self):
        from astrmai.memory.retrieval import vector_store
        from astrmai.memory.retrieval.vector_store import VectorRetriever

        class _Faiss:
            async def retrieve(self, **_kwargs):
                return []

        captured = {}

        async def _wait_for(awaitable, *args, **kwargs):
            captured["timeout"] = kwargs.get("timeout", args[0] if args else None)
            return await awaitable

        with patch.object(
            vector_store,
            "clamp_timeout_to_turn_budget",
            return_value=1.25,
        ), patch.object(vector_store.asyncio, "wait_for", new=_wait_for):
            observation = {}
            results = asyncio.run(VectorRetriever(_Faiss()).search("hello", observation=observation))

        self.assertEqual(results, [])
        self.assertEqual(captured["timeout"], 1.25)
        self.assertEqual(observation["configured_timeout_sec"], 20.0)
        self.assertEqual(observation["effective_timeout_sec"], 1.25)
        self.assertTrue(observation["timeout_budget_clamped"])

    def test_vector_store_limits_query_concurrency_and_reports_queue_wait(self):
        from astrmai.memory.retrieval.vector_store import VectorRetriever

        class _Faiss:
            def __init__(self):
                self.started = 0
                self.active = 0
                self.maximum_active = 0
                self.release = asyncio.Event()

            async def retrieve(self, **kwargs):
                self.started += 1
                self.active += 1
                self.maximum_active = max(self.maximum_active, self.active)
                try:
                    await self.release.wait()
                    return []
                finally:
                    self.active -= 1

        config = SimpleNamespace(timing=SimpleNamespace(faiss_query_concurrency=1, faiss_timeout_sec=2.0))
        faiss = _Faiss()
        retriever = VectorRetriever(faiss, config=config)
        first_observation, second_observation = {}, {}

        async def _run():
            first = asyncio.create_task(retriever.search("first", observation=first_observation))
            while faiss.started < 1:
                await asyncio.sleep(0)
            second = asyncio.create_task(retriever.search("second", observation=second_observation))
            await asyncio.sleep(0.01)
            self.assertEqual(faiss.maximum_active, 1)
            faiss.release.set()
            await asyncio.gather(first, second)

        asyncio.run(_run())
        self.assertEqual(faiss.maximum_active, 1)
        self.assertGreaterEqual(second_observation["query_queue_wait_ms"], 0.0)

    def test_vector_store_phased_retrieval_reports_embedding_index_and_document_timings(self):
        from astrmai.memory.retrieval.vector_store import VectorRetriever

        search_threads = []

        class _Embedding:
            async def get_embedding(self, query):
                await asyncio.sleep(0)
                return [1.0, 0.0]

        class _Index:
            def search(self, vector, k):
                search_threads.append(threading.current_thread().name)
                return [[0.1]], [[7]]

        class _Storage:
            index = _Index()

        class _Documents:
            async def get_documents(self, metadata_filters, ids):
                return [{"id": 7, "text": "hit", "metadata": {"kind": "memory"}}]

        faiss = SimpleNamespace(
            embedding_provider=_Embedding(),
            embedding_storage=_Storage(),
            document_storage=_Documents(),
        )
        observation = {}
        results = asyncio.run(VectorRetriever(faiss).search("hello", observation=observation))

        self.assertEqual(len(results), 1, observation)
        self.assertEqual(results[0].content, "hit")
        self.assertEqual(observation["retrieve_stage"], "phased")
        self.assertIn("embedding_ms", observation["stage_timings"])
        self.assertIn("faiss_index_ms", observation["stage_timings"])
        self.assertIn("document_read_ms", observation["stage_timings"])
        self.assertEqual(observation["timeout_origin"], "")
        self.assertTrue(search_threads[0].startswith("astrmai-faiss"))

    def test_vector_store_phased_timeout_identifies_embedding_stage(self):
        from astrmai.memory.retrieval.vector_store import VectorRetriever

        class _Embedding:
            async def get_embedding(self, query):
                await asyncio.Event().wait()

        faiss = SimpleNamespace(
            embedding_provider=_Embedding(),
            embedding_storage=SimpleNamespace(index=object()),
            document_storage=SimpleNamespace(),
        )
        config = SimpleNamespace(timing=SimpleNamespace(faiss_timeout_sec=0.5, faiss_failure_threshold=3))
        observation = {}
        with patch(
            "astrmai.memory.retrieval.vector_store.clamp_timeout_to_turn_budget",
            return_value=0.01,
        ):
            results = asyncio.run(VectorRetriever(faiss, config).search("hello", observation=observation))

        self.assertEqual(results, [])
        self.assertEqual(observation["status"], "timeout")
        self.assertEqual(observation["timeout_origin"], "embedding")
        self.assertIn("embedding_ms", observation["stage_timings"])

    def test_vector_store_keeps_slot_until_timed_out_index_thread_finishes(self):
        from astrmai.memory.retrieval.vector_store import VectorRetriever

        class _Embedding:
            async def get_embedding(self, query):
                return [1.0, 0.0]

        release_index = threading.Event()
        index_started = threading.Event()
        faiss = SimpleNamespace(
            embedding_provider=_Embedding(),
            embedding_storage=SimpleNamespace(index=object()),
            document_storage=SimpleNamespace(),
        )
        retriever = VectorRetriever(
            faiss,
            SimpleNamespace(
                timing=SimpleNamespace(
                    faiss_timeout_sec=0.5,
                    faiss_query_concurrency=1,
                    faiss_failure_threshold=10,
                )
            ),
        )

        def blocking_search(vector, k):
            index_started.set()
            release_index.wait(timeout=2.0)
            return [[0.1]], [[-1]]

        retriever._search_index_sync = blocking_search

        async def run():
            with patch(
                "astrmai.memory.retrieval.vector_store.clamp_timeout_to_turn_budget",
                return_value=0.02,
            ):
                first_observation = {}
                first = asyncio.create_task(retriever.search("first", observation=first_observation))
                while not index_started.is_set():
                    await asyncio.sleep(0)
                await first
                self.assertEqual(first_observation["timeout_origin"], "faiss_index")
                self.assertEqual(retriever.describe_status()["active_queries"], 1)
                self.assertEqual(retriever.describe_status()["background_index_jobs"], 1)

                second_observation = {}
                await retriever.search("second", observation=second_observation)
                self.assertEqual(second_observation["status"], "query_queue_timeout")
                self.assertEqual(retriever.describe_status()["active_queries"], 1)

                release_index.set()
                for _ in range(100):
                    if retriever.describe_status()["active_queries"] == 0:
                        break
                    await asyncio.sleep(0.01)
                self.assertEqual(retriever.describe_status()["active_queries"], 0)
                self.assertEqual(retriever.describe_status()["background_index_jobs"], 0)

        asyncio.run(run())

    def test_vector_store_allows_only_one_half_open_probe(self):
        from astrmai.memory.retrieval.vector_store import VectorRetriever

        class _Faiss:
            def __init__(self):
                self.calls = 0
                self.started = asyncio.Event()
                self.release = asyncio.Event()

            async def retrieve(self, **kwargs):
                self.calls += 1
                self.started.set()
                await self.release.wait()
                return []

        faiss = _Faiss()
        retriever = VectorRetriever(
            faiss,
            SimpleNamespace(
                timing=SimpleNamespace(
                    faiss_failure_threshold=1,
                    faiss_timeout_sec=2.0,
                )
            ),
        )
        retriever._failure_count = 1
        retriever._unavailable_until = 0.0

        async def run():
            first_observation = {}
            first = asyncio.create_task(retriever.search("probe", observation=first_observation))
            await faiss.started.wait()
            second_observation = {}
            second = await retriever.search("parallel", observation=second_observation)
            self.assertEqual(second, [])
            self.assertEqual(second_observation["status"], "circuit_open")
            self.assertTrue(second_observation["runtime_metrics"]["half_open_probe_active"])
            faiss.release.set()
            await first

        asyncio.run(run())
        self.assertEqual(faiss.calls, 1)
        self.assertEqual(retriever.describe_status()["failure_count"], 0)

    def test_hybrid_retriever_reports_bm25_fallback_when_vector_is_unhealthy(self):
        from astrmai.memory.retrieval.hybrid_retriever import HybridRetriever
        from astrmai.memory.utils import SearchResult

        class _BM25:
            async def search(self, *args, **kwargs):
                return [
                    SearchResult(
                        doc_id=1,
                        score=0.8,
                        content="lexical",
                        metadata={},
                        source="bm25",
                    )
                ]

        class _Vector:
            async def search(self, *args, observation=None, **kwargs):
                observation.update(
                    {
                        "status": "circuit_open",
                        "circuit_open": True,
                        "result_count": 0,
                    }
                )
                return []

        observation = {}
        results = asyncio.run(
            HybridRetriever(_BM25(), _Vector()).search(
                "hello",
                observation=observation,
            )
        )

        self.assertEqual(len(results), 1)
        self.assertEqual(observation["fallback_source"], "bm25")
        self.assertEqual(observation["vector"]["status"], "circuit_open")

    def test_hybrid_retriever_times_out_bm25_and_keeps_vector_results(self):
        from astrmai.memory.retrieval.hybrid_retriever import HybridRetriever
        from astrmai.memory.utils import SearchResult

        class _BM25:
            async def search(self, *args, **kwargs):
                await asyncio.Event().wait()

        class _Vector:
            async def search(self, *args, observation=None, **kwargs):
                observation["status"] = "success"
                return [
                    SearchResult(
                        doc_id=1,
                        score=0.9,
                        content="vector result",
                        metadata={},
                        source="vector",
                    )
                ]

        config = SimpleNamespace(
            timing=SimpleNamespace(memory_bm25_timeout_sec=0.01),
            memory=SimpleNamespace(time_decay_rate=0.01),
        )
        observation = {}
        started = time.monotonic()
        results = asyncio.run(
            HybridRetriever(_BM25(), _Vector(), config=config).search(
                "hello",
                observation=observation,
            )
        )

        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual([item.content for item in results], ["vector result"])
        self.assertEqual(observation["bm25_status"], "timeout")
        self.assertEqual(observation["fallback_source"], "vector")

    def test_vector_status_awaits_document_and_projection_counts(self):
        from astrmai.memory.retrieval.vector_store import VectorRetriever

        class _DocumentStorage:
            async def count_documents(self):
                return 7

        async def _projection_count():
            return 6

        retriever = VectorRetriever(
            SimpleNamespace(
                embedding_storage=SimpleNamespace(index=SimpleNamespace(ntotal=5)),
                document_storage=_DocumentStorage(),
            ),
            projection_count_provider=_projection_count,
        )

        asyncio.run(retriever.refresh_storage_metrics(force=True))
        status = retriever.describe_status()

        self.assertEqual(status["index_ntotal"], 5)
        self.assertEqual(status["document_storage_count"], 7)
        self.assertEqual(status["projection_count"], 6)
        self.assertEqual(status["index_delta_vs_projection"], -1)
        self.assertEqual(status["document_delta_vs_projection"], 1)

    def test_vector_queue_timeout_is_included_in_latency_samples(self):
        from astrmai.memory.retrieval.vector_store import VectorRetriever

        class _Faiss:
            async def retrieve(self, **kwargs):
                return []

        retriever = VectorRetriever(
            _Faiss(),
            SimpleNamespace(
                timing=SimpleNamespace(
                    faiss_timeout_sec=0.01,
                    faiss_query_concurrency=1,
                    faiss_failure_threshold=10,
                )
            ),
        )

        async def _run():
            await retriever._query_semaphore.acquire()
            try:
                observation = {}
                await retriever.search("blocked", observation=observation)
                return observation
            finally:
                retriever._query_semaphore.release()

        observation = asyncio.run(_run())
        samples = retriever.describe_status()["stage_latency_ms"]["query_queue_wait_ms"]

        self.assertEqual(observation["status"], "query_queue_timeout")
        self.assertEqual(samples["count"], 1)
        self.assertGreater(samples["max"], 0.0)

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

    def test_memory_tool_identity_query_exposes_verified_qq_but_not_historical_group(self):
        from astrmai.memory.services.memory_tool_service import MemoryToolService

        profile = SimpleNamespace(
            name="萤",
            social_score=1.0,
            persona_analysis="群友",
            profile_metadata={
                "verified_identity": {
                    "platform": "qq",
                    "user_id": "3650815443",
                    "verified": True,
                }
            },
        )

        class _Db:
            def get_profile_by_name(self, name):
                return profile if name == "萤" else None

        class _Service(MemoryToolService):
            async def search_memory(self, **kwargs):
                return SimpleNamespace(items=[])

        result = asyncio.run(
            _Service(SimpleNamespace(), db_service=_Db()).omni_query(
                query="",
                target_name="萤",
            )
        )

        self.assertIn("Verified QQ identity: 3650815443", result)
        self.assertIn("Current-group membership: unverified", result)
        self.assertNotIn("曾在群", result)

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
