from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(slots=True)
class TopicUnit:
    slot: str
    text: str
    event_ids: list[str] = field(default_factory=list)


@dataclass(slots=True)
class ColdSummaryStructure:
    topics: list[TopicUnit] = field(default_factory=list)
    decisions: list[TopicUnit] = field(default_factory=list)
    open_items: list[TopicUnit] = field(default_factory=list)
    relationship_changes: list[TopicUnit] = field(default_factory=list)
    emotional_turns: list[TopicUnit] = field(default_factory=list)
    visual_notes: list[TopicUnit] = field(default_factory=list)
    long_term_constraints: list[TopicUnit] = field(default_factory=list)

    def section_counts(self) -> dict[str, int]:
        return {
            "topics": len(self.topics),
            "decisions": len(self.decisions),
            "open_items": len(self.open_items),
            "relationship_changes": len(self.relationship_changes),
            "emotional_turns": len(self.emotional_turns),
            "visual_notes": len(self.visual_notes),
            "long_term_constraints": len(self.long_term_constraints),
        }


SECTION_ORDER = (
    "topics",
    "decisions",
    "open_items",
    "relationship_changes",
    "emotional_turns",
    "visual_notes",
    "long_term_constraints",
)


__all__ = ["ColdSummaryStructure", "SECTION_ORDER", "TopicUnit"]
