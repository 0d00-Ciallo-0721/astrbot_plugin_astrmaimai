### 📄 core/evolution_cortex.py
import random
import time
from typing import Optional
from astrbot.api.star import Context
from astrbot.api import logger

from ..services.evolution.pattern_learner import PatternLearner
from ..config import HeartflowConfig

class EvolutionCortex:
    """
    进化皮层 (The Soul)
    职责：
    1. 人格突变 (Persona Mutation): 随机产生临时性格状态。
    2. 风格镜像 (Style Mirroring): 通过 PatternLearner 模仿用户。
    """
    
    def __init__(self, context: Context, config: HeartflowConfig):
        self.context = context
        self.config = config
        self.learner = PatternLearner()
        
        # 突变状态池
        self.mutations = [
            "（状态：有点累了）回复要慵懒一点，多用“。。。”，不想说话。",
            "（状态：异常兴奋）回复要元气满满！多用感叹号！和颜文字 (≧∇≦)/",
            "（状态：傲娇）回复要带点刺，口是心非。",
            "（状态：温柔）像知心大姐姐一样温柔地回复。",
            "（状态：中二病发作）说话要带点中二设定的词汇。",
            "（状态：吃瓜群众）对什么都很惊讶，喜欢八卦。",
            None, None, None, None # 增加 None 的权重，保持常态
        ]
        
        # 缓存每个会话的当前突变
        # map[session_id, {"mutation": str, "expire_at": float}]
        self.active_mutations = {} 

    async def get_mutation_state(self, session_id: str) -> str:
        """获取当前时刻的突变状态"""
        now = time.time()
        
        # 1. 检查缓存是否过期 (每 30 分钟刷新一次状态)
        cache = self.active_mutations.get(session_id)
        if cache and now < cache["expire_at"]:
            return cache["mutation"] or ""

        # 2. 随机生成新状态
        # 只有在配置开启时才突变
        if self.config.enable_evolution and random.random() < self.config.persona_mutation_rate:
            mutation = random.choice(self.mutations)
        else:
            mutation = None
            
        # 3. 更新缓存
        self.active_mutations[session_id] = {
            "mutation": mutation,
            "expire_at": now + 1800 # 30 mins
        }
        
        if mutation:
            logger.info(f"🧬 [Evolution] Persona Mutated: {mutation}")
            
        return mutation or ""

    async def get_style_learning_prompt(self, recent_user_msgs: list) -> str:
        """获取风格模仿建议"""
        if not recent_user_msgs or len(recent_user_msgs) < 5:
            return ""
            
        analysis = self.learner.analyze_patterns(recent_user_msgs)
        return analysis.get("style_prompt", "")