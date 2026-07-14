import asyncio
import hashlib
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from astrmai.infrastructure.persistence import ExpressionPattern, Jargon, MemoryEvent
from astrmai.memory.contracts.memory_query import CommittedMemoryTurn, MemoryWriteRequest
from astrmai.memory.dream.dream_agent import DreamAgent
from astrmai.memory.dream.dream_generator import DreamGenerator
from astrmai.memory.dream.fact_contract import format_dream_fact_log
from astrmai.memory.persona.persona_summarizer import PersonaSummarizer
from astrmai.memory.services.expression_pattern_service import ExpressionPatternService
from astrmai.memory.services.instant_memory_gate import InstantMemoryGate
from astrmai.memory.services.memory_engine import MemoryEngine
from astrmai.memory.services.memory_write_service import MemoryWriteService
from astrmai.memory.services.v2_store import MemoryV2Store


class _MigrationResult:
    def __init__(self, rows):
        self.rows = rows

    def all(self):
        return list(self.rows)


class _MigrationSession:
    def __init__(self, rows_by_model):
        self.rows_by_model = rows_by_model

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def exec(self, statement):
        model = statement.column_descriptions[0]["entity"]
        rows = self.rows_by_model[model.__name__]
        offset_clause = getattr(statement, "_offset_clause", None)
        limit_clause = getattr(statement, "_limit_clause", None)
        offset = int(getattr(offset_clause, "value", 0) or 0)
        limit = int(getattr(limit_clause, "value", len(rows)) or len(rows))
        return _MigrationResult(rows[offset : offset + limit])


class _MigrationDbService:
    def __init__(self, rows_by_model):
        self.rows_by_model = rows_by_model

    def get_session(self):
        return _MigrationSession(self.rows_by_model)


class _MigrationStore:
    def __init__(self):
        self.records = []
        self.dedup_keys = set()

    async def migration_applied(self, _version):
        return False

    async def record_migration(self, version, **payload):
        self.records.append((version, payload))

    async def get_by_dedup_key(self, dedup_key, *, include_inactive=False):
        return SimpleNamespace(id="existing") if dedup_key in self.dedup_keys else None


class _MigrationWriter:
    def __init__(self, store):
        self.store = store
        self.requests = []

    async def write(self, request):
        self.requests.append(request)
        self.store.dedup_keys.add(request.dedup_key)
        return f"mem-{len(self.requests)}"


class _MigrationExpressionService:
    build_dedup_key = staticmethod(ExpressionPatternService.build_dedup_key)

    def __init__(self, store):
        self.store = store
        self.calls = []

    async def write_pattern(self, group_id, payload, *, source):
        self.calls.append((group_id, payload, source))
        self.store.dedup_keys.add(
            self.build_dedup_key(group_id, payload["situation"], payload["expression"], payload.get("shared_scope", ""))
        )
        return f"expr-{len(self.calls)}"


class _PersonaPersistence:
    def __init__(self, cache=None):
        self.cache = dict(cache or {})
        self.saves = 0

    def load_persona_cache(self):
        return dict(self.cache)

    async def save_persona_cache_async(self, cache):
        self.cache = dict(cache)
        self.saves += 1


