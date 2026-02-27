from pydantic import BaseModel, Field
from typing import List,Dict

class ProviderConfig(BaseModel):
    system1_provider_id: str = Field(default="")
    system2_provider_id: str = Field(default="")
    embedding_provider_id: str = Field(default="")

class GlobalSettingsConfig(BaseModel):
    debug_mode: bool = Field(default=False)
    command_prefixes: List[str] = Field(default=["/", "!", "！"])
    enabled_groups: List[str] = Field(default=[])

class PersonaConfig(BaseModel):
    persona_id: str = Field(default="")

class AgentConfig(BaseModel):
    max_steps: int = Field(default=5)
    timeout: int = Field(default=60)

class PerformanceConfig(BaseModel):
    summary_threshold: int = Field(default=300)

class System1Config(BaseModel):
    wakeup_words: List[str] = Field(default=[])
    nicknames: List[str] = Field(default=[])
    # [新增] 额外指令黑名单，用于手动兜底隔离
    extra_command_list: List[str] = Field(default=[])

class AttentionConfig(BaseModel):
    debounce_window: float = Field(default=2.0)
    bg_pool_size: int = Field(default=20)

class EnergyConfig(BaseModel):
    min_reply_threshold: float = Field(default=0.1)
    cost_per_reply: float = Field(default=0.05)
    daily_recovery: float = Field(default=0.2)
    recovery_silence_min: int = Field(default=60)

class MoodConfig(BaseModel):
    decay_interval: int = Field(default=3600)
    decay_rate: float = Field(default=0.1)
    unknown_decay: float = Field(default=0.9)
    emotion_mapping: List[str] = Field(default=[
        "happy: 积极、开心、感谢",
        "sad: 悲伤、遗憾、道歉",
        "angry: 生气、抱怨、攻击",
        "neutral: 平静、客观、陈述",
        "curious: 好奇、提问、困惑",
        "surprise: 惊讶、意外"
    ])

class EvolutionConfig(BaseModel):
    min_mining_context: int = Field(default=10)
    batch_size: int = Field(default=50)
    mining_trigger: int = Field(default=20)

class LifeConfig(BaseModel):
    silence_threshold: int = Field(default=120)
    wakeup_min_energy: float = Field(default=0.6)
    wakeup_cost: float = Field(default=0.2)
    wakeup_cooldown: int = Field(default=28800)

class ReplyConfig(BaseModel):
    fallback_text: str = Field(default="（陷入了短暂的沉默...）")
    segment_min_len: int = Field(default=15)
    no_segment_max_len: int = Field(default=120)
    meme_probability: int = Field(default=60)
    typing_speed_factor: float = Field(default=0.1)

class MemoryConfig(BaseModel):
    time_decay_rate: float = Field(default=0.01)
    cleanup_interval: int = Field(default=3600)
    summary_threshold: int = Field(default=30)
    recall_top_k: int = Field(default=5)

class InfraConfig(BaseModel):
    llm_retries: int = Field(default=2)
    backoff_factor: float = Field(default=1.5)

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