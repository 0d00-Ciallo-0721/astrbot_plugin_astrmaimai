from __future__ import annotations

import asyncio
import importlib
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from tests.helpers.astrbot_stubs import install_astrbot_stubs


def _candidate(contracts, memory_id: str, *, score: float = 0.5, **kwargs):
    return contracts.MemoryCandidate(
        id=memory_id,
        kind=kwargs.pop("kind", "memory"),
        source=kwargs.pop("source", "unit"),
        summary=kwargs.pop("summary", f"summary {memory_id}"),
        content=kwargs.pop("content", f"content {memory_id}"),
        session_id=kwargs.pop("session_id", "chat-1"),
        importance=kwargs.pop("importance", 0.5),
        confidence=kwargs.pop("confidence", 0.8),
        relevance_score=score,
        recency_score=kwargs.pop("recency_score", 0.8),
        status=kwargs.pop("status", "active"),
        visibility=kwargs.pop("visibility", "auto_and_tool"),
        created_at=kwargs.pop("created_at", 1.0),
        updated_at=kwargs.pop("updated_at", 1.0),
        last_access_time=kwargs.pop("last_access_time", 1.0),
        access_count=kwargs.pop("access_count", 0),
        decay_score=kwargs.pop("decay_score", 1.0),
        metadata=kwargs.pop("metadata", {}),
    )


class MemoryGapCoverageTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        for name in list(sys.modules):
            if name.startswith("astrmai.memory."):
                sys.modules.pop(name, None)
        self.contracts = importlib.import_module("astrmai.memory.contracts.memory_query")
        self.retrieval_mod = importlib.import_module("astrmai.memory.services.memory_retrieval_service")
        self.summarizer_mod = importlib.import_module("astrmai.memory.services.session_memory_summarizer")
        self.projector_mod = importlib.import_module("astrmai.memory.services.memory_index_projector")
        self.hybrid_mod = importlib.import_module("astrmai.memory.retrieval.hybrid_retriever")
        self.injection_mod = importlib.import_module("astrmai.memory.services.memory_injection_service")
        self.migration_mod = importlib.import_module("astrmai.memory.services.memory_migration_service")
        self.utils_mod = importlib.import_module("astrmai.memory.utils")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_retrieve_deep_falls_back_to_single_query_when_rewrite_fails(self):
        class _Store:
            def __init__(self, contracts):
                self.contracts = contracts
                self.search_calls = []

            async def search(self, query, **kwargs):
                self.search_calls.append((query, kwargs))
                return [_candidate(self.contracts, "fallback-1", score=0.7, summary="fallback hit")]

        class _Gateway:
            async def call_data_process_task(self, **kwargs):
                raise RuntimeError("rewrite failed")

        store = _Store(self.contracts)
        service = self.retrieval_mod.MemoryRetrievalService(store, engine=SimpleNamespace(gateway=_Gateway()))
        query = self.contracts.MemoryQuery(query="Alice project", session_id="chat-1", policy="deep", top_k=2)

        result = asyncio.run(service.retrieve_deep(query))

        self.assertEqual([item.id for item in result], ["fallback-1"])
        self.assertEqual([call[0] for call in store.search_calls], ["Alice project"])

    def test_retrieval_deadline_keeps_hybrid_when_canonical_fts_blocks(self):
        class _Store:
            async def search(self, *args, **kwargs):
                await asyncio.Event().wait()

        class _Engine:
            config = SimpleNamespace(
                timing=SimpleNamespace(
                    memory_retrieval_timeout_sec=0.05,
                    memory_fts_timeout_sec=0.01,
                )
            )

            async def search_memories(self, *args, **kwargs):
                return [
                    self_result
                ]

        self_result = self.utils_mod.SearchResult(
            doc_id=1,
            score=0.9,
            content="hybrid survived",
            metadata={"status": "active", "visibility": "auto_and_tool"},
            source="vector",
        )
        service = self.retrieval_mod.MemoryRetrievalService(_Store(), engine=_Engine())
        query = self.contracts.MemoryQuery(query="Alice", session_id="chat-1", top_k=2)

        started = time.monotonic()
        results = asyncio.run(service._retrieve_once(query))

        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual([item.content for item in results], ["hybrid survived"])
        branches = query.metadata["_trace"]["retrieval_branches"]
        self.assertEqual(branches["canonical_fts"]["status"], "timeout")
        self.assertEqual(branches["hybrid"]["status"], "success")

    def test_deep_memory_total_budget_is_a_hard_deadline(self):
        class _Store:
            async def search(self, *args, **kwargs):
                await asyncio.Event().wait()

        engine = SimpleNamespace(
            gateway=SimpleNamespace(),
            config=SimpleNamespace(
                timing=SimpleNamespace(
                    deep_memory_total_budget_sec=0.02,
                    memory_retrieval_timeout_sec=1.0,
                    memory_fts_timeout_sec=1.0,
                )
            ),
        )
        service = self.retrieval_mod.MemoryRetrievalService(_Store(), engine=engine)
        query = self.contracts.MemoryQuery(
            query="Alice",
            session_id="chat-1",
            policy="deep",
            top_k=2,
        )

        started = time.monotonic()
        results = asyncio.run(service.retrieve_deep(query))

        self.assertLess(time.monotonic() - started, 0.2)
        self.assertEqual(results, [])
        self.assertEqual(
            query.metadata["deep_memory_budget_trace"]["status"],
            "timeout",
        )

    def test_deep_memory_timeout_never_returns_unfiltered_actor_candidates(self):
        service = self.retrieval_mod.MemoryRetrievalService(
            SimpleNamespace(),
            engine=SimpleNamespace(
                gateway=SimpleNamespace(),
                config=SimpleNamespace(
                    timing=SimpleNamespace(deep_memory_total_budget_sec=0.03)
                ),
            ),
        )
        foreign = _candidate(
            self.contracts,
            "foreign-memory",
            kind="identity",
            metadata={"sender_id": "other"},
        )

        async def _rewrite(_query):
            return ["first", "second"]

        async def _retrieve_once(scoped_query):
            if scoped_query.query == "first":
                return [foreign]
            await asyncio.Event().wait()

        service._rewrite_queries = _rewrite
        service._retrieve_once = _retrieve_once
        query = self.contracts.MemoryQuery(
            query="remember",
            session_id="group-1",
            sender_id="current",
            policy="deep",
            top_k=5,
            metadata={
                "actor_memory_scope": {
                    "is_group": True,
                    "group_id": "group-1",
                    "current_actor_id": "current",
                    "allowed_actor_ids": ["current"],
                }
            },
        )

        result = asyncio.run(service.retrieve_deep(query))

        self.assertEqual(result, [])
        self.assertEqual(query.metadata["deep_memory_budget_trace"]["status"], "timeout")

    def test_retrieval_branch_trace_uses_each_branch_actual_elapsed_time(self):
        class _Store:
            async def search(self, *args, **kwargs):
                await asyncio.sleep(0.01)
                return []

        class _Engine:
            config = SimpleNamespace(
                timing=SimpleNamespace(
                    memory_retrieval_timeout_sec=0.2,
                    memory_fts_timeout_sec=0.2,
                )
            )

            async def search_memories(self, *args, **kwargs):
                await asyncio.sleep(0.09)
                return []

        service = self.retrieval_mod.MemoryRetrievalService(_Store(), engine=_Engine())
        query = self.contracts.MemoryQuery(query="timing", session_id="chat-1")

        asyncio.run(service._retrieve_once(query))

        branches = query.metadata["_trace"]["retrieval_branches"]
        self.assertLess(branches["canonical_fts"]["elapsed_ms"], 50.0)
        self.assertGreater(branches["hybrid"]["elapsed_ms"], 70.0)

    def test_session_summarizer_skips_low_importance_without_writing_memory(self):
        observed = []

        class _Processor:
            async def process_conversation(self, text, session_id=None):
                return {
                    "summary": "small talk",
                    "key_facts": ["minor detail"],
                    "topics": ["chat"],
                    "sentiment": "neutral",
                    "importance": 0.1,
                }

        class _Observer:
            async def record(self, **kwargs):
                observed.append(kwargs)

        engine = SimpleNamespace(
            write_service=SimpleNamespace(write=lambda *_args, **_kwargs: (_ for _ in ()).throw(AssertionError("should not write"))),
            memory_observer=_Observer(),
        )
        gateway = SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(cleanup_interval=3600, summary_threshold=2)))
        summarizer = self.summarizer_mod.SessionMemorySummarizer(SimpleNamespace(), gateway, engine, config=gateway.config)
        summarizer.processor = _Processor()
        summarizer.topic_summarizer = SimpleNamespace(process_history=lambda **_kwargs: asyncio.sleep(0, result=[]))

        asyncio.run(summarizer.summarize_session("chat-1", "[00:00] Alice: hello"))

        self.assertTrue(any(item["stage"] == "summarize_skipped" for item in observed))
        self.assertTrue(any(item.get("reason") == "low_importance" for item in observed))

    def test_index_projector_tracks_failed_projection_as_missing_until_repaired(self):
        class _Retriever:
            def __init__(self):
                self.fail = True
                self.added = []

            async def add_memory(self, content, metadata):
                if self.fail:
                    raise RuntimeError("vector offline")
                self.added.append((content, metadata))
                return 1

        class _Store:
            def __init__(self, contracts):
                self.contracts = contracts

            async def get_by_id(self, memory_id, allow_stale=False):
                return _candidate(self.contracts, memory_id, content="projectable content")

            async def list_projectable(self, session_id=""):
                return [_candidate(self.contracts, "mem-1")]

            async def get_canonical(self, memory_id, include_inactive=False):
                return _candidate(self.contracts, memory_id)

        retriever = _Retriever()
        engine = SimpleNamespace(
            retriever=retriever,
            v2_store=_Store(self.contracts),
            _build_memory_metadata=lambda **kwargs: dict(kwargs),
            _run_documents_query=lambda *args, **kwargs: asyncio.sleep(0, result=[]),
            _execute_documents_write=lambda *args, **kwargs: asyncio.sleep(0, result=0),
        )
        projector = self.projector_mod.MemoryIndexProjector(engine)

        failed = asyncio.run(projector.project("mem-1"))
        report = asyncio.run(projector.check_consistency())
        retriever.fail = False
        repaired = asyncio.run(projector.repair_consistency(report))

        self.assertFalse(failed)
        self.assertEqual(report["missing_projection_ids"], ["mem-1"])
        self.assertEqual(repaired["rebuilt_missing"], 1)
        self.assertEqual(len(retriever.added), 1)

    def test_hybrid_retriever_ignores_bad_metadata_json_without_dropping_result(self):
        result = self.utils_mod.SearchResult(
            doc_id=1,
            score=1.0,
            content="Alice likes notes",
            metadata="{bad-json",
            source="bm25",
        )
        retriever = self.hybrid_mod.HybridRetriever(bm25=None, vector=None, config=None)

        weighted = retriever._apply_weighting([result])

        self.assertEqual(len(weighted), 1)
        self.assertEqual(weighted[0].metadata, {})
        self.assertGreater(weighted[0].score, 0.0)

    def test_memory_injection_light_think_without_memory_intent_records_skip_state(self):
        class _Retrieval:
            async def retrieve(self, query):
                raise AssertionError("retrieve should be skipped for non-memory think-level-1 turn")

        class _Event:
            message_str = "plain social reply"
            unified_msg_origin = "chat-1"

            def __init__(self):
                self._extra = {"astrmai_think_level": 1}

            def get_extra(self, key, default=None):
                return self._extra.get(key, default)

            def set_extra(self, key, value):
                self._extra[key] = value

            def get_sender_id(self):
                return "user-1"

        event = _Event()
        service = self.injection_mod.MemoryInjectionService(_Retrieval())

        bundle = asyncio.run(service.build_bundle(event=event, prompt="hello"))

        self.assertEqual(bundle.skip_reason, "think_level_1_no_memory_intent")
        self.assertEqual(event.get_extra("astrmai_memory_injection_trace").skip_reason, "think_level_1_no_memory_intent")
        self.assertEqual(
            event.get_extra("astrmai_memory_funnel"),
            {
                "status": "skipped",
                "policy": "light",
                "skip_reason": "think_level_1_no_memory_intent",
                "candidate_count": 0,
                "selected_count": 0,
                "rendered_chars": 0,
                "actor_whitelist_count": 0,
                "actor_suppressed_count": 0,
                "actor_candidate_count_before_filter": 0,
            },
        )
        turn_context = event.get_extra("astrmai_turn_context")
        self.assertEqual(turn_context.memory.skip_reason, "think_level_1_no_memory_intent")

    def test_memory_migration_verify_counts_index_and_legacy_anomalies(self):
        class _Store:
            db_path = "unused.db"
            data_path = "unused"

            async def initialize(self):
                return None

            async def migration_report(self):
                return {"applied": ["base"]}

            async def list_candidates(self, kinds=None, limit=5000, include_inactive=True):
                if kinds == ["jargon"]:
                    return [
                        _candidate(
                            self_contracts,
                            "jargon-1",
                            kind="jargon",
                            visibility="maintenance_only",
                            metadata={"review_status": "pending_human"},
                        )
                    ]
                if kinds == ["expression_pattern"]:
                    return [
                        _candidate(
                            self_contracts,
                            "expr-1",
                            kind="expression_pattern",
                            metadata={},
                        )
                    ]
                return []

            async def get_canonical(self, memory_id, include_inactive=False):
                return _candidate(self_contracts, memory_id, kind="jargon" if memory_id.startswith("jargon") else "memory")

        self_contracts = self.contracts

        class _Projector:
            async def check_consistency(self):
                return {
                    "missing_projection_ids": ["jargon-1", "mem-1"],
                    "orphan_projection_ids": [],
                    "inactive_projection_ids": ["jargon-2"],
                }

        migration = self.migration_mod.MemoryMigrationService(_Store(), index_projector=_Projector())
        migration._scan_memory_events = lambda: asyncio.sleep(0, result={"importable": 2})
        migration._scan_jargons = lambda: asyncio.sleep(0, result={"importable": 1})
        migration._scan_expression_patterns = lambda: asyncio.sleep(0, result={"importable": 3})

        report = asyncio.run(migration.verify())

        self.assertEqual(report["legacy"]["unmapped_memory_events"], 2)
        self.assertEqual(report["legacy"]["unmapped_jargons"], 1)
        self.assertEqual(report["legacy"]["unmapped_expression_patterns"], 3)
        self.assertEqual(report["jargon"]["active_missing_projection"], 1)
        self.assertEqual(report["jargon"]["visibility_anomalies"], 1)
        self.assertEqual(report["jargon"]["pending_human_without_review_suggestion"], 1)
        self.assertEqual(report["expression_pattern"]["missing_situation"], 1)

    def test_memory_engine_add_memory_builds_legacy_write_request(self):
        memory_engine_mod = importlib.import_module("astrmai.memory.services.memory_engine")

        class _WriteService:
            def __init__(self):
                self.requests = []

            async def write(self, request):
                self.requests.append(request)
                return "mem-written"

        gateway = SimpleNamespace(config=SimpleNamespace(provider=SimpleNamespace(embedding_models=[]), memory=SimpleNamespace(recall_top_k=5)))
        engine = memory_engine_mod.MemoryEngine(SimpleNamespace(), gateway, embedding_models=[], config=gateway.config)
        engine.write_service = _WriteService()

        result = asyncio.run(
            engine.add_memory(
                "remember this",
                session_id="chat-1",
                persona_id="persona-1",
                importance=0.9,
                sender_id="user-1",
                created_at=123.0,
            )
        )

        request = engine.write_service.requests[0]
        self.assertEqual(result, "mem-written")
        self.assertEqual(request.source, "legacy_add_memory")
        self.assertEqual(request.kind, "memory")
        self.assertEqual(request.session_id, "chat-1")
        self.assertEqual(request.persona_id, "persona-1")
        self.assertEqual(request.sender_id, "user-1")
        self.assertEqual(request.content, "remember this")
        self.assertEqual(request.importance, 0.9)
        self.assertEqual(request.source_ref, "memory_engine.add_memory")

    def test_memory_engine_search_memories_returns_empty_when_faiss_initialization_fails(self):
        memory_engine_mod = importlib.import_module("astrmai.memory.services.memory_engine")
        gateway = SimpleNamespace(config=SimpleNamespace(provider=SimpleNamespace(embedding_models=[]), memory=SimpleNamespace(recall_top_k=5)))
        engine = memory_engine_mod.MemoryEngine(SimpleNamespace(), gateway, embedding_models=[], config=gateway.config)
        engine._ensure_faiss_initialized = lambda: asyncio.sleep(0, result=False)

        result = asyncio.run(engine.search_memories("query", top_k=3, session_id="chat-1"))

        self.assertEqual(result, [])

    def test_memory_engine_first_search_does_not_wait_for_background_rebuild(self):
        memory_engine_mod = importlib.import_module("astrmai.memory.services.memory_engine")
        gateway = SimpleNamespace(
            config=SimpleNamespace(
                provider=SimpleNamespace(embedding_models=["embedding"]),
                memory=SimpleNamespace(recall_top_k=5),
                timing=SimpleNamespace(faiss_bootstrap_timeout_sec=60),
            )
        )
        engine = memory_engine_mod.MemoryEngine(
            SimpleNamespace(),
            gateway,
            embedding_models=["embedding"],
            config=gateway.config,
        )
        rebuild_started = asyncio.Event()
        release_rebuild = asyncio.Event()

        async def _bootstrap():
            engine._vector_state = "rebuilding"
            rebuild_started.set()
            await release_rebuild.wait()
            engine._vector_state = "ready"
            engine._is_ready = True

        engine._bootstrap_vector_index = _bootstrap

        async def _run():
            self.assertFalse(await engine._ensure_faiss_initialized())
            await rebuild_started.wait()
            started = time.monotonic()
            observation = {}
            result = await engine.search_memories(
                "query",
                top_k=3,
                session_id="chat-1",
                observation=observation,
            )
            elapsed = time.monotonic() - started
            self.assertEqual(engine.describe_vector_status()["state"], "rebuilding")
            release_rebuild.set()
            await engine._vector_bootstrap_task
            return result, observation, elapsed

        result, observation, elapsed = asyncio.run(_run())

        self.assertEqual(result, [])
        self.assertLess(elapsed, 0.1)
        self.assertEqual(observation["vector"]["status"], "rebuilding")

    def test_memory_engine_failed_candidate_bootstrap_closes_candidate_stack(self):
        memory_engine_mod = importlib.import_module("astrmai.memory.services.memory_engine")
        config = SimpleNamespace(
            provider=SimpleNamespace(embedding_models=["embedding"]),
            memory=SimpleNamespace(recall_top_k=5),
            timing=SimpleNamespace(faiss_bootstrap_timeout_sec=60),
        )
        provider = object()
        engine = memory_engine_mod.MemoryEngine(
            SimpleNamespace(get_provider_by_id=lambda _model_id: provider),
            SimpleNamespace(config=config),
            config=config,
        )
        engine.bm25_retriever = object()

        class _Store:
            async def migration_applied(self, _version):
                return True

            async def record_migration(self, *args, **kwargs):
                return None

        engine.v2_store = _Store()

        class _Faiss:
            def __init__(self, **kwargs):
                self.closed = False

            async def initialize(self):
                return None

            async def close(self):
                self.closed = True

        class _Vector:
            instances = []

            def __init__(self, faiss_db, config):
                self.faiss_db = faiss_db
                self.closed = False
                self.__class__.instances.append(self)

            def close(self):
                self.closed = True

            async def refresh_storage_metrics(self, *, force=False):
                return None

        class _Hybrid:
            def __init__(self, *args, **kwargs):
                pass

        class _Projector:
            def __init__(self, candidate_engine):
                self.engine = candidate_engine

            async def check_consistency(self):
                return {"error": "consistency failed"}

            async def rebuild_all(self):
                return 0

            async def projection_count(self):
                return 0

        with (
            patch.object(memory_engine_mod, "FaissVecDB", _Faiss),
            patch.object(memory_engine_mod, "VectorRetriever", _Vector),
            patch.object(memory_engine_mod, "HybridRetriever", _Hybrid),
            patch.object(memory_engine_mod, "MemoryIndexProjector", _Projector),
        ):
            with self.assertRaisesRegex(RuntimeError, "vector consistency scan failed"):
                asyncio.run(engine._bootstrap_vector_index())

        self.assertTrue(_Vector.instances[0].closed)
        self.assertTrue(_Vector.instances[0].faiss_db.closed)

    def test_memory_engine_vector_generations_use_isolated_published_index_files(self):
        memory_engine_mod = importlib.import_module("astrmai.memory.services.memory_engine")
        config = SimpleNamespace(
            provider=SimpleNamespace(embedding_models=["embedding"]),
            memory=SimpleNamespace(recall_top_k=5),
        )
        engine = memory_engine_mod.MemoryEngine(
            SimpleNamespace(),
            SimpleNamespace(config=config),
            config=config,
        )
        engine.data_path = Path(self.temp_dir.name)
        first = engine._new_vector_index_path(1)
        second = engine._new_vector_index_path(2)
        first.write_bytes(b"index")

        engine._publish_vector_index_manifest(first, ["embedding"])

        self.assertNotEqual(first, second)
        self.assertEqual(engine._load_published_vector_index(["embedding"]), first)
        self.assertIsNone(engine._load_published_vector_index(["other-embedding"]))

    def test_memory_engine_hot_refresh_without_running_loop_closes_full_vector_stack(self):
        memory_engine_mod = importlib.import_module("astrmai.memory.services.memory_engine")
        old_config = SimpleNamespace(
            provider=SimpleNamespace(embedding_models=["embedding"]),
            memory=SimpleNamespace(recall_top_k=5),
        )
        new_config = SimpleNamespace(
            provider=SimpleNamespace(embedding_models=["embedding-next"]),
            memory=SimpleNamespace(recall_top_k=5),
        )
        engine = memory_engine_mod.MemoryEngine(
            SimpleNamespace(),
            SimpleNamespace(config=old_config),
            config=old_config,
        )

        class _Retriever:
            closed = False

            def close(self):
                self.closed = True

        class _Faiss:
            closed = False

            async def close(self):
                await asyncio.sleep(0)
                self.closed = True

        retriever = _Retriever()
        faiss_db = _Faiss()
        engine.vec_retriever = retriever
        engine.faiss_db = faiss_db
        engine.retriever = object()
        engine._is_ready = True

        engine.refresh_config(new_config)

        self.assertTrue(retriever.closed)
        self.assertTrue(faiss_db.closed)
        self.assertIsNone(engine.vec_retriever)
        self.assertIsNone(engine.faiss_db)
        self.assertFalse(engine._is_ready)

    def test_memory_engine_vector_generation_cleanup_keeps_current_and_one_history(self):
        memory_engine_mod = importlib.import_module("astrmai.memory.services.memory_engine")
        config = SimpleNamespace(
            provider=SimpleNamespace(embedding_models=["embedding"]),
            memory=SimpleNamespace(recall_top_k=5),
        )
        engine = memory_engine_mod.MemoryEngine(
            SimpleNamespace(),
            SimpleNamespace(config=config),
            config=config,
        )
        engine.data_path = Path(self.temp_dir.name)
        first = engine.data_path / "vectors.g1.first.index"
        second = engine.data_path / "vectors.g2.second.index"
        current = engine.data_path / "vectors.g3.current.index"
        for position, path in enumerate((first, second, current), start=1):
            path.write_bytes(b"index")
            path.touch()
            time.sleep(0.01 * position)

        removed = engine._cleanup_stale_vector_indexes(current, keep_history=1)

        self.assertEqual(removed, 1)
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())
        self.assertTrue(current.exists())

    def test_memory_engine_refresh_config_updates_embedding_models_and_invalidates_vector_state(self):
        memory_engine_mod = importlib.import_module("astrmai.memory.services.memory_engine")
        old_config = SimpleNamespace(provider=SimpleNamespace(embedding_models=["ed-old"]), memory=SimpleNamespace(recall_top_k=5))
        new_config = SimpleNamespace(provider=SimpleNamespace(embedding_models=["ed-new"]), memory=SimpleNamespace(recall_top_k=9))
        engine = memory_engine_mod.MemoryEngine(SimpleNamespace(), SimpleNamespace(config=old_config), config=old_config)
        engine.faiss_db = object()
        engine.vec_retriever = object()
        engine.retriever = object()
        engine._is_ready = True
        engine._init_failures = 3
        engine._next_retry_time = 123.0
        engine._index_consistency_repaired = True

        engine.refresh_config(new_config)

        self.assertIs(engine.config, new_config)
        self.assertEqual(engine.embedding_models, ["ed-new"])
        self.assertIsNone(engine.faiss_db)
        self.assertIsNone(engine.vec_retriever)
        self.assertIsNone(engine.retriever)
        self.assertFalse(engine._is_ready)
        self.assertEqual(engine._init_failures, 0)
        self.assertEqual(engine._next_retry_time, 0.0)
        self.assertFalse(engine._index_consistency_repaired)
        self.assertTrue(engine._force_index_rebuild)

    def test_memory_engine_refresh_config_can_clear_embedding_models(self):
        memory_engine_mod = importlib.import_module("astrmai.memory.services.memory_engine")
        old_config = SimpleNamespace(provider=SimpleNamespace(embedding_models=["ed-old"]), memory=SimpleNamespace(recall_top_k=5))
        new_config = SimpleNamespace(provider=SimpleNamespace(embedding_models=[]), memory=SimpleNamespace(recall_top_k=9))
        engine = memory_engine_mod.MemoryEngine(SimpleNamespace(), SimpleNamespace(config=old_config), config=old_config)

        engine.refresh_config(new_config)

        self.assertEqual(engine.embedding_models, [])
        self.assertTrue(engine._force_index_rebuild)

    def test_memory_engine_vector_index_reset_only_removes_rebuildable_index_file(self):
        memory_engine_mod = importlib.import_module("astrmai.memory.services.memory_engine")
        config = SimpleNamespace(provider=SimpleNamespace(embedding_models=["ed-old"]), memory=SimpleNamespace(recall_top_k=5))
        engine = memory_engine_mod.MemoryEngine(SimpleNamespace(), SimpleNamespace(config=config), config=config)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            engine.data_path = Path(temp_dir)
            index_path = engine.data_path / "vectors.index"
            docs_path = engine.data_path / "docs.db"
            index_path.write_text("old-vector-index", encoding="utf-8")
            docs_path.write_text("documents", encoding="utf-8")

            engine._reset_vector_index_file()

            self.assertFalse(index_path.exists())
            self.assertTrue(docs_path.exists())

    def test_memory_engine_recall_query_and_search_render_retrieval_results(self):
        memory_engine_mod = importlib.import_module("astrmai.memory.services.memory_engine")

        class _RetrievalService:
            def __init__(self, contracts):
                self.contracts = contracts
                self.queries = []

            async def retrieve(self, query):
                self.queries.append(query)
                return [
                    _candidate(self.contracts, "mem-1", content="visible memory", summary="visible summary"),
                    _candidate(self.contracts, "mem-feedback", content="[cognitive_feedback:test]\nsummary: hidden"),
                ]

            def render_recall(self, query, candidates):
                return f"{query.session_id}|" + ",".join(item.id for item in candidates)

        gateway = SimpleNamespace(config=SimpleNamespace(provider=SimpleNamespace(embedding_models=[]), memory=SimpleNamespace(recall_top_k=7)))
        engine = memory_engine_mod.MemoryEngine(SimpleNamespace(), gateway, embedding_models=[], config=gateway.config)
        engine.retrieval_service = _RetrievalService(self.contracts)

        recalled = asyncio.run(engine.recall("hello", session_id="chat-1", persona_id="persona-1", layers=["memory"], top_k=2))
        queried = asyncio.run(engine.query("hello", session_id="chat-2", top_k=4))
        searched = asyncio.run(engine.search("hello", session_id="chat-3", top_k=5))

        self.assertEqual(recalled, "chat-1|mem-1")
        self.assertEqual(queried, "chat-2|mem-1")
        self.assertEqual(searched, "chat-3|mem-1")
        first_query = engine.retrieval_service.queries[0]
        self.assertEqual(first_query.query, "hello")
        self.assertEqual(first_query.persona_id, "persona-1")
        self.assertEqual(first_query.layers, ["memory"])
        self.assertEqual(first_query.top_k, 2)
        self.assertEqual(first_query.exclude_kinds, ["feedback"])

    def test_memory_engine_recall_returns_no_result_message_when_empty(self):
        memory_engine_mod = importlib.import_module("astrmai.memory.services.memory_engine")

        class _RetrievalService:
            async def retrieve(self, query):
                return []

            def render_recall(self, query, candidates):
                raise AssertionError("empty recall should not render")

        gateway = SimpleNamespace(config=SimpleNamespace(provider=SimpleNamespace(embedding_models=[]), memory=SimpleNamespace(recall_top_k=5)))
        engine = memory_engine_mod.MemoryEngine(SimpleNamespace(), gateway, embedding_models=[], config=gateway.config)
        engine.retrieval_service = _RetrievalService()

        result = asyncio.run(engine.recall("missing", session_id="chat-1"))

        self.assertEqual(result, "No relevant memory found for 'missing'.")

    def test_memory_turn_pipeline_reports_maintenance_concurrency(self):
        pipeline_mod = importlib.import_module("astrmai.memory.services.memory_turn_pipeline")
        pipeline = pipeline_mod.MemoryTurnPipeline(
            context=SimpleNamespace(),
            gateway=SimpleNamespace(config=SimpleNamespace(memory=SimpleNamespace(maintenance_concurrency=3))),
            engine=SimpleNamespace(),
            session_summarizer=SimpleNamespace(summarize_session=lambda **kwargs: asyncio.sleep(0)),
            instant_gate=SimpleNamespace(),
            config=SimpleNamespace(memory=SimpleNamespace(maintenance_concurrency=3), timing=SimpleNamespace()),
        )
        status = pipeline.describe_runtime_status()
        self.assertEqual(status["maintenance_concurrency"], 3)
        self.assertEqual(status["active_maintenance"], 0)


if __name__ == "__main__":
    unittest.main()
