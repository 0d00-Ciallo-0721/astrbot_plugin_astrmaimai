from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import gc
import weakref


class _Event:
    def __init__(self, *, trace_id: str = "turn-1", generation: int = 1):
        self._extra = {
            "astrmai_trace_id": trace_id,
            "astrmai_turn_identity": SimpleNamespace(
                mode="group",
                chat_id="group-1",
                thread_id="thread-1",
                generation=generation,
            ),
        }

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class _NonWeakEvent:
    __slots__ = ("_extra",)

    def __init__(self, trace_id: str):
        self._extra = {"astrmai_trace_id": trace_id}

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        if key == "_astrmai_turn_outcome_lock":
            raise TypeError("lock attachment unsupported")
        self._extra[key] = value




def _contracts():
    from astrmai.conversation.contracts import turn_outcome

    return turn_outcome


def test_normal_reply_blocks_fallback():
    mod = _contracts()
    event = _Event()
    assert mod.claim_text_output(event, "reply")
    mod.record_text_sent(event, segments=2, kind="reply")

    assert not mod.claim_text_output(event, "fallback")
    assert mod.get_turn_outcome(event).terminal_status == mod.TurnOutcomeStatus.COMPLETED


def test_fallback_blocks_normal_reply():
    mod = _contracts()
    event = _Event()
    assert mod.claim_text_output(event, "fallback")
    mod.record_text_sent(event, segments=1, kind="fallback")

    assert not mod.claim_text_output(event, "reply")
    assert mod.get_turn_outcome(event).terminal_status == mod.TurnOutcomeStatus.FALLBACK


def test_system2_handled_blocks_executor_reply():
    mod = _contracts()
    event = _Event()
    mod.mark_system2_handled(event, reason="system2_failure_handled")

    assert not mod.claim_text_output(event, "reply")
    assert mod.get_turn_outcome(event).system2_handled


def test_deferred_replay_success_is_idempotent():
    mod = _contracts()
    event = _Event()
    assert mod.can_deferred_replay(event)
    mod.mark_deferred_replayed(event, reply_sent=False)

    assert not mod.can_deferred_replay(event)
    assert mod.get_turn_outcome(event).deferred_replayed


def test_deferred_replay_expiry_blocks_output():
    mod = _contracts()
    event = _Event()
    mod.mark_terminal(event, mod.TurnOutcomeStatus.SKIPPED, "deferred_ttl_expired")

    assert not mod.can_deferred_replay(event)
    assert not mod.claim_text_output(event, "reply")


def test_sent_tool_action_survives_text_failure_without_reexecution():
    mod = _contracts()
    event = _Event()
    assert mod.can_send_tool_action(event, "action-1")
    mod.record_tool_action_result(event, "action-1", "sent")
    mod.record_text_failed(event, "provider_timeout")

    assert not mod.can_send_tool_action(event, "action-1")
    assert mod.can_send_fallback(event)
    assert mod.get_turn_outcome(event).tool_action_count == 1


def test_uncertain_tool_action_is_not_automatically_retried():
    mod = _contracts()
    event = _Event()
    mod.record_tool_action_result(event, "action-1", "uncertain")

    assert not mod.can_send_tool_action(event, "action-1")
    assert mod.get_turn_outcome(event).tool_action_uncertain


def test_shutdown_completion_callback_cannot_reopen_output():
    mod = _contracts()
    event = _Event()
    mod.mark_terminal(event, mod.TurnOutcomeStatus.SHUTDOWN, "shutdown_rejected")

    assert not mod.claim_completion_callback(event)
    assert not mod.claim_text_output(event, "reply")
    assert mod.get_turn_outcome(event).terminal_status == mod.TurnOutcomeStatus.SHUTDOWN


def test_cancelled_and_budget_exhausted_are_non_sendable():
    mod = _contracts()
    for status in (
        mod.TurnOutcomeStatus.CANCELLED,
        mod.TurnOutcomeStatus.BUDGET_EXHAUSTED,
    ):
        event = _Event(trace_id=status.value)
        mod.mark_terminal(event, status, status.value)
        assert not mod.claim_text_output(event, "reply")
        assert not mod.can_send_tool_action(event, "action-1")
        assert not mod.can_deferred_replay(event)


