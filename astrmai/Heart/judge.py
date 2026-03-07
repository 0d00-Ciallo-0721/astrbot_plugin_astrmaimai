# astrmai/Heart/judge.py
from ..infra.gateway import GlobalModelGateway
from .state_engine import StateEngine
import time
import json
from astrbot.api import logger
from ..infra.datamodels import BrainActionPlan


class Judge:
    """
    判官 (System 1: Fused 3-State Version)
    职责: 决定 System 2 的初步动作倾向 (REPLY, WAIT, IGNORE)
    """
    def __init__(self, gateway: GlobalModelGateway, state_engine: StateEngine, config=None):
        self.gateway = gateway
        self.state_engine = state_engine
        self.config = config if config else gateway.config

    async def evaluate(self, chat_id: str, message: str, is_force_wakeup: bool, persona_summary: str = "") -> BrainActionPlan:
        """
        输出结构化的 BrainActionPlan，融合了 HeartFlow 的评分机制和 3 态决策。
        [修改] 注入 persona_summary，要求模型以第一人称和角色语气生成 thought 思维链。
        """
        start_time = time.perf_counter()
        state = await self.state_engine.get_state(chat_id)
        
        # 1. 能量硬限制 (接入 Config)
        if state.energy < self.config.energy.min_reply_threshold and not is_force_wakeup:
            logger.debug(f"[{chat_id}] Judge: 能量过低 ({state.energy:.2f})，抑制回复。")
            return BrainActionPlan(action="IGNORE", thought="好累...不想说话...", necessity=0.0)

        # 2. 强唤醒直接通过
        if is_force_wakeup:
            logger.debug(f"[{chat_id}] Judge: 强唤醒，最高优先级放行。")
            plan = BrainActionPlan(action="REPLY", thought="有人在很用力地叫我，我必须回应！", necessity=10.0, relevance=10)
            plan.meta["retrieve_keys"] = ["ALL"] # 强唤醒默认直接完整降临
            return plan

        # 3. 关键词短路 (接入 Config 修复隐患 Bug)
        wakeup_words = self.config.system1.wakeup_words
        msg_lower = message.strip().lower()
        
        for kw in wakeup_words:
            if msg_lower.startswith(kw.lower()):
                logger.debug(f"[{chat_id}] Judge: 唤醒词 [{kw}] 命中首部，快速放行。")
                plan = BrainActionPlan(action="REPLY", thought=f"听到了熟悉的呼唤 [{kw}]，马上回答！", necessity=9.0, relevance=10)
                plan.meta["retrieve_keys"] = []
                return plan

        # 4. LLM 三态判决 (REPLY / WAIT / IGNORE) + 沉浸式思维链寻址 (CoT)
        prompt = f"""
        你是群聊中的这个角色（System 1 大脑），请完全沉浸于以下设定中：
        [你的核心人设]: {persona_summary if persona_summary else '保持你原本的性格特征'}

        当前群聊情绪: {state.mood:.2f} (-1.0 到 1.0)。
        用户消息: "{message}"
        
        【思考与决策流】
        1. 首先，开启思维链 (thought)，请**完全以你角色第一人称的口吻和性格**，分析用户的意图，以及你此刻的心情是否想理会这条消息。
           - REPLY: 包含明确问题，或话题直接相关，必须立刻回复。
           - WAIT: 话似乎没说完（例如“那个..”或半截句子），稍微等等看。
           - IGNORE: 明显的闲聊、无意义刷屏且没叫你，没兴趣理会。
        2. 其次，**仅当**你的 action 决定为 REPLY 时，才需要判断当前回复需要调用你脑海中的哪部分【人格记忆 (retrieve_keys)】。如果 action 为 WAIT 或 IGNORE，或者只是极简单的日常寒暄，列表请严格保持为空 []。
        
        可选的人格维度 Key (中英双语说明):
        - logic_style (性格逻辑): 内在行为模式、战斗/日常切换、思考方式
        - speech_style (语言风格): 口癖、特殊发声、语调、标志性词汇
        - world_view (世界观): 常识、阵营、地理、政治立场
        - timeline (生平经历): 过去的关键事件、创伤、童年回忆
        - relations (人际关系): 对特定人的称呼、态度和关系
        - skills (技能能力): 战斗方式、生活技能、特殊天赋
        - values (价值观): 喜好、厌恶、恐惧、面临道德抉择时的倾向
        - secrets (深层秘密): 黑历史、潜意识深处的恐惧
        - ALL (完整降临): 无法确定具体领域，或需要调动全部灵魂设定进行深度交互时
        
        请严格按照以下 JSON 格式输出（必须先输出 thought 进行推理分析）：
        {{
            "thought": "以第一人称和你的角色语气，写下对这段话的内心吐槽/想法，以及你决定调用哪些记忆的过程...",
            "action": "REPLY"|"WAIT"|"IGNORE",
            "relevance": int(1-10),
            "necessity": float(1.0-10.0),
            "retrieve_keys": ["key1"] // 仅在 REPLY 且需深层记忆时填写，否则 []
        }}
        """
        
        plan = BrainActionPlan()
        try:
            result = await self.gateway.call_judge(prompt)
            plan.thought = result.get("thought", "")
            plan.action = result.get("action", "IGNORE").upper()
            
            # 强化类型转换包容度
            try:
                plan.relevance = int(float(result.get("relevance", 0)))
            except (ValueError, TypeError):
                plan.relevance = 0
                
            try:
                plan.necessity = float(result.get("necessity", 0.0))
            except (ValueError, TypeError):
                plan.necessity = 0.0
            
            # 提取 retrieve_keys 写入 meta，交给下游
            keys = result.get("retrieve_keys", [])
            if not isinstance(keys, list):
                keys = []
            plan.meta["retrieve_keys"] = keys
            
            if plan.action not in ["REPLY", "WAIT", "IGNORE"]:
                plan.action = "IGNORE"
                
            elapsed = time.perf_counter() - start_time
            logger.debug(f"[{chat_id}] Judge 耗时 {elapsed:.2f}s | Action: {plan.action} | Keys: {keys} | 内部思考: {plan.thought}")
            
        except Exception as e:
            logger.warning(f"[{chat_id}] Judge LLM 失败，默认放行: {e}")
            plan.action = "REPLY" # 降级放行
            plan.meta["retrieve_keys"] = []
            
        return plan