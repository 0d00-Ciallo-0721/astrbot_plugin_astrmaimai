import asyncio
from types import SimpleNamespace

from astrmai.conversation.attention.window_buffer import AttentionWindowBuffer
from astrmai.conversation.contracts.vision_candidate import VisionCandidate


class _Event:
    def __init__(self, *, candidates=None, pure_at=False):
        self._extra = {
            "astrmai_vision_candidates": list(candidates or []),
            "astrmai_pure_at_bot": pure_at,
        }

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


def _candidate(message_id="image-1", sender_id="user-1", source_kind="inline"):
    return VisionCandidate(
        message_id=message_id,
        group_id="group-1",
        sender_id=sender_id,
        timestamp=10.0,
        image_index=0,
        candidate_refs=(f"{message_id}.jpg",),
        source_kind=source_kind,
    ).as_dict()


def _session():
    return SimpleNamespace(
        pending_vision_images={},
        pending_vision_mentions={},
        vision_pair_signal=asyncio.Event(),
    )


def _buffer():
    gate = SimpleNamespace(
        config=SimpleNamespace(vision=SimpleNamespace(at_image_pair_window_sec=3.0))
    )
    return AttentionWindowBuffer(gate)


def test_same_message_at_image_reply_is_bound_without_pending_state():
    session = _session()
    event = _Event(candidates=[_candidate(source_kind="reply")])

    mode = _buffer().register_group_vision_pairing(
        session, event, sender_id="user-1", is_at_bot=True
    )

    assert mode == "same_message_reply"
    assert event.get_extra("vision_prefilter_selected") is True
    paired = event.get_extra("astrmai_vision_candidates")[0]
    assert paired["pairing_mode"] == mode
    assert paired["pairing_verified"] is True
    assert paired["paired_sender_id"] == "user-1"
    assert paired["paired_group_id"] == "group-1"
    assert session.pending_vision_images == {}


def test_image_then_at_binds_only_same_sender():
    session = _session()
    image = _Event(candidates=[_candidate(sender_id="user-1")])
    other_at = _Event(pure_at=True)
    matching_at = _Event(pure_at=True)
    buffer = _buffer()

    assert buffer.register_group_vision_pairing(
        session, image, sender_id="user-1", is_at_bot=False
    ) == "pending_image"
    assert buffer.pending_pair_wait_seconds(session) > 0
    assert buffer.register_group_vision_pairing(
        session, other_at, sender_id="user-2", is_at_bot=True
    ) == "pending_at"
    assert other_at.get_extra("astrmai_vision_candidates") == []
    assert buffer.register_group_vision_pairing(
        session, matching_at, sender_id="user-1", is_at_bot=True
    ) == "image_then_at"
    assert matching_at.get_extra("extracted_image_refs") == ["image-1.jpg"]
    assert matching_at.get_extra("astrmai_cross_message_vision_bound") is True
    assert matching_at.get_extra("astrmai_release_vision_pair_waiter") is True
    paired = matching_at.get_extra("astrmai_vision_candidates")[0]
    assert paired["pairing_verified"] is True
    assert paired["paired_sender_id"] == "user-1"


def test_at_then_image_wakes_pair_waiter_and_copies_candidate_to_mention():
    session = _session()
    mention = _Event(pure_at=True)
    image = _Event(candidates=[_candidate()])
    buffer = _buffer()

    assert buffer.register_group_vision_pairing(
        session, mention, sender_id="user-1", is_at_bot=True
    ) == "pending_at"
    assert buffer.pending_pair_wait_seconds(session) > 0
    assert buffer.register_group_vision_pairing(
        session, image, sender_id="user-1", is_at_bot=False
    ) == "at_then_image"

    assert mention.get_extra("extracted_image_refs") == ["image-1.jpg"]
    assert image.get_extra("astrmai_group_direct_wakeup") is True
    assert image.get_extra("astrmai_release_vision_pair_waiter") is True
    assert session.vision_pair_signal.is_set() is False
    assert buffer.pending_pair_wait_seconds(session) == 0


def test_expired_image_is_not_rebound_to_later_at():
    session = _session()
    image = _Event(candidates=[_candidate()])
    mention = _Event(pure_at=True)
    buffer = _buffer()
    buffer.register_group_vision_pairing(
        session, image, sender_id="user-1", is_at_bot=False
    )
    session.pending_vision_images["user-1"]["expires_at"] = 0.0

    mode = buffer.register_group_vision_pairing(
        session, mention, sender_id="user-1", is_at_bot=True
    )

    assert mode == "pending_at"
    assert mention.get_extra("astrmai_vision_candidates") == []
