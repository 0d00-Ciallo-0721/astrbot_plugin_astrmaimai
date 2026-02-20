import json
import re
import time
from typing import List, Dict, Any, Tuple
from json_repair import repair_json
from astrbot.api import logger

from astrmai.infra.gateway import GlobalModelGateway
from .context_engine import ContextEngine

class Planner:
    """
    规划器 (System 2)
    职责: CoT 规划, JSON 提取, ReAct 循环
    Reference: Maibot/brain_planner.py
    """
    def __init__(self, gateway: GlobalModelGateway, context_engine: ContextEngine):
        self.gateway = gateway
        self.context_engine = context_engine

    async def plan(self, 
                   chat_id: str, 
                   event_messages: List[Any], # 聚合的消息列表
                   tools_map: Dict[str, Any]) -> Dict[str, Any]:
        """
        生成行动计划
        Returns: {
            "thought": str,
            "action": "reply" | "tool_use" | "wait",
            "args": dict
        }
        """
        # 1. 构建 Prompt
        # TODO: 将 event_messages 转换为文本历史
        context_str = "\n".join([f"{m.get_sender_name()}: {m.message_str}" for m in event_messages])
        
        # 简单构建 Tool 描述
        tool_descs = "\n".join([f"- {name}: {info}" for name, info in tools_map.items()])
        
        system_prompt = await self.context_engine.build_prompt(chat_id, [], tool_descs)
        
        full_prompt = f"{system_prompt}\n\nUser Messages:\n{context_str}\n\nThink and Respond:"

        # 2. 调用 System 2 (Brain)
        raw_response = await self.gateway.call_planner(
            messages=[{"role": "user", "content": full_prompt}]
        )

        # 3. 解析响应 (提取 JSON)
        return self._parse_response(raw_response)

    def _parse_response(self, content: str) -> Dict[str, Any]:
        """
        移植自 Maibot BrainPlanner._extract_json_from_markdown
        """
        try:
            # 1. 尝试提取 ```json ... ```
            match = re.search(r"```json\s*(.*?)\s*```", content, re.DOTALL)
            json_str = match.group(1) if match else content

            # 2. 修复 JSON
            json_str = repair_json(json_str)
            
            # 3. 解析
            data = json.loads(json_str)
            
            # 兼容列表或字典
            if isinstance(data, list): data = data[0]
            
            return {
                "thought": data.get("reason", "No thought provided"),
                "action": data.get("action", "reply"),
                "args": data
            }

        except Exception as e:
            logger.warning(f"[Planner] JSON Parse Failed: {e}. Fallback to direct reply.")
            return {
                "thought": "Failed to parse JSON, treating as direct reply.",
                "action": "reply",
                "args": {"content": content} # 假设整个内容就是回复
            }