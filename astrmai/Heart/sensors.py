from astrbot.api.event import AstrMessageEvent
from astrbot.api import logger
import astrbot.api.message_components as Comp

class PreFilters:
    """
    感知与过滤器 (System 1)
    Reference: HeartCore/core/message_handler.py (Step 0-2)
    """
    def __init__(self, config):
        self.config = config

    def is_noise(self, event: AstrMessageEvent) -> bool:
        """
        判断是否为噪音 (指令、重复消息等)
        """
        # 1. 过滤指令 (AstrBot 通常已处理，但为了保险)
        if event.message_str.startswith("/"):
            return True

        # 2. 简易复读机检测 (这里简化实现，复杂逻辑可由 StateEngine 维护 last_msg_hash)
        # TODO: 实现 last_msg_content 对比

        return False

    def is_wakeup_signal(self, event: AstrMessageEvent, bot_self_id: str) -> bool:
        """
        检测是否为强唤醒信号 (@Bot 或 包含昵称)
        Reference: MessageHandler._check_wakeup
        """
        # 1. 检测 At
        for component in event.message_obj.message:
            if isinstance(component, Comp.At):
                 if str(component.qq) == str(bot_self_id):
                     return True
        
        # 2. 检测昵称 (需从配置获取)
        # nicknames = self.config.get('nicknames', [])
        # for name in nicknames:
        #     if name in event.message_str:
        #         return True

        return False