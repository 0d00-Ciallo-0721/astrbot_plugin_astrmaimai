"""P0 regression coverage for group-scoped third-party identity resolution."""

import asyncio
import unittest
from types import SimpleNamespace

from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope
from astrmai.conversation.planning.group_entity_resolution import (
    EntityEvidence,
    build_referenced_entities,
    collect_event_identity_evidence,
    render_referenced_entity_block,
    resolve_group_references,
)
from astrmai.conversation.planning.message_renderer import MessageRenderer
from astrmai.conversation.planning.planner_prompt_context import PlannerPromptContextMixin
from astrmai.conversation.planning.prompt_refiner import PromptRefiner


class _Event:
    def __init__(self, sender_id: str, sender_name: str, text: str, group_id: str = "7000"):
        self._sender_id = sender_id
        self._sender_name = sender_name
        self.message_str = text
        self.unified_msg_origin = f"ff:GroupMessage:{group_id}"
        self._extras = {}

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_group_id(self):
        return self.unified_msg_origin.rsplit(":", 1)[-1]

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)


class _Log:
    def __init__(self, sender_id: str, sender_name: str, timestamp: float = 1.0):
        self.sender_id = sender_id
        self.sender_name = sender_name
        self.timestamp = timestamp


class _Db:
    def __init__(self, rows):
        self.rows = rows
        self.calls = []

    def get_recent_message_logs(self, group_id, limit=8, max_age_seconds=None, include_processed=True):
        self.calls.append(group_id)
        return list(self.rows)


class _PrivateEvent(_Event):
    def __init__(self, sender_id: str, sender_name: str, text: str):
        super().__init__(sender_id, sender_name, text)
        self.unified_msg_origin = f"ff:FriendMessage:{sender_id}"

    def get_group_id(self):
        return ""


class _Planner(PlannerPromptContextMixin):
    def __init__(self, db_service=None):
        self.context_engine = None
        self.gateway = SimpleNamespace(db_service=db_service)


