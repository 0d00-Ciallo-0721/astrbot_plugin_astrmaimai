from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from ...shared.constants.defaults import (
    GatewaySettings,
    InfrastructureSettings,
    LaneRuntimeSettings,
    RuntimeFeatureFlags,
)
from ...conversation.contracts.focus_context import (
    FocusThreadContext,
    FreshnessState,
    ReplyFreshnessBudget,
    ReplyMode,
    VisionBundle,
)
from ...conversation.contracts.prompt_envelope import PromptEnvelope
from ...conversation.contracts.reply_artifact import OutboundPolicy, VisibleReplyArtifact
from ...conversation.contracts.turn_context import (
    AttentionSnapshot,
    CognitiveSnapshot,
    ContinuitySnapshot,
    PerceptionSnapshot,
    ToolDecisionTrace,
    ToolSnapshot,
    TurnContext,
)


class FailureKind(str, Enum):
    NONE = "none"
    EMPTY_RESPONSE = "empty_response"
    PROVIDER_FAILURE_TEXT = "provider_failure_text"
    BAD_PAYLOAD = "bad_payload"
    JSON_DECODE_ERROR = "json_decode_error"
    TIMEOUT = "timeout"
    CASCADE_FAILURE = "cascade_failure"
    UNKNOWN = "unknown"


@dataclass
class SocialTranscriptTurn:
    speaker_name: str
    target_name: str = ""
    turn_type: str = "message"
    content: str = ""
    relative_time: str = ""
    reply_mode_hint: str = ""


@dataclass
class LLMCallResult:
    ok: bool
    text: str = ""
    parsed_json: Any = None
    error_kind: FailureKind = FailureKind.NONE
    error_message: str = ""
    model_id: str = ""
    provider_family: str = ""
    usage: dict[str, int] = field(default_factory=dict)
    raw_completion: str = ""
    economy: dict[str, Any] = field(default_factory=dict)


__all__ = [
    "FailureKind",
    "FreshnessState",
    "FocusThreadContext",
    "GatewaySettings",
    "InfrastructureSettings",
    "LLMCallResult",
    "LaneRuntimeSettings",
    "OutboundPolicy",
    "PromptEnvelope",
    "ReplyFreshnessBudget",
    "ReplyMode",
    "RuntimeFeatureFlags",
    "SocialTranscriptTurn",
    "AttentionSnapshot",
    "CognitiveSnapshot",
    "ContinuitySnapshot",
    "PerceptionSnapshot",
    "ToolDecisionTrace",
    "ToolSnapshot",
    "TurnContext",
    "VisionBundle",
    "VisibleReplyArtifact",
]
