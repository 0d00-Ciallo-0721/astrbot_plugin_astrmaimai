from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astrmai.memory.contracts.memory_query import MemoryCandidate, MemoryQuery
from astrmai.memory.services.actor_memory_scope import build_actor_memory_scope
from astrmai.memory.services.memory_injection_service import MemoryInjectionService
from astrmai.memory.services.memory_retrieval_service import MemoryRetrievalService
from astrmai.conversation.contracts.turn_context import TurnContext
from astrmai.conversation.contracts.turn_target import ActorSet, TargetKind, TurnTarget


class _Store:
    def __init__(self, candidates):
        self.candidates = list(candidates)

    async def search(self, *_args, **_kwargs):
        return list(self.candidates)

    async def batch_get_by_ids(self, _ids, allow_stale=False):
        return {}

    async def batch_get_memory_meta(self, _ids):
        return {}


def _candidate(
    memory_id: str,
    *,
    sender_id: str = "",
    kind: str = "fact",
    content: str = "公共事实",
    metadata: dict | None = None,
) -> MemoryCandidate:
    return MemoryCandidate(
        id=memory_id,
        kind=kind,
        source="canonical",
        summary=content,
        content=content,
        session_id="ff:GroupMessage:552752264",
        sender_id=sender_id,
        relevance_score=0.9,
        metadata_hydrated=True,
        metadata=dict(metadata or {}),
    )


def _query(*allowed_actor_ids: str) -> MemoryQuery:
    return MemoryQuery(
        query="群聊记忆",
        session_id="ff:GroupMessage:552752264",
        sender_id="current",
        top_k=20,
        metadata={
            "visibility_mode": "auto",
            "actor_memory_scope": {
                "is_group": True,
                "group_id": "552752264",
                "current_actor_id": "current",
                "allowed_actor_ids": list(allowed_actor_ids),
            },
        },
    )


def test_group_actor_filter_keeps_current_and_explicit_target_memories():
    candidates = [
        _candidate(
            "current-memory",
            sender_id="current",
            kind="preference",
            content="当前用户喜欢布丁",
        ),
        _candidate(
            "target-memory",
            sender_id="target",
            kind="identity",
            content="目标用户昵称为空酱",
        ),
        _candidate(
            "other-memory",
            sender_id="other",
            kind="identity",
            content="其他用户昵称为萤",
        ),
    ]

    query = _query("current", "target")
    result = asyncio.run(MemoryRetrievalService(_Store(candidates)).retrieve(query))

    assert [item.id for item in result] == ["current-memory", "target-memory"]
    actor_filter = query.metadata["_trace"]["actor_scope_filter"]
    assert actor_filter["allowed_actor_ids"] == ["current", "target"]
    assert actor_filter["before_count"] == 3
    assert actor_filter["after_count"] == 2
    assert actor_filter["suppressed_ids"] == ["other-memory"]
    assert actor_filter["suppressed_reasons"]["other-memory"] == "actor_not_allowed"


def test_group_actor_filter_blocks_actorless_relationship_memory():
    candidates = [
        _candidate(
            "actorless-relationship",
            kind="relationship",
            content="欧尼酱是唯一恋人",
        ),
        _candidate(
            "actorless-profile",
            kind="profile",
            content="喜欢焦糖布丁",
        ),
    ]

    query = _query("current")
    result = asyncio.run(MemoryRetrievalService(_Store(candidates)).retrieve(query))

    assert result == []
    reasons = query.metadata["_trace"]["actor_scope_filter"]["suppressed_reasons"]
    assert reasons["actorless-relationship"] == "actorless_sensitive_memory"
    assert reasons["actorless-profile"] == "actorless_sensitive_memory"


def test_group_actor_filter_keeps_public_fact_but_blocks_exclusive_group_relation():
    candidates = [
        _candidate(
            "shared-game",
            kind="topic",
            content="群里正在进行布丁称号游戏",
            metadata={
                "scope": "group_shared",
                "speaker_ids": ["current", "target", "other"],
            },
        ),
        _candidate(
            "shared-exclusive",
            kind="relationship",
            content="某位群友是妃爱唯一恋人",
            metadata={
                "scope": "group_shared",
                "speaker_ids": ["current", "target"],
            },
        ),
    ]

    query = _query("current")
    result = asyncio.run(MemoryRetrievalService(_Store(candidates)).retrieve(query))

    assert [item.id for item in result] == ["shared-game"]
    actor_filter = query.metadata["_trace"]["actor_scope_filter"]
    assert actor_filter["group_shared_count"] == 1
    assert (
        actor_filter["suppressed_reasons"]["shared-exclusive"]
        == "group_shared_sensitive_memory"
    )


