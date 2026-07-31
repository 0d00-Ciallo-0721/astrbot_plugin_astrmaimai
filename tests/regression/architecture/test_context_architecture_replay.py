from pathlib import Path

from astrmai.conversation.replay.context_architecture_harness import (
    ContextArchitectureReplayHarness,
    ReplayClock,
    load_replay_cases,
)


FIXTURE = Path(__file__).resolve().parents[2] / "fixtures" / "context_architecture_incidents.jsonl"


def test_all_anonymized_context_architecture_incidents_replay_deterministically():
    harness = ContextArchitectureReplayHarness(clock=ReplayClock())
    cases = load_replay_cases(FIXTURE)

    assert len(cases) == 10
    for case in cases:
        result = harness.run(case)
        expected = case["expected"]
        assert result.target["target_actor_id"] == expected["target_actor_id"], case["case_id"]
        if "participation" in expected:
            assert result.participation["action"] == expected["participation"], case["case_id"]
        if "shared_actor_id" in expected:
            assert expected["shared_actor_id"] in result.actor_set["recent_topic_actor_ids"]
        if "explicit_actor_id" in expected:
            assert expected["explicit_actor_id"] in result.actor_set["explicit_target_actor_ids"]
        if "sent_segment_count" in expected:
            assert len(result.reply_commit["sent_segments"]) == expected["sent_segment_count"]
            assert result.reply_commit["partial_send"] is expected["partial_send"]
        if "proactive_generation_current" in expected:
            assert result.proactive_generation_current is expected["proactive_generation_current"]
        if expected.get("commit_absent"):
            assert result.reply_commit == {}
        if "media_event_count" in expected:
            assert result.context["media_event_count"] == expected["media_event_count"]
        assert set(result.timings_ms) == {
            "normalization",
            "target",
            "renderer",
            "commit",
        }
        assert all(value >= 0.0 for value in result.timings_ms.values())


def test_replay_output_contains_no_fixture_message_text():
    harness = ContextArchitectureReplayHarness(clock=ReplayClock())
    case = load_replay_cases(FIXTURE)[0]

    result = harness.run(case)
    serialized = repr(result)

    assert "连续聊天" not in serialized
    assert "轮到我" not in serialized
    assert result.context["context_blocks"]
