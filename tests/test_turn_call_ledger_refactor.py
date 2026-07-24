from astrmai.infrastructure.runtime.turn_call_ledger import (
    CALL_LEDGER_KEY,
    CONTEXT_BLOCK_STATS_KEY,
    REPLY_STATS_KEY,
    begin_llm_call,
    finish_llm_call,
    record_context_block_stats,
    record_reply_stats,
)


class _Event:
    def __init__(self):
        self._extra = {}

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


def test_call_ledger_records_lengths_without_prompt_content():
    event = _Event()

    call_id = begin_llm_call(
        event,
        stage="gateway.chat",
        family="chat_dialog",
        pool="dialog",
        system_prompt="private system prompt",
        prompt="用户的私密原话",
        contexts=["历史一", "历史二"],
        metadata={"lane_enabled": True},
    )
    finish_llm_call(
        event,
        call_id,
        model="model-a",
        provider="provider-a",
        output="回复",
        attempts=2,
    )

    entry = event.get_extra(CALL_LEDGER_KEY)[0]
    assert entry["status"] == "success"
    assert entry["system_chars"] == len("private system prompt")
    assert entry["prompt_chars"] == len("用户的私密原话")
    assert entry["context_count"] == 2
    assert entry["attempts"] == 2
    assert "用户的私密原话" not in str(entry)
    assert "历史一" not in str(entry)


def test_context_block_stats_keep_hashes_and_lengths_only():
    event = _Event()

    record_context_block_stats(
        event,
        stage="planner.final_prompt",
        blocks={"focus": "当前问题", "memory": "一段长期记忆"},
        metadata={"think_level": 2},
    )

    payload = event.get_extra(CONTEXT_BLOCK_STATS_KEY)[0]
    assert payload["blocks"]["focus"]["chars"] == 4
    assert payload["blocks"]["focus"]["nonempty"] is True
    assert payload["blocks"]["focus"]["hash"]
    assert "当前问题" not in str(payload)
    assert payload["metadata"]["think_level"] == 2


def test_reply_stats_support_multiple_segments_and_sent_count():
    event = _Event()

    record_reply_stats(
        event,
        segment_count=3,
        segment_lengths=[4, 5, 6],
        total_chars=15,
        strategy="smart",
        send_status="sent",
        sent_segment_count=3,
    )

    assert event.get_extra(REPLY_STATS_KEY) == {
        "segment_count": 3,
        "segment_lengths": [4, 5, 6],
        "total_chars": 15,
        "strategy": "smart",
        "send_status": "sent",
        "sent_segment_count": 3,
    }
