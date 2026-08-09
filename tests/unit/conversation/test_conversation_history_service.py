import asyncio
from types import SimpleNamespace

from astrmai.conversation.history import (
    ConversationHistoryService,
    build_friend_umo,
    extract_text_history,
    render_context_block,
)


def test_history_pure_helpers_filter_and_bound_content():
    assert build_friend_umo("ff:GroupMessage:1", "42") == "ff:FriendMessage:42"
    history = extract_text_history(
        [
            {"role": "system", "content": "secret"},
            {"role": "user", "content": " hello  world "},
            {"role": "assistant", "content": [{"type": "text", "text": "ok"}, {"type": "image_url"}]},
        ]
    )
    assert history == [{"role": "user", "content": "hello world"}, {"role": "assistant", "content": "ok"}]
    block = render_context_block(history, max_messages=1, max_chars=100)
    assert "你: ok" in block
    assert "hello world" not in block


def test_host_private_history_is_explicit_and_structured():
    class Manager:
        async def get_curr_conversation_id(self, umo):
            assert umo == "ff:FriendMessage:42"
            return "conv"

        async def get_conversation(self, umo, conversation_id):
            return SimpleNamespace(
                history=[
                    {"role": "user", "content": "前面的用户消息"},
                    {"role": "assistant", "content": "前面的机器人回复"},
                ]
            )

    service = ConversationHistoryService(SimpleNamespace(conversation_manager=Manager()))
    records = asyncio.run(
        service.read_host_private_history(current_umo="ff:GroupMessage:99", sender_id="42", count=4)
    )
    assert [item.text for item in records] == ["前面的用户消息", "前面的机器人回复"]
    assert records[0].is_current_user is True
    assert records[1].sender_id == "bot"


def test_napcat_group_history_preserves_sender_ownership():
    class API:
        async def call_action(self, action, **kwargs):
            assert action == "get_group_msg_history"
            return {
                "data": {
                    "messages": [
                        {"user_id": 1, "sender": {"nickname": "甲"}, "raw_message": "a", "message_id": 10},
                        {"user_id": 2, "sender": {"nickname": "乙"}, "message": [{"type": "text", "data": {"text": "b"}}]},
                    ]
                }
            }

    event = SimpleNamespace(bot=SimpleNamespace(api=API()), get_sender_id=lambda: "2")
    service = ConversationHistoryService(SimpleNamespace())
    records = asyncio.run(service.read_napcat_history(event=event, chat_type="group", target_id="99"))
    assert [(item.sender_id, item.sender_name, item.text) for item in records] == [("1", "甲", "a"), ("2", "乙", "b")]
    assert records[1].is_current_user is True


def test_disabled_history_lookup_does_not_call_napcat():
    class API:
        async def call_action(self, action, **kwargs):
            raise AssertionError(f"history API must stay disabled: {action} {kwargs}")

    config = SimpleNamespace(
        conversation=SimpleNamespace(
            history_lookup_enabled=False,
            history_lookup_group_enabled=True,
            history_lookup_private_enabled=True,
        )
    )
    event = SimpleNamespace(bot=SimpleNamespace(api=API()), get_sender_id=lambda: "2")
    service = ConversationHistoryService(SimpleNamespace(), config)

    records = asyncio.run(
        service.read_napcat_history(event=event, chat_type="group", target_id="99")
    )

    assert records == []
