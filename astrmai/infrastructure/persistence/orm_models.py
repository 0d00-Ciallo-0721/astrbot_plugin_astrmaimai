from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from sqlmodel import Field, SQLModel, UniqueConstraint


class LastMessageMetadataDB(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: str = Field(index=True)
    sender_id: str
    has_image: bool = Field(default=False)
    image_urls: str = Field(default="[]")
    vl_executed: bool = Field(default=False)
    timestamp: float = Field(default_factory=time.time)


class ExpressionPattern(SQLModel, table=True):
    # ponytail: R6 — UniqueConstraint prevents duplicate rows from concurrent INSERTs

    id: Optional[int] = Field(default=None, primary_key=True)
    situation: str = Field(index=True)
    expression: str
    style: str = Field(default="")
    content_list: str = Field(default="[]")
    count: int = Field(default=1)
    checked: bool = Field(default=False, index=True)
    rejected: bool = Field(default=False, index=True)
    modified_by: str = Field(default="")
    source: str = Field(default="learning")
    shared_scope: str = Field(default="")
    think_level: int = Field(default=0)
    review_status: str = Field(default="pending", index=True)
    review_reason: str = Field(default="")
    review_suggestion: str = Field(default="")
    last_review_time: float = Field(default=0.0)
    weight: float = Field(default=1.0)
    last_active_time: float = Field(default_factory=time.time)
    create_time: float = Field(default_factory=time.time)
    group_id: str = Field(index=True)

    __table_args__ = (
        UniqueConstraint("group_id", "situation", "expression", name="uq_expression_pattern"),
        {"extend_existing": True},
    )


class MessageLog(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: str = Field(index=True)
    sender_id: str
    sender_name: str
    content: str
    timestamp: float = Field(default_factory=time.time)
    processed: bool = Field(default=False)
    event_id: str = Field(default="")
    event_schema_version: int = Field(default=0)
    platform_message_id: str = Field(default="")
    chat_kind: str = Field(default="")
    role: str = Field(default="")
    message_kind: str = Field(default="")
    is_bot: bool = Field(default=False)
    reply_target_event_id: str = Field(default="")
    reply_target_actor_id: str = Field(default="")
    reply_target_actor_name: str = Field(default="")
    quote_event_id: str = Field(default="")
    at_actor_ids: str = Field(default="[]")
    topic_epoch: int = Field(default=0)
    causal_parent_event_id: str = Field(default="")
    source_event_ids: str = Field(default="[]")
    provenance: str = Field(default="legacy")
    image_refs: str = Field(default="[]")
    interaction_kind: str = Field(default="")
    recalled: bool = Field(default=False)
    outcome: str = Field(default="")


@dataclass
class LastMessageMetadata:
    sender_id: str = ""
    has_image: bool = False
    image_urls: List[str] = field(default_factory=list)
    vl_executed: bool = False


@dataclass
class ChatState:
    chat_id: str = ""
    energy: float = 0.5
    mood: float = 0.0
    group_config: Dict[str, Any] = field(default_factory=dict)
    last_reset_date: str = ""
    total_replies: int = 0
    last_reply_time: float = 0.0
    total_messages: int = 0
    judgment_mode: str = "single"
    last_msg_info: LastMessageMetadata = field(default_factory=LastMessageMetadata)
    last_passive_decay_time: float = 0.0
    last_energy_recovery_time: float = 0.0
    is_dirty: bool = False
    last_access_time: float = field(default_factory=time.time)
    next_wakeup_timestamp: float = 0.0
    chat_kind: str = ""
    last_real_user_activity_at: float = 0.0
    last_committed_bot_reply_at: float = 0.0
    next_proactive_due_at: float = 0.0
    proactive_generation: int = 0
    unanswered_proactive_count: int = 0
    last_proactive_commit_id: str = ""
    last_proactive_cancel_reason: str = ""
    proactive_claim_token: str = ""
    proactive_claimed_at: float = 0.0


@dataclass
class UserProfile:
    user_id: str = ""
    name: str = "Unknown"
    social_score: float = 0.0
    last_seen: float = 0.0
    persona_analysis: str = ""
    message_count_for_profiling: int = 0
    last_persona_gen_time: float = 0.0
    identity: str = ""
    tags: List[str] = field(default_factory=list)
    nickname: str = ""
    nickname_reason: str = ""
    know_times: int = 0
    is_known: bool = False
    memory_points: List[Any] = field(default_factory=list)
    identity_points: List[str] = field(default_factory=list)
    preference_points: List[str] = field(default_factory=list)
    relationship_points: List[str] = field(default_factory=list)
    speech_style_points: List[str] = field(default_factory=list)
    group_footprints: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    profile_metadata: Dict[str, Any] = field(default_factory=dict)
    relationship_vector: Dict[str, Any] = field(default_factory=dict)
    is_dirty: bool = False
    last_access_time: float = field(default_factory=time.time)


class Jargon(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    content: str = Field(index=True)
    raw_content: str = Field(default="")
    meaning: str = Field(default="")
    is_jargon: bool = Field(default=False)
    count: int = Field(default=1)
    is_complete: bool = Field(default=False)
    group_id: str = Field(index=True)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class SocialRelation(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: str = Field(index=True)
    from_user: str = Field(index=True)
    to_user: str = Field(index=True)
    relation_type: str = Field(default="interaction")
    strength: float = Field(default=0.0)
    frequency: int = Field(default=0)
    last_interaction: float = Field(default_factory=time.time)


class VisualMemory(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    picid: str = Field(primary_key=True)
    type: str = Field(default="image")
    description: str = Field(default="")
    emotion_tags: str = Field(default="[]")
    timestamp: float = Field(default_factory=time.time)


class VisualAsset(SQLModel, table=True):
    """Content-addressed visual analysis shared across messages and sessions."""

    __table_args__ = {"extend_existing": True}

    asset_id: str = Field(primary_key=True)
    blob_hash: str = Field(default="", index=True)
    pixel_hash: str = Field(default="", index=True)
    perceptual_hash: str = Field(default="", index=True)
    prompt_version: str = Field(default="v1", index=True)
    type: str = Field(default="image")
    description: str = Field(default="")
    emotion_tags: str = Field(default="[]")
    model_id: str = Field(default="")
    mime_type: str = Field(default="")
    width: int = Field(default=0)
    height: int = Field(default=0)
    frame_count: int = Field(default=1)
    byte_size: int = Field(default=0)
    initial_recognition_elapsed_ms: float = Field(default=0.0)
    storage_path: str = Field(default="")
    status: str = Field(default="ready", index=True)
    hit_count: int = Field(default=0)
    last_error: str = Field(default="")
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)
    last_access_at: float = Field(default_factory=time.time, index=True)


class VisualMessageBinding(SQLModel, table=True):
    """Maps one concrete message image to its reusable visual asset."""

    __table_args__ = (
        UniqueConstraint(
            "chat_id",
            "message_id",
            "image_index",
            name="uq_visual_message_binding",
        ),
        {"extend_existing": True},
    )

    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: str = Field(default="", index=True)
    message_id: str = Field(default="", index=True)
    sender_id: str = Field(default="", index=True)
    image_index: int = Field(default=0)
    asset_id: str = Field(default="", index=True)
    legacy_picid: str = Field(default="", index=True)
    source_ref_hash: str = Field(default="")
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


class MemoryNode(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    name: str = Field(index=True, unique=True)
    type: str = Field(default="")
    description: str = Field(default="")
    last_updated: float = Field(default_factory=time.time)


class DailyReflection(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    date: str = Field(index=True, unique=True)
    reflection: str = Field(default="")
    created_at: float = Field(default_factory=time.time)


class MemoryEvent(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    event_id: str = Field(index=True, unique=True)
    session_id: str = Field(default="", index=True)
    date: str = Field(index=True)
    narrative: str = Field(default="")
    emotion: str = Field(default="")
    importance: int = Field(default=5)
    emotional_intensity: int = Field(default=5)
    reflection: str = Field(default="")
    memory_kind: str = Field(default="event", index=True)
    source_layer: str = Field(default="fact", index=True)
    tags: str = Field(default="[]")
    created_at: float = Field(default_factory=time.time)


class MemoryRetrievalTrace(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    id: Optional[int] = Field(default=None, primary_key=True)
    trace_id: str = Field(index=True, unique=True)
    chat_id: str = Field(index=True)
    sender_name: str = Field(default="")
    query: str = Field(default="")
    planner_question: str = Field(default="")
    tool_calls: str = Field(default="[]")
    trace_summary: str = Field(default="")
    selected_memory_ids: str = Field(default="[]")
    final_answer: str = Field(default="")
    source_layers: str = Field(default="[]")
    confidence: float = Field(default=0.0)
    created_at: float = Field(default_factory=time.time, index=True)


class CronSnapshot(SQLModel, table=True):
    __table_args__ = {"extend_existing": True}

    job_id: str = Field(primary_key=True)
    name: str = Field(default="")
    cron_expression: Optional[str] = Field(default=None)
    run_at: Optional[float] = Field(default=None)
    run_once: bool = Field(default=False)
    target_origin: str = Field(default="")
    payload: str = Field(default="{}")
    note: str = Field(default="")
    is_active: bool = Field(default=True)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


__all__ = [
    "ChatState",
    "CronSnapshot",
    "DailyReflection",
    "ExpressionPattern",
    "Jargon",
    "LastMessageMetadata",
    "LastMessageMetadataDB",
    "MemoryEvent",
    "MemoryNode",
    "MemoryRetrievalTrace",
    "MessageLog",
    "SocialRelation",
    "UserProfile",
    "VisualAsset",
    "VisualMessageBinding",
    "VisualMemory",
]
