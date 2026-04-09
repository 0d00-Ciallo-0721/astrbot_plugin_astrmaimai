import html
import json
import re

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent


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
        disable_rag: bool,
        is_fast_mode: bool,
        retrieve_keys: list[str],
    ) -> str:
        if event.get_extra("astrmai_near_context_priority", False):
            return ""
        if disable_rag or is_fast_mode:
            return ""

        current_query = event.message_str
        if not current_query:
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
        recent_transcript = str(event.get_extra("astrmai_recent_transcript", "") or "").strip()
        raw_user_text = str(event.get_extra("astrmai_raw_user_text", prompt) or prompt).strip()
        near_context_priority = bool(event.get_extra("astrmai_near_context_priority", False))
        use_lane_history = bool(event.get_extra("astrmai_use_lane_history", False))
        is_fast_mode = "CORE_ONLY" in retrieve_keys

        injection = await self._build_memory_injection(
            event=event,
            prompt=prompt,
            disable_rag=disable_rag,
            is_fast_mode=is_fast_mode,
            retrieve_keys=retrieve_keys,
        )

        history_block = recent_transcript or "（暂无最近对话记录）"
        current_block = raw_user_text or prompt

        final_system_prompt = re.sub(
            r"<CHAT_HISTORY>|\{HISTORY_PLACEHOLDER\}",
            f"最近真实对话：\n{history_block}",
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

        visual_current_block = await self._resolve_visual_memory(current_block)
        prompt_lines = [visual_current_block]
        if not is_fast_mode:
            prompt_lines.append("请直接接住上一轮语义继续说，不要重启话题。")
        final_prompt = "\n\n".join(line for line in prompt_lines if line).strip()

        if getattr(getattr(self.config, "global_settings", None), "debug_mode", False):
            logger.debug(
                f"[{event.unified_msg_origin}] PromptRefiner preview "
                f"raw_user_text={current_block[:120]!r} "
                f"recent_transcript={history_block[:160]!r} "
                f"near_context_priority={near_context_priority}"
            )

        logger.info(
            f"[{event.unified_msg_origin}] PromptRefiner ready "
            f"(lane_history={use_lane_history}, inject_memory={'yes' if injection else 'no'}, "
            f"near_context_priority={near_context_priority}, recent_transcript={'yes' if recent_transcript else 'no'})"
        )
        return final_system_prompt, final_prompt
