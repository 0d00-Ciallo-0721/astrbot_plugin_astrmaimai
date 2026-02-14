### 📄 config.py
# heartflow/config.py
# (HeartCore 2.0 Update)
import json
from dataclasses import dataclass, field
from astrbot.api import logger

@dataclass
class HeartflowConfig:
    # --- 原 v4.14 配置 (保持兼容) ---
    enable_heartflow: bool = False
    general_pool: list = field(default_factory=list)
    
    # 防噪
    enable_noise_control: bool = True
    reply_composite_threshold: float = 60.0
    extreme_relevance_trigger: int = 9 # (P2: 将被 ImpulseEngine 的新逻辑替代，暂时保留)
    energy_soft_filter_limit: float = 0.6
    image_spam_limit: int = 2
    
    # 大脑与生成
    judge_provider_names: list = field(default_factory=list) # (P2: ImpulseEngine 使用)
    summarize_provider_name: str = ""
    humanization_word_count: int = 30
    judge_max_retries: int = 3
    context_messages_count: int = 10
    bot_nicknames: list = field(default_factory=list)
    
    # 状态
    default_energy: float = 1.0
    energy_decay_rate: float = 0.05
    energy_recovery_rate: float = 0.02
    score_positive_interaction: float = 2.0
    max_consecutive_replies: int = 3
    
    # 节流
    enable_throttling: bool = False
    throttling_buffer_size: int = 5
    active_window_count: int = 10
    filter_short_length: int = 1
    enable_repeater: bool = False
    min_reply_interval: float = 2.0
    
    # 拟人化
    enable_segmentation: bool = True
    segmentation_threshold: int = 30
    
    # 权限
    super_admin_id: str = ""
    enable_group_admin: bool = True
    
    # 情感
    enable_emotion_sending: bool = True
    emotions_probability: int = 50
    emotion_model_provider_name: str = ""
    mood_decay: float = 0.1
    emotion_mapping: dict = field(default_factory=dict)
    emotion_mapping_string: str = ""
    
    # --- HeartCore 2.0 新增配置 ---
    
    # P3: 海马体 (LivingMemory)
    enable_memory_glands: bool = False # 是否启用主动记忆检索
    memory_importance_threshold: float = 0.6 # 记忆存入阈值
    
    # P4: 进化皮层 (SelfLearning)
    enable_evolution: bool = False # 是否启用自我进化
    persona_mutation_rate: float = 0.1 # 人格突变概率 (0.0 - 1.0)
    
    # 视觉感知
    use_native_vision: bool = True # 默认开启原生视觉
    image_recognition_provider_name: str = "" # 如果不支持原生视觉，使用的 VL 模型
    image_recognition_prompt: str = "请用一句话描述这张图片的内容，包含主体和氛围。"

    @classmethod
    def from_astrbot_config(cls, raw_config: dict):
        # (保持原有的加载逻辑不变，自动映射新字段)
        instance = cls()
        for key, value in raw_config.items():
            if hasattr(instance, key):
                setattr(instance, key, value)
        
        # 处理表情映射 (保持原逻辑)
        emotion_json_str = raw_config.get("emotion_descriptions", "{}")
        try:
            if isinstance(emotion_json_str, str):
                instance.emotion_mapping = json.loads(emotion_json_str)
            elif isinstance(emotion_json_str, dict):
                instance.emotion_mapping = emotion_json_str
            
            instance.emotion_mapping_string = "\n".join(
                [f"- {key}: {desc}" for key, desc in instance.emotion_mapping.items()]
            )
        except Exception as e:
            logger.error(f"Config Error: {e}")
            
        return instance