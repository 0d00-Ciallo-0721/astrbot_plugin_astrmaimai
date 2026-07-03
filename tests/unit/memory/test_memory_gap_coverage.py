from __future__ import annotations

import asyncio
import importlib
import json
import sys
import tempfile
import unittest
from types import SimpleNamespace

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


if __name__ == "__main__":
    unittest.main()
