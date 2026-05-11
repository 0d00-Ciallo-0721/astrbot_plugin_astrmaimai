from __future__ import annotations

import asyncio
import copy
from typing import Any, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

try:
    from astrbot.api.message_components import Plain
except ImportError:  # pragma: no cover
    class Plain:  # type: ignore[override]
        def __init__(self, text: str):
            self.text = text


try:
    from astrbot.core.agent.tool import ToolSet
except ImportError:  # pragma: no cover
    class ToolSet:  # type: ignore[override]
        def __init__(self, tools):
            self.tools = tools


from ...infrastructure.runtime.lane_manager import LaneKey
from ...infrastructure.runtime.trace_runtime import debug_trace, preview_text
from ..contracts.focus_context import FocusThreadContext, FreshnessState, VisionBundle
from ..contracts.prompt_envelope import PromptEnvelope


class ConcurrentExecutor:
    _PROVIDER_ERROR_KEYWORDS = (
        "request failed",
        "error type",
        "error message",
        "api error",
        "all chat models fail",
        "connection error",
        "notfounderror",
        "exception:",
    )
    def __init__(
        self,
        context,
        gateway,
        reply_engine,
        evolution_manager,
        config=None,
        runtime_coordinator=None,
    ):
        self.context = context
        self.gateway = gateway
        self.reply_engine = reply_engine
        self.evolution_manager = evolution_manager
        self.config = config if config else gateway.config
        self.runtime_coordinator = runtime_coordinator
        self._chat_locks = {}
        self._chat_pending_count = {}
        self._global_lock = asyncio.Lock()

    def _build_vision_bundle(
        self,
        event: AstrMessageEvent,
        direct_vision_urls: Optional[list[str]],
    ) -> VisionBundle:
        focus_context = event.get_extra("astrmai_focus_thread_context", None)
        if isinstance(focus_context, FocusThreadContext):
            bundle = focus_context.vision_bundle
            image_urls = list(dict.fromkeys(list(bundle.image_urls or []) + list(direct_vision_urls or [])))
            direct_urls = list(dict.fromkeys(list(bundle.direct_image_urls or []) + list(direct_vision_urls or [])))
            return VisionBundle(
                image_urls=image_urls,
                direct_image_urls=direct_urls,
                is_direct_request=bundle.is_direct_request or bool(direct_urls),
                is_image_only=bundle.is_image_only,
                source=bundle.source or "focus_thread",
            )

        urls = list(dict.fromkeys(list(direct_vision_urls or [])))
        return VisionBundle(
            image_urls=urls,
            direct_image_urls=urls[:],
            is_direct_request=bool(urls),
            is_image_only=bool(urls and not (event.message_str or "").strip()),
            source="event_extra",
        )

    def _build_sanitized_execution_event(
        self,
        event: AstrMessageEvent,
        vision_bundle: VisionBundle,
    ) -> AstrMessageEvent:
        try:
            sanitized_event = copy.copy(event)
            if hasattr(event, "message_obj") and event.message_obj:
                sanitized_message_obj = copy.copy(event.message_obj)
                safe_text = (
                    event.message_str.strip()
                    if event.message_str
                    else ("[image-or-special-message]" if vision_bundle.image_urls else "[special-message]")
                )
                sanitized_message_obj.message = [Plain(safe_text)]
                sanitized_event.message_obj = sanitized_message_obj
            return sanitized_event
        except Exception:
            return event

    async def _evaluate_execution_freshness(self, event: AstrMessageEvent, chat_id: str) -> tuple[FreshnessState, str]:
        if not self.runtime_coordinator:
            return FreshnessState.FRESH, ""

        focus_context = event.get_extra("astrmai_focus_thread_context", None)
        prompt_envelope = event.get_extra("astrmai_prompt_envelope", None)
        focus_timestamp = float(event.get_extra("astrmai_timestamp", getattr(event, "timestamp", 0.0)) or 0.0)
        thread_signature = ""

        if isinstance(focus_context, FocusThreadContext):
            focus_timestamp = float(focus_context.freshness_budget.created_at or focus_timestamp)
            thread_signature = str(focus_context.thread_signature or "")
        elif isinstance(prompt_envelope, PromptEnvelope):
            thread_signature = str(prompt_envelope.thread_signature or "")

        max_age_seconds = float(getattr(getattr(self.config, "reply", None), "stale_reply_max_age_sec", 0.0) or 0.0)
        if max_age_seconds <= 0:
            api_timeout = float(getattr(getattr(self.config, "infra", None), "api_timeout", 15.0) or 15.0)
            max_age_seconds = max(30.0, min(90.0, api_timeout * 2.5))

        return await self.runtime_coordinator.evaluate_reply_freshness(
            chat_id,
            focus_timestamp,
            max_age_seconds=max_age_seconds,
            thread_signature=thread_signature,
        )

    async def _acquire_chat_execution_lock(self, chat_id: str):
        using_runtime_coordinator = self.runtime_coordinator is not None
        if using_runtime_coordinator:
            chat_lock = await self.runtime_coordinator.try_acquire_executor(chat_id, max_pending=2)
            if chat_lock is None:
                return None, True
            return chat_lock, True

        async with self._global_lock:
            if chat_id not in self._chat_locks:
                self._chat_locks[chat_id] = asyncio.Lock()
                self._chat_pending_count[chat_id] = 0
            if self._chat_pending_count[chat_id] >= 2:
                return None, False
            self._chat_pending_count[chat_id] += 1
            return self._chat_locks[chat_id], False

    async def _release_chat_execution_lock(self, chat_id: str, using_runtime_coordinator: bool) -> None:
        if using_runtime_coordinator:
            await self.runtime_coordinator.release_executor(chat_id)
            return
        async with self._global_lock:
            self._chat_pending_count[chat_id] -= 1
            if self._chat_pending_count[chat_id] == 0:
                self._chat_locks.pop(chat_id, None)
                self._chat_pending_count.pop(chat_id, None)

    def _execution_runtime_values(self, event: AstrMessageEvent, chat_id: str) -> dict[str, Any]:
        bot_id = str(event.get_self_id()) if hasattr(event, "get_self_id") else "SELF_BOT"
        is_fast_mode = event.get_extra("is_fast_mode", False)
        config_max_steps = getattr(self.config.agent, "max_steps", 5)
        tool_tier = str(event.get_extra("astrmai_tool_tier", "full") or "full")
        max_steps = 2 if tool_tier == "chat" else max(5, config_max_steps)
        timeout = 15 if is_fast_mode else self.config.agent.timeout
        prefix_hash = event.get_extra("astrmai_prefix_hash", "")
        prompt_envelope = event.get_extra("astrmai_prompt_envelope", None)
        raw_user_text = (
            prompt_envelope.raw_user_text
            if isinstance(prompt_envelope, PromptEnvelope)
            else event.get_extra("astrmai_raw_user_text", "")
        )
        dialog_lane_key = LaneKey(subsystem="sys2", task_family="dialog", scope_id=chat_id)
        return {
            "bot_id": bot_id,
            "is_fast_mode": is_fast_mode,
            "max_steps": max_steps,
            "timeout": timeout,
            "tool_tier": tool_tier,
            "prefix_hash": prefix_hash,
            "raw_user_text": raw_user_text,
            "dialog_lane_key": dialog_lane_key,
        }

    async def _inject_direct_vision_context(
        self,
        event: AstrMessageEvent,
        chat_id: str,
        model_prompt: str,
        system_prompt: str,
        vision_bundle: VisionBundle,
    ) -> tuple[str, str]:
        if not vision_bundle.direct_image_urls:
            return model_prompt, system_prompt

        import aiohttp
        import base64
        import io
        import os
        import tempfile
        from PIL import Image

        logger.info(f"[{chat_id}] vision direct path triggered in executor")
        vision_descriptions: list[str] = []
        for url_or_path in vision_bundle.direct_image_urls:
            temp_file_path = None
            is_temp = False
            try:
                image_bytes = None
                if str(url_or_path).startswith("http"):
                    async with aiohttp.ClientSession() as session:
                        async with session.get(url_or_path, timeout=15) as resp:
                            if resp.status == 200:
                                image_bytes = await resp.read()
                elif str(url_or_path).startswith("data:image"):
                    _, encoded = str(url_or_path).split(",", 1)
                    image_bytes = base64.b64decode(encoded)
                elif os.path.exists(url_or_path):
                    temp_file_path = url_or_path

                if image_bytes:
                    try:
                        img_format = Image.open(io.BytesIO(image_bytes)).format.lower()
                    except Exception:
                        img_format = "jpeg"
                    fd, temp_file_path = tempfile.mkstemp(suffix=f".{img_format}")
                    with os.fdopen(fd, "wb") as file_obj:
                        file_obj.write(image_bytes)
                    is_temp = True

                if temp_file_path and os.path.exists(temp_file_path):
                    result_dict = await self.gateway.call_vision_task(
                        image_data=temp_file_path,
                        prompt="Analyze this image in detail.",
                        system_prompt=(
                            'You are an image analysis assistant. Return only JSON with keys '
                            '"type", "description", and "emotion_tags".'
                        ),
                        lane_key=LaneKey(subsystem="sys1", task_family="vision", scope_id=chat_id),
                        base_origin=chat_id,
                    )
                    if result_dict:
                        desc = result_dict.get("description", "unknown image")
                        tags = result_dict.get("emotion_tags", [])
                        tags_str = ", ".join(tags) if isinstance(tags, list) else str(tags)
                        vision_line = f"我刚看到一张图片，画面是：{desc}。"
                        if tags_str and tags_str.lower() not in {"", "none", "null"}:
                            vision_line += f" 它给我的感觉是：{tags_str}。"
                        vision_descriptions.append(vision_line)
            except Exception as exc:
                logger.error(f"[{chat_id}] vision side-path failed: {exc}")
            finally:
                if is_temp and temp_file_path and os.path.exists(temp_file_path):
                    try:
                        os.remove(temp_file_path)
                    except Exception:
                        pass

        if vision_descriptions:
            vision_inject = (
                "\n\n"
                + "\n".join(vision_descriptions)
            )
            model_prompt += vision_inject
            system_prompt += vision_inject
        return model_prompt, system_prompt

    async def _check_pre_model_freshness(self, event: AstrMessageEvent, chat_id: str, label: str) -> bool:
        freshness_state, freshness_reason = await self._evaluate_execution_freshness(event, chat_id)
        if freshness_state == FreshnessState.EXPIRED:
            logger.info(f"[{chat_id}] stop expired {label}: {freshness_reason}")
            return False
        return True

    async def _finalize_reply(self, event: AstrMessageEvent, chat_id: str, bot_id: str, reply_text: str, *, trace_mode: str, model: str) -> str:
        await self.reply_engine.handle_reply(event, reply_text, chat_id)
        if hasattr(self.evolution_manager, "process_bot_reply"):
            await self.evolution_manager.process_bot_reply(chat_id, bot_id, reply_text)
        debug_trace(
            event,
            "execution.executor.exit",
            mode=trace_mode,
            model=model,
            reply_preview=preview_text(reply_text, 120),
        )
        return reply_text

    async def _run_text_mode(self, event: AstrMessageEvent, chat_id: str, api_prompt: str, system_prompt: str, runtime: dict[str, Any]) -> Optional[str]:
        last_error = ""
        for provider_id in self.gateway.get_agent_models():
            if not await self._check_pre_model_freshness(event, chat_id, "text execution"):
                return None
            try:
                result = await self.gateway.chat_in_lane_result(
                    lane_key=runtime["dialog_lane_key"],
                    base_origin=chat_id,
                    prompt=api_prompt,
                    system_prompt=system_prompt,
                    models=[provider_id],
                    prefix_hash=runtime["prefix_hash"],
                    use_fallback=False,
                    raw_user_text=runtime["raw_user_text"],
                )
                reply_text = result.text
                if not reply_text:
                    raise ValueError(f"model {provider_id} returned empty text")
                if any(keyword in reply_text.lower() for keyword in self._PROVIDER_ERROR_KEYWORDS):
                    raise RuntimeError(f"model surfaced backend error: {reply_text}")
                return await self._finalize_reply(
                    event,
                    chat_id,
                    runtime["bot_id"],
                    reply_text,
                    trace_mode="chat",
                    model=provider_id,
                )
            except Exception as exc:
                last_error = str(exc)
                logger.warning(f"[{chat_id}] chat model {provider_id} failed, trying next: {exc}")
                continue

        logger.error(f"[{chat_id}] all chat models exhausted: {last_error}")
        await self._handle_fatal_fallback(event, chat_id, f"all chat models exhausted:\n{last_error}")
        return None

    async def _run_tool_mode(self, event: AstrMessageEvent, chat_id: str, execution_event: AstrMessageEvent, api_prompt: str, system_prompt: str, tools: list[Any], runtime: dict[str, Any]) -> Optional[str]:
        tool_set = ToolSet(tools)
        last_error = ""
        for provider_id in self.gateway.get_agent_models():
            if not await self._check_pre_model_freshness(event, chat_id, "tool execution"):
                return None
            try:
                result = await self.gateway.tool_chat_in_lane_result(
                    lane_key=runtime["dialog_lane_key"],
                    base_origin=chat_id,
                    event=execution_event,
                    prompt=api_prompt,
                    system_prompt=system_prompt,
                    tools=tool_set,
                    models=[provider_id],
                    max_steps=runtime["max_steps"],
                    timeout=runtime["timeout"],
                    prefix_hash=runtime["prefix_hash"],
                    raw_user_text=runtime["raw_user_text"],
                )
                reply_text = result.text
                if not reply_text:
                    raise ValueError("empty tool reply")
                if any(keyword in reply_text.lower() for keyword in self._PROVIDER_ERROR_KEYWORDS):
                    raise RuntimeError(f"tool model surfaced backend error: {reply_text}")
                if "[SYSTEM_WAIT_SIGNAL]" in reply_text:
                    debug_trace(event, "execution.executor.wait_signal", model=provider_id)
                    return None
                if "[TERMINAL_YIELD]:" in reply_text:
                    idx = reply_text.find("[TERMINAL_YIELD]:")
                    terminal_content = reply_text[idx + len("[TERMINAL_YIELD]:") :].strip()
                    return await self._finalize_reply(
                        event,
                        chat_id,
                        runtime["bot_id"],
                        terminal_content,
                        trace_mode="tool_terminal_yield",
                        model=provider_id,
                    )
                return await self._finalize_reply(
                    event,
                    chat_id,
                    runtime["bot_id"],
                    reply_text,
                    trace_mode="tool",
                    model=provider_id,
                )
            except Exception as exc:
                last_error = str(exc)
                logger.warning(f"[{chat_id}] tool model {provider_id} failed, trying next: {exc}")
                continue

        logger.error(f"[{chat_id}] all tool models exhausted: {last_error}")
        await self._handle_fatal_fallback(
            event,
            chat_id,
            last_error if last_error else "tool model pool exhausted",
        )
        return None

    async def execute(
        self,
        event: AstrMessageEvent,
        prompt: str,
        system_prompt: str,
        tools: list[Any] = None,
        direct_vision_urls: list[str] = None,
    ) -> Optional[str]:
        debug_trace(
            event,
            "execution.executor.enter",
            tool_count=len(tools or []),
            has_vision=bool(direct_vision_urls),
            prompt=preview_text(prompt, 120),
        )

        chat_id = event.unified_msg_origin
        chat_lock, using_runtime_coordinator = await self._acquire_chat_execution_lock(chat_id)
        if chat_lock is None:
            logger.warning(f"[{chat_id}] executor dropped because pending tasks exceeded budget")
            debug_trace(event, "execution.executor.dropped", reason="too_many_pending")
            return None

        try:
            async with chat_lock:
                models = self.gateway.get_agent_models()
                if not models:
                    logger.error(f"[{chat_id}] no configured agent model")
                    return None

                runtime = self._execution_runtime_values(event, chat_id)
                try:
                    event._is_final_reply_phase = True
                    if not await self._check_pre_model_freshness(event, chat_id, "executor calculation"):
                        debug_trace(event, "execution.executor.stale_drop", reason="freshness_check_failed")
                        return None

                    vision_bundle = self._build_vision_bundle(event, direct_vision_urls)
                    execution_event = self._build_sanitized_execution_event(event, vision_bundle)
                    api_prompt, system_prompt = await self._inject_direct_vision_context(
                        event, chat_id, prompt, system_prompt, vision_bundle
                    )

                    if tools is None or len(tools) == 0:
                        return await self._run_text_mode(event, chat_id, api_prompt, system_prompt, runtime)
                    return await self._run_tool_mode(event, chat_id, execution_event, api_prompt, system_prompt, tools, runtime)
                except Exception as exc:
                    logger.error(f"[{chat_id}] executor core crashed: {exc}")
                    await self._handle_fatal_fallback(event, chat_id, f"executor core exception:\n{exc}")
                    return None
                finally:
                    if hasattr(event, "_is_final_reply_phase"):
                        delattr(event, "_is_final_reply_phase")
        finally:
            await self._release_chat_execution_lock(chat_id, using_runtime_coordinator)
    async def _handle_fatal_fallback(self, event: AstrMessageEvent, chat_id: str, error_detail: str):
        logger.error(f"[{chat_id}] fatal executor fallback triggered")
        fallback_msg = getattr(self.config.reply, "fallback_text", "(temporary silence...)")
        await self.reply_engine.handle_reply(event, fallback_msg, chat_id)

        config_global = getattr(self.config, "global_settings", None)
        if not (config_global and getattr(config_global, "enable_error_interception", True)):
            return

        admin_ids = getattr(config_global, "admin_ids", [])
        if not admin_ids:
            return

        from astrbot.api.event import MessageChain

        platform_id = event.unified_msg_origin.split(":")[0]
        error_report = (
            "[AstrMai executor alert]\n"
            f"Target: {event.unified_msg_origin}\n"
            f"Error detail:\n{error_detail}"
        )
        chain = MessageChain().message(error_report)

        for admin_id in admin_ids:
            try:
                admin_umo = f"{platform_id}:FriendMessage:{admin_id}"
                await self.context.send_message(admin_umo, chain)
                logger.debug(f"[Executor] pushed alert to admin {admin_id}")
            except Exception as exc:
                logger.error(f"[Executor] failed to push alert to admin {admin_id}: {exc}")


__all__ = ["ConcurrentExecutor"]
