import asyncio
import importlib
import sys
import tempfile
import unittest
from types import SimpleNamespace

from tests.helpers.executor_stubs import install_executor_stubs
from tests.helpers.planner_stubs import install_planner_stubs
from tests.original_ported.helpers import _install_astrbot_stubs
from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope


class _FakeEvent:
    def __init__(self):
        self.message_str = "why not?"
        self.unified_msg_origin = "default:GroupMessage:group-1"
        self._extras = {
            "retrieve_keys": [],
            "astrmai_use_lane_history": True,
            "astrmai_recent_transcript": "AstrMai: no, that is not allowed\nUser: why not?",
            "astrmai_raw_user_text": "Alice: why not?",
            "astrmai_focus_message_text": "Alice: why not?",
            "astrmai_direct_context_text": "Focus block",
            "astrmai_related_context_text": "Related\nAstrMai: no, that is not allowed",
            "astrmai_ambient_background_text": "Bob: stay on topic\nCarol: I am reading too",
            "astrmai_focus_reason": "reply_to_bot",
            "astrmai_focus_thread_reason": "reply_to_bot",
        }

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def get_sender_name(self):
        return "Alice"


class _FakeReactRetriever:
    async def retrieve(self, **kwargs):
        return "related memory"


class _FakeMemoryEngine:
    async def recall(self, query, session_id=""):
        return "should not hit recall fallback"


class PromptRefinerFocusLayoutPortedTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        _install_astrbot_stubs(self.temp_dir.name)
        install_executor_stubs()
        install_planner_stubs()
        sys.modules.pop("astrmai.conversation.planning.prompt_refiner", None)
        self.prompt_refiner_mod = importlib.import_module("astrmai.conversation.planning.prompt_refiner")
        self.prompt_refiner_mod = importlib.reload(self.prompt_refiner_mod)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_focus_sections_precede_ambient_background(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=_FakeMemoryEngine(),
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=True)),
            react_retriever=_FakeReactRetriever(),
        )
        event = _FakeEvent()

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": False},
            )

        final_system_prompt, final_prompt = asyncio.run(_run())
        self.assertEqual(final_system_prompt, "system prompt only")
        self.assertLess(final_prompt.index("---前因---\nFocus block"), final_prompt.index("---旁边在聊的---\nBob: stay on topic"))
        self.assertIn("---眼前正在对我说的---\n<user_input>\nAlice: why not?\n</user_input>", final_prompt)
        self.assertIn("---补充---\nRelated\nAstrMai: no, that is not allowed", final_prompt)
        self.assertIn("Carol: I am reading too", final_prompt)

    def test_current_speaker_boundary_precedes_focus_message(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="哥哥: [图片]",
            recent_transcript="萤: 我没戳你",
            warm_zone_summary="最近主要是 萤 / Bot 在聊。",
            warm_zone_quotes="萤: 我没戳你",
            focus_message_text="哥哥: [图片]",
            current_speaker_block=(
                "本轮正在回应的对象只看这一位：\n"
                "- QQ: 2639044966\n"
                "- 昵称: 哥哥\n"
                "历史里的其他发言人只是背景，不能当作当前用户。"
            ),
            guidance_lines=["本轮先遵守当前发言人边界；不要把近期脉络中的其他人名当作当前用户。"],
            near_context_priority=True,
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": True},
            )

        _system_prompt, final_prompt = asyncio.run(_run())
        self.assertIn("---当前发言人边界---", final_prompt)
        self.assertIn("QQ: 2639044966", final_prompt)
        self.assertIn("昵称: 哥哥", final_prompt)
        self.assertLess(final_prompt.index("---当前发言人边界---"), final_prompt.index("---眼前正在对我说的---"))
        self.assertIn("不要把近期脉络中的其他人名当作当前用户", final_prompt)

    def test_final_speaker_lock_follows_runtime_guidance(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="6: 妃妃",
            focus_message_text="6: 妃妃",
            current_speaker_block=(
                "本轮正在回应的对象只看这一位：\n"
                "- QQ: 3650815443\n"
                "- 昵称: 6\n"
                "历史里的其他发言人只是背景，不能当作当前用户。"
            ),
            cognitive_drive_block=(
                "最近我的短期行动残留：\n"
                "- 上轮姿态=tease，结果=reply；萤哥哥又干什么啦"
            ),
            guidance_lines=["自然回应当前消息。"],
            near_context_priority=True,
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": True},
            )

        _system_prompt, final_prompt = asyncio.run(_run())

        self.assertIn("---最终发言人归因锁---", final_prompt)
        self.assertGreater(
            final_prompt.index("---最终发言人归因锁---"),
            final_prompt.index("---内在驱动---"),
        )
        self.assertIn("当前唯一对话对象是 6（QQ 3650815443）", final_prompt)

    def test_transcript_dedup_keeps_semantic_context_sections(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        event._extras["astrmai_recent_transcript"] = (
            "Alice: why not?\n"
            "Bob: ok\n"
            "Carol: longer duplicated clue\n"
            "Dave: separate transcript line"
        )
        event._extras["astrmai_direct_context_text"] = "Carol: longer duplicated clue"
        event._extras["astrmai_related_context_text"] = ""
        event._extras["astrmai_focus_message_text"] = "Alice: why not?"

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": True},
            )

        _system_prompt, final_prompt = asyncio.run(_run())
        transcript_block = final_prompt.split("---对话记录---\n", 1)[1].split("\n\n---眼前正在对我说的---", 1)[0]

        self.assertNotIn("Alice: why not?", transcript_block)
        self.assertNotIn("Carol: longer duplicated clue", transcript_block)
        self.assertIn("Bob: ok", transcript_block)
        self.assertIn("Dave: separate transcript line", transcript_block)
        self.assertIn("---前因---\nCarol: longer duplicated clue", final_prompt)

    def test_memory_block_deduplicates_proactive_recall_against_current_injection(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )

        rendered, metadata = refiner._render_memory_block_for_budget(
            proactive_recall="共同事实：用户喜欢焦糖布丁\n主动回忆独有线索",
            injection="共同事实：用户喜欢焦糖布丁\n本轮检索独有线索",
        )

        self.assertEqual(rendered.count("共同事实：用户喜欢焦糖布丁"), 1)
        self.assertIn("主动回忆独有线索", rendered)
        self.assertIn("本轮检索独有线索", rendered)
        self.assertEqual(metadata["dedup_removed_lines"], 1)
        self.assertEqual(
            metadata["sections"]["proactive_recall"],
            "主动回忆独有线索",
        )

    def test_source_aware_dedup_removes_current_speaker_only(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(
                memory=SimpleNamespace(enable_react_agent=False),
                conversation=SimpleNamespace(
                    context_dedup_enabled=True,
                    context_dedup_observe_only=False,
                ),
            ),
            react_retriever=None,
        )
        event = _FakeEvent()
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="妃妃",
            focus_message_text="妃妃",
            focus_message_identity="6: 妃妃",
            recent_transcript="6: 妃妃\n萤: 妃妃\nBot: 在这里哦",
            recent_transcript_source="lane",
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": True},
            )

        _system_prompt, final_prompt = asyncio.run(_run())
        transcript_block = final_prompt.split("---对话记录（来源：lane）---\n", 1)[1].split("\n\n", 1)[0]
        self.assertNotIn("6: 妃妃", transcript_block)
        self.assertIn("萤: 妃妃", transcript_block)
        self.assertEqual(event.get_extra("astrmai_context_dedup_stats")["removed_lines"], 1)
        self.assertNotIn("6: 妃妃", event.get_extra("astrmai_prompt_envelope").recent_transcript)

    def test_context_dedup_observe_only_keeps_prompt_unchanged(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(
                memory=SimpleNamespace(enable_react_agent=False),
                conversation=SimpleNamespace(
                    context_dedup_enabled=True,
                    context_dedup_observe_only=True,
                ),
            ),
            react_retriever=None,
        )
        event = _FakeEvent()
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="妃妃",
            focus_message_text="妃妃",
            focus_message_identity="6: 妃妃",
            recent_transcript="6: 妃妃\nBot: 在这里哦",
            recent_transcript_source="lane",
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": True},
            )

        _system_prompt, final_prompt = asyncio.run(_run())
        self.assertIn("6: 妃妃", final_prompt)
        stats = event.get_extra("astrmai_context_dedup_stats")
        self.assertTrue(stats["observe_only"])
        self.assertEqual(stats["removed_lines"], 1)

    def test_refiner_places_cognitive_drive_in_user_prompt(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="Alice: why not?",
            recent_transcript="User: why not?",
            recent_transcript_source="",
            recent_transcript_reason="",
            warm_zone_transcript="",
            warm_zone_transcript_source="",
            warm_zone_summary="",
            warm_zone_quotes="",
            focus_message_text="Alice: why not?",
            direct_context_text="Focus block",
            related_context_text="Related\nAstrMai: no, that is not allowed",
            ambient_background_text="Bob: stay on topic\nCarol: I am reading too",
            focus_reason="reply_to_bot",
            focus_thread_reason="reply_to_bot",
            near_context_priority=False,
            guidance_lines=[],
            cognitive_drive_block="agency posture: keep pushback restrained",
            planner_runtime_instruction_block="mode note: keep it brief and answer the current ask",
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": True},
            )

        final_system_prompt, final_prompt = asyncio.run(_run())
        self.assertEqual(final_system_prompt, "system prompt only")
        self.assertIn("---内在驱动---", final_prompt)
        self.assertIn("agency posture: keep pushback restrained", final_prompt)
        self.assertIn("mode note: keep it brief and answer the current ask", final_prompt)
        self.assertNotIn("---当前状态与约束---", final_prompt)
        self.assertGreater(
            final_prompt.index("---内在驱动---"),
            final_prompt.index("---眼前正在对我说的---\n<user_input>\nAlice: why not?\n</user_input>"),
        )


    def test_refiner_places_soft_background_in_prompt_not_system(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="Alice: why not?",
            recent_transcript="User: why not?",
            recent_transcript_source="",
            recent_transcript_reason="",
            warm_zone_transcript="",
            warm_zone_transcript_source="",
            warm_zone_summary="",
            warm_zone_quotes="",
            focus_message_text="Alice: why not?",
            direct_context_text="Focus block",
            related_context_text="",
            ambient_background_text="",
            focus_reason="reply_to_bot",
            focus_thread_reason="reply_to_bot",
            near_context_priority=False,
            guidance_lines=["answer the current question first"],
            soft_background_block="冷区摘要：旧话题\n稳定状态：心情平稳",
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": True},
            )

        final_system_prompt, final_prompt = asyncio.run(_run())
        self.assertEqual(final_system_prompt, "system prompt only")
        self.assertNotIn("冷区摘要：旧话题", final_system_prompt)
        self.assertIn("---背景理解（仅作背景，不要主动续写旧话题，不要覆盖当前用户当前问题）---", final_prompt)
        self.assertIn("冷区摘要：旧话题", final_prompt)
        self.assertIn("answer the current question first", final_prompt)
        self.assertLess(
            final_prompt.index("---背景理解（仅作背景，不要主动续写旧话题，不要覆盖当前用户当前问题）---"),
            final_prompt.index("answer the current question first"),
        )

    def test_refiner_places_focus_before_recent_warm_memory_and_runtime_guidance(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="Alice: why not?",
            recent_transcript="Recent tail",
            recent_transcript_source="lane",
            recent_transcript_reason="fallback_tail",
            warm_zone_transcript="Warm transcript",
            warm_zone_transcript_source="store",
            warm_zone_summary="",
            warm_zone_quotes="",
            focus_message_text="Alice: why not?",
            direct_context_text="Focus block",
            related_context_text="Related context",
            ambient_background_text="Background chatter",
            focus_reason="reply_to_bot",
            focus_thread_reason="reply_to_bot",
            near_context_priority=False,
            guidance_lines=["answer the current question first"],
            cognitive_drive_block="agency posture: keep pushback restrained",
            planner_runtime_instruction_block="mode note: keep it brief",
            soft_background_sections={"cold_summary": "cold summary"},
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": False},
                proactive_recall="memory note",
            )

        _system_prompt, final_prompt = asyncio.run(_run())
        self.assertLess(final_prompt.index("---眼前正在对我说的---"), final_prompt.index("---前因---"))
        self.assertLess(final_prompt.index("---前因---"), final_prompt.index("---对话记录"))
        self.assertLess(final_prompt.index("---对话记录"), final_prompt.index("---旁边在聊的---"))
        self.assertLess(final_prompt.index("---旁边在聊的---"), final_prompt.index("---记忆闪回"))
        self.assertLess(final_prompt.index("---记忆闪回"), final_prompt.index("---背景理解"))
        self.assertLess(final_prompt.index("---背景理解"), final_prompt.index("---内在驱动---"))
        self.assertLess(final_prompt.index("---内在驱动---"), final_prompt.index("---本轮指引---"))

    def test_refiner_skips_soft_background_in_fast_mode(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        event._extras["retrieve_keys"] = ["CORE_ONLY"]
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="Alice: why not?",
            recent_transcript="",
            focus_message_text="Alice: why not?",
            direct_context_text="Focus block",
            focus_reason="reply_to_bot",
            focus_thread_reason="reply_to_bot",
            near_context_priority=False,
            soft_background_block="冷区摘要：旧话题",
            soft_background_sections={"cold_summary": "冷区摘要：旧话题"},
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": True},
            )

        _system_prompt, final_prompt = asyncio.run(_run())
        envelope = event.get_extra("astrmai_prompt_envelope")
        self.assertNotIn("冷区摘要：旧话题", final_prompt)
        self.assertEqual(envelope.soft_background_skipped_reason, "fast_mode")
        self.assertEqual(envelope.soft_background_rendered_chars, 0)

    def test_refiner_skips_soft_background_when_near_context_priority(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="Alice: why not?",
            recent_transcript="",
            focus_message_text="Alice: why not?",
            direct_context_text="Focus block",
            focus_reason="reply_to_bot",
            focus_thread_reason="reply_to_bot",
            near_context_priority=True,
            soft_background_block="冷区摘要：旧话题",
            soft_background_sections={"cold_summary": "冷区摘要：旧话题"},
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": True},
            )

        _system_prompt, final_prompt = asyncio.run(_run())
        envelope = event.get_extra("astrmai_prompt_envelope")
        self.assertNotIn("冷区摘要：旧话题", final_prompt)
        self.assertEqual(envelope.soft_background_skipped_reason, "near_context_priority")
        self.assertEqual(envelope.soft_background_rendered_chars, 0)

    def test_refiner_trims_soft_background_from_low_priority_tail_first(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="Alice: why not?",
            recent_transcript="",
            focus_message_text="Alice: why not?",
            direct_context_text="Focus block",
            focus_reason="reply_to_bot",
            focus_thread_reason="reply_to_bot",
            near_context_priority=False,
            soft_background_sections={
                "cold_summary": "冷区摘要：" + ("A" * 120),
                "stable_state": "稳定状态：" + ("B" * 120),
                "stable_behavior_rules": "稳定行为：" + ("C" * 120),
                "stable_private_chat": "私聊画像：" + ("D" * 120),
                "stable_expression": "表达习惯：" + ("E" * 120),
                "stable_slang": "群聊黑话：" + ("F" * 120),
                "stable_jargon": "术语说明：" + ("G" * 120),
            },
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": True},
            )

        _system_prompt, final_prompt = asyncio.run(_run())
        envelope = event.get_extra("astrmai_prompt_envelope")
        self.assertIn("冷区摘要：", final_prompt)
        self.assertNotIn("术语说明：", final_prompt)
        self.assertNotIn("群聊黑话：", final_prompt)
        self.assertIn("stable_jargon", envelope.soft_background_trimmed_sections)
        self.assertIn("stable_slang", envelope.soft_background_trimmed_sections)
        self.assertLessEqual(
            envelope.soft_background_rendered_chars,
            self.prompt_refiner_mod.PromptRefiner.SOFT_BACKGROUND_BUDGET_CHARS,
        )

    def test_refiner_applies_flexible_budget_without_dropping_focus_direct_or_recent(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        long_recent = "\n".join(f"Turn {idx}: recent mainline detail " + ("R" * 80) for idx in range(1, 12))
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="Alice: why not?",
            recent_transcript=long_recent,
            recent_transcript_source="lane",
            recent_transcript_reason="fallback_tail",
            warm_zone_transcript="",
            warm_zone_transcript_source="store",
            warm_zone_summary="Warm summary " + ("W" * 900),
            warm_zone_quotes="\n".join(f"quote {idx}: " + ("Q" * 80) for idx in range(1, 6)),
            focus_message_text="Alice: why not?",
            direct_context_text="Focus block that must stay",
            related_context_text="",
            ambient_background_text="",
            focus_reason="reply_to_bot",
            focus_thread_reason="reply_to_bot",
            near_context_priority=False,
            soft_background_sections={
                "cold_summary": "cold summary " + ("C" * 260),
                "stable_state": "stable state " + ("S" * 260),
            },
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": True},
                proactive_recall="proactive memory " + ("P" * 450),
            )

        _system_prompt, final_prompt = asyncio.run(_run())
        envelope = event.get_extra("astrmai_prompt_envelope")
        self.assertIn("---眼前正在对我说的---\n<user_input>\nAlice: why not?\n</user_input>", final_prompt)
        self.assertIn("---前因---\nFocus block that must stay", final_prompt)
        self.assertIn("---对话记录", final_prompt)
        self.assertIn("Turn 11: recent mainline detail", final_prompt)
        self.assertNotIn("cold summary " + ("C" * 40), final_prompt)
        self.assertIn("soft_background", envelope.flex_context_trimmed_sections)
        self.assertGreater(envelope.recent_context_rendered_chars, 0)
        self.assertGreater(envelope.warm_context_rendered_chars, 0)
        self.assertIn("focus_message", envelope.flex_context_protected_sections)
        self.assertIn("direct_context", envelope.flex_context_protected_sections)

    def test_refiner_compresses_memory_before_dropping_recent(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="Alice: why not?",
            recent_transcript="\n".join(f"Recent {idx}: " + ("R" * 70) for idx in range(1, 8)),
            warm_zone_summary="Warm summary " + ("W" * 1000),
            warm_zone_quotes="\n".join(f"Quote {idx}: " + ("Q" * 120) for idx in range(1, 5)),
            focus_message_text="Alice: why not?",
            direct_context_text="Focus block",
            focus_reason="reply_to_bot",
            focus_thread_reason="reply_to_bot",
            near_context_priority=False,
            soft_background_sections={"cold_summary": "cold summary " + ("C" * 500)},
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": False},
                proactive_recall="proactive recall " + ("M" * 1200),
            )

        _system_prompt, final_prompt = asyncio.run(_run())
        envelope = event.get_extra("astrmai_prompt_envelope")
        self.assertIn("---对话记录", final_prompt)
        self.assertIn("Recent 7:", final_prompt)
        self.assertIn("memory:preview", envelope.flex_context_trimmed_sections)
        self.assertLessEqual(
            envelope.memory_context_rendered_chars,
            self.prompt_refiner_mod.PromptRefiner.MEMORY_PREVIEW_TARGET_CHARS * 2 + 1,
        )

    def test_refiner_tail_truncates_recent_as_last_resort(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        very_long_recent = "\n".join(f"Recent {idx}: " + ("R" * 120) for idx in range(1, 18))
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="Alice: why not?",
            recent_transcript=very_long_recent,
            warm_zone_transcript="",
            warm_zone_summary="Warm summary " + ("W" * 1000),
            warm_zone_quotes="\n".join(f"Quote {idx}: " + ("Q" * 120) for idx in range(1, 7)),
            focus_message_text="Alice: why not?",
            direct_context_text="Focus block",
            focus_reason="reply_to_bot",
            focus_thread_reason="reply_to_bot",
            near_context_priority=False,
            soft_background_sections={"cold_summary": "cold " + ("C" * 500)},
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": False},
                proactive_recall="memory " + ("M" * 700),
            )

        _system_prompt, final_prompt = asyncio.run(_run())
        envelope = event.get_extra("astrmai_prompt_envelope")
        self.assertIn("---对话记录", final_prompt)
        self.assertIn("Recent 17:", final_prompt)
        self.assertIn("recent:tail_truncated", envelope.flex_context_trimmed_sections)
        self.assertLessEqual(
            envelope.recent_context_rendered_chars,
            len(very_long_recent),
        )
        self.assertNotIn("direct_context", envelope.flex_context_trimmed_sections)

    def test_refiner_skips_time_anchor_for_plain_chat(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        event.message_str = "why not?"
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="Alice: why not?",
            recent_transcript="",
            focus_message_text="Alice: why not?",
            direct_context_text="Focus block",
            focus_reason="reply_to_bot",
            focus_thread_reason="reply_to_bot",
            near_context_priority=False,
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": True},
            )

        _system_prompt, final_prompt = asyncio.run(_run())
        self.assertNotIn("现在是", final_prompt)

    def test_refiner_keeps_time_anchor_for_relative_time_question(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        event.message_str = "你还记得刚才说的那个吗？"
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="Alice: 你还记得刚才说的那个吗？",
            recent_transcript="",
            focus_message_text="Alice: 你还记得刚才说的那个吗？",
            direct_context_text="Focus block",
            focus_reason="reply_to_bot",
            focus_thread_reason="reply_to_bot",
            near_context_priority=False,
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": True},
            )

        _system_prompt, final_prompt = asyncio.run(_run())
        self.assertIn("现在是", final_prompt)

    def test_refiner_keeps_time_anchor_for_proactive_event(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        event.message_str = "synthetic proactive nudge"
        event._extras["astrmai_is_proactive_event"] = True
        event._extras["astrmai_proactive_guidance"] = "say one short natural line"
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="Alice: synthetic proactive nudge",
            recent_transcript="",
            focus_message_text="Alice: synthetic proactive nudge",
            direct_context_text="Focus block",
            focus_reason="reply_to_bot",
            focus_thread_reason="reply_to_bot",
            near_context_priority=False,
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": True},
            )

        _system_prompt, final_prompt = asyncio.run(_run())
        self.assertIn("现在是", final_prompt)

    def test_refiner_keeps_time_anchor_for_schedule_request(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        event.message_str = "明天 8 点提醒我开会"
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="Alice: 明天 8 点提醒我开会",
            recent_transcript="",
            focus_message_text="Alice: 明天 8 点提醒我开会",
            direct_context_text="Focus block",
            focus_reason="reply_to_bot",
            focus_thread_reason="reply_to_bot",
            near_context_priority=False,
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": True},
            )

        _system_prompt, final_prompt = asyncio.run(_run())
        self.assertIn("现在是", final_prompt)

    def test_refiner_keeps_time_anchor_for_wait_resume_signal(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        event.message_str = "继续说"
        event._extras["astrmai_wait_resume_thought"] = "Alice 接上了你刚才的话题，立刻自然地继续回应。"
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="Alice: 继续说",
            recent_transcript="",
            focus_message_text="Alice: 继续说",
            direct_context_text="Focus block",
            focus_reason="reply_to_bot",
            focus_thread_reason="reply_to_bot",
            near_context_priority=False,
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": True},
            )

        _system_prompt, final_prompt = asyncio.run(_run())
        self.assertIn("现在是", final_prompt)

    def test_refiner_keeps_time_anchor_for_post_compaction_recovery_rounds(self):
        refiner = self.prompt_refiner_mod.PromptRefiner(
            memory_engine=None,
            config=SimpleNamespace(memory=SimpleNamespace(enable_react_agent=False)),
            react_retriever=None,
        )
        event = _FakeEvent()
        event.message_str = "继续刚才那个点"
        turn_context_mod = importlib.import_module("astrmai.conversation.contracts.turn_context")
        turn_context = turn_context_mod.ensure_turn_context(event)
        turn_context.continuity.post_compaction_recovery_rounds = 2
        event._extras["astrmai_prompt_envelope"] = PromptEnvelope(
            raw_user_text="Alice: 继续刚才那个点",
            recent_transcript="",
            focus_message_text="Alice: 继续刚才那个点",
            direct_context_text="Focus block",
            focus_reason="reply_to_bot",
            focus_thread_reason="reply_to_bot",
            near_context_priority=False,
        )

        async def _run():
            return await refiner.refine_prompt(
                event=event,
                system_prompt="system prompt only",
                prompt="wrapped prompt",
                context={"disable_rag_injection": True},
            )

        _system_prompt, final_prompt = asyncio.run(_run())
        self.assertIn("现在是", final_prompt)


if __name__ == "__main__":
    unittest.main()
