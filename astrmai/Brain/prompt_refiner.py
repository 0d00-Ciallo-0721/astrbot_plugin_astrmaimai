import html
import json
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent
from ..infra.legacy_compat import read_legacy_prompt_envelope
from ..infra.runtime_contracts import PromptEnvelope


class PromptRefiner:
    """轻量 PromptRefiner，只保留本轮注入，不再脚本化整段历史。"""

    def __init__(self, memory_engine, db_service=None, config=None, react_retriever=None):
        self.memory_engine = memory_engine
        self.db_service = db_service
        self.config = config
        self.react_retriever = react_retriever

    async def _build_memory_injection(
        self,
        event: AstrMessageEvent,
        prompt: str,
        prompt_envelope: PromptEnvelope | None = None,
        disable_rag: bool = False,
        is_fast_mode: bool = False,
        retrieve_keys: list[str] | None = None,
    ) -> str:
        retrieve_keys = retrieve_keys or []
        if isinstance(prompt_envelope, PromptEnvelope):
            if prompt_envelope.near_context_priority:
                return ""
            current_query = str(
                prompt_envelope.raw_user_text
                or prompt_envelope.focus_thread_text
                or prompt
                or event.message_str
                or ""
            ).strip()
        else:
            if event.get_extra("astrmai_near_context_priority", False):
                return ""
            current_query = str(
                event.get_extra("astrmai_focus_message_text", "")
                or event.get_extra("astrmai_raw_user_text", "")
                or event.message_str
                or ""
            ).strip()
        if not current_query:
            return ""
        if disable_rag or is_fast_mode:
            return ""

        chat_id = event.unified_msg_origin
        react_result = ""
        enable_react = True
        if self.config and hasattr(self.config, "memory"):
            enable_react = self.config.memory.enable_react_agent

        if self.react_retriever and enable_react:
            try:
                react_result = await self.react_retriever.retrieve(
                    query=current_query,
                    chat_id=chat_id,
                    chat_context=prompt,
                    sender_name=event.get_sender_name() or "",
                    retrieve_keys=retrieve_keys,
                )
            except Exception as exc:
                logger.debug(f"[PromptRefiner] ReAct retrieve failed, fallback to recall: {exc}")

        if react_result:
            return f"<injected_memory>\n{html.escape(react_result)}\n</injected_memory>\n"

        if not self.memory_engine:
            return ""

        memory_text = await self.memory_engine.recall(current_query, session_id=chat_id)
        if memory_text and "什么也没想起来" not in memory_text:
            return (
                "<injected_memory>\n"
                "记忆浮现：基于当前话题，你想起了以下内容：\n"
                f"{html.escape(memory_text)}\n"
                "</injected_memory>\n"
            )
        return ""

    async def _resolve_visual_memory(self, text: str) -> str:
        if not isinstance(text, str):
            return text

        picids = set(re.findall(r"\[picid:([a-fA-F0-9]{32})\]", text))
        if not picids or not self.db_service:
            return text

        for picid in picids:
            resolved_text = "[一张尚未看清的图片]"
            try:
                with self.db_service.get_session() as session:
                    from ..infra.datamodels import VisualMemory

                    mem = session.get(VisualMemory, picid)
                    if mem and mem.description:
                        try:
                            tags = json.loads(mem.emotion_tags)
                            tags_str = ", ".join(tags) if isinstance(tags, list) else str(tags)
                        except Exception:
                            tags_str = ""
                        if mem.type == "emoji":
                            resolved_text = (
                                f"[发了一个表情包，画面是：{mem.description}，传达了：{tags_str}]"
                                if tags_str
                                else f"[发了一个表情包，画面是：{mem.description}]"
                            )
                        else:
                            resolved_text = f"[发了一张图片，画面是：{mem.description}]"
            except Exception as exc:
                logger.debug(f"[PromptRefiner] visual memory resolve failed {picid}: {exc}")

            text = text.replace(f"[picid:{picid}]", resolved_text)

        return text

    async def refine_prompt(
        self,
        event: AstrMessageEvent,
        system_prompt: str,
        prompt: str,
        context,
    ) -> tuple[str, str]:
        disable_rag = False
        if hasattr(context, "get"):
            disable_rag = context.get("disable_rag_injection")
        elif hasattr(context, "shared_dict"):
            disable_rag = context.shared_dict.get("disable_rag_injection", False)

        retrieve_keys = event.get_extra("retrieve_keys", [])
        prompt_envelope = event.get_extra("astrmai_prompt_envelope", None)
        if isinstance(prompt_envelope, PromptEnvelope):
            recent_transcript = prompt_envelope.recent_transcript.strip()
            raw_user_text = (prompt_envelope.raw_user_text or prompt).strip()
            focus_thread_text = prompt_envelope.focus_thread_text.strip()
            background_window_text = prompt_envelope.ambient_background_text.strip()
            focus_reason = prompt_envelope.focus_reason.strip()
            focus_thread_reason = (prompt_envelope.focus_thread_reason or focus_reason).strip()
            near_context_priority = bool(prompt_envelope.near_context_priority)
        else:
            prompt_envelope = read_legacy_prompt_envelope(event, prompt=prompt)
            recent_transcript = prompt_envelope.recent_transcript.strip()
            raw_user_text = prompt_envelope.raw_user_text.strip()
            focus_thread_text = prompt_envelope.focus_thread_text.strip()
            background_window_text = prompt_envelope.ambient_background_text.strip()
            focus_reason = prompt_envelope.focus_reason.strip()
            focus_thread_reason = (prompt_envelope.focus_thread_reason or focus_reason).strip()
            near_context_priority = bool(prompt_envelope.near_context_priority)
        use_lane_history = bool(event.get_extra("astrmai_use_lane_history", False))
        is_fast_mode = "CORE_ONLY" in retrieve_keys

        injection = await self._build_memory_injection(
            event=event,
            prompt=prompt,
            prompt_envelope=prompt_envelope if isinstance(prompt_envelope, PromptEnvelope) else None,
            disable_rag=disable_rag,
            is_fast_mode=is_fast_mode,
            retrieve_keys=retrieve_keys,
        )

        history_block = recent_transcript or "（暂无最近对话记录）"
        focus_block = focus_thread_text or raw_user_text or prompt
        if background_window_text and near_context_priority:
            background_lines = [line for line in background_window_text.splitlines() if line.strip()]
            background_window_text = "\n".join(background_lines[-1:])

        if isinstance(prompt_envelope, PromptEnvelope):
            prompt_envelope.ambient_background_text = background_window_text
            prompt_envelope.near_context_priority = near_context_priority
            current_block = prompt_envelope.current_block() or (focus_block or prompt)
        else:
            current_sections = []
            if focus_block:
                current_sections.append(f"请优先接住这条对话线索并回答：\n{focus_block}")
            if background_window_text:
                current_sections.append(f"其他背景只作参考，不必逐条回应：\n{background_window_text}")
            current_block = "\n\n".join(current_sections) if current_sections else (focus_block or prompt)

        final_system_prompt = re.sub(
            r"<CHAT_HISTORY>|\{HISTORY_PLACEHOLDER\}",
            f"最近几轮对话：\n{history_block}",
            system_prompt,
        )
        final_system_prompt = re.sub(
            r"<CURRENT_MESSAGES>|\{CURRENT_MSG_PLACEHOLDER\}",
            current_block,
            final_system_prompt,
        )
        final_system_prompt = re.sub(
            r"<RAG_MEMORY>|\{MEMORY_PLACEHOLDER\}",
            injection,
            final_system_prompt,
        )
        final_system_prompt = await self._resolve_visual_memory(final_system_prompt)

        visual_focus_block = await self._resolve_visual_memory(focus_block)
        prompt_lines = []
        if visual_focus_block:
            prompt_lines.append(f"请优先接住这条对话线索并回答：\n{visual_focus_block}")
        if background_window_text:
            visual_background_block = await self._resolve_visual_memory(background_window_text)
            prompt_lines.append(f"其他背景只作参考，不必逐条回应：\n{visual_background_block}")
        if not is_fast_mode:
            prompt_lines.append("请顺着刚才的话继续回应，不要另起话题。")
        final_prompt = "\n\n".join(line for line in prompt_lines if line).strip()

        if getattr(getattr(self.config, "global_settings", None), "debug_mode", False):
            logger.debug(
                f"[{event.unified_msg_origin}] PromptRefiner preview "
                f"raw_user_text={raw_user_text[:120]!r} "
                f"focus_thread={focus_block[:160]!r} "
                f"background={background_window_text[:120]!r} "
                f"recent_transcript={history_block[:160]!r} "
                f"focus_reason={focus_reason!r} "
                f"focus_thread_reason={focus_thread_reason!r} "
                f"near_context_priority={near_context_priority}"
            )

        logger.info(
            f"[{event.unified_msg_origin}] PromptRefiner ready "
            f"(lane_history={use_lane_history}, inject_memory={'yes' if injection else 'no'}, "
            f"near_context_priority={near_context_priority}, recent_transcript={'yes' if recent_transcript else 'no'})"
        )
        return final_system_prompt, final_prompt
