import asyncio
import hashlib
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.original_ported.helpers import _install_astrbot_stubs


class _FakePersistence:
    def __init__(self, initial_cache=None):
        self.cache = dict(initial_cache or {})
        self.saved_snapshots = []

    def load_persona_cache(self):
        return dict(self.cache)

    def save_persona_cache(self, cache_data):
        self.cache = dict(cache_data)
        self.saved_snapshots.append(dict(cache_data))

    async def save_persona_cache_async(self, cache_data):
        self.save_persona_cache(cache_data)


class _FailingPersonaPersistence(_FakePersistence):
    async def save_persona_cache_async(self, cache_data):
        return False


class _CorruptingPersonaPersistence(_FakePersistence):
    def load_persona_cache(self):
        cache = super().load_persona_cache()
        for payload in cache.values():
            if not isinstance(payload, dict) or not payload.get("is_full_ready"):
                continue
            payload = dict(payload)
            payload["shards"] = dict(payload.get("shards", {}))
            payload["shards"].pop("secrets", None)
            cache = dict(cache)
            for cache_key, candidate in cache.items():
                if candidate.get("is_full_ready"):
                    cache[cache_key] = payload
                    break
        return cache


class _FakeGateway:
    def __init__(self, responses, config=None):
        self.responses = list(responses)
        self.calls = []
        if config is None:
            from config import AstrMaiConfig

            config = AstrMaiConfig(performance={"summary_threshold": 10})
        self.config = config

    async def call_persona_task(self, prompt, **kwargs):
        self.calls.append({"prompt": prompt, "kwargs": kwargs})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class _FakeDB:
    def get_chat_state(self, chat_id):
        return SimpleNamespace(mood=0.0, energy=0.8)

    def get_session(self):
        class _Ctx:
            def __enter__(self_inner):
                return SimpleNamespace(get=lambda *args, **kwargs: None)

            def __exit__(self_inner, exc_type, exc, tb):
                return False

        return _Ctx()


class _FakeEvent:
    def __init__(self, text):
        self.message_str = text


class _FakeMemoryEngine:
    def __init__(self):
        self.calls = []

    async def recall(self, query, session_id=""):
        self.calls.append((query, session_id))
        return "should not be used"


class _RecallMemoryEngine:
    def __init__(self):
        self.calls = []

    async def recall(self, query, session_id=""):
        self.calls.append((query, session_id))
        return "上周小明问过天气，我当时建议他带伞。"


class _FakePrivateProfilePersistence:
    async def load_user_profile(self, user_id):
        return {
            "nickname": "小明",
            "name": "张三",
            "tags": ["熟人", "夜猫子"],
            "persona_analysis": "说话慢热，但熟了之后会主动接梗。",
            "memory_points": ["昨晚聊过电影", "会在半夜突然发消息"],
            "identity_points": ["大学生"],
            "preference_points": ["喜欢悬疑片"],
        }


class _FakePrivateEvent:
    def get_sender_id(self):
        return "user-1"


class PersonaContextRefactorTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        sys.modules.pop("astrmai.memory.persona.persona_summarizer", None)
        sys.modules.pop("astrmai.conversation.planning.context_engine", None)
        self.persona_mod = importlib.import_module("astrmai.memory.persona.persona_summarizer")
        self.persona_mod = importlib.reload(self.persona_mod)
        self.context_mod = importlib.import_module("astrmai.conversation.planning.context_engine")
        self.context_mod = importlib.reload(self.context_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_persona_summary_generates_and_persists_first_person_rewrite(self):
        persistence = _FakePersistence()
        gateway = _FakeGateway(["core summary", "style summary", "I stay in character and answer naturally."])
        summarizer = self.persona_mod.PersonaSummarizer(persistence, gateway, config=gateway.config)

        async def _noop_background(*args, **kwargs):
            return None

        summarizer._generate_all_shards_background = _noop_background
        payload = asyncio.run(
            summarizer.get_summary(
                original_prompt="This is a long enough persona prompt for testing.",
                persona_id="persona-1",
            )
        )

        self.assertEqual(payload["first_person_rewrite"], "I stay in character and answer naturally.")
        self.assertEqual(
            persistence.cache["persona-1"]["first_person_rewrite"],
            "I stay in character and answer naturally.",
        )

    def test_persona_cache_hit_without_first_person_field_falls_back_safely(self):
        persistence = _FakePersistence(
            {
                "persona-1": {
                    "summary": "summary fallback",
                    "style": "style rules",
                    "shards": {},
                    "is_full_ready": True,
                    "raw": "raw persona",
                }
            }
        )
        gateway = _FakeGateway([RuntimeError("rewrite failed")])
        summarizer = self.persona_mod.PersonaSummarizer(persistence, gateway, config=gateway.config)

        payload = asyncio.run(summarizer.get_summary(original_prompt="raw persona", persona_id="persona-1"))

        self.assertEqual(payload["first_person_rewrite"], "summary fallback")
        self.assertEqual(payload["summary"], "summary fallback")

    def test_persona_summary_reads_threshold_from_real_performance_config(self):
        from config import AstrMaiConfig

        persistence = _FakePersistence()
        gateway = _FakeGateway([], config=AstrMaiConfig(performance={"summary_threshold": 100}))
        summarizer = self.persona_mod.PersonaSummarizer(persistence, gateway, config=gateway.config)

        payload = asyncio.run(
            summarizer.get_summary(
                original_prompt="short prompt",
                persona_id="persona-threshold",
            )
        )

        self.assertEqual(payload["summary"], "short prompt")
        self.assertEqual(payload["first_person_rewrite"], "short prompt")
        self.assertEqual(gateway.calls, [])

    def test_persona_core_is_not_ready_when_strict_persistence_fails(self):
        from config import AstrMaiConfig

        persistence = _FailingPersonaPersistence()
        gateway = _FakeGateway(
            ["core summary long enough", "strict style", "I remain fully in character."],
            config=AstrMaiConfig(performance={"summary_threshold": 5}),
        )
        summarizer = self.persona_mod.PersonaSummarizer(persistence, gateway, config=gateway.config)

        with self.assertRaises(OSError):
            asyncio.run(summarizer.ensure_core_ready("long persona prompt", persona_id="strict-persona"))

        self.assertFalse(summarizer.cache["strict-persona"].get("core_ready", False))

    def test_persona_enrichment_retries_and_resumes_completed_shards(self):
        from config import AstrMaiConfig

        raw_prompt = "long persona prompt"
        persistence = _FakePersistence(
            {
                "persona-retry": {
                    "summary": "core",
                    "style": "style",
                    "first_person_rewrite": "I stay in character.",
                    "core_components": {
                        "summary": "completed",
                        "style": "completed",
                        "first_person_rewrite": "completed",
                    },
                    "core_ready": True,
                    "raw": raw_prompt,
                    "raw_hash": hashlib.sha256(raw_prompt.encode("utf-8")).hexdigest(),
                    "shards": {"logic_style": "saved logic"},
                    "shard_status": {"logic_style": "completed"},
                    "self_lore_ready": True,
                    "is_full_ready": False,
                }
            }
        )
        config = AstrMaiConfig(
            performance={"summary_threshold": 5},
            persona={"retry_interval_sec": 1, "retry_max_interval_sec": 1},
        )
        gateway = _FakeGateway([], config=config)
        summarizer = self.persona_mod.PersonaSummarizer(persistence, gateway, config=config)
        summarizer._retry_delay_bounds = lambda: (0.0, 0.0)
        calls = {name: 0 for name in summarizer.REQUIRED_SHARDS}

        def _builder(name):
            async def _run(_prompt, _cache_key, **_kwargs):
                calls[name] += 1
                if name == "speech_style" and calls[name] == 1:
                    raise RuntimeError("temporary model timeout")
                return f"{name} result"

            return _run

        for shard_name in summarizer.REQUIRED_SHARDS:
            setattr(summarizer, f"_summarize_{shard_name}", _builder(shard_name))

        asyncio.run(summarizer._run_enrichment_until_complete(raw_prompt, "persona-retry"))

        payload = summarizer.cache["persona-retry"]
        self.assertTrue(payload["is_full_ready"])
        self.assertEqual(calls["logic_style"], 0)
        self.assertEqual(calls["speech_style"], 2)
        self.assertEqual(set(payload["shard_status"]), set(summarizer.REQUIRED_SHARDS))

    def test_persona_enrichment_does_not_report_full_when_persisted_shards_are_incomplete(self):
        from config import AstrMaiConfig

        raw_prompt = "long persona prompt"
        persistence = _CorruptingPersonaPersistence(
            {
                "persona-corrupt": {
                    "summary": "core",
                    "style": "style",
                    "first_person_rewrite": "I stay in character.",
                    "core_components": {
                        "summary": "completed",
                        "style": "completed",
                        "first_person_rewrite": "completed",
                    },
                    "core_ready": True,
                    "raw": raw_prompt,
                    "raw_hash": hashlib.sha256(raw_prompt.encode("utf-8")).hexdigest(),
                    "shards": {},
                    "shard_status": {},
                    "self_lore_ready": True,
                    "is_full_ready": False,
                }
            }
        )
        config = AstrMaiConfig(performance={"summary_threshold": 5})
        gateway = _FakeGateway([], config=config)
        summarizer = self.persona_mod.PersonaSummarizer(persistence, gateway, config=config)

        for shard_name in summarizer.REQUIRED_SHARDS:
            async def _build(_prompt, _cache_key, _name=shard_name, **_kwargs):
                return f"{_name} result"

            setattr(summarizer, f"_summarize_{shard_name}", _build)

        with self.assertRaises(OSError):
            asyncio.run(
                summarizer._generate_all_shards_background(
                    raw_prompt,
                    "persona-corrupt",
                    raise_on_failure=True,
                )
            )

        self.assertFalse(summarizer.cache["persona-corrupt"]["is_full_ready"])

    def test_persona_core_cache_downgrades_false_full_marker_and_schedules_enrichment(self):
        raw_prompt = "long persona prompt"
        persistence = _FakePersistence(
            {
                "persona-false-full": {
                    "summary": "core",
                    "style": "style",
                    "first_person_rewrite": "I stay in character.",
                    "core_components": {
                        "summary": "completed",
                        "style": "completed",
                        "first_person_rewrite": "completed",
                    },
                    "core_ready": True,
                    "raw": raw_prompt,
                    "raw_hash": hashlib.sha256(raw_prompt.encode("utf-8")).hexdigest(),
                    "shards": {"logic_style": "logic"},
                    "shard_status": {"logic_style": "completed"},
                    "self_lore_ready": True,
                    "is_full_ready": True,
                }
            }
        )
        gateway = _FakeGateway([])
        summarizer = self.persona_mod.PersonaSummarizer(persistence, gateway, config=gateway.config)
        started = []

        def _start(_prompt, cache_key):
            started.append(cache_key)
            return None

        summarizer._start_shard_task = _start
        payload = asyncio.run(
            summarizer.get_summary(raw_prompt, persona_id="persona-false-full")
        )

        self.assertFalse(payload["is_full_ready"])
        self.assertEqual(started, ["persona-false-full"])

    def test_persona_self_lore_empty_write_is_retried_instead_of_marked_ready(self):
        from config import AstrMaiConfig

        class _Memory:
            async def clear_persona_lore(self, _persona_id):
                return 0

            async def add_persona_lore(self, _content, _persona_id):
                return ""

        raw_prompt = "long persona prompt"
        persistence = _FakePersistence(
            {
                "persona-lore": {
                    "summary": "core",
                    "style": "style",
                    "first_person_rewrite": "I stay in character.",
                    "core_ready": True,
                    "raw": raw_prompt,
                    "shards": {},
                    "shard_status": {},
                    "self_lore_ready": False,
                    "is_full_ready": False,
                }
            }
        )
        config = AstrMaiConfig(
            performance={"summary_threshold": 5},
            persona={"include_self_lore_in_prompt": True},
        )
        summarizer = self.persona_mod.PersonaSummarizer(
            persistence,
            _FakeGateway([], config=config),
            config=config,
            memory_engine=_Memory(),
        )

        with self.assertRaises(RuntimeError):
            asyncio.run(
                summarizer._generate_all_shards_background(
                    raw_prompt,
                    "persona-lore",
                    raise_on_failure=True,
                )
            )

        self.assertFalse(summarizer.cache["persona-lore"]["self_lore_ready"])

    def test_persona_summarizer_refresh_config_updates_only_config_reference(self):
        persistence = _FakePersistence()
        gateway = _FakeGateway([])
        summarizer = self.persona_mod.PersonaSummarizer(persistence, gateway, config=gateway.config)
        cache = summarizer.cache
        pending_tasks = summarizer.pending_tasks
        new_config = SimpleNamespace(performance=SimpleNamespace(summary_threshold=123))

        summarizer.refresh_config(new_config)

        self.assertIs(summarizer.config, new_config)
        self.assertIs(summarizer.cache, cache)
        self.assertIs(summarizer.pending_tasks, pending_tasks)

    def test_persona_summary_empty_prompt_uses_ready_fallback_without_gateway(self):
        from config import AstrMaiConfig

        persistence = _FakePersistence()
        gateway = _FakeGateway([], config=AstrMaiConfig(performance={"summary_threshold": 10}))
        summarizer = self.persona_mod.PersonaSummarizer(persistence, gateway, config=gateway.config)

        payload = asyncio.run(summarizer.get_summary(original_prompt="", persona_id="persona-empty"))

        self.assertEqual(payload["summary"], "")
        self.assertEqual(payload["first_person_rewrite"], "")
        self.assertTrue(payload["is_full_ready"])
        self.assertEqual(payload["raw"], "")
        self.assertEqual(gateway.calls, [])

    def test_persona_summary_concurrent_requests_share_single_generation(self):
        from config import AstrMaiConfig

        persistence = _FakePersistence()
        gateway = _FakeGateway([], config=AstrMaiConfig(performance={"summary_threshold": 5}))
        summarizer = self.persona_mod.PersonaSummarizer(persistence, gateway, config=gateway.config)
        calls = {"core": 0, "style": 0, "rewrite": 0, "background": 0}

        async def _core(_original_prompt, _cache_key):
            calls["core"] += 1
            await asyncio.sleep(0)
            return "core summary"

        async def _style(_original_prompt, _cache_key):
            calls["style"] += 1
            await asyncio.sleep(0)
            return "style summary"

        async def _rewrite(**_kwargs):
            calls["rewrite"] += 1
            return "I answer in my own voice."

        async def _background(*_args, **_kwargs):
            calls["background"] += 1

        summarizer._summarize_core_identity_with_retry = _core
        summarizer._summarize_style_with_retry = _style
        summarizer._build_first_person_rewrite = _rewrite
        summarizer._generate_all_shards_background = _background

        async def _run():
            return await asyncio.gather(
                summarizer.get_summary("long persona prompt", persona_id="persona-concurrent"),
                summarizer.get_summary("long persona prompt", persona_id="persona-concurrent"),
            )

        first, second = asyncio.run(_run())

        self.assertEqual(first["summary"], "core summary")
        self.assertEqual(second["summary"], "core summary")
        self.assertEqual(calls["core"], 1)
        self.assertEqual(calls["style"], 1)
        self.assertEqual(calls["rewrite"], 1)
        self.assertEqual(calls["background"], 1)

    def test_persona_background_shard_failure_keeps_cache_recoverable_and_clears_pending(self):
        persistence = _FakePersistence(
            {
                "persona-shards": {
                    "summary": "core",
                    "style": "style",
                    "shards": {},
                    "is_full_ready": False,
                    "raw": "raw persona",
                }
            }
        )
        gateway = _FakeGateway([])
        memory_engine = SimpleNamespace(
            clear_persona_lore=lambda _persona_id: asyncio.sleep(0),
            add_persona_lore=lambda _prompt, _persona_id: asyncio.sleep(0),
        )
        summarizer = self.persona_mod.PersonaSummarizer(
            persistence,
            gateway,
            config=gateway.config,
            memory_engine=memory_engine,
        )

        async def _logic(_original_prompt, _cache_key):
            return "logic"

        async def _speech(_original_prompt, _cache_key):
            raise RuntimeError("speech shard failed")

        summarizer._summarize_logic_style = _logic
        summarizer._summarize_speech_style = _speech
        summarizer.pending_tasks["persona-shards"] = object()

        asyncio.run(summarizer._generate_all_shards_background("raw persona", "persona-shards"))

        self.assertFalse(summarizer.cache["persona-shards"]["is_full_ready"])
        self.assertEqual(summarizer.cache["persona-shards"]["shards"], {"logic_style": "logic"})
        self.assertEqual(
            summarizer.cache["persona-shards"]["shard_status"],
            {"logic_style": "completed"},
        )
        self.assertNotIn("persona-shards", summarizer.pending_tasks)

    def test_persona_core_identity_template_and_fallback_use_same_expert_role_shell(self):
        persistence = _FakePersistence()
        gateway = _FakeGateway(["core summary"])
        summarizer = self.persona_mod.PersonaSummarizer(persistence, gateway, config=gateway.config)

        async def _run_template():
            return await summarizer._call_persona_template(
                self.persona_mod.PromptTemplateId.PERSONA_CORE_IDENTITY,
                original_prompt="Long persona prompt for testing.",
                cache_key="persona-template",
                is_json=False,
                fallback_prompt="fallback body",
                fallback_system_prompt="你是一个资深的角色扮演设定提取专家。",
            )

        asyncio.run(_run_template())
        template_call = gateway.calls[-1]
        self.assertEqual(
            template_call["kwargs"]["system_prompt"].split("\n\n")[0],
            "你是一个资深的角色扮演设定提取专家。",
        )

        summarizer.prompt_registry = None
        fallback_gateway = _FakeGateway(["core summary"])
        summarizer.gateway = fallback_gateway

        async def _run_fallback():
            return await summarizer._call_persona_template(
                self.persona_mod.PromptTemplateId.PERSONA_CORE_IDENTITY,
                original_prompt="Long persona prompt for testing.",
                cache_key="persona-fallback",
                is_json=False,
                fallback_prompt="fallback body",
                fallback_system_prompt="你是一个资深的角色扮演设定提取专家。",
            )

        asyncio.run(_run_fallback())
        fallback_call = fallback_gateway.calls[-1]
        self.assertEqual(
            fallback_call["kwargs"]["system_prompt"],
            "你是一个资深的角色扮演设定提取专家。",
        )

    def test_persona_remaining_shards_use_expected_templates(self):
        persistence = _FakePersistence()
        gateway = _FakeGateway([])
        summarizer = self.persona_mod.PersonaSummarizer(persistence, gateway, config=gateway.config)
        calls = []

        async def _call_template(template_id, **kwargs):
            calls.append((template_id, kwargs))
            return f"shard:{template_id.value}"

        summarizer._call_persona_template = _call_template
        shard_methods = [
            (
                summarizer._summarize_world_view,
                self.persona_mod.PromptTemplateId.PERSONA_WORLD_VIEW,
            ),
            (
                summarizer._summarize_timeline,
                self.persona_mod.PromptTemplateId.PERSONA_TIMELINE,
            ),
            (
                summarizer._summarize_relations,
                self.persona_mod.PromptTemplateId.PERSONA_RELATIONS,
            ),
            (
                summarizer._summarize_skills,
                self.persona_mod.PromptTemplateId.PERSONA_SKILLS,
            ),
            (
                summarizer._summarize_values,
                self.persona_mod.PromptTemplateId.PERSONA_VALUES,
            ),
            (
                summarizer._summarize_secrets,
                self.persona_mod.PromptTemplateId.PERSONA_SECRETS,
            ),
        ]

        async def _run():
            return [
                await method("raw persona facts", "persona-shards")
                for method, _template_id in shard_methods
            ]

        results = asyncio.run(_run())

        self.assertEqual(
            results,
            [f"shard:{template_id.value}" for _method, template_id in shard_methods],
        )
        self.assertEqual(
            [template_id for template_id, _kwargs in calls],
            [template_id for _method, template_id in shard_methods],
        )
        for _template_id, kwargs in calls:
            self.assertEqual(kwargs["original_prompt"], "raw persona facts")
            self.assertEqual(kwargs["cache_key"], "persona-shards")
            self.assertFalse(kwargs["is_json"])
            self.assertIn("raw persona facts", kwargs["fallback_prompt"])

    def test_persona_cache_recovery_creates_background_task_when_not_ready(self):
        persistence = _FakePersistence(
            {
                "persona-recovery": {
                    "summary": "cached summary",
                    "first_person_rewrite": "I remember who I am.",
                    "style": "brief",
                    "shards": {},
                    "is_full_ready": False,
                    "raw": "cached raw persona",
                }
            }
        )
        gateway = _FakeGateway([])
        summarizer = self.persona_mod.PersonaSummarizer(persistence, gateway, config=gateway.config)

        async def _run():
            started = asyncio.Event()
            release = asyncio.Event()
            calls = []

            async def _background(raw_text, cache_key):
                calls.append((raw_text, cache_key))
                started.set()
                await release.wait()

            summarizer._generate_all_shards_background = _background
            payload = await summarizer.get_summary(
                original_prompt="cached raw persona",
                persona_id="persona-recovery",
            )
            await started.wait()
            task = summarizer.pending_tasks["persona-recovery"]
            self.assertFalse(task.done())
            release.set()
            await task
            await asyncio.sleep(0)
            return payload, calls

        payload, calls = asyncio.run(_run())

        self.assertEqual(payload["summary"], "cached summary")
        self.assertEqual(calls, [("cached raw persona", "persona-recovery")])
        self.assertNotIn("persona-recovery", summarizer.pending_tasks)

    def test_first_person_rewrite_without_template_uses_persona_lane(self):
        persistence = _FakePersistence()
        gateway = _FakeGateway([])
        summarizer = self.persona_mod.PersonaSummarizer(persistence, gateway, config=gateway.config)
        summarizer.prompt_registry = None
        calls = []

        async def _call_lane(prompt, cache_key, **kwargs):
            calls.append((prompt, cache_key, kwargs))
            return "I answer briefly in my own voice."

        summarizer._call_persona_lane = _call_lane

        result = asyncio.run(
            summarizer._build_first_person_rewrite(
                original_prompt="raw persona",
                summary="brief summary",
                style="calm",
                cache_key="persona-1",
            )
        )

        self.assertEqual(result, "I answer briefly in my own voice.")
        self.assertEqual(calls[0][1], "persona-1")
        self.assertIn("brief summary", calls[0][0])
        self.assertEqual(
            calls[0][2]["system_prompt"],
            "Rewrite persona summaries into concise first-person self-awareness text.",
        )
        self.assertFalse(calls[0][2]["is_json"])

    def test_first_person_rewrite_with_template_passes_envelope_to_persona_lane(self):
        persistence = _FakePersistence()
        gateway = _FakeGateway([])
        summarizer = self.persona_mod.PersonaSummarizer(persistence, gateway, config=gateway.config)
        rendered = []
        lane_calls = []
        envelope = SimpleNamespace(prompt="template prompt", system_prompt="template system")

        def _render(template_id, variables):
            rendered.append((template_id, variables))
            return envelope

        async def _call_lane(prompt, cache_key, **kwargs):
            lane_calls.append((prompt, cache_key, kwargs))
            return "I use the rendered template."

        summarizer.prompt_registry = SimpleNamespace(render_template=_render)
        summarizer._call_persona_lane = _call_lane

        result = asyncio.run(
            summarizer._build_first_person_rewrite(
                original_prompt="raw persona",
                summary="brief summary",
                style="calm",
                cache_key="persona-1",
            )
        )

        self.assertEqual(result, "I use the rendered template.")
        self.assertEqual(
            rendered,
            [
                (
                    self.persona_mod.PromptTemplateId.PERSONA_FIRST_PERSON_REWRITE,
                    {
                        "original_prompt": "raw persona",
                        "summary": "brief summary",
                        "style": "calm",
                    },
                )
            ],
        )
        self.assertEqual(lane_calls[0][0:2], ("template prompt", "persona-1"))
        self.assertEqual(lane_calls[0][2]["system_prompt"], "template system")
        self.assertIs(lane_calls[0][2]["template_envelope"], envelope)

    def test_persona_cache_hit_without_id_persists_generated_rewrite(self):
        persistence = _FakePersistence(
            {
                "session_chat-1": {
                    "summary": "cached summary",
                    "first_person_rewrite": "",
                    "style": "brief",
                    "shards": {},
                    "is_full_ready": True,
                    "raw": "cached raw persona",
                }
            }
        )
        gateway = _FakeGateway([])
        summarizer = self.persona_mod.PersonaSummarizer(persistence, gateway, config=gateway.config)
        summarizer._build_first_person_rewrite = lambda **_kwargs: asyncio.sleep(
            0,
            result="I use the repaired rewrite.",
        )

        payload = asyncio.run(
            summarizer.get_summary(
                original_prompt="cached raw persona",
                persona_id="",
                session_id="chat-1",
            )
        )

        self.assertEqual(payload["first_person_rewrite"], "I use the repaired rewrite.")
        self.assertEqual(
            persistence.cache["session_chat-1"]["first_person_rewrite"],
            "I use the repaired rewrite.",
        )
        self.assertGreaterEqual(len(persistence.saved_snapshots), 1)

    def test_first_person_rewrite_rejects_empty_and_too_short_results(self):
        persistence = _FakePersistence()
        gateway = _FakeGateway([])
        summarizer = self.persona_mod.PersonaSummarizer(persistence, gateway, config=gateway.config)
        summarizer.prompt_registry = None

        self.assertEqual(
            asyncio.run(
                summarizer._build_first_person_rewrite(
                    original_prompt="",
                    summary="",
                    style="",
                    cache_key="persona-empty",
                )
            ),
            "",
        )
        summarizer._call_persona_lane = lambda *_args, **_kwargs: asyncio.sleep(0, result="no")
        self.assertEqual(
            asyncio.run(
                summarizer._build_first_person_rewrite(
                    original_prompt="raw",
                    summary="summary",
                    style="calm",
                    cache_key="persona-short",
                )
            ),
            "",
        )

    def test_persist_cache_falls_back_to_sync_persistence(self):
        class _SyncPersistence:
            def __init__(self):
                self.saved = None

            @staticmethod
            def load_persona_cache():
                return {}

            def save_persona_cache(self, cache_data):
                self.saved = dict(cache_data)

        persistence = _SyncPersistence()
        gateway = _FakeGateway([])
        summarizer = self.persona_mod.PersonaSummarizer(persistence, gateway, config=gateway.config)
        summarizer.cache["persona-1"] = {"summary": "cached"}

        asyncio.run(summarizer._persist_cache())

        self.assertEqual(persistence.saved, {"persona-1": {"summary": "cached"}})

    def test_context_engine_prefers_first_person_rewrite_and_honors_disable_rag_injection(self):
        memory_engine = _FakeMemoryEngine()

        class _FakeSummarizer:
            def __init__(self):
                self.gateway = SimpleNamespace(
                    config=SimpleNamespace(
                        persona=SimpleNamespace(persona_id="p1", prompt=""),
                        memory=SimpleNamespace(auto_recall_probability=1.0),
                    ),
                    context=SimpleNamespace(
                        shared_dict={"disable_rag_injection": True},
                        astrmai_plugin=SimpleNamespace(memory_engine=memory_engine),
                    ),
                )

            async def get_summary(self, original_prompt="", persona_id="", session_id=""):
                return {
                    "summary": "She is described in third person.",
                    "first_person_rewrite": "I know who I am and I answer in my own voice.",
                    "style": "brief and natural",
                    "shards": {},
                    "raw": "Raw persona",
                    "is_full_ready": True,
                }

        engine = self.context_mod.ContextEngine(db=_FakeDB(), persona_summarizer=_FakeSummarizer())

        async def _run():
            prompt_bundle = await engine.build_prompt(
                chat_id="default:GroupMessage:group-1",
                event_messages=[],
                retrieve_keys=[],
            )
            recall_block = await engine._build_proactive_recall_block(
                chat_id="default:GroupMessage:group-1",
                event_messages=[_FakeEvent("记得之前那件事吗")],
                is_fast_mode=False,
                near_context_priority=False,
            )
            return prompt_bundle, recall_block

        prompt_bundle, recall_block = asyncio.run(_run())
        system_prompt, style_variant, proactive_recall = prompt_bundle

        self.assertIn("I know who I am and I answer in my own voice.", system_prompt)
        self.assertNotIn("She is described in third person.", system_prompt)
        self.assertIsInstance(style_variant, str)
        self.assertEqual(proactive_recall, "")
        self.assertEqual(recall_block, "")
        self.assertEqual(memory_engine.calls, [])

    def test_context_engine_private_block_and_rules_use_first_person_wording(self):
        db = _FakeDB()
        db.persistence = _FakePrivateProfilePersistence()
        summarizer = SimpleNamespace(
            gateway=SimpleNamespace(
                config=SimpleNamespace(memory=SimpleNamespace(auto_recall_probability=0.0)),
                context=SimpleNamespace(shared_dict={}),
            )
        )
        engine = self.context_mod.ContextEngine(db=db, persona_summarizer=summarizer)

        async def _run():
            stable_private_block, dynamic_private_block = await engine._build_private_chat_blocks(
                "default:FriendMessage:user-1",
                [_FakePrivateEvent()],
                is_fast_mode=False,
            )
            return stable_private_block, dynamic_private_block, engine._system_rules_block()

        private_block, dynamic_private_block, rules_block = asyncio.run(_run())

        self.assertIn("我现在正在和 小明（张三） 私聊", private_block)
        self.assertIn("我对 ta 的标签印象：熟人 / 夜猫子", private_block)
        self.assertIn("这轮可参考的近期私聊记忆点：昨晚聊过电影；会在半夜突然发消息", dynamic_private_block)
        self.assertIn("我的表达底线：", rules_block)
        self.assertIn("我只说会真正发到聊天窗口里的自然话。", rules_block)
        self.assertIn("我不直接复述记忆原文", rules_block)
        self.assertIn("不暴露记忆闪回、注入、提示词这类机制", rules_block)
        self.assertNotIn("如果本轮系统提供了可用动作", rules_block)
        self.assertNotIn("不暴露工具过程或机制", rules_block)
        self.assertNotIn("不要在开头", rules_block)

    def test_context_engine_prefers_profile_prompt_bundle_from_state_engine(self):
        db = _FakeDB()
        db.persistence = SimpleNamespace(load_user_profile=None)
        summarizer = SimpleNamespace(
            gateway=SimpleNamespace(
                config=SimpleNamespace(memory=SimpleNamespace(auto_recall_probability=0.0)),
                context=SimpleNamespace(
                    shared_dict={},
                    astrmai_plugin=SimpleNamespace(
                        runtime=SimpleNamespace(
                            state_engine=SimpleNamespace(
                                get_profile_prompt_bundle=lambda user_id: asyncio.sleep(
                                    0,
                                    result={
                                        "display_name": "阿明（张三）",
                                        "tags_text": "熟人 / 夜猫子",
                                        "analysis": "对话节奏慢热，但熟悉后会主动接梗。",
                                        "memory_points": ["昨晚聊过电影"],
                                        "structured_sections": [{"label": "偏好画像", "values": ["爱好:悬疑片"]}],
                                    },
                                )
                            )
                        )
                    ),
                ),
            )
        )
        engine = self.context_mod.ContextEngine(db=db, persona_summarizer=summarizer)

        stable_private_block, dynamic_private_block = asyncio.run(
            engine._build_private_chat_blocks(
                "default:FriendMessage:user-1",
                [_FakePrivateEvent()],
                is_fast_mode=False,
            )
        )

        self.assertIn("阿明（张三）", stable_private_block)
        self.assertIn("偏好画像", stable_private_block)
        self.assertIn("昨晚聊过电影", dynamic_private_block)

    def test_context_engine_wraps_proactive_recall_as_internal_reference(self):
        memory_engine = _RecallMemoryEngine()
        summarizer = SimpleNamespace(
            gateway=SimpleNamespace(
                config=SimpleNamespace(memory=SimpleNamespace(auto_recall_probability=0.0)),
                context=SimpleNamespace(
                    shared_dict={},
                    astrmai_plugin=SimpleNamespace(memory_engine=memory_engine),
                ),
            )
        )
        engine = self.context_mod.ContextEngine(db=_FakeDB(), persona_summarizer=summarizer)

        async def _run():
            return await engine._build_proactive_recall_block(
                chat_id="default:GroupMessage:group-1",
                event_messages=[_FakeEvent("你还记得之前天气那件事吗")],
                is_fast_mode=False,
                near_context_priority=False,
            )

        recall_block = asyncio.run(_run())

        self.assertEqual(memory_engine.calls, [])
        self.assertEqual(recall_block, "")

    def test_context_engine_keeps_agency_context_out_of_system_prompt(self):
        summarizer = SimpleNamespace(
            gateway=SimpleNamespace(
                config=SimpleNamespace(memory=SimpleNamespace(auto_recall_probability=0.0)),
                context=SimpleNamespace(shared_dict={}),
            )
        )
        engine = self.context_mod.ContextEngine(db=_FakeDB(), persona_summarizer=summarizer)

        async def _summary(*args, **kwargs):
            return {
                "summary": "summary",
                "first_person_rewrite": "I answer naturally.",
                "style": "brief",
                "shards": {},
                "raw": "raw",
                "is_full_ready": True,
            }

        engine.summarizer.get_summary = _summary

        system_prompt, _style_variant, proactive_recall = asyncio.run(
            engine.build_prompt(
                chat_id="default:GroupMessage:group-1",
                event_messages=[],
                retrieve_keys=[],
                agency_context="本轮姿态：克制反驳，最多一句。",
            )
        )

        self.assertNotIn("agency", system_prompt.lower())

    def test_context_engine_pushes_dynamic_state_and_behavior_out_of_system_prompt(self):
        summarizer = SimpleNamespace(
            gateway=SimpleNamespace(
                config=SimpleNamespace(memory=SimpleNamespace(auto_recall_probability=0.0)),
                context=SimpleNamespace(shared_dict={}),
            )
        )
        engine = self.context_mod.ContextEngine(db=_FakeDB(), persona_summarizer=summarizer)

        async def _summary(*args, **kwargs):
            return {
                "summary": "summary",
                "first_person_rewrite": "I answer naturally.",
                "style": "brief",
                "shards": {},
                "raw": "raw",
                "is_full_ready": True,
            }

        engine.summarizer.get_summary = _summary
        envelope = importlib.import_module("astrmai.conversation.contracts.prompt_envelope").PromptEnvelope(
            reply_mode=importlib.import_module("astrmai.conversation.contracts.prompt_envelope").ReplyMode.IMAGE_REACTION,
        )

        system_prompt, _style_variant, _proactive_recall = asyncio.run(
            engine.build_prompt(
                chat_id="default:GroupMessage:group-1",
                event_messages=[],
                retrieve_keys=[],
                prompt_envelope=envelope,
            )
        )

        self.assertNotIn("此刻回应倾向", system_prompt)
        self.assertNotIn("我现在心情", system_prompt)
        self.assertIn("此刻回应倾向", envelope.situational_context_block)
        self.assertIn("我现在心情", envelope.situational_context_block)
        self.assertEqual(_proactive_recall, "")

    def test_context_engine_moves_stable_expression_and_jargon_into_soft_background(self):
        summarizer = SimpleNamespace(
            gateway=SimpleNamespace(
                config=SimpleNamespace(memory=SimpleNamespace(auto_recall_probability=0.0)),
                context=SimpleNamespace(shared_dict={}),
            )
        )
        engine = self.context_mod.ContextEngine(db=_FakeDB(), persona_summarizer=summarizer)

        async def _summary(*args, **kwargs):
            return {
                "summary": "summary",
                "first_person_rewrite": "I answer naturally.",
                "style": "brief",
                "shards": {},
                "raw": "raw",
                "is_full_ready": True,
            }

        engine.summarizer.get_summary = _summary
        envelope = importlib.import_module("astrmai.conversation.contracts.prompt_envelope").PromptEnvelope()

        system_prompt, _style_variant, _proactive_recall = asyncio.run(
            engine.build_prompt(
                chat_id="default:GroupMessage:group-1",
                event_messages=[],
                retrieve_keys=[],
                prompt_envelope=envelope,
                stable_expression_habits="Use short fragments.\nKeep this turn short; avoid another long reply.",
                situational_style_cues="群里最近会说：摸了、开摆",
                stable_jargon_explanation="黑话说明：DDL 指截止时间",
            )
        )

        self.assertNotIn("Use short fragments.", system_prompt)
        self.assertNotIn("黑话说明：DDL 指截止时间", system_prompt)
        self.assertNotIn("Keep this turn short; avoid another long reply.", system_prompt)
        self.assertNotIn("我会先回应眼前这条消息，不突然另起话题。", system_prompt)
        self.assertNotIn("我会优先回应当前这条消息，不突然另起话题。", system_prompt)
        self.assertNotIn("群里最近会说：摸了、开摆", system_prompt)
        self.assertIn("Use short fragments.", envelope.soft_background_block)
        self.assertIn("黑话说明：DDL 指截止时间", envelope.soft_background_block)
        self.assertIn("Keep this turn short; avoid another long reply.", envelope.soft_background_block)
        self.assertIn("群里最近会说：摸了、开摆", envelope.situational_context_block)
        self.assertNotIn("Keep this turn short; avoid another long reply.", envelope.situational_context_block)
        self.assertIn("我会先回应眼前这条消息，不突然另起话题。", envelope.planner_runtime_instruction_block)
        self.assertIn("我会优先回应当前这条消息，不突然另起话题。", envelope.planner_runtime_instruction_block)

    def test_context_engine_no_longer_splits_expression_text_for_dynamic_turn_cues(self):
        summarizer = SimpleNamespace(
            gateway=SimpleNamespace(
                config=SimpleNamespace(memory=SimpleNamespace(auto_recall_probability=0.0)),
                context=SimpleNamespace(shared_dict={}),
            )
        )
        engine = self.context_mod.ContextEngine(db=_FakeDB(), persona_summarizer=summarizer)

        async def _summary(*args, **kwargs):
            return {
                "summary": "summary",
                "first_person_rewrite": "I answer naturally.",
                "style": "brief",
                "shards": {},
                "raw": "raw",
                "is_full_ready": True,
            }

        engine.summarizer.get_summary = _summary
        envelope = importlib.import_module("astrmai.conversation.contracts.prompt_envelope").PromptEnvelope()

        system_prompt, _style_variant, _proactive_recall = asyncio.run(
            engine.build_prompt(
                chat_id="default:GroupMessage:group-1",
                event_messages=[],
                retrieve_keys=[],
                prompt_envelope=envelope,
                stable_expression_habits="Use short fragments.\nKeep this turn short; avoid another long reply.",
                situational_style_cues="群里最近会说：摸了、开摆",
                stable_jargon_explanation="黑话说明：DDL 指截止时间",
            )
        )

        self.assertNotIn("Keep this turn short; avoid another long reply.", system_prompt)
        self.assertIn("Keep this turn short; avoid another long reply.", envelope.soft_background_block)
        self.assertNotIn("Keep this turn short; avoid another long reply.", envelope.situational_context_block)
        self.assertIn("群里最近会说：摸了、开摆", envelope.situational_context_block)

    def test_context_engine_accepts_legacy_kwargs_as_compatibility_aliases(self):
        summarizer = SimpleNamespace(
            gateway=SimpleNamespace(
                config=SimpleNamespace(memory=SimpleNamespace(auto_recall_probability=0.0)),
                context=SimpleNamespace(shared_dict={}),
            )
        )
        engine = self.context_mod.ContextEngine(db=_FakeDB(), persona_summarizer=summarizer)

        async def _summary(*args, **kwargs):
            return {
                "summary": "summary",
                "first_person_rewrite": "I answer naturally.",
                "style": "brief",
                "shards": {},
                "raw": "raw",
                "is_full_ready": True,
            }

        engine.summarizer.get_summary = _summary
        envelope = importlib.import_module("astrmai.conversation.contracts.prompt_envelope").PromptEnvelope()

        system_prompt, _style_variant, _proactive_recall = asyncio.run(
            engine.build_prompt(
                chat_id="default:GroupMessage:group-1",
                event_messages=[],
                retrieve_keys=[],
                prompt_envelope=envelope,
                expression_habits="legacy habit",
                slang_patterns="legacy slang",
                jargon_explanation="legacy jargon",
            )
        )

        self.assertNotIn("legacy habit", system_prompt)
        self.assertNotIn("legacy jargon", system_prompt)
        self.assertIn("legacy habit", envelope.soft_background_block)
        self.assertIn("legacy jargon", envelope.soft_background_block)
        self.assertIn("legacy slang", envelope.situational_context_block)
        self.assertNotIn("legacy slang", system_prompt)

    def test_context_engine_prefers_new_kwargs_over_legacy_aliases(self):
        summarizer = SimpleNamespace(
            gateway=SimpleNamespace(
                config=SimpleNamespace(memory=SimpleNamespace(auto_recall_probability=0.0)),
                context=SimpleNamespace(shared_dict={}),
            )
        )
        engine = self.context_mod.ContextEngine(db=_FakeDB(), persona_summarizer=summarizer)

        async def _summary(*args, **kwargs):
            return {
                "summary": "summary",
                "first_person_rewrite": "I answer naturally.",
                "style": "brief",
                "shards": {},
                "raw": "raw",
                "is_full_ready": True,
            }

        engine.summarizer.get_summary = _summary
        envelope = importlib.import_module("astrmai.conversation.contracts.prompt_envelope").PromptEnvelope()

        system_prompt, _style_variant, _proactive_recall = asyncio.run(
            engine.build_prompt(
                chat_id="default:GroupMessage:group-1",
                event_messages=[],
                retrieve_keys=[],
                prompt_envelope=envelope,
                stable_expression_habits="new habit",
                situational_style_cues="new slang",
                stable_jargon_explanation="new jargon",
                expression_habits="legacy habit",
                slang_patterns="legacy slang",
                jargon_explanation="legacy jargon",
            )
        )

        self.assertNotIn("new habit", system_prompt)
        self.assertNotIn("new jargon", system_prompt)
        self.assertNotIn("legacy habit", system_prompt)
        self.assertNotIn("legacy jargon", system_prompt)
        self.assertIn("new habit", envelope.soft_background_block)
        self.assertIn("new jargon", envelope.soft_background_block)
        self.assertNotIn("legacy habit", envelope.soft_background_block)
        self.assertNotIn("legacy jargon", envelope.soft_background_block)
        self.assertIn("new slang", envelope.situational_context_block)
        self.assertNotIn("legacy slang", envelope.situational_context_block)

    def test_context_engine_records_prefix_block_lengths_in_status(self):
        summarizer = SimpleNamespace(
            gateway=SimpleNamespace(
                config=SimpleNamespace(memory=SimpleNamespace(auto_recall_probability=0.0)),
                context=SimpleNamespace(shared_dict={}),
            )
        )
        engine = self.context_mod.ContextEngine(db=_FakeDB(), persona_summarizer=summarizer)

        async def _summary(*args, **kwargs):
            return {
                "summary": "summary",
                "first_person_rewrite": "I answer naturally.",
                "style": "brief",
                "shards": {},
                "raw": "raw",
                "is_full_ready": True,
            }

        engine.summarizer.get_summary = _summary
        envelope = importlib.import_module("astrmai.conversation.contracts.prompt_envelope").PromptEnvelope()

        system_prompt, _, _ = asyncio.run(
            engine.build_prompt(
                chat_id="default:GroupMessage:group-1",
                event_messages=[],
                retrieve_keys=[],
                prompt_envelope=envelope,
                stable_expression_habits="Use short fragments.",
                situational_style_cues="群里最近会说：摸了、开摆",
                stable_jargon_explanation="黑话说明：DDL 指截止时间",
            )
        )

        status = engine.get_last_prefix_status("default:GroupMessage:group-1")
        self.assertEqual(status["prefix_changed_reason"], "first_seen")
        self.assertTrue(status["semantic_system_hash"])
        self.assertGreater(status["semantic_system_length"], 0)
        self.assertGreater(status["frozen_prefix_length"], 0)
        self.assertGreaterEqual(status["semi_stable_length"], 0)
        self.assertIn("persona_core", status["frozen_prefix_blocks"])
        self.assertIn("style_block", status["frozen_prefix_blocks"])
        self.assertIn("addressing_boundary", status["frozen_prefix_blocks"])
        self.assertIn("system_rules", status["frozen_prefix_blocks"])
        self.assertIn("cold_summary", status["semi_stable_blocks"])
        self.assertIn("stable_expression", status["semi_stable_blocks"])
        self.assertGreater(status["frozen_prefix_blocks"]["persona_core"], 0)
        self.assertTrue(status["system_rules_items"])
        self.assertIn("current_message_first", status["system_rules_candidate_items"])
        self.assertIn(
            "current_message_first",
            {item["key"] for item in status["system_rules_items"]},
        )
        self.assertIn(
            "addressing_scope",
            {item["key"] for item in status["system_rules_items"]},
        )
        self.assertIn("称呼与关系边界", system_prompt)

    def test_context_engine_compresses_cold_summary_for_soft_background(self):
        dialogue_store = SimpleNamespace(
            get_cold_summary=lambda chat_id: asyncio.sleep(
                0,
                result="后来我们围绕考试焦虑聊了很多。然后她提到想把复习计划重新排一下。接着又说如果明天还有时间就继续补数学。最后还在想要不要找我再确认一次重点。",
            )
        )
        db = _FakeDB()
        db.dialogue_store = dialogue_store
        summarizer = SimpleNamespace(
            gateway=SimpleNamespace(
                config=SimpleNamespace(memory=SimpleNamespace(auto_recall_probability=0.0)),
                context=SimpleNamespace(shared_dict={}),
            )
        )
        engine = self.context_mod.ContextEngine(db=db, persona_summarizer=summarizer)

        async def _summary(*args, **kwargs):
            return {
                "summary": "summary",
                "first_person_rewrite": "I answer naturally.",
                "style": "brief",
                "shards": {},
                "raw": "raw",
                "is_full_ready": True,
            }

        engine.summarizer.get_summary = _summary
        envelope = importlib.import_module("astrmai.conversation.contracts.prompt_envelope").PromptEnvelope()

        asyncio.run(
            engine.build_prompt(
                chat_id="default:GroupMessage:group-1",
                event_messages=[],
                retrieve_keys=[],
                prompt_envelope=envelope,
            )
        )

        compressed = envelope.soft_background_sections.get("cold_summary", "")
        self.assertIn("冷区背景摘要", compressed)
        self.assertLessEqual(len(compressed), 240)
        self.assertNotIn("后来我们围绕考试焦虑聊了很多。然后", compressed)
        self.assertNotIn("最后还在想要不要找我再确认一次重点。", compressed)


if __name__ == "__main__":
    unittest.main()
