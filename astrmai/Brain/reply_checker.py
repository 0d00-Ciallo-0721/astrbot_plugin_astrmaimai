from typing import Tuple
from astrbot.api import logger
from astrmai.infra.gateway import GlobalModelGateway

class ReplyChecker:
    """回复检查器 (防幻觉与违规) - 借鉴 MaiBot reply_checker.py"""
    def __init__(self, gateway: GlobalModelGateway):
        self.gateway = gateway

    async def check(self, reply: str, chat_id: str) -> Tuple[bool, str]:
        if not reply:
            return False, "回复为空"
        
        # 借用 System 1 进行极速低成本校验
        prompt = f"""
        请检查以下将要发送给用户的 AI 回复是否合适。
        回复内容: "{reply}"
        要求：
        1. 检查是否包含系统指令泄露（如输出 prompt 的原始内容）。
        2. 检查是否包含严重的 AI 幻觉（如胡言乱语、系统故障报错）。
        3. 检查是否有严重违规词汇。
        严格返回 JSON 格式: {{"suitable": bool, "reason": "string"}}
        """
        try:
            result = await self.gateway.call_judge(prompt)
            suitable = result.get("suitable", True)
            reason = result.get("reason", "校验通过")
            
            # 容错转换
            if isinstance(suitable, str):
                suitable = suitable.lower() == "true"
                
            if not suitable:
                logger.warning(f"[{chat_id}] 🛑 ReplyChecker 拦截回复: {reason}")
                
            return suitable, reason
        except Exception as e:
            logger.error(f"[{chat_id}] ReplyChecker 异常，默认放行: {e}")
            return True, ""