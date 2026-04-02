# astrmai/Brain/goal_manager.py
"""
多目标管理器 (Multi-Goal Manager)
参考: MaiBot/PFC/pfc.py -> GoalAnalyzer

核心设计:
- 每个 chat_id 维护最多 3 个并行目标
- 新目标与旧目标进行字符重叠相似度检测，超过 0.6 则合并替换
- 目标附带"活跃度"，连续 3 轮未被 LLM 提及则自然消亡
- 支持"结束对话"特殊目标作为中止信号
"""
import time
import asyncio
import json
import re
from typing import Dict, List, Optional
from dataclasses import dataclass, field
from astrbot.api import logger


@dataclass
class ConversationGoal:
    """单个对话目标"""
    goal: str                    # 目标描述 (一句话)
    reasoning: str               # 设定原因
    created_at: float = field(default_factory=time.time)
    last_referenced: float = field(default_factory=time.time)
    stale_count: int = 0         # 连续未被提及次数

    @property
    def is_expired(self) -> bool:
        """目标过期判定: 超过 3 轮未被提及"""
        return self.stale_count >= 3


class GoalManager:
    """
    聊天级多目标管理器
    遵循 AstrBot 插件规范：使用 GlobalModelGateway 进行 LLM 调用。
    """

    MAX_GOALS = 3
    SIMILARITY_MERGE_THRESHOLD = 0.6

    def __init__(self, gateway, config=None):
        self.gateway = gateway
        self.config = config if config else gateway.config
        self._goals: Dict[str, List[ConversationGoal]] = {}  # chat_id -> goals
        self._locks: Dict[str, asyncio.Lock] = {}
        self._lock_mutex = asyncio.Lock()

    async def _get_lock(self, chat_id: str) -> asyncio.Lock:
        """安全获取或创建群组级锁"""
        async with self._lock_mutex:
            if chat_id not in self._locks:
                self._locks[chat_id] = asyncio.Lock()
            return self._locks[chat_id]

    def get_goals_context(self, chat_id: str) -> str:
        """
        供 ContextEngine 调用，编织目标上下文段。
        返回: 可直接注入 System Prompt 的文本块。
        """
        goals = self._goals.get(chat_id, [])
        if not goals:
            return "当前没有明确的对话目标，你可以根据对话自然发展。"

        lines = []
        for i, g in enumerate(goals, 1):
            age_info = ""
            if g.stale_count > 0:
                age_info = f" [已{g.stale_count}轮未提及]"
            lines.append(f"目标{i}: {g.goal} (原因: {g.reasoning}){age_info}")
        return "你当前正在推进的对话目标:\n" + "\n".join(lines)

    def has_end_signal(self, chat_id: str) -> bool:
        """检查是否有"结束对话"专用目标"""
        for g in self._goals.get(chat_id, []):
            if "结束对话" in g.goal:
                return True
        return False

    async def analyze_and_update(self, chat_id: str, recent_messages: str) -> str:
        """
        核心入口: 分析当前对话并更新目标集
        返回: 主要目标的文本描述
        """
        lock = await self._get_lock(chat_id)
        async with lock:
            current_goals = self._goals.get(chat_id, [])
            goals_str = self._format_goals_for_prompt(current_goals)

            prompt = f"""请分析以下聊天记录，确定当前最适合的对话目标。你可以：
1. 保持现有目标不变
2. 修改现有目标（话题转变时）
3. 添加新目标（发现新的对话线索时）
4. 删除不再相关的目标
5. 如果你想结束对话，设置 goal 为"结束对话"

当前目标:
{goals_str}

最近对话:
{recent_messages[-2000:]}

严格返回 JSON 数组，最多 {self.MAX_GOALS} 个目标:
[{{"goal": "简短目标 (不超过20字)", "reasoning": "原因"}}]"""

            try:
                result = await self.gateway.call_data_process_task(
                    prompt=prompt, is_json=True
                )
                new_goals = self._parse_goals(result)

                if new_goals:
                    # 老化处理: 未被新一轮提及的旧目标 stale_count +1
                    self._age_unreferenced_goals(chat_id, new_goals)

                    # 合并逻辑: 新目标与旧目标相似度检查
                    merged = self._merge_goals(current_goals, new_goals)
                    self._goals[chat_id] = merged[:self.MAX_GOALS]

                    # 清除过期目标
                    self._goals[chat_id] = [
                        g for g in self._goals[chat_id] if not g.is_expired
                    ]

                    primary = self._goals[chat_id][0] if self._goals[chat_id] else None
                    if primary:
                        logger.info(f"[GoalManager] ✅ 目标更新: {chat_id} -> 主目标: {primary.goal} (共 {len(self._goals[chat_id])} 个)")
                        return primary.goal

            except Exception as e:
                logger.error(f"[GoalManager] 目标分析失败: {e}")

            return "陪伴用户，提供有趣且连贯的对话"

    def clear_goals(self, chat_id: str):
        """清除指定聊天的所有目标 (当对话结束时调用)"""
        self._goals.pop(chat_id, None)
        logger.debug(f"[GoalManager] 已清除 {chat_id} 的所有目标")

    # ==========================================
    # 内部算法
    # ==========================================

    @staticmethod
    def _calculate_similarity(text1: str, text2: str) -> float:
        """字符重叠相似度 (参考 MaiBot pfc.py _calculate_similarity)"""
        chars1 = set(text1)
        chars2 = set(text2)
        overlap = len(chars1 & chars2)
        total = len(chars1 | chars2)
        return overlap / total if total > 0 else 0

    def _merge_goals(
        self, old: List[ConversationGoal], new: List[ConversationGoal]
    ) -> List[ConversationGoal]:
        """合并策略: 新目标优先，与旧目标超过相似度阈值的旧目标被替换"""
        result = list(new)  # 新目标优先入列
        for old_goal in old:
            is_merged = False
            for new_goal in new:
                if self._calculate_similarity(old_goal.goal, new_goal.goal) > self.SIMILARITY_MERGE_THRESHOLD:
                    is_merged = True
                    break
            if not is_merged:
                result.append(old_goal)
        return result

    def _age_unreferenced_goals(self, chat_id: str, new_goals: List[ConversationGoal]):
        """对未在新一轮中被提及的旧目标执行老化"""
        new_texts = {g.goal for g in new_goals}
        for g in self._goals.get(chat_id, []):
            # 模糊匹配: 新目标中是否有与此旧目标相似度超过 0.5 的
            matched = any(
                self._calculate_similarity(g.goal, nt) > 0.5 for nt in new_texts
            )
            if matched:
                g.stale_count = 0
                g.last_referenced = time.time()
            else:
                g.stale_count += 1

    def _parse_goals(self, raw) -> List[ConversationGoal]:
        """安全解析 LLM 返回的目标列表"""
        items = []
        if isinstance(raw, list):
            items = raw
        elif isinstance(raw, str):
            match = re.search(r'\[.*\]', raw, re.DOTALL)
            if match:
                try:
                    items = json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass

        goals = []
        for item in items:
            if isinstance(item, dict) and item.get("goal"):
                goals.append(ConversationGoal(
                    goal=str(item["goal"])[:30],  # 强制限长
                    reasoning=str(item.get("reasoning", ""))
                ))
        return goals

    def _format_goals_for_prompt(self, goals: List[ConversationGoal]) -> str:
        if not goals:
            return "暂无明确目标。"
        return "\n".join(
            f"- {g.goal} (原因: {g.reasoning})" for g in goals
        )
