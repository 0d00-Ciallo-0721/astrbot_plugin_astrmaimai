from __future__ import annotations

from astrbot.api import logger

from ..presentation.events.error_interceptor import intercept_and_notify_errors
from ..presentation.events.message_entry import handle_global_message
from ..presentation.events.result_sniffer import sniff_external_plugin_results
from ..presentation.events.startup_hooks import on_program_start as run_startup_hook
from ..infrastructure.runtime.lane_manager import LaneKey
from ..shared.helpers.plugin_helpers import format_model_pool
from .lifecycle import PluginLifecycleManager
from .runtime_context import PluginRuntimeContext


class PluginFacade:
    def __init__(self, runtime: PluginRuntimeContext):
        self.runtime = runtime
        self.lifecycle_manager = PluginLifecycleManager(runtime)
        self.runtime.bind_system2_callback(self._system2_entry)
        try:
            from ..webui.backend.adapters.plugin_api import set_active_facade

            set_active_facade(self)
        except Exception:
            pass

    async def list_pending_expression_reviews(self, group_id: str = "", limit: int = 50):
        return await self.runtime.review_service.list_pending_reviews(group_id=group_id or None, limit=limit)

    async def get_expression_review_detail(self, pattern_id: int):
        return await self.runtime.review_service.get_review_detail(pattern_id)

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
        return await self.runtime.review_service.submit_review(
            pattern_id=pattern_id,
            decision=decision,
            reviewer_id=reviewer_id,
            replacement_expression=replacement_expression,
            style=style or None,
            reason=reason,
            weight_delta=weight_delta,
        )

    async def on_program_start(self) -> None:
        await run_startup_hook(self.runtime, self.lifecycle_manager)

    async def on_global_message(self, event):
        async for result in handle_global_message(self.runtime, self, event):
            yield result

    async def sniff_external_plugin_results(self, event) -> None:
        await sniff_external_plugin_results(self.runtime, event)

    async def intercept_and_notify_errors(self, event) -> None:
        await intercept_and_notify_errors(self.runtime, event)

    async def terminate(self) -> None:
        await self.lifecycle_manager.terminate()

    async def update_user_stats(self, user_id: str) -> None:
        await self.runtime.state_engine.increment_user_message_count(user_id)

    def get_runtime_diagnostics(self) -> dict:
        diagnostics = self.runtime.build_diagnostics()
        diagnostics["models"] = {
            "task_pool": list(getattr(self.runtime.config.provider, "task_models", []) or []),
            "agent_pool": list(getattr(self.runtime.config.provider, "agent_models", []) or []),
            "embedding_pool": list(getattr(self.runtime.config.provider, "embedding_models", []) or []),
            "fallback_pool": list(getattr(self.runtime.config.provider, "fallback_models", []) or []),
        }
        diagnostics["capabilities"] = self.get_capability_overview_sync()
        return diagnostics

    def get_capability_overview_sync(self) -> dict:
        return self.runtime.build_capability_overview_sync()

    async def get_capability_overview(self) -> dict:
        return await self.runtime.build_capability_overview()

    def build_help_text(self) -> str:
        diagnostics = self.get_runtime_diagnostics()
        status = diagnostics["status"]
        models = diagnostics["models"]

        task_str = format_model_pool(models["task_pool"])
        agent_str = format_model_pool(models["agent_pool"])
        emb_str = format_model_pool(models["embedding_pool"])
        fallback_models = models["fallback_pool"]
        fallback_str = f"({len(fallback_models)} models standby)" if fallback_models else "(No fallback)"
        runtime_state = "Running" if status["lifecycle_started"] else f"Booting: {status['boot_phase']}"
        memory_state = "Ready" if status["memory_initialized"] else "Deferred"
        proactive_state = "Running" if status["proactive_started"] else "Stopped"
        degraded = status["degraded_components"]
        degraded_text = ", ".join(sorted(degraded.keys())) if degraded else "None"

        return (
            "✨ **AstrMai (v1.0.0)**\n"
            "-----------------------\n"
            f"🧠 架构状态：{runtime_state}\n"
            f"🔲 Task Pool: {task_str}\n"
            f"🔲 Agent Pool: {agent_str}\n"
            f"🔲 Emb Pool: {emb_str}\n"
            f"🛟 Fallback: {fallback_str}\n"
            f"💾 Memory: {memory_state}\n"
            f"🌱 Proactive Life: {proactive_state}\n"
            f"⚠️ Degraded: {degraded_text}"
        )

    def is_framework_command(self, msg: str) -> bool:
        if not msg:
            return False

        clean_text = msg.replace("\u200b", "").strip()
        if not clean_text:
            return False

        prefixes = getattr(self.runtime.config.global_settings, "command_prefixes", [])
        if not prefixes:
            prefixes = ["/"]

        for prefix in prefixes:
            if clean_text.startswith(prefix):
                clean_text = clean_text[len(prefix):].strip()
                break
        else:
            if clean_text.startswith("/"):
                clean_text = clean_text[1:].strip()

        if not clean_text:
            return False

        clean_cmd = clean_text.split()[0].lower()
        registered_cmds = {"help", "plugin", "restart", "reload", "stop", "start", "list", "provider"}

        try:
            from astrbot.core.star.command_management import _collect_descriptors

            descriptors = _collect_descriptors(include_sub_commands=True)
            for desc in descriptors:
                if desc.effective_command:
                    registered_cmds.add(str(desc.effective_command).split()[0].lower())
                if getattr(desc, "aliases", None):
                    for alias in desc.aliases:
                        registered_cmds.add(str(alias).split()[0].lower())
        except Exception as exc:
            logger.debug(f"[AstrMai-Filter] 内存态穿透失败，尝试降级: {exc}")
            try:
                cmd_mgr = getattr(self.runtime.context, "command_manager", None)
                if cmd_mgr and hasattr(cmd_mgr, "commands"):
                    registered_cmds.update([str(key).lower() for key in cmd_mgr.commands.keys()])
            except Exception:
                pass

        try:
            extra_cmds = getattr(self.runtime.config.system1, "extra_command_list", [])
            if extra_cmds:
                registered_cmds.update([str(command).lower() for command in extra_cmds])
        except Exception:
            pass

        return clean_cmd in registered_cmds

    async def enter_sys3_direct(self, event):
        if not self.runtime.feature_flags.work_mode_enabled:
            yield event.plain_result("Sys3 work mode is disabled. Please enable it in WebUI first.")
            return

        task_query = event.message_str.replace("/work", "").strip()
        if not task_query:
            yield event.plain_result(
                "❌ 请告诉我需要执行什么任务。\n"
                "示例：/work 帮我定一个明天早上8点的开会提醒"
            )
            return

        chat_id = event.unified_msg_origin
        models = self.runtime.gateway.get_agent_models()
        if not models or models[0] == "Unconfigured":
            yield event.plain_result("Agent model is not configured, so the task cannot run.")
            return

        full_tools = await self.runtime.sys3_router.get_full_tools_for_direct_entry()
        event.set_extra("astrmai_is_self_reply", True)
        event.call_llm = True

        logger.info(f"[{chat_id}] [/work 直连] 进入 Sys3 纯任务模式：{task_query[:50]}...")
        try:
            result = await self.runtime.gateway.tool_chat_in_lane_result(
                lane_key=LaneKey(subsystem="sys3", task_family="direct", scope_id=chat_id),
                base_origin=chat_id,
                event=event,
                prompt=task_query,
                system_prompt=(
                    "You are a task execution specialist with strong tool-using ability.\n"
                    "When a task arrives, call the most suitable tools directly and avoid unnecessary narration.\n"
                    "After the task is complete, report the result clearly and concisely."
                ),
                tools=full_tools,
                models=models,
                max_steps=30,
                timeout=120,
                persona_id=getattr(self.runtime.config.persona, "persona_id", "") or "astrmai",
            )
            await self.runtime.reply_engine.handle_reply(event, result.text, chat_id)
        except Exception as exc:
            logger.error(f"[{chat_id}] /work 直连 Sys3 异常: {exc}")
            await self.runtime.reply_engine.handle_reply(
                event,
                f"任务执行中发生错误：{str(exc)[:100]}",
                chat_id,
            )

    async def _get_sys2_lock(self, chat_id: str):
        return await self.runtime.runtime_coordinator.get_sys2_lock(chat_id)

    async def _system2_entry(self, main_event, events_to_process: list | None = None):
        if self.runtime.system2_runner:
            return await self.runtime.system2_runner.run(main_event, events_to_process)

        chat_id = main_event.unified_msg_origin
        lock = await self._get_sys2_lock(chat_id)
        logger.debug(f"[{chat_id}] System 2 请求已登记，正在排队等待进入主执行队列...")

        async with lock:
            try:
                queue_events = events_to_process.copy() if isinstance(events_to_process, list) and events_to_process else [main_event]
                main_event.set_extra("astrmai_reply_sent", False)
                main_event.set_extra("astrmai_wait_targets", [])
                main_event.set_extra("astrmai_wait_target_name", "")

                await self.runtime.state_engine.consume_energy(chat_id)
                await self.runtime.lane_manager.ensure_lane(
                    lane_key=LaneKey(subsystem="sys2", task_family="dialog", scope_id=chat_id),
                    base_origin=chat_id,
                )
                await self.runtime.system2_planner.plan_and_execute(main_event, queue_events)
                reply_sent = bool(main_event.get_extra("astrmai_reply_sent", False))

                await self.runtime.runtime_coordinator.update_wait_targets(
                    chat_id,
                    list(main_event.get_extra("astrmai_wait_targets", []) or []),
                    str(main_event.get_extra("astrmai_wait_target_name", "") or ""),
                )

                is_private = main_event.get_extra("is_private_chat", False)
                if reply_sent and is_private and self.runtime.private_chat_manager:
                    sender_id = str(main_event.get_sender_id())
                    has_reply = await self.runtime.private_chat_manager.wait_for_new_message(sender_id)
                    if not has_reply:
                        logger.info(f"[{chat_id}] 私聊用户长时间未回复，会话已自然休眠。")
                elif reply_sent and main_event.get_group_id() and self.runtime.group_reply_wait_manager:
                    self.runtime.group_reply_wait_manager.register_from_reply_event(main_event)
            finally:
                logger.debug(f"[AstrMai] System2 execution finished safely for {chat_id}.")
