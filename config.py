from pydantic import BaseModel, Field
from typing import Dict, List

try:
    from .astrmai.shared.emotion_tags import build_emotion_tag_catalog
except ImportError:  # pragma: no cover - standalone config/test import
    from astrmai.shared.emotion_tags import build_emotion_tag_catalog


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

LEGACY_TIMING_NAMESPACE_FIELDS = (
    ("model_request_timeout_sec", "infra", "api_timeout"),
    ("reply_max_age_sec", "reply", "stale_reply_max_age_sec"),
    ("agent_execution_timeout_sec", "agent", "timeout"),
    ("workmode_execution_timeout_sec", "sys3", "tool_timeout"),
    ("attention_judge_timeout_sec", "attention", "judge_timeout"),
    ("private_wait_timeout_sec", "private_chat", "wait_timeout_sec"),
    ("private_input_settle_sec", "private_chat", "input_settle_sec"),
    ("image_resolve_timeout_sec", "private_chat", "image_resolve_timeout_sec"),
    ("image_analysis_timeout_sec", "private_chat", "image_barrier_timeout_sec"),
)


def load_astrmai_config(raw_config: dict | None) -> "AstrMaiConfig":
    """OPT-10/PL-06: 坏配置降级加载而非整插件拒载。

    旧行为：任一越界值（负超时、超范围概率等）触发 ValidationError → 插件下线、
    所有会话失去响应，错误只在框架日志。现改为：剔除违例字段回退默认并逐项
    ERROR 告警；剔除后仍失败则整体回退默认配置。
    """
    from pydantic import ValidationError

    data = dict(raw_config or {})
    try:
        return AstrMaiConfig(**data)
    except ValidationError as exc:
        try:
            from astrbot.api import logger
        except Exception:  # pragma: no cover - 独立脚本环境
            import logging

            logger = logging.getLogger("astrmai.config")
        pruned = dict(data)
        for error in exc.errors():
            loc = [str(part) for part in (error.get("loc") or ())]
            if not loc:
                continue
            section = loc[0]
            if len(loc) == 1:
                dropped = pruned.pop(section, None)
            else:
                section_data = pruned.get(section)
                if not isinstance(section_data, dict):
                    dropped = pruned.pop(section, None)
                else:
                    section_data = dict(section_data)
                    dropped = section_data.pop(loc[1], None)
                    pruned[section] = section_data
            logger.error(
                f"[AstrMai] 配置项 {'.'.join(loc)} 非法（{error.get('msg', '')}），"
                f"已剔除并回退默认值；原值预览: {str(dropped)[:80]}"
            )
        try:
            return AstrMaiConfig(**pruned)
        except ValidationError:
            logger.error("[AstrMai] 剔除违例字段后配置仍不自洽，整体回退默认配置")
            return AstrMaiConfig()


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
    component_max_retries: int = Field(default=3, ge=1, le=10, description="每个人格生成步骤单轮最多重试次数")
    retry_interval_sec: float = Field(default=15.0, ge=1.0, description="人格初始化失败后的首次重试间隔")
    retry_max_interval_sec: float = Field(default=300.0, ge=1.0, description="人格初始化连续失败时的最大重试间隔")


class AgentConfig(BaseModel):
    # OPT-10/PL-11: executor 硬下限为 5（安全底线），声明与行为对齐——
    # 旧 ge=1 允许 UI 设 1-4 但实际无效
    max_steps: int = Field(default=5, ge=5)
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
    # OPT-10/PL-05: debounce_window/throttle_*/repeater_threshold/max_message_length
    # 为功能重构后的死配置（防抖硬编码分档、限流改能量驱动、复读阈值硬编码 3），
    # 已随 schema 一并删除——UI 不再展示无效承诺
    judge_timeout: float = Field(default=3.0, ge=0.1, description="System1 Judge attention gate timeout in seconds")
    bg_pool_size: int = Field(default=20, ge=1)
    focus_thread_enabled: bool = Field(default=True, description="启用 Focus Thread 算法，在窗口内选择主线程作为本轮回复目标")
    focus_thread_core_max_messages: int = Field(default=4, description="Focus Thread 核心消息的最大条数")
    focus_thread_related_max_messages: int = Field(default=3, description="Focus Thread 相关补充消息的最大条数")
    ambient_background_max_messages: int = Field(default=2, description="环境背景消息的最大注入条数")
    thread_same_speaker_followup_sec: int = Field(default=8, description="同一用户连续补充消息仍视为同一线程的时间窗口（秒）")
    thread_reply_priority_enabled: bool = Field(default=True, description="是否让回复/@/唤醒 bot 的消息拥有 Focus Thread 的最高优先级")
    affection_weights: Dict[str, float] = Field(default={"trigger": 20.0, "window": 50.0, "history": 30.0})
    adjudication_threshold: float = Field(default=50.0)
    mood_post_judge_enabled: bool = Field(
        default=True,
        description="情绪感知后置：仅对判定为回复的消息更新情绪（省去最终被忽略消息的 mood LLM 调用）",
    )
    private_skip_judge_enabled: bool = Field(
        default=True,
        description="私聊跳过 judge 判决（合并窗+settle 已承担等待职能，judge 在私聊近乎恒 REPLY 纯增延迟）",
    )
    judge_ignore_focus_cooldown_enabled: bool = Field(
        default=True,
        description="被判决忽略的消息在后续批次降权，避免同一条消息被反复判决（强唤醒不受影响）",
    )
    judge_ignore_focus_penalty: int = Field(
        default=150,
        ge=0,
        le=1000,
        description="每被忽略一轮的焦点扣分（0 等同关闭降权）",
    )
    participation_policy_enabled: bool = Field(
        default=True,
        description="启用群聊参与评分与短期承接观测",
    )
    participation_force_pass_enabled: bool = Field(
        default=True,
        description="高置信明确互动跳过 Judge，直接进入回复流程",
    )
    participation_drop_enabled: bool = Field(
        default=False,
        description="高置信无关消息直接丢弃；默认关闭并仅做影子观测",
    )
    participation_hysteresis_ttl_sec: int = Field(
        default=180,
        ge=10,
        le=1800,
        description="已参与话题和当前对象的短期承接时间（秒）",
    )
    cognitive_loop_min_think_level: int = Field(
        default=2,
        ge=1,
        le=3,
        description="cognitive_loop 无条件放行的最低 think 等级；低于该级仅在长句/复杂度信号时运行",
    )
    sensitive_words: List[str] = Field(default=["傻逼", "弱智", "滚", "死", "妈", "废物", "神经", "有病"], description="情感路由权重词：当 Bot 情绪为愤怒/悲伤且消息含这些词时，该发言者获得更高情感权重。注意：这不是内容安全过滤，不会拦截消息。")


