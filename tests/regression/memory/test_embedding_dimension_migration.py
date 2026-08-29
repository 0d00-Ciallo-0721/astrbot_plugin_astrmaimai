from __future__ import annotations

import asyncio
import importlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class EmbeddingDimensionMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        for name in list(sys.modules):
            if name.startswith("astrmai.memory."):
                sys.modules.pop(name, None)
        self.engine_mod = importlib.import_module("astrmai.memory.services.memory_engine")
        self.vector_mod = importlib.import_module("astrmai.memory.retrieval.vector_store")

    def tearDown(self):
        self.temp_dir.cleanup()

    def _engine(self):
        config = SimpleNamespace(
            provider=SimpleNamespace(embedding_models=["embedding-v2"]),
            memory=SimpleNamespace(recall_top_k=5),
        )
        engine = self.engine_mod.MemoryEngine(SimpleNamespace(), SimpleNamespace(config=config), config=config)
        engine.data_path = Path(self.temp_dir.name)
        return engine

    def test_index_dimension_reads_faiss_storage_and_unknown_is_not_zero(self):
        engine = self._engine()
        self.assertEqual(
            engine._index_dimension(SimpleNamespace(embedding_storage=SimpleNamespace(index=SimpleNamespace(d=1024)))),
            (1024, "SimpleNamespace.d"),
        )
        self.assertEqual(engine._index_dimension(SimpleNamespace()), (None, "unknown"))

    def test_manifest_dimension_mismatch_is_rejected_when_expected_dimension_given(self):
        engine = self._engine()
        index_path = engine._new_vector_index_path(4)
        index_path.write_bytes(b"placeholder")
        engine._publish_vector_index_manifest(index_path, ["embedding-v2"], dimension=4096)
        with patch.object(engine, "_index_file_dimension", return_value=(4096, "fake.index.d")):
            self.assertIsNone(engine._load_published_vector_index(["embedding-v2"], expected_dimension=1024))
        self.assertEqual(engine._vector_dimension_check_status, "mismatch")
        self.assertEqual(engine._vector_dimension_mismatch_total, 1)

    def test_manifest_dimension_is_not_trusted_when_physical_dimension_cannot_be_read(self):
        engine = self._engine()
        index_path = engine._new_vector_index_path(5)
        index_path.write_bytes(b"placeholder")
        engine._publish_vector_index_manifest(index_path, ["embedding-v2"], dimension=1024)
        with patch.object(engine, "_index_file_dimension", return_value=(None, "unknown")):
            self.assertIsNone(engine._load_published_vector_index(["embedding-v2"], expected_dimension=1024))
        self.assertEqual(engine._vector_dimension_check_status, "unknown")

    def test_dimension_probe_is_single_flight_and_validates_vector(self):
        engine = self._engine()
        calls = []

        class Provider:
            id = "embedding-v2"

            async def get_embedding(self, text):
                calls.append(text)
                await asyncio.sleep(0)
                return [0.1, 0.2, 0.3]

        async def run():
            provider = Provider()
            first, second = await asyncio.gather(
                engine._probe_embedding_dimension(provider, "embedding-v2"),
                engine._probe_embedding_dimension(provider, "embedding-v2"),
            )
            return first, second

        first, second = asyncio.run(run())
        self.assertEqual(len(calls), 1)
        self.assertEqual(first["query_dimension"], 3)
        self.assertEqual(second["query_dimension"], 3)

    def test_dimension_probe_failure_is_not_cached(self):
        engine = self._engine()

        class Provider:
            id = "embedding-v2"

            def __init__(self):
                self.calls = 0

            async def get_embedding(self, text):
                self.calls += 1
                if self.calls <= 2:
                    raise RuntimeError("temporary outage")
                return [0.1, 0.2, 0.3, 0.4]

        async def run():
            provider = Provider()
            first = await engine._probe_embedding_dimension(provider, "embedding-v2")
            second = await engine._probe_embedding_dimension(provider, "embedding-v2")
            return provider, first, second

        provider, first, second = asyncio.run(run())
        self.assertEqual(first["dimension_probe_status"], "failed")
        self.assertEqual(second["dimension_probe_status"], "ok")
        self.assertEqual(second["query_dimension"], 4)
        self.assertGreaterEqual(provider.calls, 2)

    def test_probe_supports_legacy_embedding_method_shapes(self):
        engine = self._engine()

        class Provider:
            id = "embedding-v2"

            def embedding(self, text):
                if isinstance(text, list):
                    raise TypeError("single text expected")
                return [1.0, 2.0]

        result = asyncio.run(engine._probe_embedding_dimension(Provider(), "embedding-v2"))
        self.assertEqual(result["dimension_probe_status"], "ok")
        self.assertEqual(result["query_dimension"], 2)

    def test_vector_retriever_rejects_mismatch_without_opening_circuit(self):
        db = SimpleNamespace(
            embedding_storage=SimpleNamespace(index=SimpleNamespace(d=4)),
            document_storage=SimpleNamespace(),
        )
        retriever = self.vector_mod.VectorRetriever(db)
        with self.assertRaises(self.vector_mod._VectorDimensionMismatch):
            retriever._validate_vector_dimension(np.ones(3, dtype=np.float32))
        self.assertEqual(retriever.describe_status()["dimension_mismatch_total"], 1)
        self.assertEqual(retriever.describe_status()["failure_count"], 0)

    def test_vector_retriever_rejects_unknown_dimension_fail_closed(self):
        db = SimpleNamespace(
            embedding_storage=SimpleNamespace(index=SimpleNamespace()),
            document_storage=SimpleNamespace(),
        )
        retriever = self.vector_mod.VectorRetriever(db)
        with self.assertRaises(self.vector_mod._VectorDimensionUnknown):
            retriever._validate_vector_dimension(np.ones(3, dtype=np.float32))
        status = retriever.describe_status()
        self.assertEqual(status["dimension_unknown_total"], 1)
        self.assertEqual(status["failure_count"], 0)

    def test_legacy_retrieve_preflights_embedding_dimension(self):
        calls = []

        class Provider:
            async def get_embedding(self, _text):
                return [0.1, 0.2, 0.3]

        class LegacyFaiss:
            embedding_provider = Provider()
            embedding_storage = SimpleNamespace(index=SimpleNamespace(d=4))

            async def retrieve(self, **_kwargs):
                calls.append("retrieve")
                return []

        observation = {}
        asyncio.run(self.vector_mod.VectorRetriever(LegacyFaiss()).search("hello", observation=observation))
        self.assertEqual(calls, [])
        self.assertEqual(observation["status"], "dimension_mismatch")
        self.assertEqual(observation["fallback_source"], "canonical_fts")

    def test_probe_is_rejected_after_shutdown_without_cache_write(self):
        engine = self._engine()

        class Provider:
            async def get_embedding(self, _text):
                await asyncio.sleep(1)
                return [0.1, 0.2]

        async def run():
            task = asyncio.create_task(engine._probe_embedding_dimension(Provider(), "embedding-v2"))
            await asyncio.sleep(0)
            engine.begin_shutdown()
            try:
                await task
            except asyncio.CancelledError:
                pass

        asyncio.run(run())
        self.assertEqual(engine._vector_dimension_probe_cache, {})

    def test_embedding_invocation_uses_one_total_timeout_budget(self):
        from astrmai.memory.retrieval.embedding import invoke_embedding

        class Provider:
            def get_embeddings(self, _payload):
                time.sleep(0.2)
                return [0.1, 0.2]

        async def run():
            started = time.monotonic()
            with self.assertRaises(RuntimeError):
                await invoke_embedding(Provider(), "hello", timeout_sec=0.05)
            return time.monotonic() - started

        elapsed = asyncio.run(run())
        self.assertLess(elapsed, 0.15)

    def test_runtime_dimension_fields_keep_configured_separate(self):
        engine = self._engine()
        engine._configured_vector_dimension = 1536
        engine._vector_query_dimension = 1024
        engine.faiss_db = SimpleNamespace(embedding_storage=SimpleNamespace(index=SimpleNamespace(d=1024)))
        status = engine.describe_vector_status()
        self.assertEqual(status["configured_dimension"], 1536)
        self.assertEqual(status["measured_query_dimension"], 1024)
        self.assertEqual(status["physical_index_dimension"], 1024)

    def test_api_base_is_trimmed_and_invalid_values_are_not_fingerprinted(self):
        engine = self._engine()
        self.assertEqual(
            engine._normalize_api_base("\t https://embedding.example/v1/ "),
            "https://embedding.example/v1",
        )
        self.assertEqual(engine._normalize_api_base("not a url"), "")
