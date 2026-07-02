from pydantic import BaseModel, Field
from typing import Dict, List


LEGACY_MEMORY_NAMESPACE_FIELDS = (
    "deep_temporal_alpha",
    "deep_temporal_tau_seconds",
    "deep_temporal_lambda_default",
    "deep_temporal_lambda_fact",
    "deep_temporal_candidate_pool_factor",
    "deep_temporal_candidate_pool_min",
    "deep_temporal_llm_window",
    "maintenance_hot_beta",
    "maintenance_temporal_stale_hot_threshold",
)


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
    enable_private_chat: bool = Field(default=False)
    admin_ids: List[str] = Field(default=[])
    enable_error_interception: bool = Field(default=True)
    external_result_sources: List[str] = Field(default=["astrbot_builtin"], description="外部插件结果白名单，仅嗅探这些来源的输出。'*' 表示所有来源")
    error_interception_mode: str = Field(default="block_only", description="错误拦截模式：block_and_stop(阻止+停止事件) / block_only(仅阻止) / log_only(仅日志)")


class PersonaConfig(BaseModel):
    persona_id: str = Field(
        default="",
        description="人设唯一ID。不填则默认为当前对话ID（实现千人千面）。若填写则强制绑定该ID（实现单一人设）。",
    )
    include_self_lore_in_prompt: bool = Field(default=False, description="是否在系统提示词中自动注入 self_lore 知识")


class AgentConfig(BaseModel):
    max_steps: int = Field(default=5, ge=1)
    timeout: int = Field(default=60, ge=1)


class PerformanceConfig(BaseModel):
    summary_threshold: int = Field(default=300, ge=1)


class System1Config(BaseModel):
    wakeup_words: List[str] = Field(default=[])
    nicknames: List[str] = Field(default=[])
    extra_command_list: List[str] = Field(default=[])
    keyword_reactions: List[str] = Field(
        default=[],
        description="关键词反应规则列表，格式: '关键词:反应描述'，例如 '原神:你是原神重度玩家，听到这个词会特别兴奋'",
    )


class AttentionConfig(BaseModel):
    debounce_window: float = Field(default=2.0, ge=0.0)
    bg_pool_size: int = Field(default=20, ge=1)
    throttle_probability: float = Field(default=0.1, ge=0.0, le=1.0)
    throttle_min_entropy: int = Field(default=2, ge=0)
    repeater_threshold: int = Field(default=3, ge=1)
    max_message_length: int = Field(default=100, ge=1)
    focus_thread_enabled: bool = Field(default=True, description="启用 Focus Thread 算法，在窗口内选择主线程作为本轮回复目标")
    focus_thread_core_max_messages: int = Field(default=4, description="Focus Thread 核心消息的最大条数")
    focus_thread_related_max_messages: int = Field(default=3, description="Focus Thread 相关补充消息的最大条数")
    ambient_background_max_messages: int = Field(default=2, description="环境背景消息的最大注入条数")
    thread_same_speaker_followup_sec: int = Field(default=8, description="同一用户连续补充消息仍视为同一线程的时间窗口（秒）")
    thread_reply_priority_enabled: bool = Field(default=True, description="是否让回复/@/唤醒 bot 的消息拥有 Focus Thread 的最高优先级")
    affection_weights: Dict[str, float] = Field(default={"trigger": 20.0, "window": 50.0, "history": 30.0})
    adjudication_threshold: float = Field(default=50.0)
    sensitive_words: List[str] = Field(default=["傻逼", "弱智", "滚", "死", "妈", "废物", "神经", "有病"], description="情感路由权重词：当 Bot 情绪为愤怒/悲伤且消息含这些词时，该发言者获得更高情感权重。注意：这不是内容安全过滤，不会拦截消息。")


class EnergyConfig(BaseModel):
    min_reply_threshold: float = Field(default=0.1, ge=0.0, le=1.0)
    cost_per_reply: float = Field(default=0.05, ge=0.0, le=1.0)
    daily_recovery: float = Field(default=0.2, ge=0.0, le=1.0)
    recovery_silence_min: int = Field(default=60, ge=0)


class MoodConfig(BaseModel):
    decay_interval: int = Field(default=3600, ge=1)
    decay_rate: float = Field(default=0.1, ge=0.0, le=1.0)
    unknown_decay: float = Field(default=0.1, ge=0.0, le=1.0)


class EvolutionConfig(BaseModel):
    min_mining_context: int = Field(default=10, ge=1)
    batch_size: int = Field(default=50, ge=1)
    mining_trigger: int = Field(default=20, ge=1)
    mining_window_sec: int = Field(default=60, ge=0, description="学习触发的时间窗长度(秒)")
    mining_window_min_messages: int = Field(default=20, ge=1, description="单个时间窗内触发学习所需的最少消息数")
    mining_cooldown_sec: int = Field(default=60, ge=0, description="同一会话两次学习触发之间的冷却时间(秒)")
    review_batch_size: int = Field(default=10, ge=1, description="每轮自动审核表达条目的最大数量")
    review_min_count: int = Field(default=2, ge=1, description="表达进入自动审核前所需的最少命中次数")
    enable_expression_mining: bool = Field(default=True, description="启动表达习惯的挖掘反思与模仿")
    enable_relationship_engine: bool = Field(default=True, description="启动好感度四维关系图谱推演")
    jargon_min_count: int = Field(default=2, ge=1, description="黑话进入自动审核前所需的最少证据次数")
    review_runner_interval_sec: int = 60
    review_runner_min_interval_sec: int = 45


