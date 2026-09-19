import asyncio
from types import SimpleNamespace

from config import AstrMaiConfig
from astrmai.learning.mining.expression_miner import ExpressionMiner
from astrmai.learning.mining.expression_pattern_enricher import ExpressionPatternEnricher
from astrmai.learning.mining.jargon_enricher import JargonEnricher
from astrmai.learning.mining.jargon_miner import JargonMiner


class _Gateway:
    def __init__(self, config):
        self.config = config
        self.calls = 0

    async def call_data_process_task(self, **_kwargs):
        self.calls += 1
        return {"items": []}

    async def call_data_process_task_result(self, **_kwargs):
        self.calls += 1
        return {"items": []}


def test_expression_discovery_short_circuit_has_zero_provider_calls():
    config = AstrMaiConfig()
    gateway = _Gateway(config)
    miner = ExpressionMiner(gateway, config=config)

    result = asyncio.run(
        miner.mine(
            "group-1",
            [SimpleNamespace(id=1, content="hello", sender_id="user-1")],
        )
    )

    assert result == []
    assert gateway.calls == 0
    assert miner.last_report["discovery_provider_call_count"] == 0
    assert miner.last_report["pipeline_contains_enrichment"] is False


def test_jargon_discovery_short_circuit_has_zero_provider_calls():
    config = AstrMaiConfig()
    gateway = _Gateway(config)
    expression = ExpressionMiner(gateway, config=config)
    miner = JargonMiner(expression, min_messages=2)

    result = asyncio.run(
        miner.mine(
            "group-1",
            [SimpleNamespace(id=1, content="hello", sender_id="user-1")],
        )
    )

    assert result == []
    assert gateway.calls == 0
    assert miner.last_report["discovery_provider_call_count"] == 0
    assert miner.last_report["pipeline_contains_enrichment"] is False


def test_provider_enrichment_is_fail_closed_when_feature_flag_is_missing_or_false():
    config = AstrMaiConfig()
    gateway = _Gateway(config)
    adapter = object()
    expression = ExpressionPatternEnricher(
        gateway, config=config, provider_adapter=adapter
    )
    jargon = JargonEnricher(gateway, config=config, provider_adapter=adapter)

    expression_result = asyncio.run(
        expression.enrich(
            "group-1",
            [{"candidate_id": "candidate-1", "expression": "hello"}],
        )
    )
    jargon_result = asyncio.run(
        jargon.enrich(
            "group-1",
            [{"content": "term", "candidate_id": "candidate-2"}],
        )
    )

    assert config.evolution.learning_enrichment_enabled is False
    assert expression_result.status == "blocked"
    assert expression_result.reason == "learning_enrichment_disabled"
    assert jargon_result.status == "blocked"
    assert jargon_result.reason == "learning_enrichment_disabled"
    assert gateway.calls == 0


def test_formal_provider_failure_cannot_become_expression_fallback_success():
    config = AstrMaiConfig()
    config.evolution.learning_enrichment_enabled = True
    gateway = _Gateway(config)

    class _FailedAdapter:
        async def call(self, **_kwargs):
            from astrmai.learning.runtime.provider_adapter import LearningProviderAttemptResult

            return LearningProviderAttemptResult(
                ok=False,
                failure_stage="circuit",
                failure_kind="circuit_open",
                retryable=True,
            )

    enricher = ExpressionPatternEnricher(
        gateway, config=config, provider_adapter=_FailedAdapter()
    )
    result = asyncio.run(
        enricher.enrich(
            "group-1",
            [
                {
                    "candidate_id": "candidate-1",
                    "expression": "hello",
                    "candidate_type": "exact",
                    "count": 3,
                    "distinct_turn_count": 2,
                    "content_kind": "expression",
                }
            ],
        )
    )

    assert result.status != "completed_fallback"
    assert result.retryable is True
    assert result.items == []


def test_formal_enrichers_never_invoke_legacy_gateway_api():
    config = AstrMaiConfig()
    config.evolution.learning_enrichment_enabled = True

    class _LegacyOnlyGateway:
        def __init__(self):
            self.config = config
            self.calls = 0

        async def call_data_process_task(self, **_kwargs):
            self.calls += 1
            return {"items": []}

    gateway = _LegacyOnlyGateway()
    expression = ExpressionPatternEnricher(
        gateway,
        config=config,
        provider_adapter=object(),
    )
    jargon = JargonEnricher(
        gateway,
        config=config,
        provider_adapter=object(),
    )

    expression_result = asyncio.run(
        expression.enrich(
            "group-1",
            [{"candidate_id": "candidate-1", "expression": "hello"}],
        )
    )
    jargon_result = asyncio.run(
        jargon.enrich(
            "group-1",
            [{"candidate_id": "candidate-2", "content": "term"}],
        )
    )

    assert expression_result.status == "provider_error"
    assert jargon_result.status == "provider_failure"
    assert gateway.calls == 0
