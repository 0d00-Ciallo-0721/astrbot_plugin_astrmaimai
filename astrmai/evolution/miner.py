import json
import time
from typing import List, Dict, Any
from astrbot.api import logger
from astrmai.infra.database import ExpressionPattern, MessageLog
from astrmai.infra.gateway import GlobalModelGateway

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
        执行挖掘任务
        """
        if len(messages) < 10:
            return []

        # 1. 构建 Context
        context_str = self._build_context(messages)
        
        # 2. 构建 Prompt (源自 Self_Learning)
        prompt = f"""
{context_str}

请从上面这段群聊中概括除了人名为"SELF"之外的人的语言风格
1. 只考虑文字，不要考虑表情包和图片
2. 不要涉及具体的人名，但是可以涉及具体名词  
3. 思考有没有特殊的梗，一并总结成语言风格
4. 例子仅供参考，请严格根据群聊内容总结!!!

注意：总结成如下格式的规律，总结的内容要详细，但具有概括性：
例如：当"AAAAA"时，可以"BBBBB", AAAAA代表某个具体的场景，不超过20个字。BBBBB代表对应的语言风格，特定句式或表达方式，不超过20个字。

例如：
当"对某件事表示十分惊叹"时，使用"我嘞个xxxx"
当"表示讽刺的赞同，不讲道理"时，使用"对对对"
当"想说明某个具体的事实观点，但懒得明说"时，使用"懂的都懂"
当"涉及游戏相关时，夸赞，略带戏谑意味"时，使用"这么强！"

请注意：不要总结你自己（SELF）的发言，尽量保证总结内容的逻辑性。
返回 JSON 格式: [{{"situation": "...", "expression": "..."}}, ...]
"""
        # 3. 调用 LLM (使用 System 2 模型以保证质量)
        # 注意: 这里我们请求 JSON 格式以便于解析
        try:
            raw_result = await self.gateway.call_planner(messages=[{"role": "user", "content": prompt}])
            patterns_data = self._parse_json(raw_result)
            
            patterns = []
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
            logger.error(f"[Evolution] Mining failed: {e}")
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