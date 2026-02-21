import json
import time
from typing import List, Dict, Any
from astrbot.api import logger
from ..infra.database import ExpressionPattern, MessageLog
from ..infra.gateway import GlobalModelGateway
class ExpressionMiner:
    """
    风格挖掘器 (System 2 Offline Task)
    职责: 从历史消息中提炼 Expression Patterns
    Reference: Self_Learning/expression_pattern_learner.py
    """
    def __init__(self, gateway: GlobalModelGateway):
        self.gateway = gateway

    async def mine(self, group_id: str, messages: List[MessageLog]) -> List[ExpressionPattern]:
        """
        执行挖掘任务：融合句式与黑话的双重提取 (Reference: expression_learner.py & jargon_miner.py)
        """
        if len(messages) < 10:
            return []

        # 1. 构建 Context
        context_str = self._build_context(messages)
        
        # 2. 构建融合 Prompt
        prompt = f"""
{context_str}

请从上面这段群聊记录中，分析并概括除了人名为"SELF"（也就是你自己）之外的用户的语言风格和专属黑话。
任务1（语言风格）：总结特定的表达规律，例如"当[某场景]时，喜欢说[某句话]"。
任务2（群组黑话）：提取群友发明的特殊词汇、简称或梗，并解释其场景。

严格返回 JSON 数组格式，每个对象包含 situation（场景/梗的含义）和 expression（表达方式/黑话词汇）：
[
    {{"situation": "打招呼或者赞同别人时", "expression": "确实"}},
    {{"situation": "群友发送好笑的事情时", "expression": "草"}},
    {{"situation": "用来指代游戏里的某个特定BOSS", "expression": "大鸟"}}
]
"""
        try:
            # 借用 System 1 (Judge) 的低成本请求跑离线任务
            raw_result = await self.gateway.call_planner(prompt=prompt)
            patterns_data = self._parse_json(raw_result)
            
            patterns = []
            import time

            for item in patterns_data:
                if "situation" in item and "expression" in item:
                    patterns.append(ExpressionPattern(
                        situation=item["situation"],
                        expression=item["expression"],
                        group_id=group_id,
                        weight=1.0,
                        last_active_time=time.time(),
                        create_time=time.time()
                    ))
            return patterns
            
        except Exception as e:
            logger.error(f"[Evolution] 风格与黑话挖掘失败: {e}")
            return []

    def _build_context(self, messages: List[MessageLog]) -> str:
        lines = []
        for msg in messages:
            # 简单清洗
            content = msg.content.strip()
            if not content or content.startswith("[") or len(content) > 100:
                continue
            lines.append(f"{msg.sender_name}: {content}")
        return "\n".join(lines)

    def _parse_json(self, text: str) -> List[Dict]:
        """简易 JSON 提取"""
        import re
        try:
            match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
            json_str = match.group(1) if match else text
            return json.loads(json_str)
        except:
            return []