class EnergyConfig(BaseModel):
    min_reply_threshold: float = Field(default=0.1, ge=0.0, le=1.0)
    cost_per_reply: float = Field(default=0.05, ge=0.0, le=1.0)
    daily_recovery: float = Field(default=0.2, ge=0.0, le=1.0)
    recovery_silence_min: int = Field(default=60, ge=0)


class MoodConfig(BaseModel):
    decay_interval: int = Field(default=3600, ge=1)
    decay_rate: float = Field(default=0.1, ge=0.0, le=1.0)


class EvolutionConfig(BaseModel):
    min_mining_context: int = Field(default=10, ge=1, description="兼容旧版的学习触发原始消息阈值")
    batch_size: int = Field(default=50, ge=1)
    mining_trigger: int = Field(default=20, ge=1)
    mining_window_sec: int = Field(default=60, ge=0, description="学习触发的时间窗长度(秒)")
    mining_window_min_messages: int = Field(default=20, ge=1, description="单个时间窗内触发学习所需的最少消息数")
    mining_cooldown_sec: int = Field(default=60, ge=0, description="同一会话两次学习触发之间的冷却时间(秒)")
    review_batch_size: int = Field(default=10, ge=1, description="每轮自动审核表达条目的最大数量")
    review_min_count: int = Field(default=2, ge=1, description="表达进入自动审核前所需的最少命中次数")
    expression_min_count: int = Field(default=2, ge=1, description="表达候选进入模型增强前所需的最少独立证据次数")
    expression_min_distinct_turns: int = Field(default=3, ge=2, description="群聊表达进入学习前所需的最少不同消息证据数")
    expression_min_valid_messages: int = Field(default=30, ge=3, le=500, description="表达管线每轮所需的最少有效群聊消息数")
    expression_evidence_replay_messages: int = Field(default=300, ge=0, le=5000, description="首次启用独立表达游标时回放的最近群聊消息数")
    expression_overlap_messages: int = Field(default=30, ge=0, le=500, description="表达管线成功后保留用于跨批识别的消息数")
    enable_expression_mining: bool = Field(default=True, description="启动表达习惯的挖掘反思与模仿")
    jargon_min_count: int = Field(default=2, ge=1, description="黑话进入自动审核前所需的最少证据次数")
    jargon_min_valid_messages: int = Field(default=20, ge=2, le=500, description="黑话管线每轮所需的最少有效消息数")
    jargon_overlap_messages: int = Field(default=10, ge=0, le=200, description="黑话管线成功后保留用于跨批去重的消息数")
    learning_pipeline_max_failures: int = Field(default=3, ge=1, le=20, description="单条学习管线连续失败后进入隔离的次数")
    learning_pipeline_quarantine_sec: int = Field(default=3600, ge=60, le=86400, description="学习管线连续失败后的隔离时间")
    learning_pipeline_timeout_sec: float = Field(default=60.0, ge=10.0, le=300.0, description="单轮表达或黑话学习管线的共享总时间预算")
    learning_run_retention_days: int = Field(default=30, ge=1, le=365, description="学习运行诊断记录的保留天数")
    learning_run_max_per_pipeline_chat: int = Field(default=500, ge=10, le=10000, description="每条学习管线每个会话最多保留的运行诊断记录数")
    review_runner_interval_sec: int = Field(default=60, ge=30, le=600)
    review_runner_min_interval_sec: int = Field(default=45, ge=15)
    enable_backlog_mining: bool = Field(default=True, description="启用低频积压消息学习扫描")
    backlog_scan_interval_sec: int = Field(default=900, ge=60, description="积压学习扫描间隔(秒)")
    backlog_min_unprocessed_logs: int = Field(default=40, ge=1, description="单个会话触发积压学习的最少未处理消息数")
    backlog_batch_size: int = Field(default=120, ge=1, description="单次积压学习最多处理的消息数")
    backlog_group_limit: int = Field(default=2, ge=1, description="每轮积压学习最多处理的会话数")
    backlog_failure_cooldown_sec: int = Field(default=1800, ge=60, description="积压学习失败后的会话冷却时间(秒)")


