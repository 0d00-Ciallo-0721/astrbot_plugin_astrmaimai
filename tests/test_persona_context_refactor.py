import asyncio
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

        asyncio.run(
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