def test_superseded_old_turn_does_not_pollute_new_turn():
    mod = _contracts()
    old_event = _Event(trace_id="old", generation=1)
    new_event = _Event(trace_id="new", generation=2)
    mod.mark_terminal(old_event, mod.TurnOutcomeStatus.SUPERSEDED, "new_generation")

    assert not mod.claim_text_output(old_event, "reply")
    assert mod.claim_text_output(new_event, "reply")
    assert mod.get_turn_outcome(new_event).turn_id == "new"


def test_completion_callback_claim_is_idempotent():
    mod = _contracts()
    event = _Event()

    assert mod.claim_completion_callback(event)
    assert not mod.claim_completion_callback(event)
    assert event.get_extra("astrmai_proactive_completed") is False
    mod.settle_completion_callback(event, succeeded=True)
    assert event.get_extra("astrmai_proactive_completed") is True


def test_completion_callback_failure_does_not_mark_legacy_complete():
    mod = _contracts()
    event = _Event()
    assert mod.claim_completion_callback(event).allowed


def test_authoritative_outcome_prevents_legacy_completion_reactivation():
    mod = _contracts()
    event = _Event()
    event.set_extra(
        mod.TURN_OUTCOME_EXTRA_KEY,
        {
            "turn_id": "turn-1",
            "trace_id": "turn-1",
            "terminal_status": "active",
            "completion_callback_claimed": False,
            "completion_callback_completed": False,
        },
    )
    event.set_extra("astrmai_proactive_completed", True)
    outcome = mod.ensure_turn_outcome(event)
    assert outcome.completion_callback_completed is False
    assert mod.claim_completion_callback(event).allowed
    mod.settle_completion_callback(event, succeeded=False)
    assert event.get_extra("astrmai_proactive_completed") is False
    assert mod.claim_completion_callback(event).allowed


def test_can_send_tool_action_is_read_only_and_claim_is_atomic():
    mod = _contracts()
    event = _Event()
    assert mod.can_send_tool_action(event, "action-1").allowed
    assert mod.can_send_tool_action(event, "action-1").allowed
    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(lambda _: mod.claim_tool_action(event, "action-1"), range(2)))
    assert sum(decision.allowed for decision in decisions) == 1
    mod.release_tool_action(event, "action-1")
    assert mod.claim_tool_action(event, "action-1").allowed


def test_event_lock_registry_does_not_retain_destroyed_events():
    mod = _contracts()
    events = [_Event(trace_id=f"lock-{index}") for index in range(128)]
    for event in events:
        assert mod.claim_text_output(event, "reply").allowed
    registry_size = len(mod._EVENT_LOCK_REGISTRY)
    references = [weakref.ref(event) for event in events]
    del event
    del events
    gc.collect()
    assert all(reference() is None for reference in references)
    assert len(mod._EVENT_LOCK_REGISTRY) < registry_size


def test_nonweak_events_keep_active_fallback_locks_beyond_soft_bound():
    mod = _contracts()
    events = [_NonWeakEvent(f"nonweak-{index}") for index in range(1025)]
    for event in events:
        assert mod.claim_text_output(event, "reply").allowed
    assert len(mod._EVENT_LOCK_FALLBACK) >= 1025
    old_event = events[0]
    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(lambda _: mod.claim_text_output(old_event, "reply"), range(2)))
    assert sum(decision.allowed for decision in decisions) == 0
    for event in events:
        mod.mark_terminal(event, mod.TurnOutcomeStatus.SKIPPED, "test_cleanup")
    assert len(mod._EVENT_LOCK_FALLBACK) == 0


def test_reply_fallback_and_tool_statistics_stay_consistent():
    mod = _contracts()
    event = _Event()
    mod.record_tool_action_result(event, "action-1", "sent")
    mod.record_tool_action_result(event, "action-2", "sent")
    assert mod.claim_text_output(event, "fallback")
    mod.record_text_sent(event, segments=1, kind="fallback")
    outcome = mod.get_turn_outcome(event)

    assert outcome.reply_sent
    assert outcome.reply_sent_segments == 1
    assert outcome.fallback_sent
    assert outcome.tool_actions_sent
    assert outcome.tool_action_count == 2


