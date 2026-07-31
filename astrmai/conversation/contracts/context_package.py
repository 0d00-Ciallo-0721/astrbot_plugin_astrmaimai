from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from typing import Any, Iterable, Mapping


_PROMPT_TAG_RE = re.compile(
    r"<\s*/?\s*(?:system|assistant|developer|tool|persona|trusted_system|"
    r"turn_instruction|user_input|retrieved_memory|untrusted_context)"
    r"(?:\s+[^>]*)?>",
    re.IGNORECASE,
)
_PROMPT_LABEL_RE = re.compile(
    r"\[(?:系统指令|系统消息|开发者指令|assistant|developer|system|tool)\]",
    re.IGNORECASE,
)
_PROMPT_DIVIDER_RE = re.compile(
    r"(?m)^\s*---+\s*(?:system|assistant|developer|tool|系统|指令)[^-]*---+\s*$",
    re.IGNORECASE,
)


def escape_untrusted_text(value: Any) -> str:
    text = str(value or "")
    text = _PROMPT_TAG_RE.sub(
        lambda match: f"[escaped:{match.group(0).strip('<> /').split()[0].lower()}]",
        text,
    )
    text = _PROMPT_LABEL_RE.sub("[escaped:instruction-label]", text)
    return _PROMPT_DIVIDER_RE.sub("[escaped:instruction-divider]", text)


def _ordered_unique(values: Iterable[Any]) -> tuple[str, ...]:
    result: list[str] = []
    for value in values:
        normalized = str(value or "").strip()
        if normalized and normalized not in result:
            result.append(normalized)
    return tuple(result)


def _safe_attribute(value: Any) -> str:
    return (
        str(value or "")
        .replace("&", "&amp;")
        .replace('"', "&quot;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )


@dataclass(frozen=True, slots=True)
class ContextBlock:
    block_type: str
    source: str
    provenance: str
    trusted: bool
    source_event_ids: tuple[str, ...]
    content: str
    content_hash: str
    char_count: int
    truncated: bool = False
    truncation_reason: str = ""
    metadata: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        block_type: str,
        source: str,
        content: Any,
        trusted: bool,
        provenance: str = "",
        source_event_ids: Iterable[Any] = (),
        char_budget: int = 0,
        truncation_reason: str = "char_budget",
        metadata: Mapping[str, Any] | None = None,
    ) -> "ContextBlock":
        normalized = str(content or "").strip()
        truncated = False
        limit = max(0, int(char_budget or 0))
        if limit and len(normalized) > limit:
            normalized = normalized[: max(1, limit - 3)].rstrip() + "..."
            truncated = True
        digest = hashlib.sha256(normalized.encode("utf-8")).hexdigest()
        return cls(
            block_type=str(block_type or "unknown").strip() or "unknown",
            source=str(source or "unknown").strip() or "unknown",
            provenance=str(provenance or source or "unknown").strip() or "unknown",
            trusted=bool(trusted),
            source_event_ids=_ordered_unique(source_event_ids),
            content=normalized,
            content_hash=digest,
            char_count=len(normalized),
            truncated=truncated,
            truncation_reason=truncation_reason if truncated else "",
            metadata=dict(metadata or {}),
        )

    def render(self) -> str:
        if not self.content:
            return ""
        event_ids = ",".join(self.source_event_ids)
        attributes = (
            f'type="{_safe_attribute(self.block_type)}" '
            f'source="{_safe_attribute(self.source)}" '
            f'provenance="{_safe_attribute(self.provenance)}" '
            f'trusted="{str(self.trusted).lower()}" '
            f'source_event_ids="{_safe_attribute(event_ids)}" '
            f'content_hash="{self.content_hash}" '
            f'char_count="{self.char_count}"'
        )
        if self.trusted:
            return f"<trusted_context {attributes}>\n{self.content}\n</trusted_context>"
        return (
            f"<untrusted_context {attributes}>\n"
            f"{escape_untrusted_text(self.content)}\n"
            "</untrusted_context>"
        )

    def as_dict(self) -> dict[str, Any]:
        return {
            "block_type": self.block_type,
            "source": self.source,
            "provenance": self.provenance,
            "trusted": self.trusted,
            "source_event_ids": list(self.source_event_ids),
            "content_hash": self.content_hash,
            "char_count": self.char_count,
            "truncated": self.truncated,
            "truncation_reason": self.truncation_reason,
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class ContextPackage:
    blocks: tuple[ContextBlock, ...] = field(default_factory=tuple)
    shared_event_ids: tuple[str, ...] = field(default_factory=tuple)
    owned_event_ids: tuple[str, ...] = field(default_factory=tuple)
    deduplicated_event_ids: tuple[str, ...] = field(default_factory=tuple)

    @property
    def package_hash(self) -> str:
        return hashlib.sha256(self.render().encode("utf-8")).hexdigest()

    @property
    def stats(self) -> dict[str, Any]:
        return {
            "context_blocks": [block.as_dict() for block in self.blocks],
            "shared_event_count": len(self.shared_event_ids),
            "owned_event_count": len(self.owned_event_ids),
            "deduplicated_event_count": len(self.deduplicated_event_ids),
            "untrusted_block_count": sum(1 for block in self.blocks if not block.trusted),
            "external_context_sources": sorted(
                {
                    block.source
                    for block in self.blocks
                    if block.provenance == "external_plugin"
                }
            ),
            "context_chars_after": sum(block.char_count for block in self.blocks),
        }

    def render(self) -> str:
        return "\n\n".join(
            rendered
            for rendered in (block.render() for block in self.blocks)
            if rendered
        )


__all__ = ["ContextBlock", "ContextPackage", "escape_untrusted_text"]
