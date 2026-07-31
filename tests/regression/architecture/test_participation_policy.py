from types import SimpleNamespace

from astrmai.conversation.attention.participation_policy import (
    ParticipationPolicy,
    ParticipationState,
)


class _Event:
    def __init__(self, actor_id: str, text: str, *, timestamp: float = 120.0, extras=None):
        self.message_str = text
        self.timestamp = timestamp
        self._actor_id = actor_id
        self._extras = dict(extras or {})

    def get_sender_id(self):
        return self._actor_id

    def get_extra(self, key, default=None):
        return self._extras.get(key, default)


def _committed(actor_id: str, *, timestamp: float = 100.0, topic_epoch: int = 1):
    return SimpleNamespace(
        target_sender_id=actor_id,
        timestamp=timestamp,
        topic_epoch=topic_epoch,
        turn_id="turn-1",
        source_event_ids=("event-1",),
    )


def test_batch_strong_wakeup_forces_participation():
    policy = ParticipationPolicy()
    focus = _Event("user-1", "后一句")

    result, state = policy.evaluate(
        focus_event=focus,
        batch_events=[focus],
        strong_wakeup_event_ids=("event-at",),
    )

    assert result.action == "FORCE_PASS"
    assert result.score == 100
    assert result.strong_wakeup_event_ids == ("event-at",)
    assert state.phase == "engaged"


def test_committed_target_short_continuation_forces_participation():
    policy = ParticipationPolicy()
    focus = _Event(
        "user-1",
        "继续",
        extras={"astrmai_dialog_history_policy": {"topic_epoch": 1}},
    )

    result, state = policy.evaluate(
        focus_event=focus,
        batch_events=[focus],
        recent_committed_turn=_committed("user-1"),
    )

    assert result.action == "FORCE_PASS"
    assert "committed_target_continuation" in result.signals
    assert "explicit_short_continuation" in result.signals
    assert state.actor_id == "user-1"


def test_other_actor_does_not_inherit_engaged_relationship():
    policy = ParticipationPolicy()
    focus = _Event(
        "user-2",
        "继续",
        extras={"astrmai_dialog_history_policy": {"topic_epoch": 1}},
    )
    previous = ParticipationState(
        phase="engaged",
        actor_id="user-1",
        topic_epoch=1,
        updated_at=100.0,
    )

    result, state = policy.evaluate(
        focus_event=focus,
        batch_events=[focus],
        previous_state=previous,
    )

    assert result.action == "NEED_JUDGE"
    assert "different_actor_observing" in result.signals
    assert state == previous


def test_topic_epoch_change_invalidates_hysteresis():
    policy = ParticipationPolicy()
    focus = _Event(
        "user-1",
        "继续",
        extras={"astrmai_dialog_history_policy": {"topic_epoch": 2}},
    )
    previous = ParticipationState(
        phase="engaged",
        actor_id="user-1",
        topic_epoch=1,
        updated_at=100.0,
    )

    result, state = policy.evaluate(
        focus_event=focus,
        batch_events=[focus],
        previous_state=previous,
    )

    assert result.action == "NEED_JUDGE"
    assert result.invalidated_reason == "topic_epoch_changed"
    assert state.phase == "observing"


def test_external_unaddressed_event_is_high_confidence_drop():
    policy = ParticipationPolicy()
    focus = _Event(
        "external-bot",
        "[聊天记录]",
        extras={"astrmai_event_provenance": "external_plugin"},
    )

    result, state = policy.evaluate(
        focus_event=focus,
        batch_events=[focus],
    )

    assert result.action == "DROP"
    assert result.reason == "external_plugin_unaddressed"
    assert state.phase == "detached"


def test_same_actor_engaged_hysteresis_stays_on_judge_boundary():
    policy = ParticipationPolicy()
    focus = _Event(
        "user-1",
        "继续",
        extras={"astrmai_dialog_history_policy": {"topic_epoch": 1}},
    )
    previous = ParticipationState(
        phase="engaged",
        actor_id="user-1",
        topic_epoch=1,
        updated_at=100.0,
    )

    result, _ = policy.evaluate(
        focus_event=focus,
        batch_events=[focus],
        previous_state=previous,
    )

    assert result.action == "NEED_JUDGE"
    assert result.score == 45
    assert "engaged_hysteresis" in result.signals
