"""RuntimeFacadeProtocol — contract that PluginFacade implements.

All consumers (PluginApiAdapter, presentation layer, tests) should depend on
this Protocol rather than the concrete PluginFacade class.

Defined as a typing.Protocol so structural subtyping works without explicit
inheritance, but PluginFacade explicitly inherits for documentation clarity.
"""

from __future__ import annotations

from typing import Any, AsyncIterator, Protocol, runtime_checkable


# ponytail: @runtime_checkable adds isinstance() overhead. PluginFacade explicitly
# inherits, so structural matching isn't needed. Remove when no caller uses
# isinstance(obj, RuntimeFacadeProtocol).
@runtime_checkable
class RuntimeFacadeProtocol(Protocol):
    """Public contract of the runtime facade consumed by PluginApiAdapter,
    presentation events, and architecture tests.

    PluginFacade implements this Protocol.
    """

    # ── lifecycle ──

    async def on_program_start(self) -> None:
        """Hook invoked once after the plugin boots."""
        ...

    async def on_global_message(self, event: Any) -> AsyncIterator[Any]:
        """Entry-point for every incoming chat message (group or private)."""
        ...

    async def sniff_external_plugin_results(self, event: Any) -> None:
        """Inspect results produced by other host plugins on this event."""
        ...

    async def intercept_and_notify_errors(self, event: Any) -> None:
        """Catch + report errors that the host framework fired on this event."""
        ...

    async def terminate(self) -> None:
        """Graceful shutdown: cancel tasks, flush state, release resources."""
        ...

    def apply_hot_config(self, config_dict: dict[str, Any], parsed_config: Any) -> bool:
        """Apply validated config to the live runtime."""
        ...

    # ── http / review bridge ──

    async def list_pending_expression_reviews(self, group_id: str = "", limit: int = 50) -> Any:
        ...

    async def list_recent_expression_reviews(self, group_id: str = "", limit: int = 50) -> Any:
        ...

    async def get_expression_review_detail(self, pattern_id: str) -> Any:
        ...

    async def submit_expression_review(
        self,
        pattern_id: str,
        decision: str,
        reviewer_id: str,
        replacement_expression: str = "",
        style: str = "",
        reason: str = "",
        weight_delta: float = 0.0,
    ) -> Any:
        ...

    # ── diagnostics / capabilities ──

    def get_runtime_diagnostics(self) -> dict[str, Any]:
        ...

    def get_capability_overview_sync(self) -> dict[str, Any]:
        ...

    async def get_capability_overview(self) -> dict[str, Any]:
        ...

    def build_help_text(self) -> str:
        ...

    def get_planner(self) -> Any:
        ...

    def get_gateway(self) -> Any:
        ...

    def get_proactive_task(self) -> Any:
        ...

    def get_observability_hub(self) -> Any:
        ...

    def get_memory_engine(self) -> Any:
        ...

    def get_runtime_coordinator(self) -> Any:
        ...

    def get_conversation_concurrency_flags(self) -> Any:
        ...

    def get_reflector(self) -> Any:
        ...

    def get_runtime_config(self) -> Any:
        ...

    def get_persona_summarizer(self) -> Any:
        ...

    def get_persistence(self) -> Any:
        ...

    def get_state_engine(self) -> Any:
        ...

    def get_auto_check_task(self) -> Any:
        ...

    def get_reflect_tracker(self) -> Any:
        ...

    def get_chat_loop_kernel(self) -> Any:
        ...

    def get_heartflow_manager(self) -> Any:
        ...

    def get_heartflow_topic_digest_service(self) -> Any:
        ...

    def get_v2_store(self) -> Any:
        ...

    def get_memory_observer(self) -> Any:
        ...

    def get_memory_pipeline(self) -> Any:
        ...

    def get_maintenance_service(self) -> Any:
        ...

    def get_migration_service(self) -> Any:
        ...

    def get_index_projector(self) -> Any:
        ...

    def get_write_service(self) -> Any:
        ...

    def get_session_summarizer(self) -> Any:
        ...

    def get_instant_gate(self) -> Any:
        ...

    def candidate_to_dict(self, candidate: Any) -> dict[str, Any]:
        ...

    def format_timeline_item(self, item: Any) -> Any:
        ...

    def get_expression_pattern_service(self) -> Any:
        ...

    # ── ingress helpers (consumed by presentation/message_entry) ──

    async def handle_poke(self, event: Any) -> Any:
        """Normalise poke events; returns IngressDecision."""
        ...

    def check_command_access(self, event: Any) -> Any:
        """Check command permission via permission_guard; returns IngressDecision."""
        ...

    def check_message_scope_access(self, scope: Any) -> Any:
        """Check whether a message scope is allowed; returns IngressDecision."""
        ...

    async def prepare_conversation_turn(self, event: Any, scope: Any) -> None:
        ...

    async def handle_group_reply_wait(self, event: Any, scope: Any) -> str:
        ...

    def is_debug_mode(self) -> bool:
        ...

    def is_runtime_ready(self) -> bool:
        ...

    def get_runtime_startup_message(self) -> str:
        ...

    def track_incoming_user_activity(self, user_id: str) -> None:
        ...

    async def try_consume_reflect_feedback(self, event: Any) -> Any:
        ...

    async def record_and_dispatch_attention(self, event: Any, scope: Any) -> str:
        ...

    def cancel_group_wait_if_interrupted(self, event: Any, group_wait_result: str, status: str) -> None:
        ...

    def suppress_default_llm_if_engaged(self, event: Any, status: str, is_direct_call: bool) -> Any:
        ...

    def is_framework_command(self, msg: str) -> bool:
        ...

    # ── system2 / sys3 entry ──

    async def enter_sys3_direct(self, event: Any) -> AsyncIterator[Any]:
        """Execute a /work command via Sys3 direct entry.

        This is an async generator — use ``async for`` to consume,
        NOT ``await`` (which would silently return an unconsumed generator).
        """
        ...

    async def update_user_stats(self, user_id: str) -> None:
        ...


__all__ = ["RuntimeFacadeProtocol"]
