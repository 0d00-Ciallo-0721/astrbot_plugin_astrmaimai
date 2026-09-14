from astrmai.memory.services.memory_vector_reconciliation import (
    reconcile_memory_vector,
    validate_vector_identity,
)
import asyncio
from types import SimpleNamespace
from astrmai.memory.retrieval.embedding import EmbeddingClient


def test_aligned_sets_have_no_current_mismatch():
    report = reconcile_memory_vector(["a", "b"], ["a", "b"], ["a", "b"])
    assert report["aligned"] is True
    assert report["records"] == []


def test_mismatch_sets_are_explicit_and_history_is_classified():
    report = reconcile_memory_vector(
        ["a", "b"], ["a", "c"], ["a"], current_generation=3,
        repair_rows=[
            {"memory_id": "old", "mismatch_kind": "missing_faiss", "generation": 2, "status": "pending"},
            {"memory_id": "retry", "mismatch_kind": "missing_documents", "generation": 3, "status": "retry_wait"},
            {"memory_id": "blocked", "generation": 3, "status": "blocked"},
        ],
    )
    assert report["missing_documents"] == ["b"]
    assert report["orphan_documents"] == ["c"]
    assert report["missing_faiss"] == ["c"]
    assert report["stale_count"] == 1
    assert report["retryable_count"] == 1
    assert report["blocked_count"] == 1


def test_identity_validation_is_fail_closed():
    assert validate_vector_identity({"embedding_model": "m", "provider_source": "p", "physical_dimension": 1024, "vector_count": 2, "generation": 1, "revision": 1})["publish_allowed"]
    invalid = validate_vector_identity({"embedding_model": "", "provider_source": None, "physical_dimension": "bad"})
    assert invalid["status"] == "blocked"
    assert invalid["publish_allowed"] is False
    assert "embedding_model" in invalid["missing"]
    assert "physical_dimension:invalid" in invalid["errors"]


def test_unconfigured_embedding_does_not_invoke_provider():
    calls = []

    class _Context:
        def get_all_embedding_providers(self):
            calls.append("registry")
            return []

    client = EmbeddingClient(_Context(), embedding_models=[], config=SimpleNamespace(timing=SimpleNamespace(embedding_timeout_sec=0.1)))
    assert asyncio.run(client.get_vector("offline")) is None
    assert calls == ["registry"]
