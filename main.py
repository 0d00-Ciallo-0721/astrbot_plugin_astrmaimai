from __future__ import annotations

import hashlib

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star, register

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


@register(
    "astrmai",
    "Gemini Antigravity",
    "AstrMai: Dual-Process Architecture Plugin",
    "1.0.0",
    "https://github.com/0d00-Ciallo-0721/astrbot_plugin_astrmaimai",
)
class AstrMaiPlugin(Star):
    def __init__(self, context: Context, config: dict | None = None):
        super().__init__(context)
        self.raw_config = config or {}
        self.config = AstrMaiConfig(**self.raw_config)
        self.runtime = build_runtime_context(context, self.config, self.raw_config)
        self.facade = PluginFacade(self.runtime)
        self.runtime.bind_host_plugin(self)
        self._apply_runtime_compat()
        register_astrmai_admin_pages(context, self.facade)
        logger.info("[AstrMai] ✅ 入口层重构骨架已装配，当前业务能力仍由原始领域模块承接。")

    def _apply_runtime_compat(self) -> None:
        for name, value in export_legacy_attrs(self.runtime).items():
            setattr(self, name, value)

    async def list_pending_expression_reviews(self, group_id: str = "", limit: int = 50):
        return await self.facade.list_pending_expression_reviews(group_id=group_id, limit=limit)

    async def get_expression_review_detail(self, pattern_id: int):
        return await self.facade.get_expression_review_detail(pattern_id)

    async def submit_expression_review(
        self,
        pattern_id: int,
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

    @filter.on_astrbot_loaded()
    async def on_program_start(self):
        await self.facade.on_program_start()

    @filter.on_llm_request()
    async def inject_gemini_reverse_session(self, event: AstrMessageEvent, request):
        """Inject a stable reverse-session sentinel for Gemini reverse providers."""
        try:
            provider = self.context.get_using_provider(event.unified_msg_origin)
        except Exception:
            provider = None

        request.system_prompt = maybe_attach_reverse_session_block(
            getattr(request, "system_prompt", "") or "",
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

    @filter.command("mai")
    async def mai_help(self, event: AstrMessageEvent):
        if self.facade.check_command_access(event).should_stop:
            return
        async for result in handle_mai_help(self.facade, event):
            yield result

    @filter.on_decorating_result()
    async def sniff_external_plugin_results(self, event: AstrMessageEvent):
        await self.facade.sniff_external_plugin_results(event)

    @filter.event_message_type(filter.EventMessageType.ALL, priority=10)
    async def on_global_message(self, event: AstrMessageEvent):
        async for result in self.facade.on_global_message(event):
            yield result

    @filter.on_decorating_result(priority=90)
    async def intercept_and_notify_errors(self, event: AstrMessageEvent):
        await self.facade.intercept_and_notify_errors(event)

    @filter.command("work")
    async def enter_sys3_direct(self, event: AstrMessageEvent):
        if self.facade.check_command_access(event).should_stop:
            return
        async for result in handle_work_mode(self.facade, event):
            yield result

    async def terminate(self):
        await self.facade.terminate()