def test_group_actor_filter_runs_before_deep_rerank_and_compress():
    candidates = [
        _candidate(
            "current-memory",
            sender_id="current",
            kind="preference",
            content="当前用户喜欢炒面",
        ),
        _candidate(
            "other-memory",
            sender_id="other",
            kind="relationship",
            content="其他用户是专属恋人",
        ),
    ]
    query = _query("current")
    query.policy = "deep"
    query.think_level = 3
    query.metadata["query_rewrite_eligible"] = False

    service = MemoryRetrievalService(_Store(candidates))
    seen: dict[str, list[str]] = {}

    async def rerank(_query, values):
        seen["rerank"] = [item.id for item in values]
        return list(values)

    async def compress(_query, values):
        seen["compress"] = [item.id for item in values]
        return ""

    service._rerank_candidates = rerank
    service._compress_guidance = compress

    result = asyncio.run(service.retrieve_deep(query))

    assert [item.id for item in result] == ["current-memory"]
    assert seen["rerank"] == ["current-memory"]
    assert seen["compress"] == ["current-memory"]


def test_actor_scope_uses_only_stable_evidence_sources():
    class _Event:
        unified_msg_origin = "ff:GroupMessage:552752264"

        def __init__(self):
            self._extras = {}

        def get_group_id(self):
            return "552752264"

        def get_sender_id(self):
            return "current"

        def get_extra(self, key, default=None):
            return self._extras.get(key, default)

    event = _Event()
    turn_context = TurnContext()
    turn_context.attention.actor_set = ActorSet(
        current_actor_id="current",
        explicit_target_actor_ids=("target",),
        at_actor_ids=("at-user",),
        quoted_actor_ids=("quoted-user",),
        recent_topic_actor_ids=("topic-user",),
        bot_id="bot",
    )
    turn_context.attention.turn_target = TurnTarget(
        target_kind=TargetKind.ACTOR,
        target_actor_id="target",
        evidence="reply",
    )
    event._extras["astrmai_turn_context"] = turn_context
    event._extras["astrmai_referenced_entities"] = [
        {
            "resolved_id": "resolved-user",
            "ambiguous": False,
        },
        {
            "resolved_id": "ambiguous-user",
            "ambiguous": True,
        },
        {
            "candidate_ids": ["nickname-only-user"],
            "ambiguous": False,
        },
    ]

    scope = build_actor_memory_scope(event)

    assert scope.allowed_actor_ids == (
        "current",
        "target",
        "at-user",
        "quoted-user",
        "topic-user",
        "resolved-user",
    )
    assert "bot" not in scope.allowed_actor_ids
    assert "ambiguous-user" not in scope.allowed_actor_ids
    assert "nickname-only-user" not in scope.allowed_actor_ids


def test_injection_no_result_keeps_actor_suppression_observability():
    class _Event:
        message_str = "还记得那个人吗"
        unified_msg_origin = "ff:GroupMessage:552752264"

        def __init__(self):
            self._extras = {"astrmai_think_level": 2}

        def get_group_id(self):
            return "552752264"

        def get_sender_id(self):
            return "current"

        def get_sender_name(self):
            return "当前用户"

        def get_extra(self, key, default=None):
            return self._extras.get(key, default)

        def set_extra(self, key, value):
            self._extras[key] = value

    class _Retrieval:
        engine = None

        async def retrieve(self, query):
            query.metadata.setdefault("_trace", {})["actor_scope_filter"] = {
                "allowed_actor_ids": ["current"],
                "before_count": 2,
                "after_count": 0,
                "suppressed_ids": ["other-relation", "actorless-profile"],
                "suppressed_count": 2,
            }
            return []

    config = SimpleNamespace(
        memory=SimpleNamespace(
            recall_top_k=5,
            memory_query_builder_enabled=True,
            intent_rerank_enabled=True,
            adaptive_top_k_enabled=True,
            memory_retrieval_debug_trace_enabled=False,
        ),
        persona=SimpleNamespace(persona_id=""),
    )
    event = _Event()
    bundle = asyncio.run(
        MemoryInjectionService(_Retrieval(), config=config).build_bundle(event=event)
    )

    assert bundle.skip_reason == "no_result"
    decision = event.get_extra("astrmai_turn_context").memory
    assert decision.actor_whitelist == ["current"]
    assert decision.suppressed_candidate_ids == [
        "other-relation",
        "actorless-profile",
    ]
    assert decision.suppressed_candidate_count == 2
    funnel = event.get_extra("astrmai_memory_funnel")
    assert funnel["actor_suppressed_count"] == 2
    assert funnel["actor_candidate_count_before_filter"] == 2
