import time
from types import SimpleNamespace

from astrmai.conversation.contracts.conversation_event import ConversationEvent
from astrmai.conversation.planning.conversation_continuity import ConversationContinuityStore
from astrmai.conversation.planning.planner import Planner


class _Event:
    def __init__(self, canonical: ConversationEvent):
        self.message_str = ""
        self.message_obj = SimpleNamespace(message_id=canonical.event_id)
        self._extras = {
            "astrmai_conversation_event": canonical,
            "astrmai_rich_text": canonical.rich_text,
            "astrmai_vision_barrier_complete": True,
        }

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)

    def set_extra(self, key, value):
        self._extras[key] = value

    def get_sender_id(self):
        return "516779421"


def test_planner_commits_safe_image_topic_instead_of_prompt_focus_envelope():
    canonical = ConversationEvent(
        event_id="evt-image",
        chat_id="default:FriendMessage:516779421",
        chat_kind="private",
        timestamp=1.0,
        actor_id="516779421",
        actor_name="恸",
        visible_text="",
        rich_text="[表情包转述：一个金发双马尾女孩举着‘V我50’的碗]",
        message_kind="image",
        role="user",
        image_refs=("pic-1",),
    )
    event = _Event(canonical)
    planner = object.__new__(Planner)
    planner.conversation_continuity = ConversationContinuityStore()
    prompt_envelope = SimpleNamespace(
        focus_message_text=(
            "[事件=evt-image | 发言人=恸（ID:516779421） | 角色=成员 | "
            "类型=image | 来源=original | 媒体=图片:1]\n"
            "内容：[表情包转述：一个金发双马尾女孩举着‘V我50’的碗]"
        ),
        raw_user_text="",
    )

    planner._record_conversation_continuity(
        "default:FriendMessage:516779421",
        prompt_envelope,
        "这张表情包很可爱～",
        [],
        SimpleNamespace(social_intent="answer", action_tier="reply", reply_need="reply"),
        event=event,
    )

    snapshot = planner.conversation_continuity.snapshot(
        "default:FriendMessage:516779421"
    )
    assert "V我50" in snapshot["current_topic"]
    assert "事件=" not in snapshot["current_topic"]
    assert "516779421" not in snapshot["current_topic"]
    assert event.get_extra("astrmai_topic_preview_source") == "vision_description"
    assert event.get_extra("astrmai_topic_preview_safe") is True
    assert event.get_extra("astrmai_topic_preview_committed") is True
    assert event.get_extra("astrmai_vision_state_at_topic_commit") == "complete"


def test_planner_does_not_replace_topic_with_failed_image_placeholder():
    chat_id = "default:FriendMessage:516779421"
    canonical = ConversationEvent(
        event_id="evt-image-failed",
        chat_id=chat_id,
        chat_kind="private",
        timestamp=2.0,
        actor_id="516779421",
        actor_name="恸",
        visible_text="",
        rich_text="[图片]",
        message_kind="image",
        role="user",
        image_refs=("pic-failed",),
    )
    event = _Event(canonical)
    event._extras["astrmai_vision_barrier_complete"] = False
    event._extras["astrmai_vision_barrier_failed"] = True
    planner = object.__new__(Planner)
    planner.conversation_continuity = ConversationContinuityStore()
    started_at = time.time() - 60
    planner.conversation_continuity.record(
        chat_id=chat_id,
        focus_preview="温泉旅行计划",
        reply_need="reply",
        now=started_at,
    )

    planner._record_conversation_continuity(
        chat_id,
        SimpleNamespace(focus_message_text="[图片]", raw_user_text=""),
        "图片暂时没有识别出来",
        [],
        SimpleNamespace(social_intent="answer", action_tier="reply", reply_need="reply"),
        event=event,
    )

    snapshot = planner.conversation_continuity.snapshot(chat_id)
    assert snapshot["current_topic"] == "温泉旅行计划"
    assert snapshot["topic_started_at"] == started_at
    assert event.get_extra("astrmai_topic_preview") == ""
    assert event.get_extra("astrmai_topic_preview_safe") is False
    assert (
        event.get_extra("astrmai_topic_preview_rejected_reason")
        == "unresolved_image_has_no_semantic_topic"
    )
    assert event.get_extra("astrmai_topic_preview_committed") is False
    assert event.get_extra("astrmai_vision_state_at_topic_commit") == "failed"
