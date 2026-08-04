from __future__ import annotations

import hashlib
import re
import unicodedata


GLOBAL_JARGON_SESSION_ID = "__global_jargon__"


def _normalize(value: str, *, compact: bool) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip().lower()
    text = text.replace("～", "~")
    text = re.sub(r"[\u200b-\u200d\ufeff]", "", text)
    if compact:
        text = re.sub(r"\s+", "", text)
    else:
        text = " ".join(text.split())
    return text


def normalize_expression_text(value: str) -> str:
    return _normalize(value, compact=True)


def normalize_situation(value: str) -> str:
    return _normalize(value, compact=False)


def normalize_jargon_term(value: str) -> str:
    return _normalize(value, compact=True)


def normalize_jargon_meaning(value: str) -> str:
    return _normalize(value, compact=False)


def _digest(parts: tuple[str, ...]) -> str:
    return hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()[:24]


def expression_fingerprint(
    group_id: str,
    habit_type: str,
    pattern: str,
    situation: str,
) -> str:
    return "expression:" + _digest(
        (
            str(group_id or "").strip(),
            _normalize(habit_type, compact=True),
            normalize_expression_text(pattern),
        )
    )


def jargon_fingerprint(term: str, meaning: str = "") -> str:
    # A jargon term has one global identity. Meanings can be corrected or
    # enriched later without creating a second canonical record.
    return "jargon:" + _digest((normalize_jargon_term(term),))


__all__ = [
    "GLOBAL_JARGON_SESSION_ID",
    "expression_fingerprint",
    "jargon_fingerprint",
    "normalize_expression_text",
    "normalize_jargon_meaning",
    "normalize_jargon_term",
    "normalize_situation",
]
