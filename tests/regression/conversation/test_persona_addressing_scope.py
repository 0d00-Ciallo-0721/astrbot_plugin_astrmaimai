"""P0 OOC 回归：人格默认称呼与条件关系称呼必须分层。"""

import asyncio
import unittest
from types import SimpleNamespace

from astrmai.conversation.planning.context_engine import ContextEngine
from astrmai.conversation.planning.planner_prompt_context import PlannerPromptContextMixin
from astrmai.conversation.planning.prompt_refiner import PromptRefiner
from astrmai.infrastructure.context_economy import PromptTemplateId, PromptTemplateRegistry
from astrmai.memory.persona.persona_summarizer import PersonaSummarizer


class _Event:
    def __init__(self, sender_id="222", sender_name="普通群友"):
        self._extras = {}
        self._sender_id = sender_id
        self._sender_name = sender_name
        self.message_str = "早安"
        self.unified_msg_origin = "ff:GroupMessage:7000"

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name


class PersonaAddressingScopeTests(unittest.TestCase):
    def test_structured_persona_core_is_rendered_before_legacy_fields(self):
        engine = ContextEngine.__new__(ContextEngine)
        block = engine._build_role_block(
            {
                "summary": "旧摘要",
                "first_person_rewrite": "旧自述",
                "style": "旧风格",
                "persona_core": {
                    "identity_core": "稳定身份",
                    "voice_style": "稳定语气",
                    "behavior_policy": "先接住情绪再回答",
                    "relationship_rules": "默认称呼为你",
                    "values_boundaries": "不编造事实",
                },
                "shards": {"world_view": "不应自动注入的世界观"},
            },
            retrieve_keys=[],
            is_fast_mode=True,
        )

        for value in ("稳定身份", "先接住情绪再回答", "默认称呼为你", "不编造事实"):
            self.assertIn(value, block)
        self.assertEqual(engine._build_style_block({"persona_core": {"voice_style": "稳定语气"}}), "稳定语气")
        self.assertNotIn("不应自动注入的世界观", block)

    def test_legacy_persona_payload_normalizes_without_inventing_missing_core(self):
        payload = {
            "summary": "核心摘要",
            "style": "语言规则",
            "shards": {"values": "价值切片"},
        }

        core = PersonaSummarizer.normalize_structured_core(payload)

        self.assertEqual(core["identity_core"], "核心摘要")
        self.assertEqual(core["voice_style"], "语言规则")
        self.assertEqual(core["behavior_policy"], "")
        self.assertEqual(core["values_boundaries"], "")
        self.assertEqual(payload["persona_schema_version"], PersonaSummarizer.PERSONA_SCHEMA_VERSION)
        self.assertIn("behavior_policy", payload["persona_core_fields_missing"])
        self.assertIn("values_boundaries", payload["persona_core_fields_missing"])

    def test_core_only_uses_compact_persona_without_raw_prompt(self):
        engine = ContextEngine.__new__(ContextEngine)
        block = engine._build_role_block(
            {
                "raw": "RAW_PERSONA_SENTINEL",
                "summary": "核心摘要",
                "first_person_rewrite": "第一人称短自述",
                "shards": {"relations": "关系切片"},
            },
            retrieve_keys=[],
            is_fast_mode=True,
        )

        self.assertIn("核心摘要", block)
        self.assertIn("第一人称短自述", block)
        self.assertNotIn("RAW_PERSONA_SENTINEL", block)
        self.assertNotIn("关系切片", block)

    def test_normal_mode_only_adds_selected_persona_shards(self):
        engine = ContextEngine.__new__(ContextEngine)
        block = engine._build_role_block(
            {
                "raw": "RAW_PERSONA_SENTINEL",
                "summary": "核心摘要",
                "first_person_rewrite": "第一人称短自述",
                "shards": {
                    "relations": "关系切片",
                    "timeline": "生平切片",
                },
            },
            retrieve_keys=["relations"],
            is_fast_mode=False,
        )

        self.assertIn("核心摘要", block)
        self.assertIn("第一人称短自述", block)
        self.assertIn("关系切片", block)
        self.assertNotIn("生平切片", block)
        self.assertNotIn("RAW_PERSONA_SENTINEL", block)

    def test_all_mode_is_the_only_path_that_loads_raw_persona(self):
        engine = ContextEngine.__new__(ContextEngine)
        block = engine._build_role_block(
            {
                "raw": "RAW_PERSONA_SENTINEL",
                "summary": "核心摘要",
                "first_person_rewrite": "第一人称短自述",
                "shards": {},
            },
            retrieve_keys=["ALL"],
            is_fast_mode=False,
        )

        self.assertIn("RAW_PERSONA_SENTINEL", block)

    def test_stable_boundary_preserves_default_rule_without_promoting_relationship(self):
        engine = ContextEngine.__new__(ContextEngine)
        block = engine._build_addressing_boundary_block(
            {
                "summary": "默认对用户使用欧尼酱。",
                "style": "对用户称谓：欧尼酱；条件关系称呼：哥哥（仅在关系明确时使用）。",
                "shards": {},
            }
        )

        self.assertIn("默认称呼按原始人设", block)
        self.assertIn("限定给某种关系", block)
        self.assertIn("不能因为昵称相似", block)
        self.assertIn("机器人过去说过的话", block)
        self.assertIn("称呼风格不等于关系证明", block)
        self.assertNotIn("QQ", block)

    def test_current_speaker_boundary_does_not_turn_identity_into_relationship(self):
        event = _Event()
        block = PlannerPromptContextMixin._build_current_speaker_block(
            event,
            SimpleNamespace(
                focus_sender_id="222",
                focus_sender_name="普通群友",
                vision_bundle=None,
            ),
            is_lightweight_event=False,
        )

        self.assertIn("QQ: 222", block)
        self.assertIn("默认称呼", block)
        self.assertIn("不能据此把当前发言人升级", block)

    def test_final_lock_separates_target_attribution_from_relationship_scope(self):
        block = PromptRefiner._render_final_speaker_lock(
            "本轮正在回应的对象只看这一位：\n"
            "- QQ: 222\n"
            "- 昵称: 普通群友\n"
        )

        self.assertIn("当前唯一对话对象是 普通群友（QQ 222）", block)
        self.assertIn("不会自动改变人格中的默认称呼", block)
        self.assertIn("关系称呼只有在当前消息或稳定事实明确支持时才能使用", block)
        self.assertNotIn("关系称呼必须指向这一位", block)

    def test_persona_templates_require_address_scope(self):
        registry = PromptTemplateRegistry()
        for template_id in (
            PromptTemplateId.PERSONA_FIRST_PERSON_REWRITE,
            PromptTemplateId.PERSONA_CORE_IDENTITY,
            PromptTemplateId.PERSONA_STYLE,
            PromptTemplateId.PERSONA_SPEECH_STYLE,
            PromptTemplateId.PERSONA_TIMELINE,
            PromptTemplateId.PERSONA_RELATIONS,
        ):
            envelope = registry.render_template(
                template_id,
                {"original_prompt": "默认对用户称呼为欧尼酱，哥哥只在特定关系下使用。"},
            )
            if template_id == PromptTemplateId.PERSONA_FIRST_PERSON_REWRITE:
                self.assertIn("default interlocutor", envelope.system_prompt)
                self.assertIn("conditional relationship", envelope.system_prompt)
            else:
                self.assertIn("默认", envelope.system_prompt)
                self.assertIn("条件", envelope.system_prompt)
                self.assertTrue(
                    any(marker in envelope.system_prompt for marker in ("不能", "不得", "不确定", "不要"))
                )

    def test_timeline_fallback_preserves_relationship_scope(self):
        summarizer = PersonaSummarizer.__new__(PersonaSummarizer)
        captured = {}

        async def _capture(template_id, **kwargs):
            captured["template_id"] = template_id
            captured["prompt"] = kwargs["fallback_prompt"]
            return "ok"

        summarizer._call_persona_template = _capture
        result = asyncio.run(
            summarizer._summarize_timeline(
                "默认对话者称呼为欧尼酱，哥哥只在特定关系下使用。",
                "persona-1",
            )
        )

        self.assertEqual(result, "ok")
        self.assertIn("默认对话者", captured["prompt"])
        self.assertIn("适用范围", captured["prompt"])
        self.assertNotIn("用户（如哥哥、老师等）", captured["prompt"])


if __name__ == "__main__":
    unittest.main()
