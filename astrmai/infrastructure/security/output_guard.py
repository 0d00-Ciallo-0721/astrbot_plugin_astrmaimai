"""Output guard — re-export from gateway/output_guard.py for unified security entry.

Original implementation lives in `astrmai/infrastructure/gateway/output_guard.py`.
This module provides a stable re-export surface so callers do not depend on
the internal gateway layout.
"""

from ..gateway.output_guard import (  # noqa: F401
    validate_visible_output_text,
    looks_like_provider_failure_text,
    looks_like_prompt_scaffold_text,
    looks_like_tool_protocol_text,
    is_safe_visible_text,
    is_sendable_segment,
    sanitize_visible_reply_text,
)

__all__ = [
    "validate_visible_output_text",
    "looks_like_provider_failure_text",
    "looks_like_prompt_scaffold_text",
    "looks_like_tool_protocol_text",
    "is_safe_visible_text",
    "is_sendable_segment",
    "sanitize_visible_reply_text",
]
