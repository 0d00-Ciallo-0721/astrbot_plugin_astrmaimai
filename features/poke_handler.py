# heartflow/features/poke_handler.py
# (v16.1 修复 - 移除已废弃的 force_reply_bonus_score 引用)
import time
import json
import random
from astrbot.api import logger
from astrbot.api.star import Context
from astrbot.api.event import AstrMessageEvent, filter as event_filter

# (v13.0) 导入更新后的 BrainActionPlan
from ..datamodels import BrainActionPlan, ChatState, UserProfile
from ..config import HeartflowConfig
from ..core.state_manager import StateManager
from ..core.reply_engine import ReplyEngine
from ..persistence import PersistenceManager

class PokeHandler:
    """
    (v13.0) 戳一戳处理器
    职责：负责处理 on_poke 事件
    """

    def __init__(self, 
                 context: Context, 
                 config: HeartflowConfig, 
                 state_manager: StateManager,
                 reply_engine: ReplyEngine,
                 persistence: PersistenceManager
                 ):
        self.context = context
        self.config = config
        self.state_manager = state_manager
        self.reply_engine = reply_engine
        self.persistence = persistence

    @event_filter.event_message_type(event_filter.EventMessageType.ALL)
    async def on_poke(self, event: AstrMessageEvent):
        """
        (v13.0) 50% 戳回 (停止)，50% 转交大脑 (继续)
        """
        if not self.config.enable_poke_response or event.get_platform_name() != "aiocqhttp":
            return
        
        if event.get_platform_name() not in ["aiocqhttp", "onebot"]:
            return
        
        raw_message = getattr(event.message_obj, "raw_message", None)

        if (not raw_message or
            raw_message.get('post_type') != 'notice' or
            raw_message.get('notice_type') != 'notify' or
            raw_message.get('sub_type') != 'poke'):
            return

        bot_id = raw_message.get('self_id')
        sender_id = raw_message.get('user_id')
        target_id = raw_message.get('target_id')
        group_id = raw_message.get('group_id')

        if not bot_id or not sender_id or not target_id or str(target_id) != str(bot_id):
            return

        chat_id = event.unified_msg_origin
        logger.info(f"🔥 [群聊] 心流检测到戳一戳 | 来自: {sender_id}")

        if sender_id in self.config.user_blacklist:
            logger.debug(f"戳一戳来自黑名单 {sender_id}，忽略。")
            return
        
        sender_name = event.get_sender_name() or sender_id
        
        if random.random() < 0.5:
            # --- 分支 B (50%)：反戳回复 ---
            try:
                # [修改] 更安全的调用方式
                client = getattr(event, 'bot', None)
                if client and hasattr(client, 'api'):
                    # --- [修改点 1] 区分群聊和私聊，补全 group_id ---
                    if group_id:
                        # 如果是群聊，必须带上 group_id
                        await client.api.call_action('send_poke', user_id=int(sender_id), group_id=int(group_id))
                    else:
                        # 私聊则只需要 user_id
                        await client.api.call_action('send_poke', user_id=int(sender_id))
                        
                    logger.info(f"🔥 [群聊] 反戳成功")
                else:
                    logger.warning("PokeHandler: 无法获取 bot 实例，跳过反戳")
            except Exception as e:
                logger.warning(f"反戳失败: {e}")

            # (v13.0) 创建一个 BrainActionPlan
            poke_plan = BrainActionPlan(
                thought="Poke Event (Branch B: Poke Back)",
                action="IGNORE" # Action 是 IGNORE 因为没有文本回复
            )
            user_poke_text = f"[{sender_name} 戳了你一下]"
            
            # (v11.0) 使用新的签名
            self.state_manager._update_active_state(event, poke_plan, is_poke_or_nickname=True)
            
            # (v11.0 保持不变) 保存戳一戳历史
            await self.persistence.save_history_message(
                chat_id, "user", user_poke_text, 
                self.reply_engine.bot_name, sender_name=sender_name
            )
            
            # --- [修改点 2] 定义缺失的 reply_placeholder 变量 ---
            reply_placeholder = "[戳了戳]" 
            
            await self.persistence.save_history_message(
                chat_id, "assistant", reply_placeholder, self.reply_engine.bot_name
            )
            
            event.stop_event() # 必须停止
            return
            
        else:
            # --- 分支 A (50%)：文本回复 (v16.1 修复) ---
            logger.info(f"🔥 [群聊] 心流触发回复 (Poke：转交标准流，添加奖励分)")
            
            # (v16.1 修复) 不再读取 config.force_reply_bonus_score
            # 直接设置 1.0 作为标志
            event.set_extra("heartflow_bonus_score", 1.0)
            event.set_extra("heartflow_is_poke_event", True)
            event.set_extra("heartflow_poke_sender_name", sender_name)
            
            # 必须 *不* 停止事件，让 main.py -> message_handler.py 接管
            return