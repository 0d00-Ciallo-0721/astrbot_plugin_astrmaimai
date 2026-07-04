from __future__ import annotations

import threading

from astrbot.api import logger

from ..presentation.dto.message_scope import IngressDecision
from ..presentation.events.error_interceptor import intercept_and_notify_errors
from ..presentation.events.message_entry import handle_global_message
from ..presentation.events.result_sniffer import sniff_external_plugin_results
from ..presentation.events.startup_hooks import on_program_start as run_startup_hook
from ..infrastructure.runtime.lane_manager import LaneKey
from ..infrastructure.gateway.gateway_exceptions import LLMCascadeFailureException
from ..shared.helpers.plugin_helpers import format_model_pool
from .lifecycle import PluginLifecycleManager
from .runtime_context import PluginRuntimeContext
from .runtime_facade_protocol import RuntimeFacadeProtocol


class PluginFacade(RuntimeFacadeProtocol):
    def __init__(self, runtime: PluginRuntimeContext):
        self.runtime = runtime
        self.lifecycle_manager = PluginLifecycleManager(runtime)
        self._hot_config_lock = threading.RLock()
        self.runtime.bind_system2_callback(self._system2_entry)
        # ponytail: WebUI adapter registration failure is logged but silently
        # swallowed — admin pages work without it. Surface to user if audit page
        # reports 404 after boot.
        try:
            from ..webui.backend.adapters.plugin_api import set_active_facade

            set_active_facade(self)
        except Exception as exc:
            logger.warning(f"[AstrMai] Failed to register WebUI facade adapter: {exc}")

    async def list_pending_expression_reviews(self, group_id: str = "", limit: int = 50):
        return await self.runtime.review_service.list_pending_reviews(group_id=group_id or None, limit=limit)

    async def list_recent_expression_reviews(self, group_id: str = "", limit: int = 50):
        return await self.runtime.review_service.list_recent_reviews(group_id=group_id or None, limit=limit)

    async def get_expression_review_detail(self, pattern_id: str):
        return await self.runtime.review_service.get_review_detail(pattern_id)

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
        async for result in handle_global_message(self, event):
            yield result

    def _find_notice_payload(self, obj, depth: int = 0) -> dict:
        if depth > 4:
            return {}
        if isinstance(obj, dict):
            if obj.get("notice_type") == "group_decrease":
                return obj
            for value in obj.values():
                found = self._find_notice_payload(value, depth + 1)
                if found:
                    return found
        elif hasattr(obj, "__dict__"):
            for value in vars(obj).values():
                found = self._find_notice_payload(value, depth + 1)
                if found:
                    return found
        return {}

    def _group_decrease_notice_for_self(self, event) -> tuple[str, str] | None:
        payload = self._find_notice_payload(event)
        if not payload:
            return None
        group_id = str(payload.get("group_id") or "")
        user_id = str(payload.get("user_id") or "")
        self_id = str(payload.get("self_id") or "")
        if not self_id and hasattr(event, "get_self_id"):
            try:
                self_id = str(event.get_self_id() or "")
            except Exception:
                self_id = ""
        if not group_id or not user_id or not self_id or user_id != self_id:
            return None
        chat_id = str(getattr(event, "unified_msg_origin", "") or "").strip()
        if not chat_id:
            chat_id = f"default:GroupMessage:{group_id}"
        return chat_id, group_id

    async def handle_group_membership_notice(self, event) -> bool:
        resolved = self._group_decrease_notice_for_self(event)
        if not resolved:
            return False
        chat_id, group_id = resolved
        await self.clear_group_runtime_state(chat_id, group_id=group_id)
        logger.info(f"[AstrMai] bot left group {group_id}; cleared runtime state for {chat_id}")
        return True

    async def clear_group_runtime_state(self, chat_id: str, *, group_id: str = "") -> dict[str, bool]:
        result: dict[str, bool] = {}
        runtime = self.runtime
        components = [
            ("attention_gate", getattr(runtime, "attention_gate", None), "clear_chat_state", True),
            ("state_engine", getattr(runtime, "state_engine", None), "clear_chat_state", True),
            ("runtime_coordinator", getattr(runtime, "runtime_coordinator", None), "clear_runtime_state", True),
            ("dialogue_store", getattr(runtime, "dialogue_store", None), "clear_chat", True),
            ("chat_loop_kernel", getattr(runtime, "chat_loop_kernel", None), "clear_chat_state", True),
        ]
        proactive_task = getattr(runtime, "proactive_task", None)
        heartflow_manager = getattr(proactive_task, "heartflow_manager", None) if proactive_task else None
        components.append(("heartflow_manager", heartflow_manager, "clear_chat", False))
        for name, component, method_name, is_async in components:
            method = getattr(component, method_name, None) if component is not None else None
            if not callable(method):
                result[name] = False
                continue
            try:
                cleared = await method(chat_id) if is_async else method(chat_id)
                result[name] = bool(cleared)
            except Exception as exc:
                result[name] = False
                logger.debug(f"[AstrMai] runtime clear degraded for {name}/{chat_id}: {exc}", exc_info=True)
        group_wait = getattr(runtime, "group_reply_wait_manager", None)
        if group_wait is not None and hasattr(group_wait, "cancel_wait"):
            try:
                result["group_reply_wait"] = bool(group_wait.cancel_wait(chat_id, reason="bot_left_group"))
            except Exception as exc:
                result["group_reply_wait"] = False
                logger.debug(f"[AstrMai] group wait clear degraded for {chat_id}: {exc}", exc_info=True)
        return result

    async def sniff_external_plugin_results(self, event) -> None:
        await sniff_external_plugin_results(self.runtime, event)

    async def intercept_and_notify_errors(self, event) -> None:
        await intercept_and_notify_errors(self.runtime, event)

    async def terminate(self) -> None:
        await self.lifecycle_manager.terminate()
        if getattr(self.runtime, "persistence", None):
            self.runtime.persistence.dispose()
        if getattr(self.runtime, "runtime_coordinator", None):
            await self.runtime.runtime_coordinator.prune_inactive()

    async def update_user_stats(self, user_id: str) -> None:
        try:
            await self.runtime.state_engine.increment_user_message_count(user_id)
        except Exception as exc:
            logger.warning(f"[AstrMai] Failed to update user stats for {user_id}: {exc}")

    # ── config hot-apply ────────────────────────────────────────────

    def _get_hot_config_lock(self):
        lock = getattr(self, "_hot_config_lock", None)
        if lock is None:
            lock = threading.RLock()
            self._hot_config_lock = lock
        return lock

    def apply_hot_config(self, config_dict: dict, parsed_config) -> bool:
        with self._get_hot_config_lock():
            return self._apply_hot_config_locked(config_dict, parsed_config)

    def _apply_hot_config_locked(self, config_dict: dict, parsed_config) -> bool:
        """热应用配置到运行时。遍历所有组件刷新。"""
        old_raw_config = dict(getattr(self.runtime, "raw_config", {}) or {})
        old_config = getattr(self.runtime, "config", None)
        components = [
            ("gateway", getattr(self.runtime, "gateway", None)),
            ("lane_manager", getattr(self.runtime, "lane_manager", None)),
            ("state_engine", getattr(self.runtime, "state_engine", None)),
            ("sensors", getattr(self.runtime, "sensors", None)),
            ("frequency_controller", getattr(self.runtime, "frequency_controller", None)),
            ("private_chat_manager", getattr(self.runtime, "private_chat_manager", None)),
            ("attention_gate", getattr(self.runtime, "attention_gate", None)),
            ("reply_engine", getattr(self.runtime, "reply_engine", None)),
            ("evolution", getattr(self.runtime, "evolution", None)),
            ("memory_engine", getattr(self.runtime, "memory_engine", None)),
            ("judge", getattr(self.runtime, "judge", None)),
            ("proactive_task", getattr(self.runtime, "proactive_task", None)),
        ]

        def _apply_runtime(raw_config, config) -> None:
            self.runtime.raw_config = dict(raw_config)
            self.runtime.config = config
            if hasattr(self.runtime, "rebuild_infrastructure_settings"):
                self.runtime.rebuild_infrastructure_settings()

        def _refresh_components(config) -> None:
            for name, comp in components:
                if comp is not None and hasattr(comp, "refresh_config"):
                    comp.refresh_config(config)

        try:
            _apply_runtime(config_dict, parsed_config)
            _refresh_components(parsed_config)
            if hasattr(self.runtime, "sync_host_compat_attrs"):
                self.runtime.sync_host_compat_attrs()
        except Exception as exc:
            logger.warning(f"[AstrMai] hot-apply failed, rolling back: {exc}")
            rollback_errors = []
            try:
                _apply_runtime(old_raw_config, old_config)
            except Exception as rollback_exc:
                rollback_errors.append(f"runtime:{rollback_exc}")
            for name, comp in components:
                if comp is not None and hasattr(comp, "refresh_config"):
                    try:
                        comp.refresh_config(old_config)
                    except Exception as rollback_exc:
                        rollback_errors.append(f"{name}:{rollback_exc}")
            try:
                if hasattr(self.runtime, "sync_host_compat_attrs"):
                    self.runtime.sync_host_compat_attrs()
            except Exception as rollback_exc:
                rollback_errors.append(f"sync:{rollback_exc}")
            if rollback_errors:
                logger.error(f"[AstrMai] hot-apply rollback degraded: {rollback_errors}")
            return False
        return True

    # ── narrow-domain methods for ingress / message_entry ──

    def check_command_access(self, event) -> IngressDecision:
        from ..conversation.ingress.permission_guard import check_command_access as _check

        return _check(self.runtime, event)

    async def handle_poke(self, event) -> IngressDecision:
        from ..conversation.ingress.poke_handler import handle_poke_if_needed

        return await handle_poke_if_needed(self.runtime, event)

    def check_message_scope_access(self, scope) -> IngressDecision:
        from ..conversation.ingress.permission_guard import check_message_scope_access as _check

        return _check(self.runtime, scope)

    async def handle_group_reply_wait(self, event, scope) -> str:
        if not event.get_group_id() or not self.runtime.group_reply_wait_manager:
            return "NONE"
        group_wait_result = self.runtime.group_reply_wait_manager.handle_incoming_message(event)
        if getattr(self.runtime, "chat_loop_kernel", None) is not None:
            if group_wait_result == "RESUME":
                await self.runtime.chat_loop_kernel.resume_wait(
                    scope.chat_id,
                    "group_wait_resumed",
                    resume_target_id=scope.sender_id,
                    resume_source="group_reply_wait_manager",
                )
            elif group_wait_result == "EXPIRED":
                await self.runtime.chat_loop_kernel.expire_wait(scope.chat_id, "group_wait_expired")
            group_wait_info = self.runtime.group_reply_wait_manager.get_wait_info(scope.chat_id)
            if group_wait_info:
                await self.runtime.chat_loop_kernel.arm_group_wait(scope.chat_id, group_wait_info)
        return group_wait_result

    def is_debug_mode(self) -> bool:
        return getattr(self.runtime.config.global_settings, "debug_mode", False)

    def track_incoming_user_activity(self, user_id: str) -> None:
        if user_id and self.runtime.lifecycle.manager:
            self.runtime.lifecycle.manager.track_task(self.update_user_stats(user_id))

    async def try_consume_reflect_feedback(self, event):
        if self.runtime.reflect_tracker:
            return await self.runtime.reflect_tracker.try_consume_feedback(event)
        return None

    async def record_and_dispatch_attention(self, event, scope) -> str:
        if not getattr(scope, "is_anonymous_sender", False):
            await self.runtime.evolution.record_user_message(event)
        if getattr(self.runtime, "chat_loop_kernel", None) is not None:
            tick_result = await self.runtime.chat_loop_kernel.tick(
                chat_id=scope.chat_id,
                trigger="message",
                event=event,
            )
            return tick_result.dispatch_result
        return await self.runtime.attention_gate.process_event(event)

    def cancel_group_wait_if_interrupted(self, event, group_wait_result, status) -> None:
        if (
            event.get_group_id()
            and self.runtime.group_reply_wait_manager
            and group_wait_result != "RESUME"
            and status in {"ENGAGED", "BUFFERED"}
        ):
            self.runtime.group_reply_wait_manager.cancel_wait(
                event.unified_msg_origin,
                reason=f"interrupted_by_{status.lower()}",
            )

    def suppress_default_llm_if_engaged(self, event, status, is_direct_call):
        """ponytail: returns suppress result but does NOT call event.stop_event().
        Caller must call event.stop_event() when this returns non-None."""
        if status == "ENGAGED" or is_direct_call:
            return self.runtime.host_bridge.suppress_default_llm(event)
        return None

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

    def get_planner(self):
        # ponytail: defensive getattr on @property — masks init failures if property raises.
        return getattr(self.runtime, "system2_planner", None)

    def get_gateway(self):
        return getattr(self.runtime, "gateway", None)

    def get_proactive_task(self):
        return getattr(self.runtime, "proactive_task", None)

    def get_observability_hub(self):
        return getattr(self.runtime, "observability_hub", None)

    def get_memory_engine(self):
        return getattr(self.runtime, "memory_engine", None)

    def get_runtime_coordinator(self):
        return getattr(self.runtime, "runtime_coordinator", None)

    def get_reflector(self):
        return getattr(self.runtime, "reflector", None)

    def get_runtime_config(self):
        return getattr(self.runtime, "config", None)

    def get_persona_summarizer(self):
        return getattr(self.runtime, "persona_summarizer", None)

    def get_state_engine(self):
        return getattr(self.runtime, "state_engine", None)

    def get_auto_check_task(self):
        return getattr(self.runtime, "auto_check_task", None)

    def get_reflect_tracker(self):
        return getattr(self.runtime, "reflect_tracker", None)

    def get_chat_loop_kernel(self):
        return self.runtime.chat_loop_kernel_with_fallback

    def get_heartflow_manager(self):
        task = self.get_proactive_task()
        return getattr(task, "heartflow_manager", None) if task else None

    def get_heartflow_topic_digest_service(self):
        task = self.get_proactive_task()
        return getattr(task, "heartflow_topic_digest_service", None) if task else None

    def _memory_sub(self, attr: str):
        engine = self.get_memory_engine()
        return getattr(engine, attr, None) if engine else None

    def get_v2_store(self):
        return self._memory_sub("v2_store")

    def get_memory_observer(self):
        return self._memory_sub("memory_observer")

    def get_memory_pipeline(self):
        return self._memory_sub("memory_pipeline")

    def get_maintenance_service(self):
        return self._memory_sub("maintenance_service")

    def get_migration_service(self):
        return self._memory_sub("migration_service")

    def get_index_projector(self):
        return self._memory_sub("index_projector")

    def get_write_service(self):
        return self._memory_sub("write_service")

    def get_session_summarizer(self):
        return self._memory_sub("session_summarizer")

    def get_instant_gate(self):
        return self._memory_sub("instant_gate")

    def candidate_to_dict(self, candidate):
        store = self.get_v2_store()
        if store and hasattr(store, "_candidate_to_dict"):
            return store._candidate_to_dict(candidate)
        return dict(candidate) if hasattr(candidate, "__dict__") else {}

    def format_timeline_item(self, item):
        observer = self.get_memory_observer()
        if observer:
            formatter = getattr(observer, "format_timeline_item", None)
            if callable(formatter):
                return formatter(item)
        return item

    def get_expression_pattern_service(self):
        engine = self.get_memory_engine()
        return getattr(engine, "expression_pattern_service", None) if engine else None

    def is_framework_command(self, msg: str) -> bool:
        # ponytail: uses private AstrBot API _collect_descriptors. This API may
        # change without notice. Fallback to command_manager is already wired.
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
        except ImportError:
            logger.debug("[AstrMai-Filter] _collect_descriptors not importable, using command_manager fallback")
            try:
                cmd_mgr = getattr(self.runtime.context, "command_manager", None)
                if cmd_mgr and hasattr(cmd_mgr, "commands"):
                    registered_cmds.update([str(key).lower() for key in cmd_mgr.commands.keys()])
            except Exception as exc2:
                logger.debug(f"[AstrMai-Filter] Fallback command scan also failed: {exc2}")
        except Exception as exc:
            logger.debug(f"[AstrMai-Filter] 内存态穿透失败，尝试降级: {exc}")
            try:
                cmd_mgr = getattr(self.runtime.context, "command_manager", None)
                if cmd_mgr and hasattr(cmd_mgr, "commands"):
                    registered_cmds.update([str(key).lower() for key in cmd_mgr.commands.keys()])
            except Exception as exc2:
                logger.debug(f"[AstrMai-Filter] Fallback command scan also failed: {exc2}")

        try:
            extra_cmds = getattr(self.runtime.config.system1, "extra_command_list", [])
            if extra_cmds:
                registered_cmds.update([str(command).lower() for command in extra_cmds])
        except Exception as exc:
            logger.debug(f"[AstrMai-Filter] Extra command list scan failed: {exc}")

        return clean_cmd in registered_cmds

    async def enter_sys3_direct(self, event):
        """Execute a /work command via Sys3 direct entry.

        This is an async generator — use ``async for`` to consume,
        NOT ``await`` (which would silently return an unconsumed generator).
        """
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
                max_steps=getattr(self.runtime.config.sys3, "max_steps", 30) if hasattr(self.runtime.config, "sys3") else 30,
                timeout=getattr(self.runtime.config.sys3, "tool_timeout", 120) if hasattr(self.runtime.config, "sys3") else 120,
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

                wait_targets = list(main_event.get_extra("astrmai_wait_targets", []) or [])
                wait_target_name = str(main_event.get_extra("astrmai_wait_target_name", "") or "")
                await self.runtime.runtime_coordinator.update_wait_targets(chat_id, wait_targets, wait_target_name)
                if getattr(self.runtime, "chat_loop_kernel", None) is not None:
                    await self.runtime.chat_loop_kernel.sync_runtime_wait_targets(chat_id, wait_targets, wait_target_name)

                is_private = main_event.get_extra("is_private_chat", False)
                if reply_sent and is_private and self.runtime.private_chat_manager:
                    sender_id = str(main_event.get_sender_id())
                    if getattr(self.runtime, "chat_loop_kernel", None) is not None:
                        await self.runtime.chat_loop_kernel.arm_private_wait(
                            chat_id,
                            {
                                "user_id": sender_id,
                                "target_ids": [sender_id],
                                "target_name": str(main_event.get_sender_name() or ""),
                                "timeout": float(getattr(self.runtime.private_chat_manager, "timeout_sec", 0.0) or 0.0),
                                "reason": "private_followup_wait",
                            },
                        )
                    has_reply = await self.runtime.private_chat_manager.wait_for_new_message(sender_id, chat_id=chat_id)
                    if not has_reply:
                        logger.info(f"[{chat_id}] 私聊用户长时间未回复，会话已自然休眠。")
                        if getattr(self.runtime, "chat_loop_kernel", None) is not None:
                            await self.runtime.chat_loop_kernel.expire_wait(chat_id, "private_wait_timeout")
                elif reply_sent and main_event.get_group_id() and self.runtime.group_reply_wait_manager:
                    if self.runtime.group_reply_wait_manager.register_from_reply_event(main_event):
                        if getattr(self.runtime, "chat_loop_kernel", None) is not None:
                            payload = self.runtime.group_reply_wait_manager.get_wait_info(chat_id)
                            if payload:
                                await self.runtime.chat_loop_kernel.arm_group_wait(chat_id, payload)
            except LLMCascadeFailureException:
                logger.exception(f"[AstrMai] Gateway cascade failure for {chat_id}, returning fallback")
                fallback = str(getattr(getattr(self.runtime.config, "reply", None), "fallback_text", "") or "（陷入了短暂的沉默...）")
                await self.runtime.reply_engine.handle_reply(main_event, fallback, chat_id)
            except Exception as e:
                logger.error(f"[AstrMai] System2 unexpected error for {chat_id}: {e}", exc_info=True)
                fallback = str(getattr(getattr(self.runtime.config, "reply", None), "fallback_text", "") or "（陷入了短暂的沉默...）")
                await self.runtime.reply_engine.handle_reply(main_event, fallback, chat_id)
            finally:
                logger.debug(f"[AstrMai] System2 execution finished safely for {chat_id}.")
