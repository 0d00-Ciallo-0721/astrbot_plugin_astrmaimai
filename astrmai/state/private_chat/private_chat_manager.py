# astrmai/Heart/private_chat_manager.py
"""
私聊专用会话管理器 (Private Chat Manager) — Phase 8.3
参考: MaiBot/heart_flow/heartFC_chat.py BrainChatting

核心功能: 为私聊实现 MaiBot 原版的 "等待→被打断→继续" 自然节奏。
         区别于群聊的被动响应，私聊应该像两个人真正在聊天一样，
         Bot 说完话后主动等待对方回复，新消息抵达时打断等待。

设计:
- 每个 user_id 维护一个 PrivateSession（含 asyncio.Event）
- Bot 说完话后调用 wait_for_new_message()，进入等待阻塞
- 新消息到来时调用 signal_new_message()，打断等待
- 超时（5分钟无回复）自动关闭当前等待，释放资源
"""
import asyncio
import time
from typing import Dict, Optional
from dataclasses import dataclass, field
from astrbot.api import logger


@dataclass
class PrivateSession:
    """单用户的私聊会话状态"""
    user_id: str
    new_message_event: asyncio.Event = field(default_factory=asyncio.Event)
    last_message_time: float = field(default_factory=time.time)
    is_bot_waiting: bool = False     # Bot 当前是否在等待对方回复
    pending_messages: list = field(default_factory=list)  # 等待期间积累的消息
    turn_count: int = 0              # 本轮对话的轮次数（用于判断亲密紧密度）


class PrivateChatManager:
    """
    私聊专用会话管理器
    
    使用方式:
    1. 在 AttentionGate 处理私聊消息时，调用 signal_new_message(user_id)
    2. 在 Bot 回复完成后，调用 wait_for_new_message(user_id) 等待对方
    3. 若超时返回 False，可触发主动破冰
    
    注意: 该管理器目前作为可选功能存在，
    需要在 AttentionGate 和 Replyer 中显式集成。
    """

    DEFAULT_TIMEOUT_SEC = 300.0   # 5 分钟无回复关闭等待
    MAX_SESSIONS = 100            # 最多同时维护的私聊会话数量

    def __init__(self, config=None):
        self.config = config
        self._sessions: Dict[str, PrivateSession] = {}
        self._cleanup_lock = asyncio.Lock()

        if config and hasattr(config, 'private_chat'):
            self.timeout_sec = config.private_chat.wait_timeout_sec
        else:
            self.timeout_sec = self.DEFAULT_TIMEOUT_SEC

    async def signal_new_message(self, user_id: str, message_str: str = ""):
        """
        外部触发: 有新消息到达私聊。
        
        打断 Bot 的等待状态，将消息加入积累池。
        """
        session = self._get_or_create_session(user_id)
        session.last_message_time = time.time()
        session.turn_count += 1
        
        if message_str:
            session.pending_messages.append(message_str)

        # 打断等待
        if session.is_bot_waiting:
            session.new_message_event.set()
            logger.debug(f"[PrivateChat] ✉️ 用户 {user_id} 的消息打断了 Bot 的等待")

    async def wait_for_new_message(
        self, user_id: str, timeout: Optional[float] = None
    ) -> bool:
        """
        Bot 说完话后进入等待，直到用户回复或超时。
        
        Args:
            user_id: 用户 ID
            timeout:  超时秒数（None 表示使用默认值）
            
        Returns:
            True  = 等到了用户的新消息（可继续对话）
            False = 超时（用户沉默）
        """
        session = self._get_or_create_session(user_id)
        wait_timeout = timeout if timeout is not None else self.timeout_sec

        # 重置等待事件
        session.new_message_event.clear()
        session.is_bot_waiting = True
        logger.debug(f"[PrivateChat] ⏳ Bot 等待用户 {user_id} 回复（超时: {wait_timeout}s）")

        try:
            # asyncio.wait_for 等待事件被触发，超时抛出 TimeoutError
            await asyncio.wait_for(session.new_message_event.wait(), timeout=wait_timeout)
            logger.debug(f"[PrivateChat] ✅ 用户 {user_id} 已回复，继续对话")
            return True
        except asyncio.TimeoutError:
            logger.info(f"[PrivateChat] ⌛ 用户 {user_id} 超过 {wait_timeout}s 未回复，沉默处理")
            return False
        finally:
            session.is_bot_waiting = False

    def get_pending_messages(self, user_id: str) -> list:
        """获取并清空等待期间积累的消息"""
        session = self._sessions.get(user_id)
        if not session:
            return []
        msgs = list(session.pending_messages)
        session.pending_messages.clear()
        return msgs

    def get_session_info(self, user_id: str) -> Optional[dict]:
        """获取当前会话状态信息"""
        session = self._sessions.get(user_id)
        if not session:
            return None
        return {
            "user_id": user_id,
            "turn_count": session.turn_count,
            "is_bot_waiting": session.is_bot_waiting,
            "last_message_time": session.last_message_time,
            "silence_sec": time.time() - session.last_message_time,
        }

    def close_session(self, user_id: str):
        """关闭并清理指定用户的会话"""
        if user_id in self._sessions:
            session = self._sessions.pop(user_id)
            session.new_message_event.set()  # 释放所有等待者
            logger.debug(f"[PrivateChat] 🔒 关闭用户 {user_id} 的私聊会话")

    async def cleanup_stale_sessions(self, max_silence_min: float = 30.0):
        """清理长时间无活动的私聊会话"""
        async with self._cleanup_lock:
            now = time.time()
            stale = []
            for uid, session in self._sessions.items():
                silence_min = (now - session.last_message_time) / 60.0
                if silence_min > max_silence_min and not session.is_bot_waiting:
                    stale.append(uid)
            for uid in stale:
                self.close_session(uid)
            if stale:
                logger.debug(f"[PrivateChat] 🧹 清理 {len(stale)} 个过期私聊会话")

    # ==========================================
    # 内部工具
    # ==========================================

    def _get_or_create_session(self, user_id: str) -> PrivateSession:
        if user_id not in self._sessions:
            # 超出上限时清理最旧的
            if len(self._sessions) >= self.MAX_SESSIONS:
                oldest = min(
                    self._sessions.items(),
                    key=lambda x: x[1].last_message_time
                )
                self.close_session(oldest[0])
            self._sessions[user_id] = PrivateSession(user_id=user_id)
        return self._sessions[user_id]
