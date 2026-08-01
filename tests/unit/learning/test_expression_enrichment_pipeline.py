import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from config import AstrMaiConfig
from astrmai.learning.evolution_manager import EvolutionManager
from astrmai.learning.mining.expression_candidate_extractor import ExpressionCandidateExtractor
from astrmai.learning.mining.expression_pattern_enricher import ExpressionPatternEnricher
from astrmai.memory.services.expression_pattern_service import ExpressionPatternService
from astrmai.memory.services.memory_write_service import MemoryWriteService
from astrmai.memory.services.v2_store import MemoryV2Store


def _candidate(candidate_id: str, *, candidate_type: str = "exact", count: int = 2):
    return {
        "candidate_id": candidate_id,
        "candidate_type": candidate_type,
        "expression": "唉嘿嘿",
        "situation": "轻松回应",
        "style": "轻松",
        "content_samples": ["唉嘿嘿"],
        "evidence_message_ids": ["1", "2", "3"][:count],
        "count": count,
        "activation_score": 0.72,
    }


class _Gateway:
    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0

    async def call_data_process_task(self, **kwargs):
        self.calls += 1
        result = self.results[min(self.calls - 1, len(self.results) - 1)]
        if isinstance(result, BaseException):
            raise result
        return result


class _Store:
    def __init__(self):
        self.by_key = {}

    async def get_by_dedup_key(self, key, include_inactive=True):
        return self.by_key.get(key)

    async def resolve_dedup_key(self, key):
        return key


class _WriteService:
    def __init__(self, store):
        self.store = store
        self.calls = []

    async def write(self, request):
        self.calls.append(request)
        memory_id = "mem-expression-1"
        self.store.by_key[request.dedup_key] = SimpleNamespace(
            id=memory_id,
            content=request.content,
            metadata=dict(request.metadata),
        )
        return memory_id


class _BackfillDB:
    def __init__(self, logs):
        self.logs = list(logs)
        self.marked = []
        self.memory_engine = None

    async def get_recent_message_logs_async(self, group_id, **kwargs):
        return list(self.logs)

    async def get_unprocessed_logs_async(self, group_id, limit=999):
        return list(self.logs)[:limit]

    async def mark_logs_processed_async(self, ids):
        self.marked.append(list(ids))


