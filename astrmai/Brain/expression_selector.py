# astrmai/Brain/expression_selector.py
"""
表达习惯选择器 (Expression Selector) — Phase 6.1
参考: MaiBot/bw_learner/expression_selector.py

职责: 从 ExpressionPattern DB 中读取已学到的表达方式，
      按语境匹配后注入到 System Prompt，让 Bot 说话风格鲜活有个性。

设计:
- think_level=0 (快速/默认): 随机抽 weight-top5，直接拼接注入，零 LLM
- think_level=1 (深度):    top5高权重 + 随机5个 → 打乱 → 1次LLM选出最匹配

注入格式 (模仿 MaiBot):
  在回复时,你可以参考以下的语言习惯，不要生硬使用：
  当[situation]时：[expression]
"""
import random
import asyncio
from typing import List, Optional
from astrbot.api import logger
from ..infra.database import DatabaseService
from ..infra.datamodels import ExpressionPattern
from ..infra.gateway import GlobalModelGateway
from ..infra.lane_manager import LaneKey


class ExpressionSelector:
    """
    表达习惯选择器
    
    学习→存储 链路: ExpressionMiner → ExpressionPattern DB ← ExpressionReflector(审计)
    读取→注入 链路: ExpressionSelector (本模块) → ContextEngine → System Prompt
    """

    # think_level=0 时注入的条数上限
    FAST_SELECT_LIMIT = 5
    # think_level=1 时候选池大小
    DEEP_CANDIDATE_LIMIT = 10
    EXPRESSION_SYSTEM_PROMPT = "浣犳槸琛ㄨ揪椋庢牸鍖归厤鍣紝闇€瑕佷粠鍊欓€夎〃杈句腑鎸戦€夊綋鍓嶈澧冩渶鑷劧鐨勫嚑鏉°€?"

    def __init__(self, db: DatabaseService, gateway: GlobalModelGateway, config=None):
        self.db = db
        self.gateway = gateway
        self.config = config if config else gateway.config

    async def select(
        self,
        chat_id: str,
        context_text: str = "",
        think_level: int = 0,
    ) -> str:
        """
        主入口: 选择表达习惯并返回格式化的注入文本。

        Args:
            chat_id:      当前会话 ID (用于分组查询)
            context_text: 最近几条对话内容 (level=1 时供 LLM 匹配用)
            think_level:  0=快速随机抽取  1=LLM 深度匹配

        Returns:
            格式化的表达习惯提示文本，空字符串表示无可用数据
        """
        try:
            if think_level == 0:
                return await self._fast_select(chat_id)
            else:
                return await self._deep_select(chat_id, context_text)
        except Exception as e:
            logger.warning(f"[ExpressionSelector] 选择表达习惯失败: {e}")
            return ""

    # ==========================================
    # think_level=0: 快速模式（零 LLM）
    # ==========================================

    async def _fast_select(self, chat_id: str) -> str:
        """
        快速模式: 按 weight 倒序取 top-N，随机打乱后取前5条注入。
        零 LLM 消耗。
        """
        # 利用 DB 异步接口按权重排序获取
        patterns = await asyncio.to_thread(
            self.db.get_patterns, chat_id, 20  # 取前20个高权重
        )

        if not patterns:
            return ""

        # 随机打乱，提高多样性（避免每次都是相同几条）
        sample_size = min(self.FAST_SELECT_LIMIT, len(patterns))
        selected = random.sample(patterns, sample_size)

        return self._format_habits(selected)

    # ==========================================
    # think_level=1: 深度模式（1次 LLM）
    # ==========================================

    async def _deep_select(self, chat_id: str, context_text: str) -> str:
        """
        深度模式: 高权重 Top5 + 全量随机5个 → 打乱10个候选 → LLM选最匹配的3条。
        共消耗 1 次 LLM 调用。
        """
        # 高权重 top5
        top_patterns = await asyncio.to_thread(self.db.get_patterns, chat_id, 5)
        # 全量随机池
        all_patterns = await asyncio.to_thread(self.db.get_patterns, chat_id, 50)

        if not top_patterns and not all_patterns:
            return ""

        # 随机补充
        random_pool = [p for p in all_patterns if p not in top_patterns]
        random_supplement = random.sample(
            random_pool, min(5, len(random_pool))
        )

        candidates = top_patterns + random_supplement
        random.shuffle(candidates)

        if not candidates:
            return ""

        # 只有候选数量足够才值得调 LLM
        if len(candidates) <= 3 or not context_text:
            return self._format_habits(candidates[:3])

        selected = await self._llm_pick_best(chat_id, candidates, context_text)
        return self._format_habits(selected)

    async def _llm_pick_best(
        self, chat_id: str, candidates: List[ExpressionPattern], context_text: str
    ) -> List[ExpressionPattern]:
        """
        用 1 次 LLM 从候选中选出最符合当前语境的表达习惯。
        """
        candidates_desc = "\n".join(
            [f"{i+1}. 当[{p.situation}]时：{p.expression}"
             for i, p in enumerate(candidates)]
        )

        prompt = f"""当前对话语境：
"{context_text[-300:]}"

以下是可供参考的语言习惯候选列表：
{candidates_desc}

请从以上候选中，选出最适合当前语境、最自然、最有个性的 3 条（用序号回答，用逗号分隔，例如：1,3,5）："""

        try:
            result = await self.gateway.call_data_process_task(
                prompt,
                system_prompt=self.EXPRESSION_SYSTEM_PROMPT,
                is_json=False,
                lane_key=LaneKey(subsystem="sys2", task_family="expression", scope_id=chat_id),
                base_origin=chat_id,
            )
            result_str = str(result).strip()

            # 解析序号
            import re
            nums = re.findall(r'\d+', result_str)
            selected_indices = [int(n) - 1 for n in nums if 0 < int(n) <= len(candidates)]

            if selected_indices:
                return [candidates[i] for i in selected_indices[:3]]
        except Exception as e:
            logger.debug(f"[ExpressionSelector] LLM 匹配失败，降级为随机: {e}")

        # 降级: 随机取3条
        return random.sample(candidates, min(3, len(candidates)))

    # ==========================================
    # 格式化工具
    # ==========================================

    @staticmethod
    def _format_habits(patterns: List[ExpressionPattern]) -> str:
        """
        将选中的表达模式格式化为 MaiBot 兼容的注入文本。
        """
        if not patterns:
            return ""

        lines = ["在回复时,你可以参考以下的语言习惯，不要生硬使用："]
        for p in patterns:
            lines.append(f"当{p.situation}时：{p.expression}")

        result = "\n".join(lines)
        logger.debug(f"[ExpressionSelector] 💬 注入 {len(patterns)} 条表达习惯")
        return result
