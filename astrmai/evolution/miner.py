import json
import time
from typing import List, Dict, Any
from astrbot.api import logger
from ..infra.database import ExpressionPattern, MessageLog
from ..infra.gateway import GlobalModelGateway
from ..infra.datamodels import ExpressionPattern, MessageLog, Jargon

class ExpressionMiner:
    """
    风格挖掘器 (System 2 Offline Task)
    职责: 从历史消息中提炼 Expression Patterns
    Reference: Self_Learning/expression_pattern_learner.py
    """
    def __init__(self, gateway: GlobalModelGateway, config=None):
        self.gateway = gateway
        self.config = config if config else gateway.config
    async def mine(self, group_id: str, messages: List[MessageLog]) -> List[ExpressionPattern]:
        """
        执行挖掘任务：融合句式与黑话的双重提取 (Reference: expression_learner.py & jargon_miner.py)
        """
        # 接入 Config 阈值
        min_mining_context = self.config.evolution.min_mining_context
        if len(messages) < min_mining_context:
            return []

        # 1. 构建 Context
        context_str = self._build_context(messages)
        
        # [省略下方原有 _build_context 和 prompt 构建及调用等未修改的代码...]
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
        






    async def mine_jargons(self, group_id: str, messages: List[MessageLog]) -> List[Jargon]:
        """
        [新增] 从历史消息中挖掘群组黑话，并使用三步推断法尝试解析含义
        """
        min_mining_context = getattr(self.config.evolution, 'min_mining_context', 20)
        if len(messages) < min_mining_context:
            return []

        context_str = self._build_context(messages)
        
        # 步骤 1: 识别潜在黑话
        extract_prompt = f"""
{context_str}

请从上面这段群聊记录中，提取出群员频繁使用、具有特定小圈子含义的“黑话”、网络用语或特殊简称。
严格返回 JSON 格式的列表：
[
  {{
    "jargon": "黑话词汇",
    "raw_context": "包含该词汇的原话（用于后续推断）"
  }}
]
如果没有找到任何黑话，请返回 []。
"""
        try:
            raw_result = await self.gateway.call_planner(prompt=extract_prompt)
            jargon_candidates = self._parse_json(raw_result)
            
            jargons = []
            import time
            
            for item in jargon_candidates:
                word = item.get("jargon")
                raw_context = item.get("raw_context")
                if word and raw_context:
                    # 步骤 2 & 3: 三步推断法解析含义
                    inferred_data = await self._infer_jargon_meaning(word, raw_context)
                    
                    jargons.append(Jargon(
                        content=word,
                        raw_content=raw_context,
                        meaning=inferred_data.get("meaning", ""),
                        is_jargon=inferred_data.get("is_jargon", False),
                        is_complete=inferred_data.get("is_complete", False),
                        group_id=group_id,
                        created_at=time.time(),
                        updated_at=time.time()
                    ))
            return jargons
        except Exception as e:
            logger.error(f"[Evolution] 黑话挖掘异常: {e}")
            return []

    async def _infer_jargon_meaning(self, jargon_word: str, raw_context: str) -> dict:
        """
        [新增] 核心三步推断法 (融合上下文与基础词义)
        """
        infer_prompt = f"""
**待推断词条**: {jargon_word}
**出现的上下文**: {raw_context}

请执行黑话推断分析：
1. 分析它在上下文中的实际意图。
2. 结合常规网络用语知识，补全它的具体含义。
3. 最终核对：它是否是一个真正的“黑话”或“特殊用语”？

以 JSON 格式输出：
{{
  "meaning": "详细含义说明（包含使用场景、具体解释）",
  "is_jargon": true/false (是否确认为黑话),
  "is_complete": true/false (信息是否足够推断出明确含义)
}}
"""
        try:
            result = await self.gateway.call_judge(prompt=infer_prompt)
            import re
            match = re.search(r'\{.*\}', result, re.DOTALL)
            if match:
                data = json.loads(match.group(0))
                return {
                    "meaning": data.get("meaning", ""),
                    "is_jargon": bool(data.get("is_jargon", False)),
                    "is_complete": bool(data.get("is_complete", False))
                }
        except Exception as e:
            logger.debug(f"[Evolution] 黑话推断解析失败: {e}")
            
        return {"meaning": "", "is_jargon": False, "is_complete": False}        