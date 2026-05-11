"""Presentation-layer package for hooks, commands, and DTOs."""

from .commands import (
    AdminCommandRequest,
    ReviewDecisionRequest,
    WorkCommandRequest,
    build_admin_request,
    build_admin_snapshot,
    build_help_view,
    get_review_detail,
    handle_mai_help,
    handle_work_mode,
    list_pending_reviews,
    parse_work_command,
    submit_review,
)
from .dto import HelpCommandView, IngressDecision, MessageScope

__all__ = [
    "AdminCommandRequest",
    "HelpCommandView",
    "IngressDecision",
    "MessageScope",
    "ReviewDecisionRequest",
    "WorkCommandRequest",
    "build_admin_request",
    "build_admin_snapshot",
    "build_help_view",
    "get_review_detail",
    "handle_mai_help",
    "handle_work_mode",
    "list_pending_reviews",
    "parse_work_command",
    "submit_review",
]