class LifeConfig(BaseModel):
    enable_proactive: bool = Field(default=True, description="是否启用主动发言功能")
    enable_private_proactive: bool = Field(default=True, description="是否允许在私聊中主动发言")
    enable_group_proactive: bool = Field(default=True, description="是否允许在群聊中主动发言")
    proactive_quiet_hours: List[str] = Field(
        default_factory=lambda: ["23:30-07:30"],
        description="主动开口安静时段列表，格式 HH:MM-HH:MM；留空表示关闭 quiet hours",
    )
    silence_threshold: int = Field(default=120, ge=0)
    wakeup_min_energy: float = Field(default=0.6, ge=0.0, le=1.0)
    wakeup_cost: float = Field(default=0.2, ge=0.0, le=1.0)
    wakeup_cooldown: int = Field(default=28800, ge=0)
    proactive_max_unanswered: int = Field(default=2, ge=0, le=20, description="连续主动发言未获用户回应的上限")
    proactive_failure_retry_sec: int = Field(default=900, ge=10, le=86400, description="主动发言失败后的重试间隔（秒）")
    proactive_claim_lease_sec: int = Field(default=300, ge=10, le=3600, description="主动任务领取租约时长（秒）")
    scheduled_scenarios_enabled: bool = Field(default=False, description="启用日程、问候、节日和天气主动候选")
    scheduled_scenarios_allow_inactive_chat: bool = Field(default=False, description="允许定时场景在会话不活跃时进入注意力判决")
    daily_schedule_enabled: bool = Field(default=True, description="为角色生成并持久化七时段日程")
    daily_schedule_ai_enabled: bool = Field(default=True, description="使用后台任务模型生成日程；失败时使用固定兜底日程")
    morning_greeting_enabled: bool = Field(default=True, description="在早安窗口产生一次主动问候候选")
    morning_greeting_time: str = Field(default="08:00", description="早安候选开始时间，格式 HH:MM")
    morning_greeting_window_min: int = Field(default=90, ge=5, le=360, description="早安候选有效窗口（分钟）")
    night_greeting_enabled: bool = Field(default=True, description="在晚安窗口产生一次主动问候候选")
    night_greeting_time: str = Field(default="22:30", description="晚安候选开始时间，格式 HH:MM")
    night_greeting_window_min: int = Field(default=90, ge=5, le=360, description="晚安候选有效窗口（分钟）")
    festival_greeting_enabled: bool = Field(default=True, description="问候候选中加入当天公历或农历节日信息")
    weather_context_enabled: bool = Field(default=False, description="问候候选中加入实时天气信息")
    weather_api_key: str = Field(default="", description="心知天气 API Key")
    weather_location: str = Field(default="beijing", description="天气查询地点")
    weather_timeout_sec: float = Field(default=5.0, ge=1.0, le=30.0, description="天气查询硬超时（秒）")
    weather_cache_ttl_sec: int = Field(default=1800, ge=60, le=21600, description="天气结果缓存时长（秒）")
    profiling_msg_threshold: int = Field(default=50, ge=1)
    dream_interval_min: int = Field(default=30, ge=1, description="后台触发梦境整理记忆的周期(分钟)")
    dream_time_ranges: List[str] = Field(default_factory=list, description="允许触发 dream 的时间段列表，格式 HH:MM-HH:MM")
    min_memory_events_to_dream: int = Field(default=5, ge=1, description="进入 dream 整理前需要的最少长期记忆事件数")
    dream_visible: bool = Field(default=False, description="是否将梦境文本主动发送给指定会话")
    dream_send_target: str = Field(default="", description="梦境可见时的目标会话 ID，留空则发送回当前 dream session")
    intimate_tool_threshold: float = Field(default=20.0, description="关系达到该阈值后允许更亲近的主动工具")
    hostile_threshold: float = Field(default=-20.0, description="关系低于该阈值时进入敌意工具限制")
    energy_exhaustion: float = Field(default=0.1, ge=0.0, le=1.0, description="精力低于该值时限制高消耗聊天工具")


class ReplyConfig(BaseModel):
    fallback_text: str = Field(default="（陷入了短暂的沉默...）", description="当回复流程整体失败时使用的兜底文本")
    base_frequency: float = Field(default=0.7, ge=0.0, le=1.0, description="Bot 在普通场景下主动接话的积极程度")
    follow_up_probability: float = Field(default=0.2, ge=0.0, le=1.0, description="首条回复发出后，继续自然补一句的概率 (0.0~1.0)")
    stale_reply_max_age_sec: float = Field(default=0.0, ge=0.0, description="允许聊天回复保留时效性的最长秒数；0 表示自动按系统超时推导")
    segment_min_len: int = Field(default=15, ge=1, description="允许拆成多条发送前，单条内容至少要达到的长度")
    no_segment_max_len: int = Field(default=120, ge=1, description="允许智能分段的长度上限；达到或超过此长度时整条发送，双换行除外")
    humanlike_short_reply_enabled: bool = Field(default=True, description="对低信息量口语启用简短拟人回复约束")
    short_reply_max_chars: int = Field(default=80, ge=20, le=240, description="低信息量口语回复的最大字符数")
    short_reply_max_sentences: int = Field(default=2, ge=1, le=4, description="低信息量口语回复的最大句数")
    short_reply_allow_followup_question: bool = Field(default=False, description="低信息量口语回复是否允许主动追加问题")
    meme_probability: int = Field(default=60, ge=0, le=100, description="在适合的场景下附带表情包的概率百分比")
    emotion_mapping: List[str] = Field(
        default=[
            "happy: 积极、开心、感谢",
            "sad: 悲伤、遗憾、道歉",
            "angry: 生气、抱怨、攻击",
            "neutral: 平静、客观、陈述",
            "curious: 好奇、提问、困惑",
            "surprise: 惊讶、意外",
        ],
        description="表情标签及其模型识别说明；标签名同时对应 AstrBot 数据目录下 memes_data/memes 中的同名文件夹",
    )
    emotion_relationship_mapping: List[str] = Field(
        default=[],
        description="可选的情绪标签到好感关系事件映射，格式为“标签名: 关系事件”；未配置的自定义标签按普通聊天结算",
    )
    typing_speed_factor: float = Field(default=0.1, ge=0.0, description="模拟打字等待的强度系数，越大看起来越像在慢慢打字")


