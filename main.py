from __future__ import annotations

import asyncio
import hashlib
from typing import Any

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

try:
    from astrbot.api.provider import LLMResponse
except ImportError:  # pragma: no cover - older stubs/runtime compatibility
    LLMResponse = Any

try:
    from .config import AstrMaiConfig
    from .astrmai.app import PluginFacade, build_runtime_context, export_legacy_attrs
    from .astrmai.presentation.commands import handle_mai_help, handle_work_mode
    from .astrmai.infrastructure.runtime.reverse_session import maybe_attach_reverse_session_block
    from .astrmai.webui.plugin_pages import register_astrmai_admin_pages
except ImportError:
    if __package__:
        raise
    from config import AstrMaiConfig
    from astrmai.app import PluginFacade, build_runtime_context, export_legacy_attrs
    from astrmai.presentation.commands import handle_mai_help, handle_work_mode
    from astrmai.infrastructure.runtime.reverse_session import maybe_attach_reverse_session_block
    from astrmai.webui.plugin_pages import register_astrmai_admin_pages


def _optional_filter_hook(name: str):
    hook = getattr(filter, name, None)
    if callable(hook):
        return hook()

    def _identity(func):
        logger.debug(f"[AstrMai] filter.{name} is unavailable; lifecycle visibility hook disabled")
        return func

    return _identity


