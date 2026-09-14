from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from astrmai.memory.services.memory_engine import MemoryEngine


def _engine(embedding_models: list[str] | None = None) -> MemoryEngine:
    config = SimpleNamespace(
        provider=SimpleNamespace(embedding_models=list(embedding_models or [])),
        memory=SimpleNamespace(recall_top_k=5),
    )
    context = SimpleNamespace(get_all_embedding_providers=lambda: [])
    gateway = SimpleNamespace(config=config)
    return MemoryEngine(context, gateway, embedding_models=list(embedding_models or []), config=config)


def test_unconfigured_bootstrap_is_aggregated_warn_without_traceback():
    engine = _engine()
    logger = MagicMock()

    with patch("astrmai.memory.services.memory_engine.logger", logger):
        engine._mark_vector_bootstrap_failed(
            RuntimeError("no valid embedding model found [unconfigured]"),
            include_trace=True,
        )

    logger.warning.assert_called_once()
    logger.error.assert_not_called()
    assert logger.warning.call_args.kwargs == {}
    message = logger.warning.call_args.args[0]
    assert "reason=provider_unconfigured" in message
    assert engine.describe_vector_status()["state"] == "degraded"
    assert engine.describe_vector_status()["failure_kind"] == "provider_unconfigured"


def test_unconfigured_retry_logs_are_deduplicated_without_traceback():
    engine = _engine()
    logger = MagicMock()
    exc = RuntimeError("no embedding model or default provider configured")

    with patch("astrmai.memory.services.memory_engine.logger", logger):
        engine._mark_vector_bootstrap_failed(exc, include_trace=True)
        engine._mark_vector_bootstrap_failed(exc, include_trace=True)

    assert logger.error.call_count == 0
    assert logger.warning.call_count == 1
    assert logger.debug.call_count == 1
    assert "repeated=1" in logger.warning.call_args.args[0]
    assert "repeated=2" in logger.debug.call_args.args[0]
    assert engine.describe_vector_status()["failure_log_count"] == 2


def test_provider_request_failure_remains_error_with_traceback():
    engine = _engine(["embed-v1"])
    logger = MagicMock()

    with patch("astrmai.memory.services.memory_engine.logger", logger):
        engine._mark_vector_bootstrap_failed(
            RuntimeError("embedding provider request failed: HTTP 503"),
            include_trace=True,
        )

    logger.error.assert_called_once()
    logger.warning.assert_not_called()
    assert logger.error.call_args.kwargs["exc_info"] is True
    assert "reason=provider_request_failed" in logger.error.call_args.args[0]
    assert engine.describe_vector_status()["failure_kind"] == "provider_request_failed"


def test_dimension_failure_remains_error_and_is_classified():
    engine = _engine(["embed-v1"])
    logger = MagicMock()

    with patch("astrmai.memory.services.memory_engine.logger", logger):
        engine._mark_vector_bootstrap_failed(
            RuntimeError("dimension probe failed: dimension mismatch"),
            include_trace=True,
        )

    logger.error.assert_called_once()
    assert "reason=identity/dimension_mismatch" in logger.error.call_args.args[0]
    assert engine.describe_vector_status()["failure_kind"] == "identity/dimension_mismatch"


def test_unconfigured_bootstrap_does_not_call_embedding_provider():
    calls: list[str] = []

    class Context:
        def get_all_embedding_providers(self):
            calls.append("registry")
            return []

    config = SimpleNamespace(
        provider=SimpleNamespace(embedding_models=[]),
        memory=SimpleNamespace(recall_top_k=5),
    )
    engine = MemoryEngine(Context(), SimpleNamespace(config=config), embedding_models=[], config=config)
    with patch.object(engine, "_bootstrap_vector_index", side_effect=AssertionError("must not run")):
        result = asyncio.run(engine._ensure_faiss_initialized())

    assert result is False
    assert calls == ["registry"]
    assert engine.embedding_models == []