class TTSConfig(BaseModel):
    enabled: bool = Field(default=False, description="启用 TTS 语音回复")
    plugin_name: str = Field(default="astrbot_plugin_tts_llm", description="提供语音合成能力的 AstrBot 插件目录名或插件名")
    enable_private: bool = Field(default=True, description="私聊中允许发送 TTS 语音")
    enable_group: bool = Field(default=False, description="群聊中允许发送 TTS 语音")
    group_probability: int = Field(default=10, ge=0, le=100, description="群聊中命中语音回复的概率百分比")
    group_require_direct_trigger: bool = Field(default=True, description="群聊中仅在 @、回复、戳一戳或主动唤醒等明确触发场景尝试 TTS")
    send_text_with_audio: bool = Field(default=True, description="启用 TTS 时是否仍发送文字回复")
    min_text_length: int = Field(default=2, ge=1, description="短于该长度的回复不尝试 TTS")
    max_text_length: int = Field(default=120, ge=1, description="长于该长度的回复不尝试 TTS")
    silent_on_failure: bool = Field(default=True, description="TTS 失败时只记录日志，不向用户发送错误提示")


class ConversationConfig(BaseModel):
    enable_dialogue_store: bool = Field(default=True)
    dialogue_store_persist_enabled: bool = Field(
        default=True,
        description="插件重载时把群对话热/温区落盘并在启动时恢复（受 warm_zone_ttl 与快照 schema 版本双重约束）",
    )
    enable_context_compaction: bool = Field(default=True)
    enable_prefix_caching: bool = Field(default=True)
    context_dedup_enabled: bool = Field(default=True, description="启用提示词上下文来源感知去重")
    context_dedup_observe_only: bool = Field(default=False, description="仅统计上下文重复，不实际删减")
    conversation_generation_enabled: bool = Field(default=True, repr=False)
    reply_send_claim_enabled: bool = Field(default=True, repr=False)
    group_thread_wait_enabled: bool = Field(default=True, repr=False)
    social_feedback_observation_enabled: bool = Field(default=True)
    social_feedback_window_sec: float = Field(default=45.0, ge=5.0, le=300.0)
    social_feedback_max_active_per_chat: int = Field(default=5, ge=1, le=20)
    group_shared_history_enabled: bool = Field(
        default=True,
        description="启用群聊共享话题历史；参与者事实仍按 QQ 号隔离",
    )
    group_topic_active_ttl_sec: int = Field(
        default=1200,
        description="群聊当前话题自动承接时长（秒）",
    )
    group_topic_confirm_after_sec: int = Field(
        default=1800,
        description="群聊旧话题需要明确证据后才能承接的时间边界（秒）",
    )
    group_actor_tail_ttl_sec: int = Field(
        default=1200,
        ge=60,
        le=7200,
        description="当前群友自己的近期消息可跨短期话题承接的时长（秒）",
    )
    group_actor_tail_max_segments: int = Field(
        default=8,
        ge=2,
        le=20,
        description="每轮最多注入多少条当前群友自己的近期消息",
    )
    group_pending_direct_ttl_sec: int = Field(
        default=1200,
        ge=60,
        le=7200,
        description="群友直接呼叫 Bot 后，尚未回答消息的保留时长（秒）",
    )
    group_social_incident_ttl_sec: int = Field(
        default=1800,
        ge=60,
        le=86400,
        description="群聊边界冒犯、冲突、道歉等短期事件的保留时长（秒）",
    )
    group_context_snapshot_max_chars: int = Field(
        default=5500,
        ge=800,
        le=16000,
        description="单轮群聊因果快照允许注入提示词的最大字符数",
    )
    group_pre_send_freshness_enabled: bool = Field(
        default=True,
        description="发送前检查当前群友是否又发了新的直接补充或纠正",
    )
    group_social_state_ttl_sec: int = Field(
        default=86400,
        description="群聊称号、昵称和游戏规则等临时社交状态的默认有效期（秒）",
    )
    group_social_ownership_check_enabled: bool = Field(
        default=True,
        description="启用群聊称号、昵称、关系和承诺的 QQ 归属校验",
    )
    group_provider_topic_session_enabled: bool = Field(
        default=True,
        description="按群聊话题 epoch 隔离模型远端会话",
    )
    group_history_debug_trace_enabled: bool = Field(
        default=False,
        description="记录群聊历史策略的详细调试轨迹；日常使用建议关闭",
    )
    non_conversational_guard_enabled: bool = Field(default=True, repr=False)
    conversation_concurrency_debug_trace_enabled: bool = Field(default=False, repr=False)
    qq_native_tools_enabled: bool = Field(default=True, repr=False)
    qq_deferred_action_commit_enabled: bool = Field(default=True, repr=False)
    qq_explicit_intent_override_enabled: bool = Field(default=True, repr=False)
    explicit_tool_execution_enabled: bool = Field(default=True, repr=False)
    autonomous_chat_tools_enabled: bool = Field(default=True, repr=False)
    autonomous_vision_tool_enabled: bool = Field(
        default=True,
        description="允许模型在当前语义确实依赖图片时主动调用视觉工具",
    )
    recent_image_candidate_window_sec: int = Field(
        default=30,
        ge=5,
        le=300,
        description="普通群聊可供模型按需查看的近期图片时间窗口（秒）",
    )
    recent_image_candidate_max_count: int = Field(
        default=2,
        ge=1,
        le=4,
        description="单轮最多向模型披露多少个近期图片候选",
    )
    tool_progressive_disclosure_enabled: bool = Field(default=True, repr=False)
    tool_disclosure_max_tools_chat: int = Field(default=8, ge=1, repr=False)
    tool_disclosure_max_tools_task: int = Field(default=16, ge=1, repr=False)
    tool_disclosure_allow_second_pass: bool = Field(default=True, repr=False)
    history_lookup_enabled: bool = Field(default=True, description="允许模型在用户明确请求时只读查询会话历史")
    history_lookup_private_enabled: bool = Field(default=True, description="允许显式查询机器人与好友的近期私聊历史")
    history_lookup_group_enabled: bool = Field(default=True, description="允许显式查询群聊近期历史")
    history_lookup_max_messages: int = Field(default=20, ge=1, le=50, description="单次历史查询最多返回消息数")
    history_lookup_max_chars: int = Field(default=4000, ge=200, le=12000, description="单次历史查询最多返回字符数")
    hot_zone_ttl_seconds: float = Field(default=30.0, ge=0.0)
    warm_zone_ttl_seconds: float = Field(default=300.0, ge=0.0)
    warm_zone_max_tokens: int = Field(default=1200, ge=1)
    compaction_provider_id: str = Field(default="")
    compaction_trigger_segments: int = Field(default=40, ge=1)
    compaction_trigger_tokens: int = Field(default=1800, ge=1)
    compaction_keep_recent_segments: int = Field(default=16, ge=1)
    compaction_summary_max_tokens: int = Field(default=450, ge=1)
    enable_token_estimator: bool = False


