### 📄 features/poke_handler.py
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, filter as event_filter
from astrbot.api.star import Context

from ..config import HeartflowConfig
from ..datamodels import SensoryInput
# 注意：这里不再导入 ReplyEngine 或 BrainPlanner

class PokeHandler:
    """
    (v2.0) 触觉传感器
    职责：监听戳一戳事件 -> 转化为 SensoryInput -> 发送给神经中枢
    """

    def __init__(self, 
                 context: Context, 
                 config: HeartflowConfig,
                 scheduler # 传入 MindScheduler
                 ):
        self.context = context
        self.config = config
        self.scheduler = scheduler

    @event_filter.event_message_type(event_filter.EventMessageType.ALL)
    async def on_poke(self, event: AstrMessageEvent):
        """
        监听戳一戳
        """
        # 基础过滤
        if not self.config.enable_heartflow:
            return

        # 检查是否戳了机器人
        # 注意：不同平台的戳一戳事件结构可能不同，这里假设已通过 adapter 标准化
        # 或者在 filter 中已经过滤了 target
        
        # 1. 构造触觉信号
        # 我们伪造一个文本描述，方便 LLM 理解
        sender_name = event.get_sender_name()
        tactile_text = f"[{sender_name} 戳了你一下]"
        
        sensory = SensoryInput.from_event(event)
        sensory.text = tactile_text
        # 可以在 SensoryInput 中扩展一个 type 字段，或者利用 text 标记
        
        logger.info(f"👉 [Tactile] Detected poke from {sender_name}")

        # 2. 发送给神经中枢
        # 戳一戳通常具有打断性，直接进入调度
        # 注意：这里调用的是 scheduler 的 on_message 或专门的 on_sensory_input
        # 为了复用逻辑，我们直接复用 on_message (它会处理 SensoryInput)
        
        # 由于 SensoryInput.from_event 已经封装了 event，
        # 我们需要一种方式将修改后的 text 传进去，或者修改 SensoryInput 结构
        # 这里为了简单，我们直接修改 event 的 message_str (如果 MindScheduler 允许)
        # 或者 MindScheduler 应该提供一个直接接收 SensoryInput 的接口
        
        # 调用调度器的底层分发接口
        session_id = event.unified_msg_origin
        chat_state = await self.scheduler.state_manager.get_chat_state(session_id)
        
        await self.scheduler.dispatch(session_id, sensory, chat_state)