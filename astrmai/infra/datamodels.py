# astrmai/infra/datamodels.py
import time
import asyncio
from dataclasses import dataclass, field
from typing import List, Dict, Any, Optional
from sqlmodel import SQLModel, Field

# ==========================================
# 1. 强类型 DB 模型 (SQLModel) 
# 供 Persistence 及 Memory/Evolution 使用
# ==========================================

class LastMessageMetadataDB(SQLModel, table=True):
    """[新增] 多模态记忆与上下文回溯表"""
    __table_args__ = {"extend_existing": True}
    id: Optional[int] = Field(default=None, primary_key=True)
    chat_id: str = Field(index=True)
    sender_id: str
    has_image: bool = Field(default=False)
    image_urls: str = Field(default="[]") # JSON list
    vl_executed: bool = Field(default=False)
    timestamp: float = Field(default_factory=time.time)

class ExpressionPattern(SQLModel, table=True):
    """表达模式表 (潜意识挖掘的黑话与句式)"""
    __table_args__ = {"extend_existing": True}
    id: Optional[int] = Field(default=None, primary_key=True)
    situation: str = Field(index=True)  
    expression: str                     
    weight: float = Field(default=1.0)  
    last_active_time: float = Field(default_factory=time.time)
    create_time: float = Field(default_factory=time.time)
    group_id: str = Field(index=True)

class MessageLog(SQLModel, table=True):
    """短期滚动消息日志 (用于后台离线挖掘)"""
    __table_args__ = {"extend_existing": True}
    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: str = Field(index=True)
    sender_id: str
    sender_name: str
    content: str
    timestamp: float = Field(default_factory=time.time)
    processed: bool = Field(default=False)


# ==========================================
# 2. 运行时内存模型 (@dataclass)
# 挂载锁、脏数据标记等不可持久化对象
# ==========================================

@dataclass
class LastMessageMetadata:
    sender_id: str = ""
    has_image: bool = False
    image_urls: List[str] = field(default_factory=list)
    vl_executed: bool = False 

@dataclass
class BrainActionPlan:
    """
    大脑行动计划 (System 1 & 2 通用决策载体)
    """
    action: str = "IGNORE"  # REPLY, WAIT, IGNORE, SUMMARIZE_REPLY
    thought: str = "..."
    
    # 理性评分 (1-10)
    relevance: int = 0      # 话题相关性
    necessity: int = 0      # 回复必要性
    confidence: int = 0     # 执行自信度
    
    # 携带的元数据 (如情绪标签)
    meta: Dict[str, Any] = field(default_factory=dict)

    def should_act(self) -> bool:
        return self.action in ["REPLY", "WAIT", "SUMMARIZE_REPLY"]

@dataclass
class ChatState:
    """群聊状态 (内存对象，通过 Persistence 映射到 SQLite)"""
    # --- 持久化对齐字段 ---
    chat_id: str = ""
    energy: float = 0.5
    mood: float = 0.0
    group_config: Dict[str, Any] = field(default_factory=dict)
    last_reset_date: str = ""
    total_replies: int = 0
    
    # --- 运行时管理字段 ---
    last_reply_time: float = 0.0
    total_messages: int = 0
    judgment_mode: str = "single"
    accumulation_pool: List[Any] = field(default_factory=list)
    background_buffer: List[Any] = field(default_factory=list)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    last_msg_info: LastMessageMetadata = field(default_factory=LastMessageMetadata)
    # --- Phase 6: Lifecycle Fields ---
    next_wakeup_timestamp: float = 0.0 # 下次计划唤醒时间
    last_passive_decay_time: float = 0.0 # 上次自然衰减时间
    # --- 缓存控制 ---
    is_dirty: bool = False
    last_access_time: float = field(default_factory=time.time)

@dataclass
class UserProfile:
    """用户画像 (内存对象)"""
    user_id: str = ""   # 用户id
    name: str = "Unknown"# 用户姓名
    social_score: float = 0.0 #用户好感度
    last_seen: float = 0.0  #上次看见的时间
    persona_analysis: str = "" # 深度心理侧写
    message_count_for_profiling: int = 0 # 距离上次画像生成后的消息数
    last_persona_gen_time: float = 0.0 # 上次生成时间
    identity: str = "" #身份
    #运行时字段
    group_footprints: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    
    is_dirty: bool = False
    last_access_time: float = field(default_factory=time.time)

class Jargon(SQLModel, table=True):
    """[新增] 群组黑话与网络用语表"""
    __table_args__ = {"extend_existing": True}
    id: Optional[int] = Field(default=None, primary_key=True)
    content: str = Field(index=True)        # 词条内容
    raw_content: str = Field(default="")    # 推断用的原始上下文
    meaning: str = Field(default="")        # 词条解释
    is_jargon: bool = Field(default=False)  # 是否确认为黑话
    count: int = Field(default=1)           # 出现频次
    is_complete: bool = Field(default=False)# 是否完成推断
    group_id: str = Field(index=True)
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

class SocialRelation(SQLModel, table=True):
    """[新增] 群组内成员社交关系图谱表"""
    __table_args__ = {"extend_existing": True}
    id: Optional[int] = Field(default=None, primary_key=True)
    group_id: str = Field(index=True)
    from_user: str = Field(index=True)      # 发起互动的用户
    to_user: str = Field(index=True)        # 接收互动的用户
    relation_type: str = Field(default="interaction") # 关系类型(如:mention, reply)
    strength: float = Field(default=0.0)    # 关系强度(0-1)
    frequency: int = Field(default=0)       # 互动频次
    last_interaction: float = Field(default_factory=time.time)    