class ArchitectureRolloutConfig(BaseModel):
    shadow_enabled: bool = Field(
        default=True,
        description="同时计算新旧上下文架构结果并记录差异，不改变实际回复行为",
    )
    canonical_read_enabled: bool = Field(
        default=True,
        description="下游读取规范化 ConversationEvent；关闭时保留写入但回退旧事件字段",
    )
    turn_target_read_enabled: bool = Field(
        default=True,
        description="使用 TurnTarget/ActorSet 作为本轮人物归属；关闭时回退旧目标推断",
    )
    committed_history_enabled: bool = Field(
        default=True,
        description="注意力承接只读取实际发送成功的机器人回复",
    )
    context_renderer_enabled: bool = Field(
        default=True,
        description="使用类型化 ContextPackage 渲染上下文；关闭时回退旧提示词字段",
    )
    memory_actor_filter_enabled: bool = Field(
        default=True,
        description="群聊记忆按当前人物白名单过滤；关闭时仅记录过滤差异",
    )
    proactive_due_enabled: bool = Field(
        default=True,
        description="主动发言读取持久化到期队列；关闭时仅扫描当前内存会话",
    )


class MemoryConfig(BaseModel):
    time_decay_rate: float = Field(default=0.01, ge=0.0, le=1.0)
    cleanup_interval: int = Field(default=3600, ge=1)
    summary_threshold: int = Field(default=30, ge=1)
    recall_top_k: int = Field(default=5, ge=1)
    memory_query_builder_enabled: bool = Field(default=True, repr=False)
    think1_semantic_intent_enabled: bool = Field(
        default=True,
        description="think1 记忆门放宽：关键词未命中时用意图分类器（identity/preference/location 等）判定是否检索",
    )
    maintenance_schedule_enabled: bool = Field(
        default=True,
        description="按日调度记忆维护（索引一致性修复 + 积压清理）；此前 run_once 无任何调度方",
    )
    maintenance_purge_enabled: bool = Field(
        default=False,
        description="维护调度是否执行物理清理（过期待审/墓碑 purge）；建议先观察一周 dry 报告再开启",
    )
    intent_rerank_enabled: bool = Field(default=False, repr=False)
    adaptive_top_k_enabled: bool = Field(default=False, repr=False)
    memory_rrf_fusion_enabled: bool = Field(default=False, repr=False)
    memory_mmr_enabled: bool = Field(default=False, repr=False)
    memory_retrieval_debug_trace_enabled: bool = Field(default=False, repr=False)
    enable_react_agent: bool = Field(default=True, description="启用 ReActAgent 多轮记忆检索")
    prune_threshold: float = Field(default=0.2, ge=0.0, le=1.0, description="记忆遗忘被物理剪枝的得分下限")
    min_memory_confidence: float = Field(default=0.3, ge=0.0, le=1.0, description="记忆写入最低置信度，低于此值的记忆不持久化")
    memory_quality_admission_enabled: bool = Field(default=True, description="启用长期记忆质量准入；不确定事实进入待审隔离区")


    deep_temporal_alpha: float = Field(default=0.7, ge=0.0, le=1.0)
    deep_temporal_tau_seconds: float = Field(default=86400.0, ge=0.0)
    deep_temporal_lambda_default: float = Field(default=1.0, ge=0.0)
    deep_temporal_lambda_fact: float = Field(default=0.1, ge=0.0)
    deep_temporal_candidate_pool_factor: int = Field(default=4, ge=1)
    deep_temporal_candidate_pool_min: int = Field(default=20, ge=1)
    deep_temporal_llm_window: int = Field(default=8, ge=1)
    maintenance_hot_beta: float = Field(default=0.7, ge=0.0, le=1.0)
    maintenance_temporal_stale_hot_threshold: float = Field(default=0.35, ge=0.0, le=1.0)
    auto_recall_probability: float = Field(default=0.0, ge=0.0, le=1.0)


