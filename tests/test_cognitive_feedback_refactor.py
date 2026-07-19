import asyncio
import importlib
import json
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _FakeGateway:
    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []
        self.config = SimpleNamespace(memory=SimpleNamespace(recall_top_k=5), persona=SimpleNamespace(persona_id="persona-1"))

    async def call_data_process_task(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, "kwargs": kwargs})
        return self.responses.pop(0) if self.responses else {"action": "reply"}


class _FakeEvent:
    def __init__(self, text="please remember this detail"):
        self.message_str = text
        self.unified_msg_origin = "chat-1"
        self._extra = {}

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_sender_id(self):
        return "user-1"

    def get_sender_name(self):
        return "Alice"


class CognitiveFeedbackRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        event_mod = sys.modules["astrbot.api.event"]

        class MessageChain:
            def __init__(self):
                self.chain = []

            def message(self, text):
                self.chain.append(text)
                return self

        event_mod.MessageChain = MessageChain
        for name in [
            "astrmai.memory.services.memory_engine",
            "astrmai.conversation.planning.agency_runtime",
            "astrmai.conversation.planning.agency_feedback_bridge",
            "astrmai.conversation.planning.cognitive_loop",
            "astrmai.proactive.dream_scheduler",
            "astrmai.proactive.diary_service",
        ]:
            sys.modules.pop(name, None)
        self.memory_mod = importlib.reload(importlib.import_module("astrmai.memory.services.memory_engine"))
        self.runtime_mod = importlib.reload(importlib.import_module("astrmai.conversation.planning.agency_runtime"))
        self.bridge_mod = importlib.reload(importlib.import_module("astrmai.conversation.planning.agency_feedback_bridge"))
        self.loop_mod = importlib.reload(importlib.import_module("astrmai.conversation.planning.cognitive_loop"))
        self.dream_mod = importlib.reload(importlib.import_module("astrmai.proactive.dream_scheduler"))
        self.diary_mod = importlib.reload(importlib.import_module("astrmai.proactive.diary_service"))

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _memory_engine(self):
        return self.memory_mod.MemoryEngine(SimpleNamespace(), _FakeGateway(), embedding_models=[])

    def test_memory_engine_records_feedback_in_cache_and_filters_recall(self):
        engine = self._memory_engine()

        async def _offline():
            return False

        engine._ensure_faiss_initialized = _offline
        # mock initialize() 依赖 — 真实路径需要 v2_store / faiss 等重量依赖
        engine.write_service = SimpleNamespace()

        async def _mock_write(*args, **kwargs):
            return "mock-memory-id"

        engine.write_service.write = _mock_write
        engine.retrieval_service = SimpleNamespace()

        async def _mock_retrieve(*args, **kwargs):
            return [
                SimpleNamespace(content="[cognitive_feedback:agency]\nsummary: hidden\n"),
                SimpleNamespace(content="normal memory content"),
            ]

        engine.retrieval_service.retrieve = _mock_retrieve

        def _mock_render_recall(query, candidates):
            return " ".join(item.content for item in candidates)

        engine.retrieval_service.render_recall = _mock_render_recall

        async def _run():
            await engine.record_cognitive_feedback(
                session_id="chat-1",
                source="agency",
                summary="recently used meme twice",
                guidance="avoid repeated meme",
                tags=["meme"],
            )
            return await engine.get_cognitive_feedback("chat-1", limit=2)

        signals = asyncio.run(_run())

        self.assertEqual(len(signals), 1)
        self.assertEqual(signals[0].source, "agency")
        self.assertEqual(signals[0].guidance, "avoid repeated meme")

        class _Item:
            def __init__(self, content):
                self.content = content
                self.score = 1.0

        async def _ready():
            return True

        async def _search(*args, **kwargs):
            return [
                _Item("[cognitive_feedback:agency]\nsummary: hidden\n"),
                _Item("normal memory content"),
            ]

        engine._ensure_faiss_initialized = _ready
        engine._search_memories = _search
        recalled = asyncio.run(engine.recall("anything", session_id="chat-1"))

        self.assertIn("normal memory content", recalled)
        self.assertNotIn("cognitive_feedback", recalled)

    def test_memory_engine_record_cognitive_feedback_writes_feedback_request(self):
        engine = self._memory_engine()
        captured = []

        async def _write(request):
            captured.append(request)
            return "feedback-memory-id"

        engine.write_service = SimpleNamespace(write=_write)

        asyncio.run(
            engine.record_cognitive_feedback(
                session_id="chat-1",
                source="Agency",
                summary="Use fewer repeated jokes",
                guidance="Prefer direct answers",
                tags=[" Joke ", "joke", "Tone"],
                importance=0.7,
            )
        )

        self.assertEqual(len(captured), 1)
        request = captured[0]
        self.assertEqual(request.kind, "feedback")
        self.assertEqual(request.source, "agency")
        self.assertEqual(request.session_id, "chat-1")
        self.assertEqual(request.visibility, "tool_only")
        self.assertEqual(request.tags, ["joke", "tone"])
        self.assertTrue(request.metadata["cognitive_feedback"])
        self.assertEqual(request.metadata["feedback_schema_version"], 2)
        self.assertEqual(request.metadata["guidance"], "Prefer direct answers")
        self.assertEqual(request.dedup_key, "feedback:chat-1:agency:rolling")
        self.assertGreater(request.metadata["valid_until"], time.time())
        self.assertIn("[cognitive_feedback:agency]", request.content)
        self.assertEqual(engine._cognitive_feedback_cache["chat-1"][0].summary, "Use fewer repeated jokes")

        asyncio.run(
            engine.record_cognitive_feedback(
                session_id="chat-1",
                source="Agency",
                summary="Latest feedback replaces the rolling window",
            )
        )
        self.assertEqual(len(engine._cognitive_feedback_cache["chat-1"]), 1)
        self.assertEqual(
            engine._cognitive_feedback_cache["chat-1"][0].summary,
            "Latest feedback replaces the rolling window",
        )
        self.assertEqual(captured[0].dedup_key, captured[1].dedup_key)

    def test_memory_engine_get_cognitive_feedback_merges_cache_and_db_rows(self):
        cache_engine = self._memory_engine()
        now = time.time()
        cache_engine._remember_cognitive_feedback(
            self.memory_mod.CognitiveFeedbackSignal(
                source="agency",
                chat_id="chat-1",
                summary="cache summary",
                guidance="cache guidance",
                timestamp=now,
                importance=0.6,
            )
        )

        db_calls = []

        async def _empty_query(query, params=(), *, db_path=None):
            db_calls.append((query, params, db_path))
            return []

        cache_engine._run_documents_query = _empty_query

        cache_signals = asyncio.run(cache_engine.get_cognitive_feedback("chat-1", limit=5))

        self.assertEqual([item.source for item in cache_signals], ["agency"])
        self.assertEqual(cache_signals[0].summary, "cache summary")
        self.assertEqual(len(db_calls), 1)

        db_engine = self._memory_engine()
        db_calls.clear()
        parse_calls = []

        async def _query(query, params=(), *, db_path=None):
            db_calls.append((query, params, db_path))
            return [
                (
                    "[cognitive_feedback:diary]\nsummary: db summary\nguidance: db guidance\ntags: calm, calm, focus",
                    json.dumps({"importance": 0.9}),
                    now - 10,
                ),
                ("plain memory", "{}", now),
            ]

        db_engine._run_documents_query = _query

        def _parse(text, *, chat_id, timestamp=0.0, importance=0.5):
            parse_calls.append((text, chat_id, timestamp, importance))
            if "cognitive_feedback:diary" not in str(text):
                return None
            return self.memory_mod.CognitiveFeedbackSignal(
                source="diary",
                chat_id=chat_id,
                summary="db summary",
                guidance="db guidance",
                tags=["calm", "focus"],
                timestamp=timestamp,
                importance=importance,
            )

        db_engine._parse_cognitive_feedback_content = _parse

        db_signals = asyncio.run(db_engine.get_cognitive_feedback("chat-1", limit=5))
        diary_only = asyncio.run(db_engine.get_cognitive_feedback("chat-1", limit=5, sources={"diary"}))

        self.assertEqual(len(db_calls), 2)
        self.assertEqual(len(parse_calls), 4)
        self.assertEqual([item.source for item in db_signals], ["diary"])
        self.assertEqual(db_signals[0].summary, "db summary")
        self.assertEqual(db_signals[0].guidance, "db guidance")
        self.assertEqual(db_signals[0].tags, ["calm", "focus"])
        self.assertEqual(db_signals[0].importance, 0.9)
        self.assertEqual([item.source for item in diary_only], ["diary"])
        self.assertEqual(asyncio.run(db_engine.get_cognitive_feedback("", limit=5)), [])

    def test_memory_engine_disable_cognitive_feedback_filters_and_cleans_ttl(self):
        engine = self._memory_engine()
        old_key = "old|agency|summary|guidance"
        engine._disabled_cognitive_feedback_keys[old_key] = time.time() - engine.DISABLE_TTL_SEC - 1
        signal = self.memory_mod.CognitiveFeedbackSignal(
            source="agency",
            chat_id="chat-1",
            summary="summary",
            guidance="guidance",
            timestamp=time.time(),
        )

        engine.disable_cognitive_feedback(signal)

        self.assertNotIn(old_key, engine._disabled_cognitive_feedback_keys)
        self.assertIn(engine._cognitive_feedback_key_str(signal), engine._disabled_cognitive_feedback_keys)

    def test_memory_engine_parse_cognitive_feedback_content(self):
        parsed = self.memory_mod.MemoryEngine._parse_cognitive_feedback_content(
            "[cognitive_feedback:Agency]\nsummary: Be concise\nguidance: Avoid loops\ntags: Tone, tone, Direct",
            chat_id="chat-1",
            timestamp=123.0,
            importance=0.8,
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.source, "Agency")
        self.assertEqual(parsed.chat_id, "chat-1")
        self.assertEqual(parsed.summary, "Be concise")
        self.assertEqual(parsed.guidance, "Avoid loops")
        self.assertEqual(parsed.tags, ["tone", "direct"])
        self.assertEqual(parsed.timestamp, 123.0)
        self.assertEqual(parsed.importance, 0.8)
        self.assertIsNone(
            self.memory_mod.MemoryEngine._parse_cognitive_feedback_content("plain memory", chat_id="chat-1")
        )
        self.assertIsNone(
            self.memory_mod.MemoryEngine._parse_cognitive_feedback_content(
                "[cognitive_feedback:agency]\ntags: only-tags",
                chat_id="chat-1",
            )
        )

    def test_memory_engine_localizes_legacy_agency_feedback(self):
        parsed = self.memory_mod.MemoryEngine._parse_cognitive_feedback_content(
            "[cognitive_feedback:agency]\n"
            "summary: Recent agency pattern: 6 turns, main_intent=answer, main_tier=none, "
            "main_action=reply. Cooldowns observed: long_reply, meme.\n"
            "guidance: Avoid repeating recently used actions: long_reply. "
            "Prefer shorter replies unless the user explicitly asks for detail.\n"
            "tags: long_reply, meme",
            chat_id="chat-1",
        )

        self.assertIsNotNone(parsed)
        self.assertIn("近期 6 轮", parsed.summary)
        self.assertIn("长回复", parsed.summary)
        self.assertIn("优先简短回应", parsed.guidance)
        self.assertEqual(parsed.payload["main_intent"], "answer")

    def test_memory_engine_lists_feedback_with_localized_display_fields(self):
        engine = self._memory_engine()
        engine._last_feedback_cleanup_ts = time.time()

        class _Store:
            async def list_canonical(self, **_kwargs):
                return {
                    "items": [
                        {
                            "id": "mem-feedback",
                            "source": "agency",
                            "session_id": "chat-1",
                            "summary": "Recent agency pattern: 3 turns, main_intent=answer, main_tier=none, main_action=reply.",
                            "content": "[cognitive_feedback:agency]\nsummary: Recent agency pattern: 3 turns, main_intent=answer, main_tier=none, main_action=reply.",
                            "tags": [],
                            "importance": 0.5,
                            "status": "active",
                            "created_at": 10.0,
                            "metadata": {"guidance": "Keep the next response consistent with the recent agency pattern without repeating it."},
                        }
                    ],
                    "total": 1,
                    "limit": 50,
                    "offset": 0,
                }

        engine.v2_store = _Store()
        result = asyncio.run(engine.list_cognitive_feedback_records())

        self.assertEqual(result["items"][0]["source_label"], "行为节奏")
        self.assertIn("近期 3 轮", result["items"][0]["summary"])
        self.assertIn("不要机械重复", result["items"][0]["guidance"])
        self.assertEqual(result["items"][0]["feedback_schema_version"], 1)

    def test_memory_engine_cleanup_removes_expired_and_superseded_feedback(self):
        engine = self._memory_engine()
        now = time.time()

        class _Store:
            async def list_canonical(self, **_kwargs):
                return {
                    "items": [
                        {"id": "new", "session_id": "chat-1", "source": "agency", "metadata": {"valid_until": now + 100}},
                        {"id": "old", "session_id": "chat-1", "source": "agency", "metadata": {"valid_until": now + 100}},
                        {"id": "expired", "session_id": "chat-2", "source": "diary", "metadata": {"valid_until": now - 1}},
                    ]
                }

        deleted = []

        class _Maintenance:
            async def soft_delete(self, memory_id, *, reason=""):
                deleted.append((memory_id, reason))
                return 1

        engine.v2_store = _Store()
        engine.maintenance_service = _Maintenance()
        changed = asyncio.run(engine._cleanup_cognitive_feedback_records(force=True))

        self.assertEqual(changed, 2)
        self.assertEqual(
            deleted,
            [
                ("old", "superseded_rolling_feedback"),
                ("expired", "expired_cognitive_feedback"),
            ],
        )

    def test_memory_engine_migrates_legacy_agency_feedback_to_structured_v2(self):
        engine = self._memory_engine()
        updated = []
        migrations = []

        class _Store:
            async def migration_applied(self, _version):
                return False

            async def list_canonical(self, **_kwargs):
                return {
                    "items": [
                        {
                            "id": "legacy-feedback",
                            "source": "agency",
                            "session_id": "chat-1",
                            "summary": "Recent agency pattern: 6 turns, main_intent=answer, main_tier=none, main_action=reply.",
                            "content": "[cognitive_feedback:agency]\nsummary: Recent agency pattern: 6 turns, main_intent=answer, main_tier=none, main_action=reply.",
                            "tags": [],
                            "importance": 0.5,
                            "metadata": {},
                        }
                    ]
                }

            async def update_memory(self, memory_id, **kwargs):
                updated.append((memory_id, kwargs))
                return 1

            async def record_migration(self, version, **kwargs):
                migrations.append((version, kwargs))

        engine.v2_store = _Store()
        migrated = asyncio.run(engine.migrate_legacy_cognitive_feedback())

        self.assertEqual(migrated, 1)
        self.assertEqual(updated[0][0], "legacy-feedback")
        self.assertIn("近期 6 轮", updated[0][1]["summary"])
        self.assertEqual(updated[0][1]["metadata"]["feedback_schema_version"], 2)
        self.assertEqual(updated[0][1]["metadata"]["feedback_payload"]["main_action"], "reply")
        self.assertEqual(migrations[0][0], "feedback_schema_v2")
        self.assertEqual(migrations[0][1]["status"], "applied")

    def test_memory_engine_store_topic_results_merges_similar_existing_topic(self):
        engine = self._memory_engine()
        written = []
        merged = []

        class _RetrievalService:
            async def retrieve(self, query):
                self.query = query
                return [
                    SimpleNamespace(
                        id="topic-old",
                        content="Alice prefers concise summaries about deployment readiness.",
                        kind="topic",
                        tags=["deployment"],
                        confidence=0.75,
                    )
                ]

        class _WriteService:
            async def write(self, request):
                written.append(request)
                return "topic-new"

        class _MaintenanceService:
            async def mark_merged(self, ids, superseded_by):
                merged.append((ids, superseded_by))

        engine.retrieval_service = _RetrievalService()
        engine.write_service = _WriteService()
        engine.maintenance_service = _MaintenanceService()

        asyncio.run(
            engine.store_topic_results(
                [
                    {
                        "summary": "Alice prefers concise summaries about deployment readiness.",
                        "topic_keywords": ["deployment", "readiness"],
                        "importance": 0.9,
                    }
                ],
                session_id="chat-1",
                persona_id="persona-1",
            )
        )

        self.assertEqual(len(written), 1)
        request = written[0]
        self.assertEqual(request.kind, "topic")
        self.assertEqual(request.session_id, "chat-1")
        self.assertEqual(request.persona_id, "persona-1")
        self.assertIn("Supplement:", request.content)
        self.assertEqual(request.metadata["merged_from"], ["topic-old"])
        self.assertEqual(merged, [(["topic-old"], "topic-new")])

    def test_memory_engine_ensure_faiss_initialized_ready_fast_path(self):
        engine = self._memory_engine()
        engine._is_ready = True

        self.assertTrue(asyncio.run(engine._ensure_faiss_initialized()))

    def test_memory_engine_initialize_wires_services_and_runs_legacy_imports(self):
        engine = self._memory_engine()
        calls = []

        class _Store:
            async def initialize(self):
                calls.append("store.initialize")

            async def import_legacy_documents(self):
                calls.append("legacy.documents")

            async def import_persona_cache(self):
                calls.append("legacy.persona")

        class _BM25:
            def __init__(self, db_path):
                calls.append(("bm25", db_path))

            async def initialize(self):
                calls.append("bm25.initialize")

        class _Service:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        engine.v2_store = _Store()

        async def _legacy_events():
            calls.append("legacy.events")

        async def _legacy_jargons():
            calls.append("legacy.jargons")

        async def _legacy_patterns():
            calls.append("legacy.patterns")

        engine.import_legacy_memory_events = _legacy_events
        engine.import_legacy_jargons = _legacy_jargons
        engine.import_legacy_expression_patterns = _legacy_patterns

        with patch.multiple(
            self.memory_mod,
            MemoryIndexProjector=_Service,
            MemoryWriteService=_Service,
            MemoryRetrievalService=_Service,
            ExpressionPatternService=_Service,
            MemoryInjectionService=_Service,
            MemoryToolService=_Service,
            MemoryMaintenanceService=_Service,
            MemoryMigrationService=_Service,
            BM25Retriever=_BM25,
        ):
            asyncio.run(engine.initialize())

        self.assertIs(engine.v2_store.index_projector, engine.index_projector)
        self.assertIsInstance(engine.write_service, _Service)
        self.assertIsInstance(engine.retrieval_service, _Service)
        self.assertEqual(
            calls,
            [
                "store.initialize",
                "legacy.documents",
                "legacy.persona",
                "legacy.events",
                "legacy.jargons",
                "legacy.patterns",
                ("bm25", engine.db_path),
                "bm25.initialize",
            ],
        )

    def test_memory_engine_start_background_tasks_wires_and_starts_pipeline(self):
        engine = self._memory_engine()
        engine.db_service = SimpleNamespace(raw_trace_store="trace-store", event_bus="db-events")
        engine.observability_hub = "observability"
        started = []

        class _Component:
            def __init__(self, *args, **kwargs):
                self.args = args
                self.kwargs = kwargs

        class _Pipeline(_Component):
            async def start(self):
                started.append(self)

        with patch.multiple(
            self.memory_mod,
            MemoryObserver=_Component,
            SessionMemorySummarizer=_Component,
            InstantMemoryGate=_Component,
            MemoryTurnPipeline=_Pipeline,
        ):
            asyncio.run(engine.start_background_tasks())

        self.assertEqual(started, [engine.memory_pipeline])
        self.assertEqual(engine.memory_observer.args, ("trace-store",))
        self.assertEqual(engine.memory_observer.kwargs["observability_hub"], "observability")
        self.assertIs(engine.memory_pipeline.kwargs["observer"], engine.memory_observer)
        self.assertEqual(engine.memory_pipeline.kwargs["event_bus"], "db-events")

    def test_memory_engine_legacy_imports_remain_retryable_without_db_service(self):
        engine = self._memory_engine()
        migrations = []

        class _Store:
            async def migration_applied(self, version):
                return False

            async def record_migration(self, version, **kwargs):
                migrations.append((version, kwargs))

        engine.v2_store = _Store()
        engine.db_service = None

        async def _run_imports():
            return await asyncio.gather(
                engine.import_legacy_memory_events(),
                engine.import_legacy_jargons(),
                engine.import_legacy_expression_patterns(),
            )

        results = asyncio.run(_run_imports())

        self.assertEqual(results, [0, 0, 0])
        self.assertEqual(migrations, [])
        self.assertTrue(all(item[1]["status"] == "applied" for item in migrations))

    def test_memory_engine_get_recent_memories_filters_feedback_rows(self):
        engine = self._memory_engine()
        queries = []

        async def _ready():
            return True

        async def _query(query, params=(), *, db_path=None):
            queries.append((query, params, db_path))
            if query.startswith("PRAGMA"):
                return [(0, "page_content"), (1, "metadata")]
            return [
                ("visible memory",),
                ("[cognitive_feedback:agency]\nsummary: hidden",),
                ("",),
            ]

        engine._ensure_faiss_initialized = _ready
        engine._run_documents_query = _query

        result = asyncio.run(engine.get_recent_memories("chat-1", hours=12))

        self.assertEqual(result, ["visible memory"])
        self.assertEqual(len(queries), 2)
        self.assertIn("SELECT page_content", queries[1][0])
        self.assertEqual(queries[1][1][0], "chat-1")
        self.assertEqual(queries[1][2], engine.db_path)

    def test_agency_reflection_bridge_flushes_after_threshold(self):
        runtime = self.runtime_mod.AgencyRuntimeStore()

        class _Memory:
            def __init__(self):
                self.calls = []

            async def record_cognitive_feedback(self, **kwargs):
                self.calls.append(kwargs)

        memory = _Memory()
        bridge = self.bridge_mod.AgencyReflectionBridge(memory)
        for index in range(6):
            runtime.record(
                chat_id="chat-1",
                reply_need="reply",
                social_intent="tease" if index < 4 else "answer",
                action_tier="chat",
                action_taken="reply",
                reply_preview="ok",
                cooldown_tags=["meme"] if index % 2 == 0 else ["like"],
            )

        flushed = asyncio.run(bridge.maybe_flush(runtime, "chat-1"))

        self.assertTrue(flushed)
        self.assertEqual(len(memory.calls), 1)
        self.assertEqual(memory.calls[0]["source"], "agency")
        self.assertIn("轻松互动", memory.calls[0]["summary"])
        self.assertIn("避免立即重复", memory.calls[0]["guidance"])
        self.assertEqual(memory.calls[0]["payload"]["main_intent"], "tease")
        self.assertEqual(memory.calls[0]["payload"]["turn_count"], 6)

    def test_agency_runtime_uses_monotonic_clock_for_record_and_expiry(self):
        runtime = self.runtime_mod.AgencyRuntimeStore()

        with unittest.mock.patch.object(self.runtime_mod, "monotonic", return_value=100.0):
            item = runtime.record(
                chat_id="chat-1",
                reply_need="reply",
                social_intent="answer",
                action_tier="chat",
                action_taken="reply",
                reply_preview="ok",
                cooldown_tags=["meme"],
            )

        self.assertEqual(item.timestamp, 100.0)
        self.assertEqual(runtime.recent("chat-1", now=100.0 + 30 * 60), [item])
        self.assertEqual(runtime.recent("chat-1", now=100.0 + 30 * 60 + 0.01), [])

    def test_cognitive_loop_reads_long_term_feedback_in_hidden_prompt(self):
        gateway = _FakeGateway(
            [
                {
                    "action": "reply",
                    "memory_policy": "light",
                    "social_intent": "answer",
                    "action_tier": "chat",
                }
            ]
        )
        loop = self.loop_mod.CognitiveLoop(gateway)
        event = _FakeEvent("please explain why this matters")
        event.set_extra("astrmai_memory_feedback_summary", "Long-term behavior and memory feedback:\n- agency: avoid repeated meme")

        decision = asyncio.run(loop.decide(event=event))

        self.assertIsNotNone(decision)
        self.assertIn("Long-term behavior/memory feedback", gateway.calls[0]["prompt"])
        self.assertIn("avoid repeated meme", gateway.calls[0]["prompt"])

    def test_dream_scheduler_builds_feedback_guidance_from_maintenance_tags(self):
        guidance = self.dream_mod.DreamScheduler._maintenance_guidance(["merge", "delete", "jargon_review"])

        self.assertIn("consolidated memory", guidance)
        self.assertIn("stale or noisy", guidance)
        self.assertIn("jargon", guidance)

    def test_dream_scheduler_writes_cognitive_feedback(self):
        class _Memory:
            def __init__(self):
                self.feedback_calls = []
                self.memory_calls = []

            async def record_cognitive_feedback(self, **kwargs):
                self.feedback_calls.append(kwargs)

            async def add_memory(self, **kwargs):
                self.memory_calls.append(kwargs)

        class _Agent:
            MIN_EVENTS_TO_DREAM = 5
            _last_session_id = "chat-1"

            async def run_dream_cycle(self, session_id=None):
                return "dream log"

        class _Generator:
            async def generate(self, **kwargs):
                return "dream text"

            def build_maintenance_result(self, dream_log, session_id="global"):
                return {"summary": "merged memory", "tags": ["merge", "delete"]}

        memory = _Memory()
        scheduler = self.dream_mod.DreamScheduler(
            context=SimpleNamespace(send_message=None),
            memory_engine=memory,
            config=SimpleNamespace(life=SimpleNamespace(dream_interval_min=1, min_memory_events_to_dream=5, dream_time_ranges=[]), persona=SimpleNamespace(name="Mai")),
            semaphore=asyncio.Semaphore(1),
            dream_visible=False,
        )
        scheduler.bind_dependencies(_Agent(), _Generator())

        asyncio.run(scheduler.run_once())

        self.assertEqual(len(memory.feedback_calls), 1)
        self.assertEqual(memory.feedback_calls[0]["source"], "dream")
        self.assertIn("consolidated memory", memory.feedback_calls[0]["guidance"])

    def test_diary_service_writes_cognitive_feedback(self):
        class _Memory:
            def __init__(self):
                self.feedback_calls = []
                self.memory_calls = []
                self.summarizer = None

            async def get_recent_memories(self, group_id, hours=24):
                return ["one recent memory"]

            async def record_cognitive_feedback(self, **kwargs):
                self.feedback_calls.append(kwargs)

            async def add_memory(self, **kwargs):
                self.memory_calls.append(kwargs)

        async def _call_background_lane(*args, **kwargs):
            return "quiet diary summary"

        class _FakePromptRegistry:
            def render_template(self, template_id, variables):
                return SimpleNamespace(prompt="diary prompt", system_prompt="diary system")

        memory = _Memory()
        service = self.diary_mod.DiaryService(
            persistence=SimpleNamespace(load_persona_cache=lambda: {"persona-1": {"summary": "core persona"}}),
            memory_engine=memory,
            config=SimpleNamespace(persona=SimpleNamespace(persona_id="persona-1")),
            call_background_lane=_call_background_lane,
            semaphore=asyncio.Semaphore(1),
            prompt_registry=_FakePromptRegistry(),
        )

        asyncio.run(service.run_once([SimpleNamespace(chat_id="chat-1")]))

        self.assertEqual(len(memory.feedback_calls), 1)
        self.assertEqual(memory.feedback_calls[0]["source"], "diary")
        self.assertIn("quiet diary summary", memory.feedback_calls[0]["summary"])

    def test_diary_service_writes_readable_chinese_memory_labels(self):
        class _Memory:
            def __init__(self):
                self.feedback_calls = []
                self.memory_calls = []

            async def get_recent_memories(self, group_id, hours=24):
                return []

            async def record_cognitive_feedback(self, **kwargs):
                self.feedback_calls.append(kwargs)

            async def add_memory(self, **kwargs):
                self.memory_calls.append(kwargs)

        templates_mod = importlib.import_module("astrmai.infrastructure.context_economy.prompt_templates")
        registry = templates_mod.PromptTemplateRegistry()
        captured_prompt = {}

        async def _call_background_lane(*args, **kwargs):
            captured_prompt["prompt"] = args[2]
            return "quiet diary summary"

        memory = _Memory()
        service = self.diary_mod.DiaryService(
            persistence=SimpleNamespace(load_persona_cache=lambda: {"persona-1": {"summary": "温柔陪伴型"}}),
            memory_engine=memory,
            config=SimpleNamespace(persona=SimpleNamespace(persona_id="persona-1")),
            call_background_lane=_call_background_lane,
            semaphore=asyncio.Semaphore(1),
            prompt_registry=registry,
        )

        asyncio.run(service.run_once([SimpleNamespace(chat_id="chat-1")]))

        self.assertIn("[你的核心人设]", captured_prompt["prompt"])
        self.assertIn("温柔陪伴型", captured_prompt["prompt"])
        self.assertIn("今天没有显著事件。", captured_prompt["prompt"])
        self.assertEqual(memory.memory_calls[0]["content"], "[内部日记] quiet diary summary")

    def test_diary_retries_only_failed_chat_and_continues_batch(self):
        class _Memory:
            def __init__(self):
                self.recent_calls = []
                self.memory_calls = []
                self.fail_chat_a = True

            async def get_recent_memories(self, group_id, hours=24):
                self.recent_calls.append(group_id)
                if group_id == "chat-a" and self.fail_chat_a:
                    self.fail_chat_a = False
                    raise RuntimeError("temporary read failure")
                return []

            async def add_memory(self, **kwargs):
                self.memory_calls.append(kwargs)

        class _Registry:
            def render_template(self, _template_id, _variables):
                return SimpleNamespace(prompt="diary", system_prompt="system")

        async def _call_lane(*_args, **_kwargs):
            return "summary"

        memory = _Memory()
        service = self.diary_mod.DiaryService(
            persistence=SimpleNamespace(load_persona_cache=lambda: {}),
            memory_engine=memory,
            config=SimpleNamespace(persona=SimpleNamespace(persona_id="global")),
            call_background_lane=_call_lane,
            semaphore=asyncio.Semaphore(1),
            prompt_registry=_Registry(),
        )
        states = [SimpleNamespace(chat_id="chat-a"), SimpleNamespace(chat_id="chat-b")]

        first = asyncio.run(service.run_once(states, diary_date="2026-07-14"))
        second = asyncio.run(service.run_once(states, diary_date="2026-07-14"))

        self.assertEqual((first["succeeded"], first["failed"]), (1, 1))
        self.assertEqual((second["succeeded"], second["failed"]), (1, 0))
        self.assertEqual(memory.recent_calls, ["chat-a", "chat-b", "chat-a"])
        self.assertEqual([call["session_id"] for call in memory.memory_calls], ["chat-b", "chat-a"])

    def test_dream_retry_resumes_failed_stage_without_regeneration(self):
        class _Memory:
            def __init__(self):
                self.add_calls = []
                self.maintenance_failures = 1

            async def record_cognitive_feedback(self, **_kwargs):
                return None

            async def add_memory(self, **kwargs):
                self.add_calls.append(kwargs["session_id"])
                if kwargs["session_id"] == "chat-1" and self.maintenance_failures:
                    self.maintenance_failures -= 1
                    raise RuntimeError("temporary write failure")

        class _Agent:
            MIN_EVENTS_TO_DREAM = 5
            _last_session_id = "chat-1"

            def __init__(self):
                self.calls = 0

            async def run_dream_cycle(self, session_id=None):
                self.calls += 1
                return "dream log"

        class _Generator:
            async def generate(self, **_kwargs):
                return "dream text"

            def build_maintenance_result(self, _dream_log, session_id="global"):
                return {"summary": "maintenance", "tags": []}

        memory = _Memory()
        agent = _Agent()
        scheduler = self.dream_mod.DreamScheduler(
            context=SimpleNamespace(send_message=None),
            memory_engine=memory,
            config=SimpleNamespace(
                life=SimpleNamespace(dream_interval_min=1, min_memory_events_to_dream=5, dream_time_ranges=[]),
                persona=SimpleNamespace(name="Mai"),
            ),
            semaphore=asyncio.Semaphore(1),
            dream_visible=False,
        )
        scheduler.bind_dependencies(agent, _Generator())

        first = asyncio.run(scheduler.run_once_for_session("chat-1"))
        pending_after_first = scheduler.describe_status()["pending_completions"]
        second = asyncio.run(scheduler.run_once_for_session("chat-1"))

        self.assertFalse(first["performed"])
        self.assertTrue(first["degraded"])
        self.assertEqual(pending_after_first, 1)
        self.assertEqual(scheduler.describe_status()["pending_completions"], 0)
        self.assertTrue(second["performed"])
        self.assertEqual(agent.calls, 1)
        self.assertEqual(memory.add_calls.count("__dream_diary__"), 1)
        self.assertEqual(memory.add_calls.count("chat-1"), 2)

    def test_diary_service_should_run_covers_full_early_morning_window(self):
        service = self.diary_mod.DiaryService(
            persistence=None,
            memory_engine=None,
            config=None,
            call_background_lane=None,
            semaphore=None,
        )
        at_3 = time.mktime((2026, 7, 3, 3, 30, 0, 0, 0, -1))
        at_4 = time.mktime((2026, 7, 3, 4, 30, 0, 0, 0, -1))
        at_5 = time.mktime((2026, 7, 3, 5, 0, 0, 0, 0, -1))

        self.assertTrue(service.should_run("", at_3))
        self.assertTrue(service.should_run("", at_4))
        self.assertFalse(service.should_run("", at_5))
        self.assertFalse(service.should_run("2026-07-03", at_4))


if __name__ == "__main__":
    unittest.main()
