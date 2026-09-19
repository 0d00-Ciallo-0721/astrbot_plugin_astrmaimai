import asyncio
import json
import sqlite3
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
    _learning_legacy_provider_test_double = True

    def __init__(self, *results):
        self.results = list(results)
        self.calls = 0
        self.requests = []

    async def call_data_process_task(self, **kwargs):
        self.calls += 1
        self.requests.append(dict(kwargs))
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
    def test_extractor_rejects_topic_terms_and_plain_long_sentences(self):
        extractor = ExpressionCandidateExtractor(min_count=2)
        messages = [
            SimpleNamespace(id=1, content="原生家庭", sender_id="1"),
            SimpleNamespace(id=2, content="原生家庭", sender_id="2"),
            SimpleNamespace(id=3, content="锂电池", sender_id="1"),
            SimpleNamespace(id=4, content="锂电池", sender_id="2"),
            SimpleNamespace(id=5, content="这个问题我们明天再继续认真讨论呀", sender_id="1"),
            SimpleNamespace(id=6, content="这个问题我们明天再继续认真讨论呀", sender_id="2"),
            SimpleNamespace(id=7, content="唉嘿嘿", sender_id="1"),
            SimpleNamespace(id=8, content="唉嘿嘿", sender_id="2"),
        ]

        candidates = asyncio.run(extractor.extract("chat-1", messages))

        expressions = {item["expression"] for item in candidates}
        self.assertIn("唉嘿嘿", expressions)
        self.assertNotIn("原生家庭", expressions)
        self.assertNotIn("锂电池", expressions)
        self.assertNotIn("这个问题我们明天再继续认真讨论呀", expressions)
        self.assertEqual(extractor.last_report["quality_filtered"], 2)
        self.assertEqual(
            extractor.last_report["quality_filter_reasons"],
            {"plain_sentence_with_terminal_particle": 2},
        )

    def test_render_active_patterns_requests_approved_group_patterns_only(self):
        store = _Store()
        service = ExpressionPatternService(store, _WriteService(store))
        captured = {}

        async def _list_patterns(group_id, **kwargs):
            captured.update({"group_id": group_id, **kwargs})
            return [SimpleNamespace(situation="接话", expression="唉嘿嘿")]

        service.list_patterns = _list_patterns
        rendered = asyncio.run(service.render_active_patterns("chat-1"))

        self.assertIn("唉嘿嘿", rendered)
        self.assertEqual(captured["group_id"], "chat-1")
        self.assertTrue(captured["only_checked"])
        self.assertEqual(captured["review_status"], "approved")
        self.assertEqual(captured["statuses"], ["active"])
        self.assertFalse(captured["include_rejected"])

    def test_candidates_have_stable_ids_without_personal_evidence(self):
        extractor = ExpressionCandidateExtractor(min_count=2)
        messages = [
            SimpleNamespace(id=11, sender_id="alice", content="唉嘿嘿"),
            SimpleNamespace(id=12, sender_id="bob", content="唉嘿嘿"),
        ]

        first = asyncio.run(extractor.extract("chat-1", messages))
        second = asyncio.run(extractor.extract("chat-1", messages))

        self.assertEqual(first[0]["candidate_id"], second[0]["candidate_id"])
        self.assertNotIn("evidence_message_ids", first[0])
        self.assertNotIn("speaker_id", first[0])

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

    def test_enricher_prompt_describes_group_style_without_personal_evidence(self):
        gateway = _Gateway(
            {
                "items": [
                    {
                        "candidate_id": "expr-1",
                        "decision": "keep",
                        "summary": "群内常用笑声",
                        "review_status": "pending",
                    }
                ]
            }
        )
        candidate = {**_candidate("expr-1"), "speaker_id": "10001", "speaker_name": "Alice"}

        asyncio.run(ExpressionPatternEnricher(gateway).enrich("chat-1", [candidate]))

        prompt = gateway.requests[0]["prompt"]
        self.assertIn("当前群聊中反复出现", prompt)
        self.assertNotIn("某一位群友", prompt)
        self.assertNotIn('"speaker_id"', prompt)
        self.assertNotIn('"speaker_name"', prompt)

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

    def test_stale_candidate_revision_cannot_overwrite_new_canonical_metadata(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            class _Projector:
                def __init__(self):
                    self.calls = 0

                async def project(self, **_kwargs):
                    self.calls += 1
                    return True

                async def cleanup_deleted(self, _ids):
                    return None

            store = MemoryV2Store(
                str(Path(temp_dir) / "memory.db"), data_path=Path(temp_dir)
            )
            projector = _Projector()
            service = ExpressionPatternService(
                store, MemoryWriteService(store, projector)
            )
            new_payload = {
                **_candidate("candidate-1", count=2),
                "mining_batch_id": "candidate-persist:new",
                "candidate_revision": 4,
                "evidence_digest": "new-digest",
                "source_message_ids": ["m-1", "m-2"],
                "support_count": 2,
            }
            old_payload = {
                **_candidate("candidate-1", count=1),
                "mining_batch_id": "candidate-persist:old",
                "candidate_revision": 2,
                "evidence_digest": "old-digest",
                "source_message_ids": ["m-1"],
                "support_count": 1,
            }

            memory_id = asyncio.run(service.write_pattern("chat-1", new_payload))
            replayed_id = asyncio.run(service.write_pattern("chat-1", new_payload))
            self.assertEqual(replayed_id, memory_id)
            advanced_id = asyncio.run(
                service.write_pattern(
                    "chat-1", {**new_payload, "candidate_revision": 5}
                )
            )
            self.assertEqual(advanced_id, memory_id)
            self.assertEqual(projector.calls, 1)
            before = asyncio.run(store.get_canonical(memory_id, include_inactive=True))
            with self.assertRaisesRegex(RuntimeError, "candidate_revision_conflict"):
                asyncio.run(service.write_pattern("chat-1", old_payload))
            after = asyncio.run(store.get_canonical(memory_id, include_inactive=True))

            self.assertEqual(after.id, before.id)
            self.assertEqual(after.metadata["count"], 2)
            self.assertEqual(after.metadata["support_count"], 2)
            self.assertEqual(after.metadata["evidence_digest"], "new-digest")
            self.assertEqual(after.metadata["candidate_revision"], 5)
            self.assertEqual(
                after.metadata["applied_mining_batch_ids"],
                ["candidate-persist:new"],
            )

    def test_stale_jargon_candidate_revision_is_reported_without_overwrite(self):
        async def _run():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
                store = MemoryV2Store(
                    str(Path(temp_dir) / "memory.db"), data_path=Path(temp_dir)
                )
                writer = MemoryWriteService(store)
                manager = object.__new__(EvolutionManager)
                manager.db = SimpleNamespace(
                    memory_engine=SimpleNamespace(v2_store=store, write_service=writer)
                )
                base = {
                    "candidate_id": "jargon-candidate-1",
                    "content": "脱敏词",
                    "canonical_form": "脱敏词",
                    "confidence": 0.9,
                    "activation_score": 0.8,
                    "is_jargon": True,
                    "review_status": "review_pending",
                    "source_message_ids": ["m-1", "m-2"],
                    "supported_by": ["m-1", "m-2"],
                    "count": 2,
                }
                current = await manager._save_jargons(
                    "chat-1",
                    [{**base, "meaning": "new meaning", "evidence_digest": "new-digest"}],
                    mining_batch_id="candidate-persist:new",
                    candidate_id="jargon-candidate-1",
                    candidate_revision=4,
                    candidate_persistence_id="candidate-persist:new",
                )
                stored = await store.get_canonical(
                    current.memory_ids[0], include_inactive=True
                )
                before = dict(stored.metadata)
                stale = await manager._save_jargons(
                    "chat-1",
                    [{**base, "meaning": "old meaning", "evidence_digest": "old-digest"}],
                    mining_batch_id="candidate-persist:old",
                    candidate_id="jargon-candidate-1",
                    candidate_revision=2,
                    candidate_persistence_id="candidate-persist:old",
                )
                after = await store.get_canonical(
                    current.memory_ids[0], include_inactive=True
                )

                self.assertTrue(current.complete)
                self.assertFalse(stale.complete)
                self.assertEqual(stale.failures[0].failure_kind, "candidate_revision_conflict")
                self.assertTrue(stale.failures[0].retryable)
                self.assertEqual(after.metadata, before)

        asyncio.run(_run())

    def test_concurrent_first_candidate_write_is_globally_idempotent(self):
        async def _run():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
                for round_index in range(30):
                    round_dir = Path(temp_dir) / f"round-{round_index}"
                    round_dir.mkdir()
                    db_path = str(round_dir / "memory.db")
                    candidate_id = f"candidate-concurrent-{round_index}"
                    persistence_id = f"candidate-persist:concurrent-{round_index}"
                    first_store = MemoryV2Store(db_path, data_path=round_dir)
                    second_store = MemoryV2Store(db_path, data_path=round_dir)
                    first = ExpressionPatternService(
                        first_store, MemoryWriteService(first_store)
                    )
                    second = ExpressionPatternService(
                        second_store, MemoryWriteService(second_store)
                    )
                    payload = {
                        **_candidate(candidate_id, count=2),
                        "mining_batch_id": persistence_id,
                        "candidate_revision": 1,
                        "source_message_ids": ["m-1", "m-2"],
                        "evidence_digest": "concurrent-digest",
                    }

                    memory_ids = await asyncio.gather(
                        first.write_pattern("chat-1", payload),
                        second.write_pattern("chat-1", payload),
                    )

                    self.assertEqual(memory_ids[0], memory_ids[1])
                    with sqlite3.connect(db_path) as db:
                        canonical_count = db.execute(
                            "SELECT COUNT(*) FROM canonical_memories WHERE dedup_key <> ''"
                        ).fetchone()[0]
                        fence = db.execute(
                            """
                            SELECT revision, persistence_id, canonical_memory_id
                            FROM memory_candidate_revision_fence
                            WHERE candidate_id = ?
                            """,
                            (candidate_id,),
                        ).fetchone()
                    self.assertEqual(canonical_count, 1)
                    self.assertEqual(
                        fence,
                        (1, persistence_id, memory_ids[0]),
                    )

        asyncio.run(_run())

    def test_v2_candidate_fence_backfill_rejects_ambiguous_highest_revision(self):
        async def _run():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
                db_path = str(Path(temp_dir) / "memory.db")
                store = MemoryV2Store(db_path, data_path=Path(temp_dir))
                service = ExpressionPatternService(store, MemoryWriteService(store))
                first_id = await service.write_pattern(
                    "chat-1",
                    {
                        **_candidate("candidate-a", count=2),
                        "expression": "canonical a",
                        "mining_batch_id": "persist-a",
                        "candidate_revision": 1,
                    },
                )
                second_id = await service.write_pattern(
                    "chat-1",
                    {
                        **_candidate("candidate-b", count=2),
                        "expression": "canonical b",
                        "mining_batch_id": "persist-b",
                        "candidate_revision": 1,
                    },
                )
                with sqlite3.connect(db_path) as db:
                    for memory_id, persistence_id in (
                        (first_id, "persist-a"),
                        (second_id, "persist-b"),
                    ):
                        metadata = json.loads(
                            db.execute(
                                "SELECT metadata FROM canonical_memories WHERE id = ?",
                                (memory_id,),
                            ).fetchone()[0]
                        )
                        metadata.update(
                            {
                                "candidate_id": "candidate-conflict",
                                "candidate_revision": 7,
                                "candidate_persistence_id": persistence_id,
                                "candidate_revision_fences": {
                                    "candidate-conflict": {
                                        "revision": 7,
                                        "persistence_id": persistence_id,
                                    }
                                },
                            }
                        )
                        db.execute(
                            "UPDATE canonical_memories SET metadata = ? WHERE id = ?",
                            (json.dumps(metadata), memory_id),
                        )
                    db.execute("DROP TABLE memory_candidate_revision_fence")
                    db.execute(
                        "UPDATE memory_v2_meta SET value = '2' "
                        "WHERE key = 'schema_version'"
                    )
                    db.execute(
                        "DELETE FROM memory_v2_migrations WHERE version = '3'"
                    )
                    db.commit()

                recovered_store = MemoryV2Store(
                    db_path, data_path=Path(temp_dir)
                )
                with self.assertRaisesRegex(
                    RuntimeError, "candidate_fence_backfill_conflict"
                ):
                    await recovered_store.initialize()

                with sqlite3.connect(db_path) as db:
                    schema_version = db.execute(
                        "SELECT value FROM memory_v2_meta "
                        "WHERE key = 'schema_version'"
                    ).fetchone()[0]
                    migration_v3 = db.execute(
                        "SELECT status FROM memory_v2_migrations WHERE version = '3'"
                    ).fetchone()
                    canonical_rows = db.execute(
                        "SELECT id, status FROM canonical_memories ORDER BY id"
                    ).fetchall()
                    fence_table = db.execute(
                        "SELECT 1 FROM sqlite_master "
                        "WHERE type = 'table' "
                        "AND name = 'memory_candidate_revision_fence'"
                    ).fetchone()

                self.assertEqual(schema_version, "2")
                self.assertIsNone(migration_v3)
                self.assertIsNone(fence_table)
                self.assertEqual(
                    {row[0] for row in canonical_rows}, {first_id, second_id}
                )
                self.assertEqual(
                    {row[1] for row in canonical_rows}, {"review_pending"}
                )

        asyncio.run(_run())

    def test_candidate_revision_fence_rejects_stale_cross_key_and_migrates_newer(self):
        async def _run():
            with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
                db_path = str(Path(temp_dir) / "memory.db")
                store = MemoryV2Store(db_path, data_path=Path(temp_dir))
                service = ExpressionPatternService(store, MemoryWriteService(store))
                current_payload = {
                    **_candidate("candidate-cross-key", count=2),
                    "expression": "new canonical form",
                    "mining_batch_id": "candidate-persist:new-key",
                    "candidate_revision": 5,
                    "source_message_ids": ["m-1", "m-2"],
                    "evidence_digest": "new-key-digest",
                }
                stale_payload = {
                    **_candidate("candidate-cross-key", count=1),
                    "expression": "old canonical form",
                    "mining_batch_id": "candidate-persist:old-key",
                    "candidate_revision": 2,
                    "source_message_ids": ["m-1"],
                    "evidence_digest": "old-key-digest",
                }

                current_id = await service.write_pattern("chat-1", current_payload)
                with sqlite3.connect(db_path) as db:
                    db.execute(
                        "DELETE FROM memory_candidate_revision_fence "
                        "WHERE candidate_id = 'candidate-cross-key'"
                    )
                    db.execute(
                        "UPDATE memory_v2_meta SET value = '2' "
                        "WHERE key = 'schema_version'"
                    )
                    db.commit()
                recovered_store = MemoryV2Store(
                    db_path, data_path=Path(temp_dir)
                )
                service = ExpressionPatternService(
                    recovered_store, MemoryWriteService(recovered_store)
                )
                with self.assertRaisesRegex(
                    RuntimeError, "candidate_revision_conflict"
                ):
                    await service.write_pattern(
                        "chat-1", {**stale_payload, "candidate_revision": 5}
                    )
                with self.assertRaisesRegex(
                    RuntimeError, "candidate_revision_conflict"
                ):
                    await service.write_pattern("chat-1", stale_payload)
                migrated_id = await service.write_pattern(
                    "chat-1",
                    {
                        **stale_payload,
                        "candidate_revision": 6,
                        "mining_batch_id": "candidate-persist:migrated-key",
                        "candidate_persistence_id": "candidate-persist:migrated-key",
                    },
                )

                self.assertNotEqual(migrated_id, current_id)
                with sqlite3.connect(db_path) as db:
                    rows = db.execute(
                        """
                        SELECT id, status, superseded_by
                        FROM canonical_memories
                        ORDER BY id
                        """
                    ).fetchall()
                    fence = db.execute(
                        """
                        SELECT revision, persistence_id, canonical_memory_id, dedup_key
                        FROM memory_candidate_revision_fence
                        WHERE candidate_id = 'candidate-cross-key'
                        """
                    ).fetchone()
                self.assertEqual(len(rows), 2)
                self.assertEqual(
                    [row for row in rows if row[1] != "superseded"],
                    [(migrated_id, "review_pending", "")],
                )
                self.assertIn((current_id, "superseded", migrated_id), rows)
                self.assertEqual(fence[:3], (6, "candidate-persist:migrated-key", migrated_id))

        asyncio.run(_run())

    def test_automatic_duplicate_does_not_downgrade_human_approval(self):
        store = _Store()
        writer = _WriteService(store)
        service = ExpressionPatternService(store, writer)
        payload = {
            **_candidate("expr-approved", count=3),
            "habit_type": "catchphrase",
            "review_status": "pending_human",
        }
        dedup_key = service.build_dedup_key(
            "chat-1",
            payload["situation"],
            payload["expression"],
            "chat-1",
            payload["habit_type"],
        )
        store.by_key[dedup_key] = SimpleNamespace(
            id="mem-approved",
            content=payload["expression"],
            metadata={"review_status": "approved", "count": 4, "weight": 1.2},
        )

        asyncio.run(service.write_pattern("chat-1", payload))
        request = writer.calls[-1]

        self.assertEqual(request.status, "active")
        self.assertEqual(request.visibility, "auto_and_tool")
        self.assertEqual(request.metadata["review_status"], "approved")

    def test_rejected_pattern_reopens_when_new_group_evidence_is_stronger(self):
        store = _Store()
        writer = _WriteService(store)
        service = ExpressionPatternService(store, writer)
        payload = {
            **_candidate("expr-revision", count=3),
            "habit_type": "catchphrase",
            "review_status": "pending_human",
            "source_examples": ["唉嘿嘿", "唉嘿嘿～", "唉嘿嘿呀"],
            "source_message_ids": ["11", "12", "13"],
            "source_group_ids": ["chat-1"],
            "support_count": 3,
            "contributor_count": 2,
            "evidence_digest": "new-evidence",
        }
        dedup_key = service.build_dedup_key(
            "chat-1",
            payload["situation"],
            payload["expression"],
            "chat-1",
            payload["habit_type"],
        )
        store.by_key[dedup_key] = SimpleNamespace(
            id="mem-rejected",
            content=payload["expression"],
            metadata={
                "review_status": "rejected",
                "support_count": 1,
                "source_message_ids": ["1"],
                "evidence_digest": "old-evidence",
            },
        )

        asyncio.run(service.write_pattern("chat-1", payload))
        request = writer.calls[-1]

        self.assertEqual(request.status, "review_pending")
        self.assertEqual(request.metadata["review_status"], "revision_needed")
        self.assertEqual(request.metadata["source_message_ids"], ["1", "11", "12", "13"])
        self.assertEqual(request.metadata["model_examples"], [])

    def test_pattern_write_discards_personal_evidence_and_preserves_habit_metadata(self):
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
        self.assertNotIn("speaker_id", stored.metadata)
        self.assertNotIn("speaker_name", stored.metadata)
        self.assertEqual(stored.metadata["scope_kind"], "group")
        self.assertEqual(stored.metadata["shared_scope"], "chat-1")
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

        outcomes = asyncio.run(manager.process_logs_and_mine("chat-1", logs))
        self.assertEqual(db.marked, [])
        self.assertEqual(outcomes["expression"]["status"], "retry_wait")
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

        outcomes = asyncio.run(manager.process_logs_and_mine("chat-1", logs))

        self.assertEqual(db.marked, [])
        self.assertEqual(outcomes["expression"]["status"], "completed")
        self.assertEqual(outcomes["jargon"]["status"], "retry_wait")
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

        outcomes = asyncio.run(manager.process_logs_and_mine("chat-1", logs))
        self.assertEqual(db.marked, [])
        self.assertEqual(outcomes["expression"]["status"], "failed")
        self.assertEqual(manager._last_mining_outcomes["chat-1"]["persistence"]["failed"], 1)


if __name__ == "__main__":
    unittest.main()
