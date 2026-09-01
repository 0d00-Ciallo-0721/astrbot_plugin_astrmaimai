from __future__ import annotations

import random
from typing import Optional

from astrbot.api import logger

from ...infrastructure.context_economy import PromptTemplateId
from ...infrastructure.gateway.model_gateway import GlobalModelGateway
from ...infrastructure.runtime.lane_manager import LaneKey
from ...infrastructure.runtime.background_task_budget import BackgroundTaskBudget
from .fact_contract import parse_dream_fact_log


class DreamGenerator:
    """将 Dream maintenance 日志改写为可见梦境叙事。"""

    DREAM_STYLES = [
        "奇幻冒险", "荒诞离奇", "宁静平和", "科幻未来", "古典诗意",
        "赛博朋克", "田园牧歌", "悬疑惊悚", "温馨治愈", "史诗宏大",
        "碎片化意识流", "日常流水账", "童话故事", "武侠江湖", "都市爱情",
        "恐怖怪谈", "喜剧荒诞", "哲学思辨", "末日废土", "魔法学院", "神话传说",
    ]

    def __init__(self, gateway: GlobalModelGateway, config=None, background_task_budget=None):
        self.gateway = gateway
        self.config = config if config else gateway.config
        self.prompt_registry = getattr(getattr(gateway, "context_economy", None), "templates", None)
        self.background_task_budget = background_task_budget or BackgroundTaskBudget()

    async def _call_background_llm(self, *, prompt: str, system_prompt: str, session_id: str, template_envelope=None):
        kwargs = {
            "prompt": prompt,
            "system_prompt": system_prompt,
            "is_json": False,
            "lane_key": LaneKey(
                subsystem="bg",
                task_family="dream",
                scope_id=session_id,
                scope_kind="chat" if session_id != "global" else "global",
            ),
            "base_origin": "",
        }
        if template_envelope is not None:
            kwargs["template_envelope"] = template_envelope

        async def _call():
            return await self.gateway.call_data_process_task(**kwargs)

        return await self.background_task_budget.run(
            _call,
            task_name="dream",
            scope_id=session_id,
            defer_release_on_timeout=True,
        )

    @staticmethod
    def _resolve_scope(session_id: str) -> tuple[str, str]:
        normalized = str(session_id or "").strip()
        if normalized and normalized != "global":
            return normalized, "chat"
        logger.warning("[DreamGenerator] global scope fallback engaged; expected a concrete session_id for dream generation")
        return "global", "global"

    @staticmethod
    def _normalize_dream_log(dream_log: str) -> str:
        lines = [line.strip() for line in str(dream_log or "").splitlines() if line.strip()]
        return "\n".join(lines)

    @classmethod
    def _normalize_style(cls, style: Optional[str]) -> str:
        normalized = str(style or "").strip()
        return normalized if normalized in cls.DREAM_STYLES else random.choice(cls.DREAM_STYLES)

    async def generate(
        self,
        dream_log: str,
        style: Optional[str] = None,
        persona_name: str = "Mai",
        session_id: str = "global",
    ) -> str:
        normalized_dream_log = self._normalize_dream_log(dream_log)
        if not normalized_dream_log.strip():
            return ""

        chosen_style = self._normalize_style(style)
        lane_scope_id, lane_scope_kind = self._resolve_scope(session_id)
        logger.info(f"[DreamGenerator] 生成梦境叙事 | 风格: {chosen_style}")

        try:
            if self.prompt_registry is not None:
                envelope = self.prompt_registry.render_template(
                    PromptTemplateId.DREAM_GENERATION,
                    {
                        "persona_name": persona_name,
                        "style": chosen_style,
                        "dream_log": normalized_dream_log[:800],
                    },
                )
                result = await self._call_background_llm(
                    prompt=envelope.prompt,
                    system_prompt=envelope.system_prompt,
                    session_id=lane_scope_id,
                    template_envelope=envelope,
                )
            else:
                prompt = f"""你是一个擅长幻想与叙事创作的写作助手。

以下是 {persona_name} 今晚梦中经历的记忆整理片段：
---
{normalized_dream_log[:800]}
---

请把上面的内容改写成一段由 {persona_name} 以第一人称讲述的、具有“{chosen_style}”风格的梦境日记。

要求：
1. 200-400 字，语言有画面感。
2. 不要直接提及“记忆整理”“数据库”“工具”等技术词。
3. 可以把记忆的合并、删减转化成梦中的象征性意象。
4. 保持 {persona_name} 的人格与说话方式。
5. 直接输出梦境日记，不要解释。
"""
                result = await self._call_background_llm(
                    prompt=prompt,
                    system_prompt="你是一个善于幻想与创作的写作助手，擅长用诗意的语言描述梦境。",
                    session_id=lane_scope_id,
                )

            dream_text = str(result).strip()
            if dream_text:
                logger.info(
                    f"[DreamGenerator] 梦境生成成功 "
                    f"(风格:{chosen_style}, 长度:{len(dream_text)}字)"
                )
                return dream_text
        except Exception as e:
            logger.error(f"[DreamGenerator] 梦境生成失败: {e}")

        return self._fallback_dream(normalized_dream_log, chosen_style, persona_name)

    @staticmethod
    def _fallback_dream(dream_log: str, style: str, name: str) -> str:
        import datetime

        date_str = datetime.datetime.now().strftime("%Y-%m-%d")
        return (
            f"{name}的梦境日记（{date_str}）\n\n"
            f"今晚我做了一个奇怪的梦。梦里的世界像是被轻轻整理过，"
            f"那些模糊的记忆碎片慢慢拼合，有些随风散去，有些反而变得格外清晰。"
            f"醒来时，只剩下一点淡淡的余韵，像把整晚的心事都压成了一层薄雾。\n"
            f"（{style} 风格，自动生成）"
        )

    def get_random_style(self) -> str:
        return random.choice(self.DREAM_STYLES)

    @staticmethod
    def build_maintenance_result(dream_log: str, session_id: str = "global") -> dict:
        actions = []
        tags = []
        merge_actions = []
        delete_actions = []
        update_actions = []
        jargon_suggestions = []
        detected_facts = []
        for line in str(dream_log).splitlines():
            normalized = line.strip()
            if not normalized:
                continue
            if normalized.startswith("[行动]") or normalized.startswith("[琛屽姩]"):
                actions.append(normalized)
                if "merge" in normalized:
                    tags.append("merge")
                    merge_actions.append(normalized)
                elif "delete" in normalized:
                    tags.append("delete")
                    delete_actions.append(normalized)
                elif "update" in normalized:
                    tags.append("update")
                    update_actions.append(normalized)
                elif "search" in normalized:
                    tags.append("search")
                elif "suggest_jargon_review" in normalized:
                    tags.append("jargon_review")
                    jargon_suggestions.append(normalized)
            else:
                fact = parse_dream_fact_log(normalized)
                if fact is not None:
                    detected_facts.append(fact)
        action_count = len(actions)
        summary = (
            f"session={session_id} 的 dream maintenance 完成，共执行 {action_count} 次维护动作。"
            if action_count
            else f"session={session_id} 的 dream maintenance 完成，本轮未产生显式维护动作。"
        )
        return {
            "session_id": session_id,
            "summary": summary,
            "actions": actions[:12],
            "consolidation_summary": merge_actions[:4],
            "memory_merge_result": merge_actions[-1] if merge_actions else "",
            "deletion_rationale": delete_actions[:4],
            "update_actions": update_actions[:4],
            "jargon_suggestions": jargon_suggestions[:4],
            "detected_facts": detected_facts[:12],
            "tags": sorted(set(tags)),
        }
