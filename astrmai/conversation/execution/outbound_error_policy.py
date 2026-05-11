from __future__ import annotations

from astrbot.api import logger

from ...infrastructure.runtime.trace_runtime import debug_trace, preview_text
from ...shared.helpers.plugin_helpers import extract_result_text


async def intercept_outbound_error(runtime, event) -> None:
    result = event.get_result()
    if not result:
        return

    message_str = extract_result_text(result)
    if not message_str:
        return

    debug_trace(event, "execution.outbound.inspect", preview=preview_text(message_str, 100))

    if runtime.host_bridge.is_ghost_sentinel(message_str):
        logger.debug("[AstrMai-Phantom] ghost placeholder intercepted and dropped silently.")
        event.set_result(None)
        debug_trace(event, "execution.outbound.ghost_drop")
        return

    enabled = getattr(runtime.config.global_settings, "enable_error_interception", True)
    if not enabled:
        return

    if not runtime.host_bridge.should_intercept_error(message_str, enabled=enabled):
        return

    logger.warning(f"[AstrMai-ErrorGuard] 拦截到系统报错，已阻止下发: {message_str[:50]}...")
    event.set_result(None)
    event.stop_event()
    debug_trace(event, "execution.outbound.error_intercepted", preview=preview_text(message_str, 100))

    alert_msg = runtime.host_bridge.build_admin_alert(event, message_str)
    admin_ids = getattr(runtime.config.global_settings, "admin_ids", [])
    client = getattr(event, "bot", None)
    if client and hasattr(client, "api"):
        for admin_id in runtime.host_bridge.admin_targets(admin_ids):
            if not str(admin_id).isdigit():
                continue
            try:
                await client.api.call_action("send_private_msg", user_id=int(admin_id), message=alert_msg)
            except Exception as exc:
                logger.error(f"[AstrMai-ErrorGuard] 无法向管理员 {admin_id} 推送告警: {exc}")