class LifeConfig(BaseModel):
    enable_proactive: bool = Field(default=True, description="是否启用主动发言功能")
    proactive_quiet_hours: List[str] = Field(
        default_factory=lambda: ["23:30-07:30"],
        description="主动开口安静时段列表，格式 HH:MM-HH:MM；留空表示关闭 quiet hours",
    )
    silence_threshold: int = Field(default=120, ge=0)
    wakeup_min_energy: float = Field(default=0.6, ge=0.0, le=1.0)
    wakeup_cost: float = Field(default=0.2, ge=0.0, le=1.0)
    wakeup_cooldown: int = Field(default=28800, ge=0)
    profiling_msg_threshold: int = Field(default=50, ge=1)
    dream_interval_min: int = Field(default=30, ge=1, description="后台触发梦境整理记忆的周期(分钟)")
    dream_time_ranges: List[str] = Field(default_factory=list, description="允许触发 dream 的时间段列表，格式 HH:MM-HH:MM")
    min_memory_events_to_dream: int = Field(default=5, ge=1, description="进入 dream 整理前需要的最少长期记忆事件数")
    dream_visible: bool = Field(default=False, description="是否将梦境文本主动发送给指定会话")
    dream_send_target: str = Field(default="", description="梦境可见时的目标会话 ID，留空则发送回当前 dream session")


class ReplyConfig(BaseModel):
    fallback_text: str = Field(default="（陷入了短暂的沉默...）", description="当回复流程整体失败时使用的兜底文本")
    base_frequency: float = Field(default=0.7, ge=0.0, le=1.0, description="Bot 在普通场景下主动接话的积极程度")
    follow_up_probability: float = Field(default=0.2, ge=0.0, le=1.0, description="首条回复发出后，继续自然补一句的概率 (0.0~1.0)")
    stale_reply_max_age_sec: float = Field(default=0.0, ge=0.0, description="允许聊天回复保留时效性的最长秒数；0 表示自动按系统超时推导")
    segment_min_len: int = Field(default=15, ge=1, description="允许拆成多条发送前，单条内容至少要达到的长度")
    no_segment_max_len: int = Field(default=120, ge=1, description="不超过这个长度时，尽量作为一条完整消息发出")
    meme_probability: int = Field(default=60, ge=0, le=100, description="在适合的场景下附带表情包的概率百分比")
    emotion_mapping: List[str] = Field(
        default=[
            "happy: 积极、开心、感谢",
            "sad: 悲伤、遗憾、道歉",
            "angry: 生气、抱怨、攻击",
            "neutral: 平静、客观、陈述",
            "curious: 好奇、提问、困惑",
            "surprise: 惊讶、意外",
        ]
    )
    typing_speed_factor: float = Field(default=0.1, ge=0.0, description="模拟打字等待的强度系数，越大看起来越像在慢慢打字")
    enable_content_safety_filter: bool = Field(default=False, description="启用基础内容安全过滤（NSFW/自残/PII 检测）")


class ConversationConfig(BaseModel):
    enable_dialogue_store: bool = Field(default=True)
    enable_context_compaction: bool = Field(default=True)
    enable_prefix_caching: bool = Field(default=True)
    hot_zone_ttl_seconds: float = Field(default=30.0, ge=0.0)
    warm_zone_ttl_seconds: float = Field(default=300.0, ge=0.0)
    warm_zone_max_tokens: int = Field(default=1200, ge=1)
    compaction_provider_id: str = Field(default="")
    compaction_trigger_segments: int = Field(default=40, ge=1)
    compaction_trigger_tokens: int = Field(default=1800, ge=1)
    compaction_keep_recent_segments: int = Field(default=16, ge=1)
    compaction_summary_max_tokens: int = Field(default=450, ge=1)
    enable_token_estimator: bool = False


