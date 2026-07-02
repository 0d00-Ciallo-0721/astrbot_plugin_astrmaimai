"""Security helpers — unified entry point.

Sub-modules:
- input_sanitizer: InputSanitizer — prompt injection defense
- output_guard: validate_visible_output_text — LLM output safety
- rate_limiter: TokenBucket — lightweight rate limiting
"""

from .input_sanitizer import InputSanitizer  # noqa: F401
from .output_guard import (  # noqa: F401
    validate_visible_output_text,
    looks_like_provider_failure_text,
    looks_like_prompt_scaffold_text,
    looks_like_tool_protocol_text,
    is_safe_visible_text,
    is_sendable_segment,
    sanitize_visible_reply_text,
)
from .rate_limiter import TokenBucket  # noqa: F401

__all__ = [
    "InputSanitizer",
    "TokenBucket",
    "validate_visible_output_text",
    "looks_like_provider_failure_text",
    "looks_like_prompt_scaffold_text",
    "looks_like_tool_protocol_text",
    "is_safe_visible_text",
    "is_sendable_segment",
    "sanitize_visible_reply_text",
]