def test_turn_outcome_updates_legacy_fields():
    mod = _contracts()
    event = _Event()
    mod.mark_system2_handled(event)
    mod.record_tool_action_result(event, "action-1", "sent")
    mod.mark_deferred_replayed(event, reply_sent=False)

    assert event.get_extra("astrmai_system2_failure_handled") is True
    assert event.get_extra("tool_actions_sent") is True
    assert event.get_extra("deferred_replayed") is True


def test_legacy_fields_can_be_recovered_from_serialized_outcome():
    mod = _contracts()
    event = _Event()
    event.set_extra(
        mod.TURN_OUTCOME_EXTRA_KEY,
        {
            "turn_id": "turn-1",
            "trace_id": "turn-1",
            "reply_sent": True,
            "reply_sent_segments": 3,
            "tool_actions_sent": False,
            "tool_action_count": 0,
            "fallback_sent": False,
            "system2_handled": True,
            "deferred_replayed": False,
            "terminal_reason": "reply_sent",
            "terminal_status": "completed",
            "updated_at": 1.0,
        },
    )

    outcome = mod.ensure_turn_outcome(event)
    assert outcome.reply_sent_segments == 3
    assert event.get_extra("astrmai_reply_sent") is True
    assert event.get_extra("astrmai_reply_sent_segment_count") == 3


def test_malformed_outcome_fails_closed():
    mod = _contracts()
    event = _Event()
    event.set_extra(mod.TURN_OUTCOME_EXTRA_KEY, {"terminal_status": "made_up"})

    outcome = mod.ensure_turn_outcome(event)
    assert outcome.terminal_status == mod.TurnOutcomeStatus.FAILED
    assert outcome.terminal_reason == "malformed_turn_outcome"
    assert not mod.claim_text_output(event, "reply")
    assert not mod.can_send_tool_action(event, "action-1")


def test_concurrent_reply_claims_are_atomic():
    mod = _contracts()
    event = _Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(lambda _: mod.claim_text_output(event, "reply"), range(2)))
    assert sum(decision.allowed for decision in decisions) == 1
    assert all(decision.turn_id == "turn-1" for decision in decisions)
    assert any(decision.reason == "claimed" for decision in decisions)


def test_claim_decision_is_structured_and_bool_compatible():
    mod = _contracts()
    event = _Event()
    decision = mod.claim_text_output(event, "reply")
    assert decision.allowed is True
    assert decision.claim_kind == "reply"
    assert decision.turn_id == "turn-1"
    assert decision.trace_id == "turn-1"
    assert bool(decision)


def test_concurrent_reply_and_fallback_have_one_winner():
    mod = _contracts()
    event = _Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(lambda kind: mod.claim_text_output(event, kind), ("reply", "fallback")))
    assert sum(decision.allowed for decision in decisions) == 1


def test_concurrent_fallback_and_deferred_replay_are_coordinated():
    mod = _contracts()
    event = _Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [
            pool.submit(mod.claim_text_output, event, "fallback"),
            pool.submit(mod.claim_deferred_replay, event),
        ]
        fallback, deferred = (future.result() for future in futures)
    assert sum(decision.allowed for decision in (fallback, deferred)) == 1


def test_concurrent_completion_claims_execute_once():
    mod = _contracts()
    event = _Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        decisions = list(pool.map(lambda _: mod.claim_completion_callback(event), range(2)))
    assert sum(decision.allowed for decision in decisions) == 1
    mod.settle_completion_callback(event, succeeded=True)
    assert not mod.claim_completion_callback(event).allowed


