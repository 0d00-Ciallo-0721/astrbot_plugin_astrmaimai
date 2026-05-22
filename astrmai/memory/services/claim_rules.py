from __future__ import annotations

import re


ASCII_CORRECTION_HINTS = ("wrong", "correct", "before", "now", "fix")
ASCII_SHORT_TERM_HINTS = ("today", "recently", "this week", "anx", "anxious", "mood")

SERVER_COUNT_PATTERN = re.compile(r"(\d+)")

SERVER_KEYWORDS = ("server", "servers")
ANXIETY_KEYWORDS = ("anx", "anxious", "mood")


__all__ = [
    "ASCII_CORRECTION_HINTS",
    "ASCII_SHORT_TERM_HINTS",
    "SERVER_COUNT_PATTERN",
    "SERVER_KEYWORDS",
    "ANXIETY_KEYWORDS",
]
