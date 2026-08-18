from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Iterable


DEFAULT_EMOTION_MAPPING = (
    "happy: positive, glad, relieved, affectionate",
    "sad: hurt, low, apologetic, disappointed",
    "angry: annoyed, hostile, blaming, rejecting",
    "neutral: plain, procedural, emotionally weak",
    "curious: wondering, asking, probing",
    "surprise: unexpected, startled, sudden turn",
)

DEFAULT_RELATIONSHIP_EVENTS = {
    "happy": "compliment",
    "sad": "emotional_support",
    "angry": "argument",
    "neutral": "normal_chat",
    "curious": "normal_chat",
    "surprise": "shared_interest",
}

VALID_RELATIONSHIP_EVENTS = frozenset(
    {
        "greeting",
        "normal_chat",
        "helpful_reply",
        "emotional_support",
        "compliment",
        "deep_conversation",
        "shared_interest",
        "gift",
        "insult",
        "ignore",
        "argument",
        "rudeness",
        "boundary_violation",
        "spam",
    }
)


def normalize_emotion_tag(value: Any) -> str:
    return str(value or "").strip().lower()


def _mapping_items(value: Any) -> Iterable[tuple[str, str]]:
    if isinstance(value, dict):
        for key, description in value.items():
            yield str(key or ""), str(description or "")
        return
    for entry in value or ():
        raw = str(entry or "").strip()
        if not raw:
            continue
        if ":" in raw:
            key, description = raw.split(":", 1)
        elif "：" in raw:
            key, description = raw.split("：", 1)
        else:
            yield raw, ""
            continue
        yield key, description


@dataclass(frozen=True)
class EmotionRelationshipResolution:
    event_type: str
    source: str


@dataclass(frozen=True)
class EmotionTagCatalog:
    descriptions: dict[str, str]
    relationship_overrides: dict[str, str]
    malformed_emotion_entries: tuple[str, ...] = ()
    invalid_relationship_entries: tuple[str, ...] = ()

    @classmethod
    def from_config(cls, config: Any) -> "EmotionTagCatalog":
        reply = getattr(config, "reply", None)
        raw_emotions = getattr(reply, "emotion_mapping", None)
        raw_relationships = getattr(reply, "emotion_relationship_mapping", None)

        descriptions: dict[str, str] = {}
        malformed_emotion_entries: list[str] = []
        source_entries = raw_emotions if raw_emotions else DEFAULT_EMOTION_MAPPING
        for raw_tag, raw_description in _mapping_items(source_entries):
            tag = normalize_emotion_tag(raw_tag)
            if not tag or not raw_description.strip():
                malformed_emotion_entries.append(str(raw_tag or ""))
                continue
            descriptions[tag] = raw_description.strip()

        relationship_overrides: dict[str, str] = {}
        invalid_relationship_entries: list[str] = []
        for raw_tag, raw_event in _mapping_items(raw_relationships):
            tag = normalize_emotion_tag(raw_tag)
            event_type = normalize_emotion_tag(raw_event)
            if not tag or tag not in descriptions or event_type not in VALID_RELATIONSHIP_EVENTS:
                invalid_relationship_entries.append(f"{raw_tag}:{raw_event}")
                continue
            relationship_overrides[tag] = event_type

        return cls(
            descriptions=descriptions,
            relationship_overrides=relationship_overrides,
            malformed_emotion_entries=tuple(malformed_emotion_entries),
            invalid_relationship_entries=tuple(invalid_relationship_entries),
        )

    @property
    def tags(self) -> frozenset[str]:
        return frozenset(self.descriptions)

    def contains(self, value: Any) -> bool:
        return normalize_emotion_tag(value) in self.descriptions

    def mapping_entries(self) -> list[str]:
        return [f"{tag}: {description}" for tag, description in self.descriptions.items()]

    def resolve_relationship_event(self, value: Any) -> EmotionRelationshipResolution:
        tag = normalize_emotion_tag(value)
        if tag in self.relationship_overrides:
            return EmotionRelationshipResolution(self.relationship_overrides[tag], "config")
        if tag in DEFAULT_RELATIONSHIP_EVENTS:
            return EmotionRelationshipResolution(DEFAULT_RELATIONSHIP_EVENTS[tag], "default")
        return EmotionRelationshipResolution("normal_chat", "fallback_normal_chat")


def build_emotion_tag_catalog(config: Any) -> EmotionTagCatalog:
    return EmotionTagCatalog.from_config(config)


def parse_emotion_mapping(value: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for raw_tag, raw_description in _mapping_items(value):
        tag = normalize_emotion_tag(raw_tag)
        description = str(raw_description or "").strip()
        if tag and description:
            result[tag] = description
    return result


__all__ = [
    "DEFAULT_EMOTION_MAPPING",
    "DEFAULT_RELATIONSHIP_EVENTS",
    "EmotionRelationshipResolution",
    "EmotionTagCatalog",
    "VALID_RELATIONSHIP_EVENTS",
    "build_emotion_tag_catalog",
    "normalize_emotion_tag",
    "parse_emotion_mapping",
]
