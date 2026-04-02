# astrmai/Brain/action_modifier.py
"""
动态动作修改器 (Action Modifier) — Phase 5
参考: MaiBot/planner_actions/action_modifier.py

根据当前上下文动态增减可用工具:
- 好感度低时: 禁用亲密类工具 (SpaceTransition, Poke, Like)
- 精力耗尽时: 仅保留 WaitTool
- 敌对关系: 只保留基础工具 (Wait, Hijack, OmniPerception)
- 情绪极端时: 增加/减少特定工具的可用性

AstrBot 规范:
- 工具列表中的工具均为 FunctionTool 实例，通过 .name 属性识别
- 不依赖特定框架 API，纯逻辑过滤
"""
from typing import List, Optional, Any
from astrbot.api import logger


class ActionModifier:
    """动态动作修改器"""

    # 亲密类工具名 (好感度低于阈值时禁用)
    INTIMATE_TOOLS = {
        "space_transition_action",  # 悄悄话转私聊
        "proactive_poke",           # 戳一戳
        "proactive_like_action",    # 狂点赞
    }

    # 基础生存工具名 (精力耗尽时仅保留)
    SURVIVAL_TOOLS = {
        "wait_and_listen",
    }

    # 敌对模式保留工具
    HOSTILE_TOOLS = {
        "wait_and_listen",
        "topic_hijack_action",
        "omni_perception_query",
    }

    # 好感度阈值配置
    INTIMATE_THRESHOLD = 20     # 低于此值禁用亲密工具
    HOSTILE_THRESHOLD = -20     # 低于此值进入敌对模式
    ENERGY_EXHAUSTION = 10      # 精力低于此值进入极简模式

    def __init__(self, config=None):
        self.config = config
        if config and hasattr(config, 'life'):
            # 支持从配置中读取阈值
            self.INTIMATE_THRESHOLD = getattr(config.life, 'intimate_tool_threshold', 20)
            self.HOSTILE_THRESHOLD = getattr(config.life, 'hostile_threshold', -20)
            self.ENERGY_EXHAUSTION = getattr(config.life, 'energy_exhaustion', 10)

    def modify_tools(self, tools: List[Any], state=None, profile=None, relationship_vec=None) -> List[Any]:
        """
        根据当前状态与用户关系过滤可用工具列表。
        
        Args:
            tools: 原始工具列表 (FunctionTool 实例)
            state: ChatState 对象 (包含 energy, mood)
            profile: UserProfile 对象 (包含 social_score)
            relationship_vec: RelationshipVector 对象 (Phase 5 四维关系)
            
        Returns:
            过滤后的工具列表
        """
        if not tools:
            return tools

        filtered = list(tools)
        reasons = []  # 记录过滤原因

        # 获取好感度 (优先使用四维向量，兜底用 profile.social_score)
        score = 0
        if relationship_vec:
            score = relationship_vec.social_score
            trust = relationship_vec.trust
        elif profile:
            score = getattr(profile, 'social_score', 0)
            trust = score * 0.3  # 粗略推断

        # 1. 精力耗尽: 极简模式，只保留等待工具
        if state and hasattr(state, 'energy') and state.energy < self.ENERGY_EXHAUSTION:
            filtered = [t for t in filtered if getattr(t, 'name', '') in self.SURVIVAL_TOOLS]
            reasons.append(f"精力耗尽({state.energy:.0f})")

        # 2. 好感度限制 (仅在非极简模式下生效)
        elif profile or relationship_vec:
            if score < self.HOSTILE_THRESHOLD:
                # 敌对模式: 只保留基础工具
                filtered = [t for t in filtered if getattr(t, 'name', '') in self.HOSTILE_TOOLS]
                reasons.append(f"敌对关系(好感{score:.0f})")

            elif score < self.INTIMATE_THRESHOLD:
                # 低好感: 禁止亲密类动作
                filtered = [t for t in filtered if getattr(t, 'name', '') not in self.INTIMATE_TOOLS]
                reasons.append(f"好感不足(好感{score:.0f})")

            # Phase 5: 基于信任度的额外过滤
            if relationship_vec and relationship_vec.trust < -10:
                # 低信任: 额外禁止需要信任的工具
                trust_tools = {"proactive_advice", "schedule_reminder"}
                filtered = [t for t in filtered if getattr(t, 'name', '') not in trust_tools]
                reasons.append(f"信任不足(trust:{relationship_vec.trust:.0f})")

        # 3. 情绪修正 (可选: 情绪极低时禁用复读等娱乐性工具)
        if state and hasattr(state, 'mood') and state.mood < -0.7:
            entertainment_tools = {"meme_resonance_action", "proactive_meme"}
            filtered = [t for t in filtered if getattr(t, 'name', '') not in entertainment_tools]
            reasons.append(f"情绪低落(mood:{state.mood:.2f})")

        # 日志输出
        if reasons:
            original_count = len(tools)
            filtered_count = len(filtered)
            logger.info(
                f"[ActionModifier] 🔧 工具集动态调整: {original_count} → {filtered_count} "
                f"(原因: {', '.join(reasons)})"
            )

        return filtered

    def get_filtered_tool_names(self, tools: List[Any], state=None, profile=None) -> List[str]:
        """获取过滤后的工具名称列表 (用于日志或提示词注入)"""
        filtered = self.modify_tools(tools, state, profile)
        return [getattr(t, 'name', 'unknown') for t in filtered]
