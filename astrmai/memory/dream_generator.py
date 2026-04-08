# astrmai/memory/dream_generator.py
"""
梦境叙述生成器 (Dream Generator) — Phase 7.1
参考: MaiBot/dream/dream_generator.py

功能: 将 DreamAgent 的整理日志转化为诗意的"梦境日记"，
     以 21 种随机风格生成，赋予 Bot 独特的精神内核。

可配置发送给关系亲密的用户（social_score > 60 的好友）。
"""
import random
from typing import Optional
from astrbot.api import logger
from ..infra.gateway import GlobalModelGateway
from ..infra.lane_manager import LaneKey


class DreamGenerator:
    """梦境叙述生成器"""

    # 21 种梦境风格（参考 MaiBot）
    DREAM_STYLES = [
        "奇幻冒险", "荒诞离奇", "宁静平和", "科幻未来", "古典诗意",
        "赛博朋克", "田园牧歌", "悬疑惊悚", "温馨治愈", "史诗宏大",
        "碎片化意识流", "日常流水账", "童话故事", "武侠江湖", "都市爱情",
        "恐怖哥特", "喜剧荒诞", "哲学思辨", "末日废土", "魔法学院", "神话传说"
    ]

    def __init__(self, gateway: GlobalModelGateway, config=None):
        self.gateway = gateway
        self.config = config if config else gateway.config

    async def generate(
        self,
        dream_log: str,
        style: Optional[str] = None,
        persona_name: str = "Mai",
        session_id: str = "global"
    ) -> str:
        """
        将梦境整理日志转化为指定风格的诗意梦境叙述。

        Args:
            dream_log:    DreamAgent 的整理日志文本
            style:        风格名称（None 时随机选择）
            persona_name: Bot 的名字/人设名称

        Returns:
            生成的梦境叙述文本（中文，200-400字）
        """
        if not dream_log.strip():
            return ""

        chosen_style = style if style else random.choice(self.DREAM_STYLES)
        logger.info(f"[DreamGenerator] 🌙 生成梦境叙述 | 风格: {chosen_style}")

        prompt = f"""你是一个创意写作助手。

以下是 {persona_name} 今晚在梦中经历的记忆整理过程（以内部视角描述）：
---
{dream_log[:800]}
---

请将上面的记忆整理过程，改写为一段{persona_name}**从第一人称视角**讲述的**{chosen_style}**风格的梦境日记。

要求：
1. 200-400字，语言生动富有画面感
2. 不要直接提及"记忆整理"、"数据库"、"工具"等技术词汇
3. 将记忆的合并/删除转化为梦境中的象征性意象（如：模糊的影子消散了、碎片拼合成完整的画面）
4. 以"今晚我梦见了..."或"{persona_name}的梦境日记·[日期]"开头
5. 保持{persona_name}的独特人格和说话方式

直接输出梦境日记，不要任何解释："""

        try:
            result = await self.gateway.call_data_process_task(
                prompt=prompt,
                is_json=False,
                system_prompt="你是一个善于幻想与创作的写作助手，擅长用诗意的语言描述梦境。",
                lane_key=LaneKey(subsystem="bg", task_family="dream", scope_id=session_id or "global", scope_kind="global"),
                base_origin="",
            )
            dream_text = str(result).strip()
            if dream_text:
                logger.info(
                    f"[DreamGenerator] ✨ 梦境生成成功 "
                    f"(风格:{chosen_style}, 长度:{len(dream_text)}字)"
                )
                return dream_text
        except Exception as e:
            logger.error(f"[DreamGenerator] 梦境生成失败: {e}")

        # 降级: 纯模板
        return self._fallback_dream(dream_log, chosen_style, persona_name)

    @staticmethod
    def _fallback_dream(dream_log: str, style: str, name: str) -> str:
        """LLM 失败时的纯模板降级"""
        import datetime
        date_str = datetime.datetime.now().strftime("%Y年%m月%d日")
        return (
            f"{name}的梦境日记·{date_str}\n\n"
            f"今晚我做了一个奇特的梦。梦里的世界像是被谁轻轻地整理过，"
            f"那些模糊的记忆碎片慢慢拼合，有些随风消散，有些变得格外清晰。"
            f"醒来时，感觉世界又轻盈了一些。\n"
            f"（{style}风格 · 自动生成）"
        )

    def get_random_style(self) -> str:
        """获取随机风格名称"""
        return random.choice(self.DREAM_STYLES)
