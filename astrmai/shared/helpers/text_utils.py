from __future__ import annotations

from typing import Any


def normalize_text(value: Any, default: str = "") -> str:
    if value is None:
        return default
    return str(value).strip()


def non_empty_text(value: Any) -> bool:
    return bool(normalize_text(value))


__all__ = ["non_empty_text", "normalize_text"]