class InfraConfig(BaseModel):
    llm_retries: int = Field(default=2, ge=0)
    backoff_factor: float = Field(default=1.5, ge=0.0)
    api_timeout: float = Field(default=15.0, ge=1.0, description="网关级绝对超时时间(秒)，超时后强制中断 API 请求")
    max_concurrent_llm_calls: int = Field(default=3, ge=1, description="全局 LLM 并发请求上限，防止后台任务雪崩导致 429")
    critical_path_reserved_slots: int = Field(
        default=1,
        ge=0,
        le=8,
        description="为用户可见回复链保留的并发槽位；后台任务最多占用 上限-该值 个槽（总并发不变）",
    )
    rate_limit_model_cooldown_sec: int = Field(default=120, ge=0, description="模型触发 429/rate limit 后的运行期冷却时间（秒）")
    quota_model_cooldown_sec: int = Field(default=1800, ge=0, description="模型触发 403/配额/权限失败后的运行期冷却时间（秒）")


class VisionConfig(BaseModel):
    enable_vision: bool = Field(default=True, description="多模态视觉总开关")
    image_recognition_probability: float = Field(
        default=0.5,
        ge=0.0,
        le=1.0,
        description="普通群聊图片进入回复判断的概率；仅在最终回复阶段识别 (0.0~1.0)",
    )
    at_image_pair_window_sec: float = Field(
        default=3.0,
        ge=0.5,
        le=10.0,
        description="群聊中纯 @Bot 与相邻图片跨消息配对的时间窗口（秒）",
    )
    enable_visual_result_cache: bool = Field(
        default=True,
        description="复用已经识别过的相同图片，避免重复调用视觉模型",
    )
    store_visual_asset_files: bool = Field(
        default=False,
        description="是否保存标准化后的图片副本；默认只保存图片指纹和转述结果",
    )
    visual_asset_retention_days: int = Field(
        default=30,
        ge=1,
        le=3650,
        description="标准化图片副本的保留天数",
    )
    visual_asset_max_disk_mb: int = Field(
        default=512,
        ge=16,
        le=102400,
        description="标准化图片副本最多占用的磁盘空间（MB）",
    )
    visual_asset_max_edge_px: int = Field(
        default=1600,
        ge=256,
        le=4096,
        description="保存标准化图片副本时的最长边像素",
    )
    visual_prompt_version: str = Field(
        default="v1",
        min_length=1,
        max_length=64,
        description="图片转述规则版本；修改后相同图片会重新识别",
    )
    gif_max_sample_frames: int = Field(
        default=12,
        ge=2,
        le=24,
        description="GIF 动图送入视觉模型前最多抽取的代表帧数",
    )
    gif_contact_sheet_max_edge_px: int = Field(
        default=1600,
        ge=512,
        le=4096,
        description="GIF 时间序列联系表的最长边像素",
    )
    gif_preprocess_timeout_sec: float = Field(
        default=8.0,
        ge=1.0,
        le=30.0,
        description="GIF 解码、抽帧和联系表生成的本地处理时限(秒)",
    )
    gif_max_decode_frames: int = Field(
        default=500,
        ge=10,
        le=2000,
        description="单个 GIF 最多解码的原始帧数",
    )
    vision_reply_policy: str = Field(
        default="超时后忽略图片并继续回复",
        description=(
            "图片识别失败后的回复策略；被动群聊纯图始终静默，独立文字任务始终继续，"
            "该选项只影响必须依赖图片的直接请求"
        ),
    )
    image_analysis_retries: int = Field(default=2, ge=1, le=5, description="图片识别失败重试次数")
    visual_failure_cooldown_sec: int = Field(
        default=120,
        ge=0,
        le=1800,
        description="相同图片识别失败后的重试冷却时间(秒)",
    )
    max_images_per_turn: int = Field(
        default=1,
        ge=1,
        le=8,
        description="单轮最多分析的图片数量，超出的图片不阻塞当前回复",
    )
    ignore_placeholder_without_question: bool = Field(
        default=True,
        description="用户没有询问图片时，禁止未解析图片占位符成为回复焦点",
    )
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
    max_steps: int = Field(default=30, ge=1, description="Sys3 direct work-mode tool loop maximum steps")
    tool_timeout: int = Field(default=120, ge=1, description="Sys3 direct work-mode tool loop timeout in seconds")
    computer_agent_sandbox_enabled: bool = Field(
        default=False,
        description="是否启用 ComputerAgent 的代码执行能力（需管理员权限）。开启后 ComputerAgent 才能加载 Python/Shell 工具。注意：此功能在宿主机直执，请仅在受信任环境开启。",
    )