class ExpressionEnrichmentPipelineTests(unittest.TestCase):
    def test_candidates_have_stable_ids_and_evidence_ids(self):
        extractor = ExpressionCandidateExtractor(min_count=2)
        messages = [
            SimpleNamespace(id=11, content="唉嘿嘿"),
            SimpleNamespace(id=12, content="唉嘿嘿"),
        ]

        first = asyncio.run(extractor.extract("chat-1", messages))
        second = asyncio.run(extractor.extract("chat-1", messages))

        self.assertEqual(first[0]["candidate_id"], second[0]["candidate_id"])
        self.assertEqual(first[0]["evidence_message_ids"], ["11", "12"])

    def test_enricher_returns_completed_result_for_full_response(self):
        gateway = _Gateway(
            {
                "items": [
                    {
                        "candidate_id": "expr-1",
                        "decision": "keep",
                        "summary": "轻松地笑",
                        "confidence": 0.9,
                        "review_status": "pending",
                    }
                ]
            }
        )

        result = asyncio.run(ExpressionPatternEnricher(gateway).enrich("chat-1", [_candidate("expr-1")]))

        self.assertEqual(result.status, "completed")
        self.assertTrue(result.terminal)
        self.assertEqual(result.items[0]["summary"], "轻松地笑")

    def test_enricher_distinguishes_all_rejected_from_failure(self):
        gateway = _Gateway(
            {"items": [{"candidate_id": "expr-1", "decision": "reject", "review_status": "rejected"}]}
        )

        result = asyncio.run(ExpressionPatternEnricher(gateway).enrich("chat-1", [_candidate("expr-1")]))

        self.assertEqual(result.status, "all_rejected")
        self.assertTrue(result.terminal)
        self.assertEqual(result.rejected_count, 1)

    def test_partial_response_keeps_missing_candidate_retryable(self):
        response = {
            "items": [
                {
                    "candidate_id": "expr-1",
                    "decision": "keep",
                    "summary": "保留",
                    "review_status": "pending",
                }
            ]
        }
        gateway = _Gateway(response, {"items": []})

        result = asyncio.run(
            ExpressionPatternEnricher(gateway).enrich(
                "chat-1",
                [_candidate("expr-1"), _candidate("expr-2", candidate_type="phrase")],
            )
        )

        self.assertEqual(result.status, "partial")
        self.assertTrue(result.retryable)
        self.assertEqual(result.missing_candidate_ids, ["expr-2"])

    def test_strict_exact_fallback_is_pending_human(self):
        gateway = _Gateway(RuntimeError("offline"))

        result = asyncio.run(
            ExpressionPatternEnricher(gateway).enrich("chat-1", [_candidate("expr-1", count=3)])
        )

        self.assertEqual(result.status, "completed_fallback")
        self.assertTrue(result.terminal)
        self.assertEqual(result.items[0]["review_status"], "pending_human")

    def test_strict_fallback_does_not_persist_topic_like_sentence_pattern(self):
        gateway = _Gateway(RuntimeError("offline"))
        candidate = {
            **_candidate("expr-topic-1", count=3),
            "expression": "OpenAI",
            "habit_type": "sentence_pattern",
            "content_kind": "expression",
        }

        result = asyncio.run(ExpressionPatternEnricher(gateway).enrich("chat-1", [candidate]))

        self.assertEqual(result.items, [])
        self.assertEqual(result.missing_candidate_ids, ["expr-topic-1"])
        self.assertTrue(result.retryable)

    def test_pattern_retry_does_not_inflate_count_or_weight(self):
        store = _Store()
        writer = _WriteService(store)
        service = ExpressionPatternService(store, writer)
        payload = {
            **_candidate("expr-1", count=3),
            "mining_batch_id": "batch-1",
            "review_status": "pending",
            "weight": 0.8,
        }

        first_id = asyncio.run(service.write_pattern("chat-1", payload))
        second_id = asyncio.run(service.write_pattern("chat-1", payload))
        stored = next(iter(store.by_key.values()))

        self.assertEqual(first_id, second_id)
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(stored.metadata["count"], 3)
        self.assertEqual(stored.metadata["weight"], 0.8)

    def test_pattern_write_preserves_speaker_and_habit_metadata(self):
        store = _Store()
        writer = _WriteService(store)
        service = ExpressionPatternService(store, writer)
        payload = {
            **_candidate("expr-style-1", count=3),
            "habit_type": "ending",
            "content_kind": "expression",
            "normalized_pattern": "呀",
            "speaker_id": "10001",
            "speaker_name": "测试用户",
            "scope_kind": "speaker",
            "shared_scope": "chat-1:user:10001",
            "distinct_turn_count": 3,
            "distinct_day_count": 2,
        }

        asyncio.run(service.write_pattern("chat-1", payload))
        stored = next(iter(store.by_key.values()))

        self.assertEqual(stored.metadata["habit_type"], "ending")
        self.assertEqual(stored.metadata["content_kind"], "expression")
        self.assertEqual(stored.metadata["normalized_pattern"], "呀")
        self.assertEqual(stored.metadata["speaker_id"], "10001")
        self.assertEqual(stored.metadata["speaker_name"], "测试用户")
        self.assertEqual(stored.metadata["scope_kind"], "speaker")
        self.assertEqual(stored.metadata["shared_scope"], "chat-1:user:10001")
        self.assertEqual(stored.metadata["distinct_turn_count"], 3)
        self.assertEqual(stored.metadata["distinct_day_count"], 2)

    def test_backfill_dry_run_never_changes_processed_flags(self):
        logs = [SimpleNamespace(id=index, content="唉嘿嘿", sender_name="user") for index in range(1, 4)]
        db = _BackfillDB(logs)
        config = AstrMaiConfig(evolution={"min_mining_context": 2, "expression_min_count": 2})
        manager = EvolutionManager(db, SimpleNamespace(config=config), config=config)

        async def _mine(group_id, messages):
            manager.expression_miner.last_report = {
                "candidate_count": 1,
                "enrichment": {"terminal": True, "status": "completed"},
            }
            return [_candidate("expr-1", count=3)]

        manager.expression_miner.mine = _mine
        result = asyncio.run(manager.run_expression_backfill("chat-1", dry_run=True))

        self.assertEqual(result["status"], "ok")
        self.assertTrue(result["dry_run"])
        self.assertEqual(db.marked, [])
        self.assertFalse(result["processed_flags_changed"])

    def test_backfill_execution_is_idempotent_in_sqlite_store(self):
        async def _run():
            logs = [
                SimpleNamespace(id=index, content="唉嘿嘿", sender_name="user")
                for index in range(1, 4)
            ]
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
                store = MemoryV2Store(
                    str(Path(temp_dir) / "memory.db"),
                    data_path=Path(temp_dir),
                )
                service = ExpressionPatternService(store, MemoryWriteService(store))
                db = _BackfillDB(logs)
                db.memory_engine = SimpleNamespace(
                    expression_pattern_service=service,
                    v2_store=store,
                )
                config = AstrMaiConfig(
                    evolution={"min_mining_context": 2, "expression_min_count": 2}
                )
                manager = EvolutionManager(db, SimpleNamespace(config=config), config=config)

                async def _mine(group_id, messages):
                    manager.expression_miner.last_report = {
                        "candidate_count": 1,
                        "enrichment": {
                            "terminal": True,
                            "retryable": False,
                            "status": "completed",
                        },
                    }
                    return [{**_candidate("expr-1", count=3), "group_id": group_id}]

                manager.expression_miner.mine = _mine
                first = await manager.run_expression_backfill("chat-1", dry_run=False)
                second = await manager.run_expression_backfill("chat-1", dry_run=False)
                dedup_key = service.build_dedup_key(
                    "chat-1",
                    "轻松回应",
                    "唉嘿嘿",
                    "chat-1",
                )
                stored = await store.get_by_dedup_key(dedup_key, include_inactive=True)

                self.assertEqual(first["persistence"]["saved"], 1)
                self.assertEqual(second["persistence"]["deduplicated"], 1)
                self.assertEqual(first["persistence"]["memory_ids"], second["persistence"]["memory_ids"])
                self.assertEqual(stored.metadata["count"], 3)
                self.assertEqual(len(stored.metadata["applied_mining_batch_ids"]), 1)
                self.assertEqual(stored.status, "review_pending")
                self.assertEqual(stored.visibility, "maintenance_only")
                self.assertEqual(db.marked, [])

        asyncio.run(_run())

    def test_incomplete_enrichment_does_not_consume_logs(self):
        logs = [SimpleNamespace(id=1, content="唉嘿嘿", sender_name="user")]
        db = _BackfillDB(logs)
        config = AstrMaiConfig(evolution={"min_mining_context": 1})
        manager = EvolutionManager(db, SimpleNamespace(config=config), config=config)

        async def _mine(group_id, messages):
            manager.expression_miner.last_report = {
                "candidate_count": 1,
                "enrichment": {"terminal": False, "retryable": True, "status": "partial"},
            }
            return []

        manager.expression_miner.mine = _mine

        with self.assertRaisesRegex(RuntimeError, "durable terminal state"):
            asyncio.run(manager.process_logs_and_mine("chat-1", logs))
        self.assertEqual(db.marked, [])
        self.assertTrue(manager._last_mining_outcomes["chat-1"]["retryable"])

    def test_jargon_all_rejected_is_terminal_and_consumes_logs(self):
        logs = [SimpleNamespace(id=1, content="ordinary phrase", sender_name="user")]
        db = _BackfillDB(logs)
        db.memory_engine = SimpleNamespace(write_service=SimpleNamespace())
        config = AstrMaiConfig(evolution={"min_mining_context": 1})
        manager = EvolutionManager(db, SimpleNamespace(config=config), config=config)

        async def _mine_expressions(group_id, messages):
            manager.expression_miner.last_report = {
                "candidate_count": 0,
                "enrichment": {"terminal": True, "retryable": False, "status": "completed"},
            }
            return []

        async def _mine_jargons(group_id, messages):
            manager.jargon_miner.last_report = {
                "candidate_count": 1,
                "reason": "model_rejected_all_candidates",
                "enrichment": {
                    "terminal": True,
                    "retryable": False,
                    "status": "all_rejected",
                    "rejected_count": 1,
                },
            }
            return []

        manager.expression_miner.mine = _mine_expressions
        manager.jargon_miner.mine = _mine_jargons

        asyncio.run(manager.process_logs_and_mine("chat-1", logs))

        self.assertEqual(db.marked, [[1]])
        outcome = manager._last_mining_outcomes["chat-1"]
        self.assertEqual(outcome["status"], "completed")
        self.assertEqual(outcome["jargon"]["enrichment"]["status"], "all_rejected")

    def test_jargon_provider_failure_does_not_consume_logs(self):
        logs = [SimpleNamespace(id=1, content="candidate phrase", sender_name="user")]
        db = _BackfillDB(logs)
        db.memory_engine = SimpleNamespace(write_service=SimpleNamespace())
        config = AstrMaiConfig(evolution={"min_mining_context": 1})
        manager = EvolutionManager(db, SimpleNamespace(config=config), config=config)

        async def _mine_expressions(group_id, messages):
            manager.expression_miner.last_report = {
                "candidate_count": 0,
                "enrichment": {"terminal": True, "retryable": False, "status": "completed"},
            }
            return []

        async def _mine_jargons(group_id, messages):
            manager.jargon_miner.last_report = {
                "candidate_count": 1,
                "reason": "gateway_call_failed",
                "enrichment": {
                    "terminal": False,
                    "retryable": True,
                    "status": "provider_failure",
                    "error_type": "RuntimeError",
                },
            }
            return []

        manager.expression_miner.mine = _mine_expressions
        manager.jargon_miner.mine = _mine_jargons

        with self.assertRaisesRegex(RuntimeError, "jargon enrichment failed closed"):
            asyncio.run(manager.process_logs_and_mine("chat-1", logs))

        self.assertEqual(db.marked, [])
        outcome = manager._last_mining_outcomes["chat-1"]
        self.assertTrue(outcome["retryable"])
        self.assertEqual(outcome["jargon"]["enrichment"]["status"], "provider_failure")

    def test_empty_persistence_result_does_not_consume_logs(self):
        class _PatternStore:
            async def get_by_dedup_key(self, key, include_inactive=True):
                return None

        class _PatternService:
            store = _PatternStore()

            @staticmethod
            def build_dedup_key(group_id, situation, expression, shared_scope=""):
                return f"{group_id}:{situation}:{expression}:{shared_scope}"

            async def write_pattern(self, group_id, payload, source=""):
                return ""

        logs = [SimpleNamespace(id=1, content="唉嘿嘿", sender_name="user")]
        db = _BackfillDB(logs)
        db.memory_engine = SimpleNamespace(expression_pattern_service=_PatternService())
        config = AstrMaiConfig(evolution={"min_mining_context": 1})
        manager = EvolutionManager(db, SimpleNamespace(config=config), config=config)

        async def _mine(group_id, messages):
            manager.expression_miner.last_report = {
                "candidate_count": 1,
                "enrichment": {"terminal": True, "retryable": False, "status": "completed"},
            }
            return [{**_candidate("expr-1"), "group_id": group_id}]

        manager.expression_miner.mine = _mine

        with self.assertRaisesRegex(RuntimeError, "durable terminal state"):
            asyncio.run(manager.process_logs_and_mine("chat-1", logs))
        self.assertEqual(db.marked, [])
        self.assertEqual(manager._last_mining_outcomes["chat-1"]["persistence"]["failed"], 1)


if __name__ == "__main__":
    unittest.main()
