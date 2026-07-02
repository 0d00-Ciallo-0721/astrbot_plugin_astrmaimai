"""Lightweight token estimator — zero-dependency character-based approximation.

For accurate token counting, consider enabling tiktoken (optional dependency).
"""

import re


def estimate_tokens(text: str) -> int:
    """Estimate token count from text using character-based heuristics.

    Chinese characters ~1.5 tokens each, ASCII ~0.3 tokens each.
    Returns at least 1 for non-empty input.
    """
    if not text:
        return 0
    chinese_chars = len(re.findall(r"[\u4e00-\u9fff]", text))
    other_chars = len(text) - chinese_chars
    return max(1, int(chinese_chars * 1.5 + other_chars * 0.3))


__all__ = ["estimate_tokens"]