class PrivateChatConfig(BaseModel):
    turn_merge_enabled: bool = Field(default=True, description="启用私聊连续输入合并，避免只回复最后一句")
    wait_timeout_sec: int = Field(default=300, ge=1, description="单次私聊等待反馈强制休眠阈值(秒)")
    input_settle_sec: float = Field(default=1.5, ge=0.0, le=30.0, description="私聊连续输入聚合等待时间(秒)")
    image_resolve_timeout_sec: float = Field(default=15.0, ge=1.0, le=600.0, description="私聊图片文件解析超时时间(秒)")
    image_barrier_timeout_sec: float = Field(default=90.0, ge=1.0, le=1200.0, description="私聊单张图片识别超时时间(秒)")
    image_analysis_retries: int = Field(default=2, ge=1, le=5, description="私聊图片识别失败重试次数")
    topic_continuity_enabled: bool = Field(default=True, description="启用私聊话题承接；关闭后恢复原有连续对话行为")
    topic_active_ttl_sec: int = Field(default=900, ge=600, le=1200, description="私聊话题保持强承接的时间，默认15分钟")
    topic_confirm_after_sec: int = Field(default=1800, ge=1800, le=7200, description="私聊超过此时间后，继续旧话题前先向用户确认，默认30分钟")
    topic_confirmation_wait_sec: int = Field(default=120, ge=30, le=600, description="等待用户确认是否继续旧话题的时间，默认2分钟")
    topic_summary_max_chars: int = Field(default=300, ge=80, le=1000, description="注入提示词的话题摘要最大字数")


