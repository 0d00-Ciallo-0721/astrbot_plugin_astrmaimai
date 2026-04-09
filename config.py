from pydantic import BaseModel, Field
from typing import List,Dict


class ProviderConfig(BaseModel):
    fallback_models: List[str] = Field(default=[])
    agent_models: List[str] = Field(default=[])
    task_models: List[str] = Field(default=[])
    vision_models: List[str] = Field(default=[], description="多模态视觉专属模型池 (如 gpt-4o / gemini-1.5-pro)")
    embedding_models: List[str] = Field(default=[])

class GlobalSettingsConfig(BaseModel):
    debug_mode: bool = Field(default=False)
    command_prefixes: List[str] = Field(default=["/", "!", "！"])
    whitelist_ids: List[str] = Field(default=[])
    # [新增] 全局私聊总开关，默认关闭
    enable_private_chat: bool = Field(default=False)
    # [新增] 管理员配置与错误拦截
    admin_ids: List[str] = Field(default=[])
    enable_error_interception: bool = Field(default=True)

class PersonaConfig(BaseModel):
    # [修改] 增加 description 描述，明确 ID 为空时的默认行为
    persona_id: str = Field(default="", description="人设唯一ID。不填则默认为当前对话ID（实现千人千面）。若填写则强制绑定该ID（实现单一人设）。")

class AgentConfig(BaseModel):
    max_steps: int = Field(default=5)
    timeout: int = Field(default=60)

class PerformanceConfig(BaseModel):
    summary_threshold: int = Field(default=300)

class System1Config(BaseModel):
    wakeup_words: List[str] = Field(default=[])
    nicknames: List[str] = Field(default=[])
    extra_command_list: List[str] = Field(default=[])
    # [新增 P1-T2] 关键词反应规则
    keyword_reactions: List[str] = Field(
        default=[],
        description="关键词反应规则列表，格式: '关键词:反应描述'，例如 '原神:你是原神重度玩家，听到这个词会特别兴奋'"
    )

class AttentionConfig(BaseModel):
    debounce_window: float = Field(default=2.0)
    bg_pool_size: int = Field(default=20)
    # [新增] 节流与复读控制
    throttle_probability: float = Field(default=0.1)
    throttle_min_entropy: int = Field(default=2)
    repeater_threshold: int = Field(default=3)
    max_message_length: int = Field(default=100)
    focus_thread_enabled: bool = Field(default=True, description="鍚敤 Focus Thread 绠楁硶锛屽湪绐楀彛鍐呴€夋嫨涓荤嚎绋嬩綔涓烘湰杞富鍥炲簲鐩爣")
    focus_thread_core_max_messages: int = Field(default=4, description="Focus Thread 涓婚棶棰樻牳蹇冩秷鎭殑鏈€澶氭暟閲?")
    focus_thread_related_max_messages: int = Field(default=3, description="Focus Thread 鐩稿叧琛ュ厖娑堟伅鐨勬渶澶氭暟閲?")
    ambient_background_max_messages: int = Field(default=2, description="鐜鑳屾櫙娑堟伅鐨勬渶澶氭敞鍏ユ潯鏁?")
    thread_same_speaker_followup_sec: int = Field(default=8, description="鍚屼竴鐢ㄦ埛杩炵画琛ュ厖娑堟伅浠嶈涓哄悓涓€绾跨▼鐨勬椂闂寸獥鍙?(绉?)")
    thread_reply_priority_enabled: bool = Field(default=True, description="鏄惁璁╁洖澶?/@/鍞ら啋 bot 鐨勬秷鎭綔涓?Focus Thread 鐨勬渶楂樹紭鍏堢骇")
    
    # === [新增] 情绪归因启发式算法超参数 ===
    affection_weights: Dict[str, float] = Field(default={"trigger": 20.0, "window": 50.0, "history": 30.0})
    adjudication_threshold: float = Field(default=50.0)
    sensitive_words: List[str] = Field(default=["傻逼", "弱智", "滚", "死", "妈", "废物", "神经", "有病"])

class EnergyConfig(BaseModel):
    min_reply_threshold: float = Field(default=0.1)
    cost_per_reply: float = Field(default=0.05)
    daily_recovery: float = Field(default=0.2)
    recovery_silence_min: int = Field(default=60)

class MoodConfig(BaseModel):
    decay_interval: int = Field(default=3600)
    decay_rate: float = Field(default=0.1)
    unknown_decay: float = Field(default=0.1)