@register(
    "astrmai",
    "Gemini Antigravity",
    "AstrMai: Dual-Process Architecture Plugin",
    "1.0.0",
    "https://github.com/0d00-Ciallo-0721/astrbot_plugin_astrmaimai",
)
class AstrMaiPlugin(Star):
    # ponytail: architectural debt — no unified session_waiter abstraction.
    # GroupReplyWaitManager and PrivateChatManager are independent implementations.
    # Also: persona hot-switch does not clean context from previous persona.
    # Also: no MCP connection management — MCPTool instances are created ad-hoc
    # per SubAgent; add centralized MCPClient lifecycle (connect/disconnect/health)
    # when multiple agents share MCP servers.
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.raw_config = config or {}
        self.config = AstrMaiConfig(**self.raw_config)
        self.runtime = build_runtime_context(context, self.config, self.raw_config)
        self.facade = PluginFacade(self.runtime)
        self.runtime.bind_host_plugin(self)
        self._apply_runtime_compat()
        # ponytail: Plugin Pages API changed in AstrBot v4.26.0 (Quart→FastAPI). Verify admin page works after upgrade.
        register_astrmai_admin_pages(context, self.facade)
        logger.info("[AstrMai] ✅ 入口层重构骨架已装配，当前业务能力仍由原始领域模块承接。")

    def _apply_runtime_compat(self) -> None:
        for name, value in export_legacy_attrs(self.runtime).items():
            setattr(self, name, value)

    async def list_pending_expression_reviews(self, group_id: str = "", limit: int = 50):
        return await self.facade.list_pending_expression_reviews(group_id=group_id, limit=limit)

    async def get_expression_review_detail(self, pattern_id: str):
        return await self.facade.get_expression_review_detail(pattern_id)

    async def submit_expression_review(
        self,
        pattern_id: str,
        decision: str,
        reviewer_id: str,
        replacement_expression: str = "",
        style: str = "",
        reason: str = "",
        weight_delta: float = 0.0,
    ):
        return await self.facade.submit_expression_review(
            pattern_id=pattern_id,
            decision=decision,
            reviewer_id=reviewer_id,
            replacement_expression=replacement_expression,
            style=style,
            reason=reason,
            weight_delta=weight_delta,
        )

    async def initialize(self) -> None:
        """Start the runtime whenever AstrBot activates or hot-reloads the plugin."""
        await self._ensure_runtime_started("plugin_initialize")

    async def _ensure_runtime_started(self, source: str) -> None:
        logger.info(f"[AstrMai] runtime startup requested source={source}")
        await self.facade.on_program_start()

    @filter.on_astrbot_loaded()
    async def on_program_start(self):
        await self._ensure_runtime_started("astrbot_loaded")

    # ponytail: on_llm_request runs BEFORE persona injection. Reverse session block may be overwritten.
    @filter.on_llm_request()
    async def inject_gemini_reverse_session(self, event: AstrMessageEvent, request):
        """Inject a stable reverse-session sentinel for Gemini reverse providers."""
        try:
            try:
                provider = self.context.get_using_provider(event.unified_msg_origin)
            except Exception:
                provider = None

            sp = getattr(request, "system_prompt", "") or ""
            # ponytail: only modify system_prompt when necessary to avoid breaking provider prefix caching.
            # AstrBot Skill §7.1.1: system_prompt should only carry long-lived stable content.
            # Reassigning it on every on_llm_request invalidates the deterministic cache hash.
            needs_reverse_block = provider is not None and "<astrbot_reverse_session>" not in sp
            if needs_reverse_block:
                request.system_prompt = maybe_attach_reverse_session_block(
                    sp,
                    provider,
                    session_id=str(getattr(request, "session_id", "") or event.unified_msg_origin),
                    session_scope=str(event.unified_msg_origin),
                    parent_session_id="",
                    session_kind="astrbot_native",
                    source="astrbot",
                )
            if hasattr(event, "set_extra"):
                normalized_system_prompt = str(getattr(request, "system_prompt", "") or "")
                post_hook_hash = hashlib.sha256(normalized_system_prompt.encode("utf-8")).hexdigest()[:16] if normalized_system_prompt else ""
                event.set_extra("astrmai_post_hook_system_hash", post_hook_hash)
                existing_trace = event.get_extra("astrmai_request_trace", {}) if hasattr(event, "get_extra") else {}
                if not isinstance(existing_trace, dict):
                    existing_trace = {}
                existing_trace.update(
                    {
                        "provider_visible_system_hash": post_hook_hash or str(existing_trace.get("provider_visible_system_hash", "") or ""),
                        "post_hook_system_hash": post_hook_hash,
                        "request_session_id": str(getattr(request, "session_id", "") or existing_trace.get("request_session_id", "") or ""),
                        "request_cache_control": str(existing_trace.get("request_cache_control", "") or ""),
                    }
                )
                event.set_extra("astrmai_request_trace", existing_trace)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("[AstrMai] inject_gemini_reverse_session failed, allowing LLM request to proceed without modification")

    @_optional_filter_hook("on_llm_response")
    async def on_llm_response(self, event: AstrMessageEvent, response: LLMResponse):
        try:
            chat_id = event.unified_msg_origin
            text_preview = str(response.completion_text or "")[:200]
            logger.debug(f"[AstrMai] LLM response for {chat_id}: {text_preview}")
        except Exception:
            logger.debug("[AstrMai] on_llm_response hook failed", exc_info=True)

    # ponytail: stub hooks for agent lifecycle visibility — no logic, debug logging only
    @_optional_filter_hook("on_agent_begin")
    async def on_agent_begin(self, event: AstrMessageEvent, run_context=None):
        try:
            logger.debug(f"[AstrMai] Agent begin for {event.unified_msg_origin}")
        except Exception:
            logger.debug("[AstrMai] on_agent_begin hook failed", exc_info=True)

    @_optional_filter_hook("on_agent_done")
    async def on_agent_done(self, event: AstrMessageEvent, run_context=None, resp=None):
        try:
            logger.debug(f"[AstrMai] Agent done for {event.unified_msg_origin}")
        except Exception:
            logger.debug("[AstrMai] on_agent_done hook failed", exc_info=True)

    @filter.command("mai")
    async def mai_help(self, event: AstrMessageEvent):
        if self.facade.check_command_access(event).should_stop:
            event.stop_event()
            return
        async for result in handle_mai_help(self.facade, event):
            yield result

    # ponytail: on_decorating_result is SKIPPED when streaming output is enabled (AstrBot >= v4.23.5).
    # sniff_external_plugin_results and intercept_and_notify_errors will silently not fire during streaming.
    @filter.on_decorating_result()
    async def sniff_external_plugin_results(self, event: AstrMessageEvent):
        try:
            await self.facade.sniff_external_plugin_results(event)
        except Exception:
            logger.exception("[AstrMai] sniff_external_plugin_results hook failed")

    # ponytail: priority=10 is lower than many plugins. Lower-priority plugins
    # that return content before on_global_message fires will silently suppress
    # AstrMai. Consider priority=1 if suppression is observed in the field.
    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def on_group_membership_notice(self, event: AstrMessageEvent, *args, **kwargs):
        try:
            if await self.facade.handle_group_membership_notice(event):
                event.stop_event()
        except Exception:
            logger.exception("[AstrMai] group membership notice hook failed")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_global_message(self, event: AstrMessageEvent, *args, **kwargs):
        if event.get_extra("heartflow_is_command"):
            return
        async for result in self.facade.on_global_message(event):
            yield result

    @filter.on_decorating_result(priority=90)
    async def intercept_and_notify_errors(self, event: AstrMessageEvent):
        try:
            await self.facade.intercept_and_notify_errors(event)
        except Exception:
            logger.exception("[AstrMai] intercept_and_notify_errors hook failed")

    @filter.command("work")
    async def enter_sys3_direct(self, event: AstrMessageEvent):
        if self.facade.check_command_access(event).should_stop:
            event.stop_event()
            return
        async for result in handle_work_mode(self.facade, event):
            yield result

    async def terminate(self):
        await self.facade.terminate()