class GroupEntityResolutionTests(unittest.TestCase):
    def test_stable_name_resolves_to_one_qq_in_the_same_group(self):
        entities = build_referenced_entities(
            "空酱是萝莉吗",
            group_id="7000",
            current_sender_id="100",
            current_sender_name="恸",
            evidence=[
                EntityEvidence("200", "空酱", "recent_event"),
                EntityEvidence("100", "恸", "recent_event"),
            ],
        )

        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0].resolved_id, "200")
        self.assertFalse(entities[0].ambiguous)
        self.assertEqual(entities[0].confidence, "high")

    def test_duplicate_nickname_remains_ambiguous(self):
        entities = build_referenced_entities(
            "空酱是谁",
            group_id="7000",
            current_sender_id="100",
            current_sender_name="恸",
            evidence=[
                EntityEvidence("200", "空酱", "group_message_log"),
                EntityEvidence("300", "空酱", "group_message_log"),
            ],
        )

        self.assertEqual(entities[0].candidate_ids, ("200", "300"))
        self.assertTrue(entities[0].ambiguous)
        self.assertEqual(entities[0].resolved_id, "")
        block = render_referenced_entity_block(entities)
        self.assertIn("不要猜测具体是哪一位", block)

    def test_honorific_short_name_resolves_long_group_nickname(self):
        entities = build_referenced_entities(
            "所以空酱是萝莉对吧",
            group_id="7000",
            current_sender_id="100",
            current_sender_name="恸",
            evidence=[
                EntityEvidence("1743517556", "空酱不是萝莉空", "group_message_log"),
            ],
        )

        self.assertEqual(len(entities), 1)
        self.assertEqual(entities[0].mention_text, "空酱")
        self.assertEqual(entities[0].resolved_id, "1743517556")
        self.assertIn("group_message_log_alias", entities[0].source)

    def test_shared_short_alias_remains_ambiguous(self):
        entities = build_referenced_entities(
            "空酱是谁",
            group_id="7000",
            current_sender_id="100",
            current_sender_name="恸",
            evidence=[
                EntityEvidence("200", "空酱不是萝莉", "group_message_log"),
                EntityEvidence("300", "空酱今天早睡", "group_message_log"),
            ],
        )

        self.assertEqual(entities[0].candidate_ids, ("200", "300"))
        self.assertTrue(entities[0].ambiguous)
        self.assertEqual(entities[0].resolved_id, "")

    def test_current_speaker_alias_is_not_treated_as_third_party(self):
        entities = build_referenced_entities(
            "我就是空酱呀",
            group_id="7000",
            current_sender_id="1743517556",
            current_sender_name="空酱不是萝莉空",
            evidence=[
                EntityEvidence("1743517556", "空酱不是萝莉空", "recent_event"),
            ],
        )

        self.assertEqual(entities, [])

    def test_unrelated_group_does_not_reuse_other_group_evidence(self):
        entities = build_referenced_entities(
            "空酱是萝莉吗",
            group_id="8000",
            current_sender_id="100",
            current_sender_name="恸",
            evidence=[],
        )

        # Evidence from another group is not implicitly loaded by the pure
        # resolver; the async path queries only the current group.
        self.assertEqual(entities, [])

    def test_mixed_event_evidence_is_filtered_to_current_group(self):
        current_group = _Event("200", "空酱", "我在当前群", group_id="7000")
        other_group = _Event("300", "空酱", "我在另一个群", group_id="8000")

        evidence = collect_event_identity_evidence(
            [current_group, other_group],
            group_id="7000",
        )

        self.assertEqual([(item.user_id, item.display_name) for item in evidence], [("200", "空酱")])

    def test_persisted_group_logs_fill_gap_when_recent_events_lost(self):
        db = _Db([_Log("200", "空酱")])
        entities, block = asyncio.run(
            resolve_group_references(
                "空酱是萝莉吗",
                group_id="7000",
                current_sender_id="100",
                current_sender_name="恸",
                events=[],
                db_service=db,
            )
        )

        self.assertEqual(entities[0].resolved_id, "200")
        self.assertIn("QQ 200", block)
        self.assertEqual(db.calls, ["7000"])

    def test_planner_projects_only_current_group_entities(self):
        focus = _Event("100", "恸", "所以空酱是萝莉对吧", group_id="7000")
        current_group_entity = _Event("200", "空酱", "我在", group_id="7000")
        other_group_entity = _Event("300", "空酱", "另一个空酱", group_id="8000")
        focus_context = SimpleNamespace(
            focus_sender_id="100",
            focus_sender_name="恸",
            all_thread_events=lambda: [focus, current_group_entity, other_group_entity],
        )

        entities, block = asyncio.run(
            _Planner()._resolve_referenced_entity_context(
                focus_event=focus,
                focus_context=focus_context,
                event_messages=[],
                context_events=[],
                focus_message_text="恸: 所以空酱是萝莉对吧",
            )
        )

        self.assertEqual([item["resolved_id"] for item in entities], ["200"])
        self.assertIn("QQ 200", block)
        self.assertNotIn("QQ 300", block)

    def test_planner_private_chat_skips_group_entity_resolution(self):
        private = _PrivateEvent("100", "恸", "所以空酱是萝莉对吧")
        focus_context = SimpleNamespace(
            focus_sender_id="100",
            focus_sender_name="恸",
            all_thread_events=lambda: [private],
        )

        entities, block = asyncio.run(
            _Planner(db_service=_Db([_Log("200", "空酱")]))._resolve_referenced_entity_context(
                focus_event=private,
                focus_context=focus_context,
                event_messages=[],
                context_events=[],
                focus_message_text="恸: 所以空酱是萝莉对吧",
            )
        )

        self.assertEqual(entities, [])
        self.assertEqual(block, "")

    def test_group_renderer_keeps_id_without_changing_default_renderer(self):
        event = _Event("200", "空酱", "我在")
        self.assertEqual(MessageRenderer.render_event(event), "空酱: 我在")
        self.assertEqual(MessageRenderer.render_event(event, include_identity=True), "空酱（QQ: 200）: 我在")

    def test_prompt_envelope_carries_entity_boundary(self):
        envelope = PromptEnvelope(
            focus_message_text="恸: 空酱是萝莉吗",
            referenced_entity_block="名称：空酱\n群内身份：QQ 200",
        )
        self.assertIn("QQ 200", envelope.referenced_entity_block)

    def test_refiner_injects_entity_boundary_into_prompt(self):
        event = _Event("100", "恸", "空酱是萝莉吗")
        envelope = PromptEnvelope(
            raw_user_text="恸: 空酱是萝莉吗",
            focus_message_text="恸: 空酱是萝莉吗",
            referenced_entity_block=(
                "名称：空酱\n群内身份：QQ 200\n"
                "处理：不要把提及对象当作当前发言人。"
            ),
        )
        system_prompt, prompt = asyncio.run(
            PromptRefiner(memory_engine=None).refine_prompt(
                event,
                "系统提示",
                prompt="恸: 空酱是萝莉吗",
                prompt_envelope=envelope,
            )
        )

        self.assertIn("本轮提及对象边界", prompt)
        self.assertIn("QQ 200", prompt)
        self.assertNotIn("本轮提及对象边界", system_prompt)


if __name__ == "__main__":
    unittest.main()
