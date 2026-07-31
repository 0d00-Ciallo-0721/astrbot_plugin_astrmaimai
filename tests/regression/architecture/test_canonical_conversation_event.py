from types import SimpleNamespace

import pytest

from astrmai.conversation.attention.group_dialogue_store import GroupDialogueStore
from astrmai.conversation.contracts.conversation_event import ConversationEvent
from astrmai.infrastructure.persistence.database_service import DatabaseService
from astrmai.infrastructure.persistence.orm_models import MessageLog
from astrmai.infrastructure.persistence.persistence_schema import _MIGRATIONS


class StubEvent:
    def __init__(
        self,
        *,
        sender_id: str,
        sender_name: str,
        text: str,
        message_id: str = "",
        timestamp: float = 100.25,
        components=None,
        extras=None,
    ):
        self.unified_msg_origin = "ff:GroupMessage:100"
        self.timestamp = timestamp
        self.message_str = text
        self.message_obj = SimpleNamespace(
            message_id=message_id,
            message=list(components or []),
        )
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._extras = dict(extras or {})

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_group_id(self):
        return "100"

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)


def test_platform_message_id_is_stable_and_actor_identity_is_not_nickname():
    first = StubEvent(
        sender_id="10001",
        sender_name="同名用户",
        text="你好",
        message_id="msg-1",
    )
    renamed = StubEvent(
        sender_id="10001",
        sender_name="新名字",
        text="你好",
        message_id="msg-1",
    )
    other = StubEvent(
        sender_id="10002",
        sender_name="同名用户",
        text="你好",
        message_id="msg-2",
    )

    first_event = ConversationEvent.from_astr_event(first, self_id="bot")
    renamed_event = ConversationEvent.from_astr_event(renamed, self_id="bot")
    other_event = ConversationEvent.from_astr_event(other, self_id="bot")

    assert first_event.event_id == renamed_event.event_id == "msg-1"
    assert first_event.actor_id == renamed_event.actor_id == "10001"
    assert first_event.actor_name != renamed_event.actor_name
    assert first_event.actor_id != other_event.actor_id


def test_fallback_event_id_is_reproducible_and_structure_is_preserved():
    reply = SimpleNamespace(
        type="Reply",
        id="quoted-message",
        sender_id="20001",
        sender_nickname="被回复者",
    )
    at = SimpleNamespace(type="At", qq="30001")
    event = StubEvent(
        sender_id="10001",
        sender_name="发送者",
        text="你看",
        components=[reply, at],
        extras={"astrmai_source_event_ids": ["source-a", "source-a", "source-b"]},
    )

    first = ConversationEvent.from_astr_event(
        event,
        self_id="bot",
        image_refs=["image-a"],
        direct_image_refs=["image-a"],
        is_at_bot=False,
        is_reply_to_bot=False,
        is_direct_wakeup=True,
        topic_epoch=3,
    )
    second = ConversationEvent.from_astr_event(
        event,
        self_id="bot",
        image_refs=["image-a"],
        direct_image_refs=["image-a"],
        is_direct_wakeup=True,
        topic_epoch=3,
    )

    assert first.event_id == second.event_id
    assert first.event_id.startswith("fallback_")
    assert first.reply_target_event_id == "quoted-message"
    assert first.reply_target_actor_id == "20001"
    assert first.at_actor_ids == ("30001",)
    assert first.image_refs == ("image-a",)
    assert first.source_event_ids == ("source-a", "source-b")
    assert first.message_kind == "mixed"
    assert first.topic_epoch == 3


@pytest.mark.asyncio
async def test_dialogue_store_appends_canonical_event_idempotently():
    store = GroupDialogueStore()
    raw = StubEvent(
        sender_id="10001",
        sender_name="发送者",
        text="同一条消息",
        message_id="msg-dedup",
    )
    event = ConversationEvent.from_astr_event(raw, self_id="bot")

    first = await store.append_conversation_event(event)
    second = await store.append_conversation_event(event)
    snapshot = await store.snapshot_compaction_candidates(
        "ff:GroupMessage:100",
        keep_recent_segments=20,
    )

    assert first is second
    assert [item.event_id for item in snapshot["recent_segments"]] == ["msg-dedup"]
    assert snapshot["recent_segments"][0].speaker_id == "10001"


def test_message_log_compatibility_projection_preserves_canonical_fields():
    raw = StubEvent(
        sender_id="10001",
        sender_name="发送者",
        text="引用消息",
        message_id="msg-persisted",
        components=[SimpleNamespace(type="At", qq="30001")],
    )
    event = ConversationEvent.from_astr_event(
        raw,
        self_id="bot",
        image_refs=["image-a"],
        topic_epoch=4,
    )

    fields = DatabaseService._conversation_event_log_fields(event)
    row = MessageLog(
        group_id=event.chat_id,
        sender_id=event.actor_id,
        sender_name=event.actor_name,
        content=event.rich_text,
        **fields,
    )

    assert row.event_id == "msg-persisted"
    assert row.event_schema_version == 1
    assert row.at_actor_ids == '["30001"]'
    assert row.image_refs == '["image-a"]'
    assert row.topic_epoch == 4
    assert row.provenance == "original"


def test_message_log_migrations_append_canonical_columns_without_reordering_history():
    versions = [version for version, _ddl in _MIGRATIONS]
    migration_sql = "\n".join(ddl for _version, ddl in _MIGRATIONS)

    assert versions == list(range(1, len(versions) + 1))
    assert "ALTER TABLE messagelog ADD COLUMN event_id" in migration_sql
    assert "ALTER TABLE messagelog ADD COLUMN causal_parent_event_id" in migration_sql
    assert "CREATE INDEX IF NOT EXISTS ix_messagelog_event_id" in migration_sql
