from __future__ import annotations


class AstrMaiError(Exception):
    """Base exception for AstrMai refactor-side helpers."""


class AstrMaiConfigurationError(AstrMaiError):
    """Raised when local configuration cannot be interpreted safely."""


class AstrMaiRuntimeError(AstrMaiError):
    """Raised for runtime helper failures inside the refactor package."""


__all__ = ["AstrMaiConfigurationError", "AstrMaiError", "AstrMaiRuntimeError"]