class Round7MemoryGovernanceTests(unittest.IsolatedAsyncioTestCase):
    async def test_legacy_imports_page_until_exhausted_before_marking_applied(self):
        events = [
            MemoryEvent(event_id=f"e-{index}", session_id="chat", date="2026-01-01", narrative=f"event {index}")
            for index in range(1001)
        ]
        jargons = [Jargon(content=f"j-{index}", group_id="g") for index in range(3)]
        patterns = [ExpressionPattern(situation="chat", expression=f"x-{index}", group_id="g") for index in range(3)]
        engine = object.__new__(MemoryEngine)
        engine.db_service = _MigrationDbService(
            {"MemoryEvent": events, "Jargon": jargons, "ExpressionPattern": patterns}
        )
        engine.v2_store = _MigrationStore()
        engine.write_service = _MigrationWriter(engine.v2_store)
        engine.expression_pattern_service = _MigrationExpressionService(engine.v2_store)

        event_count = await engine.import_legacy_memory_events(limit=1000)
        jargon_count = await engine.import_legacy_jargons(limit=2)
        expression_count = await engine.import_legacy_expression_patterns(limit=2)

        self.assertEqual(event_count, 1001)
        self.assertEqual(jargon_count, 3)
        self.assertEqual(expression_count, 3)
        self.assertEqual(len(engine.write_service.requests), 1004)
        self.assertEqual(len(engine.expression_pattern_service.calls), 3)
        self.assertEqual([item[1]["status"] for item in engine.v2_store.records], ["applied"] * 3)

        self.assertEqual(await engine.import_legacy_memory_events(limit=1000), 0)
        self.assertEqual(await engine.import_legacy_jargons(limit=2), 0)
        self.assertEqual(await engine.import_legacy_expression_patterns(limit=2), 0)
        self.assertEqual(len(engine.write_service.requests), 1004)
        self.assertEqual(len(engine.expression_pattern_service.calls), 3)

    async def test_memory_write_confidence_gate_honors_boundary_disable_and_refresh(self):
        class _Store:
            def __init__(self):
                self.requests = []

            async def upsert(self, request):
                self.requests.append(request)
                return {"memory_id": f"m-{len(self.requests)}"}

        store = _Store()
        config = SimpleNamespace(memory=SimpleNamespace(min_memory_confidence=0.5))
        writer = MemoryWriteService(store, config=config)

        below = await writer.write(MemoryWriteRequest(source="test", kind="fact", session_id="s", content="below", confidence=0.49))
        boundary = await writer.write(MemoryWriteRequest(source="test", kind="fact", session_id="s", content="boundary", confidence=0.5))
        writer.refresh_config(SimpleNamespace(memory=SimpleNamespace(min_memory_confidence=0.0)))
        disabled = await writer.write(MemoryWriteRequest(source="test", kind="fact", session_id="s", content="disabled", confidence=0.0))

        self.assertEqual(below, "")
        self.assertEqual(boundary, "m-1")
        self.assertEqual(disabled, "m-2")
        self.assertEqual([request.confidence for request in store.requests], [0.5, 0.0])

    async def test_expression_replacement_migrates_dedup_alias_and_resolves_conflict(self):
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as temp_dir:
            store = MemoryV2Store(str(Path(temp_dir) / "memory.db"), data_path=Path(temp_dir))
            writer = MemoryWriteService(store)
            service = ExpressionPatternService(store, writer)
            old_id = await service.write_pattern(
                "g",
                {"situation": "问候", "expression": "旧表达", "review_status": "approved", "count": 2},
                source="manual_review",
            )
            conflict_id = await service.write_pattern(
                "g",
                {"situation": "问候", "expression": "新表达", "review_status": "approved", "count": 3},
                source="manual_review",
            )

            replaced = await service.update_review(
                old_id,
                replacement_expression="新表达",
                apply_replacement=True,
                checked=True,
            )
            old_replay_id = await service.write_pattern(
                "g",
                {"situation": "问候", "expression": "旧表达", "count": 1},
            )
            new_replay_id = await service.write_pattern(
                "g",
                {"situation": "问候", "expression": "新表达", "count": 1},
            )
            conflict = await store.get_canonical(conflict_id, include_inactive=True)

            self.assertEqual(replaced.id, old_id)
            self.assertEqual(replaced.expression, "新表达")
            self.assertEqual(old_replay_id, old_id)
            self.assertEqual(new_replay_id, old_id)
            self.assertEqual(conflict.status, "superseded")
            self.assertEqual(conflict.superseded_by, old_id)
            latest = await service.get_pattern(old_id)
            self.assertEqual(latest.expression, "新表达")
            self.assertGreaterEqual(latest.count, 7)

    async def test_persona_prompt_hash_change_invalidates_cache_and_self_lore(self):
        old_prompt = "old persona prompt"
        persistence = _PersonaPersistence(
            {
                "persona": {
                    "summary": "old summary",
                    "first_person_rewrite": "old rewrite",
                    "style": "old style",
                    "shards": {"values": "old"},
                    "is_full_ready": True,
                    "raw": old_prompt,
                    "raw_hash": hashlib.sha256(old_prompt.encode("utf-8")).hexdigest(),
                }
            }
        )
        lore_clears = []

        class _Memory:
            async def clear_persona_lore(self, persona_id):
                lore_clears.append(persona_id)

        config = SimpleNamespace(performance=SimpleNamespace(summary_threshold=5))
        gateway = SimpleNamespace(config=config, context_economy=None)
        summarizer = PersonaSummarizer(persistence, gateway, config=config, memory_engine=_Memory())
        summarizer._summarize_core_identity_with_retry = lambda *_args: asyncio.sleep(0, result="new summary")
        summarizer._summarize_style_with_retry = lambda *_args: asyncio.sleep(0, result="new style")
        summarizer._build_first_person_rewrite = lambda **_kwargs: asyncio.sleep(0, result="new rewrite")
        summarizer._generate_all_shards_background = lambda *_args: asyncio.sleep(0)

        result = await summarizer.get_summary("new persona prompt", persona_id="persona")
        await asyncio.sleep(0)

        self.assertEqual(result["summary"], "new summary")
        self.assertEqual(result["raw"], "new persona prompt")
        self.assertEqual(lore_clears, ["persona"])

    async def test_persona_stop_cancels_owned_shard_tasks(self):
        persistence = _PersonaPersistence()
        config = SimpleNamespace(performance=SimpleNamespace(summary_threshold=5))
        summarizer = PersonaSummarizer(persistence, SimpleNamespace(config=config, context_economy=None), config=config)
        started = asyncio.Event()

        async def _background(*_args):
            started.set()
            await asyncio.Event().wait()

        summarizer._generate_all_shards_background = _background
        task = summarizer._start_shard_task("prompt", "persona")
        await started.wait()
        await summarizer.stop()

        self.assertTrue(task.cancelled())
        self.assertEqual(summarizer.pending_tasks, {})
        self.assertEqual(summarizer._background_tasks, set())

    async def test_dream_agent_emits_only_valid_structured_finish_facts(self):
        fact = {
            "subject_id": "u1",
            "entity": "food",
            "attribute": "preference",
            "value": "火锅",
            "confidence_score": 0.95,
            "confidence_signal": "high",
            "evidence": {"turn_id": "t1", "text": "我喜欢火锅"},
        }

        class _Gateway:
            config = SimpleNamespace()

            async def call_data_process_task(self, **_kwargs):
                return {
                    "tool": "finish_dream",
                    "thought": "普通思考不应晋升",
                    "params": {"summary": "done", "detected_facts": [fact, {"value": "invalid"}]},
                }

        agent = DreamAgent(_Gateway(), SimpleNamespace(), memory_engine=None)
        agent._get_seed_events = lambda _session_id: asyncio.sleep(
            0,
            result=[{"event_id": "e1", "narrative": "用户谈到火锅"}],
        )

        dream_log = await agent.run_dream_cycle(session_id="chat")
        maintenance = DreamGenerator.build_maintenance_result(dream_log, session_id="chat")

        self.assertEqual(len(maintenance["detected_facts"]), 1)
        self.assertEqual(maintenance["detected_facts"][0]["value"], "火锅")
        self.assertNotIn("普通思考不应晋升", str(maintenance["detected_facts"]))

    async def test_dream_styles_are_readable_controlled_and_fact_parser_is_strict(self):
        self.assertEqual(DreamGenerator._normalize_style("奇幻冒险"), "奇幻冒险")
        self.assertIn(DreamGenerator._normalize_style("not-allowed"), DreamGenerator.DREAM_STYLES)
        self.assertFalse(any("�" in style or "?" in style for style in DreamGenerator.DREAM_STYLES))
        fact_line = format_dream_fact_log(
            {
                "subject_id": "u1",
                "entity": "identity",
                "attribute": "name",
                "value": "小明",
            }
        )
        result = DreamGenerator.build_maintenance_result(
            "[思考] {\"subject_id\":\"u1\",\"entity\":\"bad\"}\n" + fact_line,
            session_id="chat",
        )
        self.assertEqual([item["value"] for item in result["detected_facts"]], ["小明"])

    async def test_instant_gate_new_write_uses_clean_user_fact(self):
        writes = []

        class _Writer:
            async def write(self, request):
                writes.append(request)
                return "m1"

        engine = SimpleNamespace(write_service=_Writer())
        gate = InstantMemoryGate(SimpleNamespace(config=SimpleNamespace(), context_economy=None), engine)
        gate.claim_extractor.extract = lambda **_kwargs: asyncio.sleep(0, result=[])
        turn = CommittedMemoryTurn(
            turn_id="t1",
            chat_id="chat",
            sender_id="u1",
            user_text="我叫测试小明",
            assistant_text="你好",
        )

        result = await gate.process_committed_turn(turn)

        self.assertTrue(result.hit)
        self.assertEqual(writes[0].content, "我叫测试小明")
        self.assertNotIn("????", writes[0].content)


if __name__ == "__main__":
    unittest.main()