def test_tool_action_and_text_claims_do_not_overwrite_each_other():
    mod = _contracts()
    event = _Event()
    with ThreadPoolExecutor(max_workers=2) as pool:
        text_decision, action_decision = pool.submit(mod.claim_text_output, event, "reply").result(), pool.submit(
            mod.can_send_tool_action, event, "action-1"
        ).result()
    assert text_decision.allowed
    assert action_decision.allowed
    mod.record_tool_action_result(event, "action-1", "sent")
    mod.record_text_sent(event, segments=1, kind="reply")
    outcome = mod.get_turn_outcome(event)
    assert outcome.reply_sent and outcome.tool_actions_sent


def test_failed_send_releases_claim_for_retry():
    mod = _contracts()
    event = _Event()
    assert mod.claim_text_output(event, "reply").allowed
    mod.record_text_failed(event, "provider_timeout")
    retry = mod.claim_text_output(event, "reply")
    assert retry.allowed
    assert retry.reason == "claimed"


def test_failed_can_transition_to_retryable_then_active():
    mod = _contracts()
    event = _Event()
    mod.record_text_failed(event, "provider_timeout")
    retryable = mod.mark_terminal(event, mod.TurnOutcomeStatus.RETRYABLE, "retry_wait")
    assert retryable.terminal_status is mod.TurnOutcomeStatus.RETRYABLE
    assert mod.claim_text_output(event, "reply").allowed
    assert mod.get_turn_outcome(event).terminal_status is mod.TurnOutcomeStatus.ACTIVE


def test_failed_cannot_jump_directly_to_successful_output_terminal():
    mod = _contracts()
    event = _Event()
    mod.record_text_failed(event, "provider_timeout")
    outcome = mod.mark_terminal(event, mod.TurnOutcomeStatus.COMPLETED, "invalid")
    assert outcome.terminal_status is mod.TurnOutcomeStatus.FAILED
    assert outcome.malformed


def test_shutdown_rejects_new_claims():
    mod = _contracts()
    event = _Event()
    mod.mark_terminal(event, mod.TurnOutcomeStatus.SHUTDOWN, "shutdown")
    decision = mod.claim_text_output(event, "reply")
    assert not decision.allowed
    assert decision.reason == "terminal_status"


def test_old_generation_callback_cannot_modify_new_turn():
    mod = _contracts()
    event = _Event(generation=1)
    mod.ensure_turn_outcome(event)
    event.set_extra(
        "astrmai_turn_identity",
        SimpleNamespace(mode="group", chat_id="group-1", thread_id="thread-1", generation=2),
    )
    decision = mod.claim_completion_callback(event)
    assert not decision.allowed
    assert decision.terminal_status is mod.TurnOutcomeStatus.SUPERSEDED


def test_malformed_outcome_cannot_be_reactivated_by_legacy_fields():
    mod = _contracts()
    event = _Event()
    event.set_extra(mod.TURN_OUTCOME_EXTRA_KEY, {"terminal_status": "unknown"})
    event.set_extra("astrmai_reply_sent", False)
    outcome = mod.ensure_turn_outcome(event)
    assert outcome.malformed
    assert not mod.claim_text_output(event, "reply").allowed


def test_terminal_statuses_are_irreversible():
    mod = _contracts()
    event = _Event()
    mod.mark_terminal(event, mod.TurnOutcomeStatus.COMPLETED, "sent")
    mod.mark_terminal(event, mod.TurnOutcomeStatus.FALLBACK, "fallback")
    outcome = mod.get_turn_outcome(event)
    assert outcome.terminal_status is mod.TurnOutcomeStatus.COMPLETED
    assert not mod.claim_text_output(event, "fallback").allowed


def test_cancelled_status_cannot_be_completed():
    mod = _contracts()
    event = _Event()
    mod.mark_terminal(event, mod.TurnOutcomeStatus.CANCELLED, "cancelled")
    mod.record_text_sent(event, segments=1, kind="reply")
    assert mod.get_turn_outcome(event).terminal_status is mod.TurnOutcomeStatus.CANCELLED


def test_settlement_callback_without_claim_is_idempotent():
    mod = _contracts()
    event = _Event()
    before = mod.ensure_turn_outcome(event).to_dict()
    mod.settle_completion_callback(event, succeeded=True)
    after = mod.get_turn_outcome(event).to_dict()
    assert after["completion_callback_completed"] == before["completion_callback_completed"]
