### 📄 datamodels.py
# heartflow/datamodels.py
# (HeartCore 2.0 Update - Sensory & Goals)
import time
import asyncio
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional

@dataclass
class SensoryInput:
    """
    (2.0) 感官输入包
    将不同渠道的事件标准化，供 MindScheduler 调度
    """
    text: str
    images: List[str]  # 图片 URL 或路径列表
    sender_id: str
    sender_name: str
    group_id: str
    raw_event: Any     # 原始 AstrMessageEvent
    timestamp: float = field(default_factory=time.time)

    @classmethod
    def from_event(cls, event) -> 'SensoryInput':
        # 简单的图像提取逻辑 (需根据实际 adapter 调整)
        images = []
        if hasattr(event, "message_obj") and event.message_obj.message:
            for comp in event.message_obj.message:
                if comp.type == "image":
                    images.append(comp.url or comp.file)
        
        return cls(
            text=event.message_str or "",
            images=images,
            sender_id=event.get_sender_id(),
            sender_name=event.get_sender_name(),
            group_id=event.unified_msg_origin,
            raw_event=event
        )

@dataclass
class Goal:
    """(2.0) 对话目标"""
    id: str
    description: str
    status: str = "active" # active, completed, failed, pending
    created_at: float = field(default_factory=time.time)

@dataclass
class ImpulseDecision:
    """(2.0) 冲动引擎的决策输出"""
    action: str           # REPLY, WAIT, COMPLETE_TALK, IGNORE
    thought: str          # 内心独白
    goals_update: List[dict] = field(default_factory=list)
    params: Dict[str, Any] = field(default_factory=dict) # 如 wait_seconds

@dataclass
class ChatState:
    """
    群聊状态 (v4.14 + 2.0 Hybrid)
    """
    # --- Persistence ---
    energy: float = 1.0
    mood: float = 0.0
    group_config: Dict[str, Any] = field(default_factory=dict)
    last_reset_date: str = "" 
    
    # --- 2.0 New Fields ---
    current_goals: List[Goal] = field(default_factory=list)
    current_persona_mutation: str = "" # 当前激活的突变状态 (如 "moody")
    
    # --- Runtime ---
    last_reply_time: float = 0.0
    total_messages: int = 0
    total_replies: int = 0
    consecutive_reply_count: int = 0
    
    # 节流与复读 (保留逻辑用于兼容，但主要由 MindScheduler 接管)
    is_in_window_mode: bool = False
    window_remaining: int = 0
    
    # 双池 (2.0 在 MindScheduler 中直接操作这些池)
    accumulation_pool: List[SensoryInput] = field(default_factory=list) # 改存 SensoryInput
    background_buffer: List[SensoryInput] = field(default_factory=list) # 改存 SensoryInput
    
    # 锁
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    
    # 缓存管理
    is_dirty: bool = False
    last_access_time: float = field(default_factory=time.time)

@dataclass
class UserProfile:
    # (保持 v4.14 不变)
    user_id: str
    name: str
    social_score: float = 0.0
    last_seen: float = 0.0
    persona_analysis: str = ""
    identity: str = ""
    last_persona_gen_time: float = 0.0
    group_footprints: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    is_dirty: bool = False
    last_access_time: float = field(default_factory=time.time)