class MemoryConfig(BaseModel):
    time_decay_rate: float = Field(default=0.01, ge=0.0, le=1.0)
    cleanup_interval: int = Field(default=3600, ge=1)
    summary_threshold: int = Field(default=30, ge=1)
    recall_top_k: int = Field(default=5, ge=1)
    enable_react_agent: bool = Field(default=True, description="启用 ReActAgent 多轮记忆检索")
    prune_threshold: float = Field(default=0.2, ge=0.0, le=1.0, description="记忆遗忘被物理剪枝的得分下限")
    min_memory_confidence: float = Field(default=0.3, ge=0.0, le=1.0, description="记忆写入最低置信度，低于此值的记忆不持久化")


    deep_temporal_alpha: float = Field(default=0.7, ge=0.0, le=1.0)
    deep_temporal_tau_seconds: float = Field(default=86400.0, ge=0.0)
    deep_temporal_lambda_default: float = Field(default=1.0, ge=0.0)
    deep_temporal_lambda_fact: float = Field(default=0.1, ge=0.0)
    deep_temporal_candidate_pool_factor: int = Field(default=4, ge=1)
    deep_temporal_candidate_pool_min: int = Field(default=20, ge=1)
    deep_temporal_llm_window: int = Field(default=8, ge=1)
    maintenance_hot_beta: float = Field(default=0.7, ge=0.0, le=1.0)
    maintenance_temporal_stale_hot_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    auto_recall_probability: float = 0.0


class InfraConfig(BaseModel):
    llm_retries: int = Field(default=2, ge=0)
    backoff_factor: float = Field(default=1.5, ge=0.0)
    api_timeout: float = Field(default=15.0, ge=1.0, description="网关级绝对超时时间(秒)，超时后强制中断 API 请求")
    max_concurrent_llm_calls: int = Field(default=3, ge=1, description="全局 LLM 并发请求上限，防止后台任务雪崩导致 429")
    rate_limit_model_cooldown_sec: int = Field(default=120, ge=0, description="模型触发 429/rate limit 后的运行期冷却时间（秒）")
    quota_model_cooldown_sec: int = Field(default=1800, ge=0, description="模型触发 403/配额/权限失败后的运行期冷却时间（秒）")


class VisionConfig(BaseModel):
    enable_vision: bool = Field(default=True, description="多模态视觉总开关")
    image_recognition_probability: float = Field(default=0.5, ge=0.0, le=1.0, description="图片被送入视觉皮层解析的概率 (0.0~1.0)")
    use_native_main_reply_vision: bool = Field(
        default=False,
        description="主回复原生识图直通开关，仅当当前主回复模型支持原生图片输入时再开启；插件不会自动判断模型能力。",
    )
    native_main_reply_failure_cooldown_sec: int = Field(
        default=180, ge=0,
        description="主回复原生识图失败后的会话冷却时间(秒)。",
    )


class Sys3Settings(BaseModel):
    enable_work_mode: bool = Field(default=False, description="是否启用 Sys3 工作任务模式")
    computer_agent_sandbox_enabled: bool = Field(
        default=False,
        description="是否启用 ComputerAgent 的代码执行能力（需管理员权限）。开启后 ComputerAgent 才能加载 Python/Shell 工具。注意：此功能在宿主机直执，请仅在受信任环境开启。",
    )


class PrivateChatConfig(BaseModel):
    wait_timeout_sec: int = Field(default=300, ge=1, description="单次私聊等待反馈强制休眠阈值(秒)")


class AstrMaiConfig(BaseModel):
    @staticmethod
    def _normalize_legacy_memory_namespace(data: dict) -> dict:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        global_settings = normalized.get("global_settings")
        memory = normalized.get("memory")
        if not isinstance(global_settings, dict):
            global_settings = {}
        if memory is None:
            memory = {}
        elif not isinstance(memory, dict):
            return normalized
        memory_values = dict(memory)
        updated = False
        for field_name in LEGACY_MEMORY_NAMESPACE_FIELDS:
            if field_name in memory_values or field_name not in global_settings:
                continue
            memory_values[field_name] = global_settings[field_name]
            updated = True
        if updated or ("memory" in normalized and isinstance(memory, dict)):
            normalized["memory"] = memory_values
        return normalized

    def __init__(self, **data):
        super().__init__(**self._normalize_legacy_memory_namespace(data))
        # ── 互斥配置检测 ──
        if getattr(self.sys3, "enable_work_mode", False) and not self.provider.agent_models:
            from astrbot.api import logger
            logger.warning("[AstrMai] Sys3 work mode enabled but agent_models is empty — work mode will silently fail")
        if getattr(self.vision, "enable_vision", True) and not self.provider.vision_models:
            from astrbot.api import logger
            logger.warning("[AstrMai] Vision enabled but vision_models is empty — image recognition will silently fail")
        # ── 格式校验 ──
        for entry in self.reply.emotion_mapping:
            if ":" not in entry:
                from astrbot.api import logger as _log
                _log.warning(f"[AstrMai] emotion_mapping entry missing colon: {entry!r}")
        for pool_name, pool in [("agent", self.provider.agent_models), ("vision", self.provider.vision_models)]:
            for model in pool:
                if "/" not in str(model):
                    from astrbot.api import logger as _log
                    _log.warning(f"[AstrMai] model in {pool_name}_models missing provider prefix: {model!r}")

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
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    infra: InfraConfig = Field(default_factory=InfraConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    sys3: Sys3Settings = Field(default_factory=Sys3Settings)
    private_chat: PrivateChatConfig = Field(default_factory=PrivateChatConfig)