class TimingConfig(BaseModel):
    # OPT-10/PL-03: schema 把该开关挂在 timing 分节（UI 写 timing.turn_merge_enabled），
    # 而消费方读 private_chat.turn_merge_enabled——旧模型无此字段被 extra-ignore 静默
    # 丢弃，UI 关闭无效。None=未设置（回退 private_chat 侧）。
    turn_merge_enabled: bool | None = Field(default=None, repr=False)
    hot_reload_shutdown_budget_sec: float = Field(default=5.0, ge=1.0, le=30.0)
    shutdown_component_timeout_sec: float = Field(default=1.5, ge=0.1, le=10.0)
    shutdown_cancel_grace_sec: float = Field(default=1.0, ge=0.0, le=10.0)
    shutdown_snapshot_timeout_sec: float = Field(default=0.5, ge=0.1, le=5.0)
    model_request_timeout_sec: float = Field(default=15.0, ge=1.0, le=3600.0)
    turn_total_budget_sec: float = Field(default=360.0, ge=30.0, le=7200.0)
    main_reply_reserve_sec: float = Field(default=90.0, ge=0.0, le=1800.0)
    reply_max_age_sec: float = Field(default=0.0, ge=0.0, le=7200.0)
    agent_execution_timeout_sec: int = Field(default=60, ge=1, le=7200)
    fast_mode_execution_timeout_sec: int = Field(default=15, ge=1, le=7200)
    workmode_execution_timeout_sec: int = Field(default=120, ge=1, le=86400)
    attention_judge_timeout_sec: float = Field(default=3.0, ge=0.1, le=600.0)
    cognitive_loop_timeout_sec: float = Field(default=2.5, ge=0.1, le=600.0)
    mood_analysis_timeout_sec: float = Field(default=30.0, ge=1.0, le=600.0)
    memory_react_timeout_sec: float = Field(default=15.0, ge=1.0, le=600.0)
    query_rewrite_timeout_sec: float = Field(default=3.0, ge=0.5, le=60.0)
    deep_memory_total_budget_sec: float = Field(default=12.0, ge=1.0, le=120.0)
    memory_rerank_timeout_sec: float = Field(default=5.0, ge=0.5, le=30.0)
    memory_compress_timeout_sec: float = Field(default=4.0, ge=0.5, le=30.0)
    compaction_timeout_sec: float = Field(default=60.0, ge=1.0, le=1200.0)
    embedding_timeout_sec: float = Field(default=15.0, ge=1.0, le=600.0)
    faiss_timeout_sec: float = Field(default=20.0, ge=0.5, le=60.0)
    faiss_failure_threshold: int = Field(default=3, ge=1, le=10)
    faiss_circuit_breaker_cooldown_sec: float = Field(default=180.0, ge=5.0, le=600.0)
    projection_retry_interval_sec: float = Field(default=60.0, ge=5.0, le=3600.0)
    projection_retry_base_delay_sec: float = Field(default=30.0, ge=1.0, le=3600.0)
    projection_retry_max_delay_sec: float = Field(default=900.0, ge=5.0, le=86400.0)
    projection_retry_batch_size: int = Field(default=20, ge=1, le=200)
    private_wait_timeout_sec: int = Field(default=300, ge=1, le=7200)
    private_input_settle_sec: float = Field(default=1.5, ge=0.0, le=30.0)
    image_resolve_timeout_sec: float = Field(default=15.0, ge=1.0, le=600.0)
    image_analysis_timeout_sec: float = Field(default=90.0, ge=1.0, le=1200.0)
    vision_barrier_total_timeout_sec: float = Field(default=300.0, ge=1.0, le=3600.0)


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
        # also migrate from top-level legacy fields (old plugin stored flat)
        for field_name in LEGACY_MEMORY_NAMESPACE_FIELDS:
            if field_name in normalized and field_name not in global_settings:
                global_settings[field_name] = normalized[field_name]
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

    @staticmethod
    def _normalize_legacy_timing_namespace(data: dict) -> dict:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        raw_timing = normalized.get("timing")
        if raw_timing is None:
            timing_values = {}
        elif isinstance(raw_timing, dict):
            timing_values = dict(raw_timing)
        else:
            return normalized
        for timing_field, section_name, legacy_field in LEGACY_TIMING_NAMESPACE_FIELDS:
            if timing_field in timing_values:
                continue
            section = normalized.get(section_name)
            if isinstance(section, dict) and legacy_field in section:
                timing_values[timing_field] = section[legacy_field]
        if timing_values or "timing" in normalized:
            normalized["timing"] = timing_values
        return normalized

    @staticmethod
    def _normalize_legacy_vision_namespace(data: dict) -> dict:
        if not isinstance(data, dict):
            return data
        normalized = dict(data)
        raw_vision = normalized.get("vision")
        vision_values = dict(raw_vision) if isinstance(raw_vision, dict) else {}
        private_chat = normalized.get("private_chat")
        if (
            "image_analysis_retries" not in vision_values
            and isinstance(private_chat, dict)
            and "image_analysis_retries" in private_chat
        ):
            vision_values["image_analysis_retries"] = private_chat["image_analysis_retries"]
        policy = str(
            vision_values.get("vision_reply_policy", "超时后忽略图片并继续回复")
            or ""
        ).strip()
        policy_aliases = {
            "strict": "必须识别成功后再回复",
            "require_analysis": "必须识别成功后再回复",
            "timeout_fallback": "超时后忽略图片并继续回复",
            "fallback": "超时后忽略图片并继续回复",
        }
        policy = policy_aliases.get(policy, policy)
        if policy not in {"必须识别成功后再回复", "超时后忽略图片并继续回复"}:
            policy = "超时后忽略图片并继续回复"
        vision_values["vision_reply_policy"] = policy
        normalized["vision"] = vision_values
        return normalized

    def _sync_legacy_timing_aliases(self) -> None:
        for timing_field, section_name, legacy_field in LEGACY_TIMING_NAMESPACE_FIELDS:
            section = getattr(self, section_name, None)
            if section is not None:
                setattr(section, legacy_field, getattr(self.timing, timing_field))

    def _sync_legacy_vision_aliases(self) -> None:
        self.private_chat.image_analysis_retries = self.vision.image_analysis_retries

    def __init__(self, **data):
        normalized = self._normalize_legacy_memory_namespace(data)
        normalized = self._normalize_legacy_timing_namespace(normalized)
        normalized = self._normalize_legacy_vision_namespace(normalized)
        super().__init__(**normalized)
        self._sync_legacy_timing_aliases()
        self._sync_legacy_vision_aliases()
        # ── 互斥配置检测 ──
        if getattr(self.sys3, "enable_work_mode", False) and not self.provider.agent_models:
            from astrbot.api import logger
            logger.warning("[AstrMai] Sys3 work mode enabled but agent_models is empty — work mode will silently fail")
        if getattr(self.vision, "enable_vision", True) and not self.provider.vision_models:
            from astrbot.api import logger
            logger.warning("[AstrMai] Vision enabled but vision_models is empty — image recognition will silently fail")
        # ── 格式校验 ──
        for entry in self.reply.emotion_mapping:
            if ":" not in entry and "：" not in entry:
                from astrbot.api import logger as _log
                _log.warning(f"[AstrMai] emotion_mapping entry missing colon: {entry!r}")
        catalog = build_emotion_tag_catalog(self)
        for entry in catalog.malformed_emotion_entries:
            from astrbot.api import logger as _log
            _log.warning(f"[AstrMai] emotion_mapping entry invalid or empty: {entry!r}")
        for entry in catalog.invalid_relationship_entries:
            from astrbot.api import logger as _log
            _log.warning(f"[AstrMai] emotion_relationship_mapping entry invalid or unknown: {entry!r}")
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
    tts: TTSConfig = Field(default_factory=TTSConfig)
    conversation: ConversationConfig = Field(default_factory=ConversationConfig)
    architecture_rollout: ArchitectureRolloutConfig = Field(
        default_factory=ArchitectureRolloutConfig
    )
    memory: MemoryConfig = Field(default_factory=MemoryConfig)
    infra: InfraConfig = Field(default_factory=InfraConfig)
    vision: VisionConfig = Field(default_factory=VisionConfig)
    sys3: Sys3Settings = Field(default_factory=Sys3Settings)
    private_chat: PrivateChatConfig = Field(default_factory=PrivateChatConfig)
    timing: TimingConfig = Field(default_factory=TimingConfig)