class EvolutionConfig(BaseModel):
    min_mining_context: int = Field(default=10)
    batch_size: int = Field(default=50)
    mining_trigger: int = Field(default=20)
    mining_window_sec: int = Field(default=60, description="学习触发的时间窗长度(秒)")
    mining_window_min_messages: int = Field(default=20, description="单个时间窗内触发学习所需的最少消息数")
    mining_cooldown_sec: int = Field(default=60, description="同一会话两次学习触发之间的冷却时间(秒)")
    review_batch_size: int = Field(default=10, description="每轮自动审核表达条目的最大数量")
    review_min_count: int = Field(default=2, description="表达进入自动审核前所需的最少命中次数")
    enable_expression_mining: bool = Field(default=True, description="启动表达习惯的挖掘反思与模仿")
    enable_relationship_engine: bool = Field(default=True, description="启动好感度四维关系图谱推演")

class LifeConfig(BaseModel):
    silence_threshold: int = Field(default=120)
    wakeup_min_energy: float = Field(default=0.6)
    wakeup_cost: float = Field(default=0.2)
    wakeup_cooldown: int = Field(default=28800)
    profiling_msg_threshold: int = Field(default=50)
    dream_interval_min: int = Field(default=30, description="后台触发梦境整理记忆的周期(分钟)")
    dream_time_ranges: List[str] = Field(default_factory=list, description="允许触发 dream 的时间段列表，格式 HH:MM-HH:MM")
    min_memory_events_to_dream: int = Field(default=5, description="进入 dream 整理前需要的最少长期记忆事件数")
    dream_visible: bool = Field(default=False, description="是否将梦境文本主动发送给指定会话")
    dream_send_target: str = Field(default="", description="梦境可见时的目标会话 ID，留空则发送回当前 dream session")
    
class ReplyConfig(BaseModel):
    fallback_text: str = Field(default="（陷入了短暂的沉默...）")
    base_frequency: float = Field(default=0.7, description="算法流基础连发跟进概率")
    follow_up_probability: float = Field(default=0.2, description="AI 在首轮回复后继续追发一句的概率门控 (0.0~1.0)")
    segment_min_len: int = Field(default=15)
    no_segment_max_len: int = Field(default=120)
    meme_probability: int = Field(default=60)
    # [新增] 对齐 _conf_schema.json 中的 reply 节点
    emotion_mapping: List[str] = Field(default=[
        "happy: 积极、开心、感谢",
        "sad: 悲伤、遗憾、道歉",
        "angry: 生气、抱怨、攻击",
        "neutral: 平静、客观、陈述",
        "curious: 好奇、提问、困惑",
        "surprise: 惊讶、意外"
    ])
    typing_speed_factor: float = Field(default=0.1)

class MemoryConfig(BaseModel):
    time_decay_rate: float = Field(default=0.01)
    cleanup_interval: int = Field(default=3600)
    summary_threshold: int = Field(default=30)
    recall_top_k: int = Field(default=5)
    enable_react_agent: bool = Field(default=True, description="启用 ReActAgent 多轮记忆检索")
    prune_threshold: float = Field(default=0.2, description="记忆遗忘被物理剪枝的得分下限")

class InfraConfig(BaseModel):
    llm_retries: int = Field(default=2)
    backoff_factor: float = Field(default=1.5)
    api_timeout: float = Field(default=15.0, description="网关级绝对超时时间(秒)，超时后强制中断 API 请求")
    max_concurrent_llm_calls: int = Field(default=3, description="全局 LLM 并发请求上限，防止后台任务雪崩导致 429")

class VisionConfig(BaseModel):
    enable_vision: bool = Field(default=True, description="多模态视觉总开关")
    image_recognition_probability: float = Field(default=0.5, description="图片被送入视觉皮层解析的概率 (0.0~1.0)")

class Sys3Settings(BaseModel):
    enable_work_mode: bool = Field(default=False, description="是否启用 Sys3 工作任务模式")

class PrivateChatConfig(BaseModel):
    wait_timeout_sec: int = Field(default=300, description="单次私聊等待反馈强制休眠阈值(秒)")

class AstrMaiConfig(BaseModel):
    provider: ProviderConfig = Field(default_factory=ProviderConfig)
    global_settings: GlobalSettingsConfig = Field(default_factory=GlobalSettingsConfig)
    persona: PersonaConfig = Field(default_factory=PersonaConfig)
    agent: AgentConfig = Field(default_factory=AgentConfig)
    performance: PerformanceConfig = Field(default_factory=PerformanceConfig)
    system1: System1Config = Field(default_factory=System1Config)
    attention: AttentionConfig = Field(default_factory=AttentionConfig)
    energy: EnergyConfig = Field(default_factory=EnergyConfig)
    mood: MoodConfig = Field(default_factory=MoodConfig)
    evolution: EvolutionConfig = Field(default_factory=EvolutionConfig)
    life: LifeConfig = Field(default_factory=LifeConfig)
    reply: ReplyConfig = Field(default_factory=ReplyConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    infra: InfraConfig = Field(default_factory=InfraConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    sys3: Sys3Settings = Field(default_factory=Sys3Settings) # 🟢 [新增] 挂载 Sys3 配置
    private_chat: PrivateChatConfig = Field(default_factory=PrivateChatConfig)
