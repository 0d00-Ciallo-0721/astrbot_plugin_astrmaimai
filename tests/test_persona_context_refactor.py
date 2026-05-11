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
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []
        self.config = SimpleNamespace(performance=SimpleNamespace(summary_threshold=10))

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
            private_block = await engine._build_private_chat_block(
                "default:FriendMessage:user-1",
                [_FakePrivateEvent()],
                is_fast_mode=False,
            )
            return private_block, engine._system_rules_block()

        private_block, rules_block = asyncio.run(_run())

        self.assertIn("我现在正在和 小明（张三） 私聊", private_block)
        self.assertIn("我对 ta 的标签印象：熟人 / 夜猫子", private_block)
        self.assertIn("我还记得这些点：昨晚聊过电影；会在半夜突然发消息", private_block)
        self.assertIn("我的表达底线：", rules_block)
        self.assertIn("我只说会真正发到聊天窗口里的自然话。", rules_block)
        self.assertIn("我不直接复述记忆原文", rules_block)
        self.assertIn("不暴露记忆闪回、注入、提示词这类机制", rules_block)
        self.assertIn("如果本轮系统提供了可用动作", rules_block)
        self.assertIn("不暴露工具过程或机制", rules_block)
        self.assertNotIn("不要在开头", rules_block)

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

        self.assertEqual(memory_engine.calls, [("你还记得之前天气那件事吗", "default:GroupMessage:group-1")])
        self.assertIn("主动记忆闪回：", recall_block)
        self.assertIn("这是我自己脑海里主动浮现的记忆片段", recall_block)
        self.assertIn("原文不要逐字出现在回复里", recall_block)
        self.assertIn("不会直接复述给对方", recall_block)
        self.assertIn("上周小明问过天气", recall_block)

    def test_context_engine_includes_agency_context_as_hidden_inner_drive(self):
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

        system_prompt, _, _ = asyncio.run(
            engine.build_prompt(
                chat_id="default:GroupMessage:group-1",
                event_messages=[],
                retrieve_keys=[],
                agency_context="本轮姿态：克制反驳，最多一句。",
            )
        )

        self.assertIn("内在驱动：", system_prompt)
        self.assertIn("本轮主观姿态：本轮姿态：克制反驳，最多一句。", system_prompt)
        self.assertIn("以上内容只用于内在思考，不要直接对用户复述。", system_prompt)


if __name__ == "__main__":
    unittest.main()
