from __future__ import annotations

from dataclasses import dataclass


BRIDGE_MAX_TURNS = 2
BRIDGE_MAX_EVENT_IDS = 4
BRIDGE_MAX_PROMPT_CHARS = 900


@dataclass(frozen=True, slots=True)
class BridgeDecision:
    allowed: bool = False
    reason: str = "no_source_anchor"
    confidence: float = 0.0
    source_chat_key: str = ""
    target_chat_key: str = ""
    source_topic_epoch: int = 0
    target_topic_epoch: int = 0
    evidence_event_ids: tuple[str, ...] = ()
    source_anchor_preview: str = ""
    source_open_loop: str = ""
    turns: tuple[str, ...] = ()
    created_at: float = 0.0
    expires_at: float = 0.0
    source_age_seconds: float = 0.0

    def is_active(self, now: float) -> bool:
        return self.allowed and self.created_at <= now < self.expires_at

    def prompt_text(self) -> str:
        if not self.allowed:
            return ""
        lines = [
            "跨话题临时承接（仅供本轮参考）：",
            f"- reason={self.reason}; confidence={self.confidence:.2f}; source_age_seconds={int(self.source_age_seconds)}; evidence_count={len(self.evidence_event_ids)}",
            f"- subject={self.source_anchor_preview}",
        ]
        if self.source_open_loop:
            lines.append(f"- open_loop={self.source_open_loop}")
        lines.extend(f"- recent_pair={turn}" for turn in self.turns[:BRIDGE_MAX_TURNS])
        return "\n".join(lines)[:BRIDGE_MAX_PROMPT_CHARS]
