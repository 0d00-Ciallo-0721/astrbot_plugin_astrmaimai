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
        # [新增] Sys1 专属群组级思考锁，防止同一群重入
        self.active_sys1_groups = set()

    async def evaluate(self, chat_id: str, message: str, is_force_wakeup: bool, persona_summary: str = "", window_events_count: int = 1, is_first_event_wakeup: bool = False) -> BrainActionPlan:
        """
        输出结构化的 BrainActionPlan，融合了 HeartFlow 的评分机制和 3 态决策。
        [修改]: 注入系统级共享配置的短期历史对话，并进行 JSON 组件树的纯文本扁平化，提供极致的沉浸式语境。
        """
        import time
        import json
        from astrbot.api import logger
        from ..infra.datamodels import BrainActionPlan

        start_time = time.perf_counter()

        # =====================================================================
        # 1. 群组思考互斥锁检查
        # =====================================================================
        if chat_id in self.active_sys1_groups:
            logger.debug(f"[{chat_id}] Judge: 该群 Sys1 正在思考中，Drop 当前请求以防止回复冲突。")
            return BrainActionPlan(action="IGNORE", thought="", necessity=0.0)
            
        self.active_sys1_groups.add(chat_id)

        try:
            state = await self.state_engine.get_state(chat_id)
            
            # --- 提取触发词特征 ---
            msg_lower = message.strip().lower()
            wakeup_words = getattr(self.config.system1, "wakeup_words", [])

            is_keyword_wakeup = False
            matched_kw = ""
            for kw in wakeup_words:
                if msg_lower.startswith(kw.lower()):
                    is_keyword_wakeup = True
                    matched_kw = kw
                    break

            # =====================================================================
            # 【最高优先级】: 极速穿透判定 (4维检查)
            # =====================================================================
            if is_force_wakeup or is_keyword_wakeup:
                time_since_last_reply = time.time() - state.last_reply_time
                is_cold_chat = time_since_last_reply > 180
                is_low_entropy = (window_events_count == 1)
                is_valid_position = (is_force_wakeup and is_first_event_wakeup) or is_keyword_wakeup

                complex_keywords = ["为什么", "怎么", "帮我", "代码", "解释", "写", "什么", "翻译", "分析"]
                clean_text = msg_lower.split("：", 1)[-1].strip() if "：" in msg_lower else msg_lower
                if is_keyword_wakeup:
                    clean_text = msg_lower[len(matched_kw):].strip()
                    
                is_simple_payload = len(clean_text) <= 15 and not any(cw in clean_text for cw in complex_keywords)

                if is_cold_chat and is_low_entropy and is_valid_position and is_simple_payload:
                    if is_keyword_wakeup:
                        logger.debug(f"[{chat_id}] Judge: 满足4维极速条件，触发唤醒词 [{matched_kw}] 穿透！")
                        plan = BrainActionPlan(action="REPLY", thought=f"[极速反射] 捕捉到指令词 [{matched_kw}]。", necessity=9.0, relevance=10)
                    else:
                        logger.debug(f"[{chat_id}] Judge: 满足4维极速条件，触发强唤醒穿透！")
                        plan = BrainActionPlan(action="REPLY", thought="[极速反射] 听到召唤，立即响应。", necessity=10.0, relevance=10)

                    plan.meta["retrieve_keys"] = ["CORE_ONLY"] 
                    plan.meta["is_fast_mode"] = True
                    return plan
                else:
                    logger.debug(f"[{chat_id}] Judge: 拦截极速穿透，回落 System 1 完整思考。")

            # =====================================================================
            # 【中间组件 2】: 窗口级节流与防冲突
            # =====================================================================
            if not is_force_wakeup and not is_keyword_wakeup:
                should_drop = await self.state_engine.should_drop_by_energy(chat_id, window_events_count)
                if should_drop:
                    return BrainActionPlan(action="IGNORE", thought="好累...不想说话...", necessity=0.0)

            # =====================================================================
            # 🟢 【架构级升级】: 提取短程历史记忆，并进行组件扁平化 (Flattening)
            # =====================================================================
            def _flatten_content(raw_val: any) -> str:
                """内部防御性解析器：将底层 JSON 格式的消息组件数组降维成纯文字剧本"""
                if not raw_val: return ""
                if isinstance(raw_val, str):
                    try:
                        parsed = json.loads(raw_val)
                        if isinstance(parsed, list): raw_val = parsed
                        else: return raw_val
                    except Exception:
                        return raw_val # 普通纯文本直接返回
                
                if isinstance(raw_val, list):
                    text_parts = []
                    for item in raw_val:
                        if isinstance(item, dict):
                            t = item.get("type", item.get("component", "")).lower()
                            if t in ["text", "plain"]: text_parts.append(str(item.get("text", "")))
                            elif t in ["image"]: text_parts.append("[图片]")
                            elif t in ["at"]: text_parts.append("[@某人]")
                        elif hasattr(item, "type") or hasattr(item, "__class__"):
                            t = getattr(item, "type", item.__class__.__name__).lower()
                            if t in ["text", "plain"]: text_parts.append(str(getattr(item, "text", "")))
                            elif t in ["image"]: text_parts.append("[图片]")
                            elif t in ["at"]: text_parts.append("[@某人]")
                    return "".join(text_parts)
                return str(raw_val)

            history_context = ""
            try:
                sys2_config = getattr(self.config, "system2", None)
                history_limit = getattr(sys2_config, "history_messages_count", 5) if sys2_config else 5
                
                history_records = []
                persistence = getattr(self.state_engine, "persistence", None)
                
                if persistence:
                    if hasattr(persistence, "get_recent_messages"):
                        history_records = await persistence.get_recent_messages(chat_id, limit=history_limit)
                    elif hasattr(persistence, "get_chat_history"):
                        history_records = await persistence.get_chat_history(chat_id, limit=history_limit)
                        
                if history_records:
                    history_context = f"【历史对话语境 (前 {history_limit} 条)】\n"
                    for record in history_records:
                        if isinstance(record, dict):
                            sender = record.get("sender_name") or record.get("role") or "User"
                            raw_content = record.get("content") or record.get("message") or ""
                        else:
                            sender = getattr(record, "sender_name", getattr(record, "role", "User"))
                            raw_content = getattr(record, "content", getattr(record, "message", ""))
                            
                        # 🟢 调用扁平化解析器，剔除机器代码
                        clean_content = _flatten_content(raw_content)
                        if clean_content:
                            history_context += f"{sender}: {clean_content}\n"
                    history_context += "\n"
            except Exception as e:
                logger.debug(f"[{chat_id}] ⚠️ 提取 Sys1 历史上下文失败，安全回落: {e}")

            # =====================================================================
            # 【正常执行 System 1 唤醒大模型判决】
            # =====================================================================
            prompt = f"""
            你是群聊中的这个角色的潜意识大脑，请完全沉浸于以下设定中：
            [你的核心人设]: {persona_summary if persona_summary else '保持你原本的性格特征'}

            当前群聊情绪: {state.mood:.2f} (-1.0 到 1.0)。
            
            {history_context}
            【近期发生的连续对话 (请重点基于以上历史语境和以下近期对话进行最终裁决)】:
            {message}
            
            【思考与决策流】
            1. 意图判决 (action): 
               - REPLY: 包含明确问题，提及你，或【根据历史语境对方正在顺着刚才的话题跟你聊天】，必须立刻回复。
               - WAIT: 话似乎没说完（例如“那个..”或半截句子），稍微等等看。
               - IGNORE: 明显的闲聊、无意义刷屏且没叫你、没顺着话题聊，没兴趣理会。
            2. 潜意识生成 (thought): **仅当 action 为 REPLY 时**，你需要以第一人称和角色语气，生成一段你此刻脑海中一闪而过的内心戏。如果决定 WAIT 或 IGNORE，请严格留空。
            3. 记忆提取 (retrieve_keys): **仅当 action 为 REPLY 时**才需要判断当前回复需要调用你脑海中的哪部分【人格记忆 (retrieve_keys)】。如果 action 为 WAIT 或 IGNORE，或者只是极简单的日常寒暄，列表请严格保持为空 []。
            
            可选的人格维度 Key:
            - logic_style (性格逻辑)
            - speech_style (语言风格)
            - world_view (世界观)
            - timeline (生平经历)
            - relations (人际关系)
            - skills (技能能力)
            - values (价值观)
            - secrets (深层秘密)
            - ALL (完整降临)
            
            请严格按照以下 JSON 格式输出（必须先输出 reason 进行极简逻辑推理）：
            {{
                "reason": "极简的判定理由，例如：'有人在提问' 或 '顺着刚才的话题在聊'（限20字内）",
                "action": "REPLY"|"WAIT"|"IGNORE",
                "thought": "【仅当 action 为 REPLY 时生成】第一人称的真实内心戏。如果不回复，请严格输出空字符串 \"\"",
                "relevance": int(1-10),
                "necessity": float(1.0-10.0),
                "retrieve_keys": ["key1"] 
            }}
            """
            
            plan = BrainActionPlan()
            try:
                result = await self.gateway.call_judge_task(prompt)
                
                plan.action = result.get("action", "IGNORE").upper()
                if plan.action == "REPLY":
                    plan.thought = result.get("thought", "")
                else:
                    plan.thought = ""
                    
                try:
                    plan.relevance = int(float(result.get("relevance", 0)))
                except (ValueError, TypeError):
                    plan.relevance = 0
                    
                try:
                    plan.necessity = float(result.get("necessity", 0.0))
                except (ValueError, TypeError):
                    plan.necessity = 0.0
                
                keys = result.get("retrieve_keys", [])
                if not isinstance(keys, list):
                    keys = []
                plan.meta["retrieve_keys"] = keys
                
                if plan.action not in ["REPLY", "WAIT", "IGNORE"]:
                    plan.action = "IGNORE"
                    
                elapsed = time.perf_counter() - start_time
                reason = result.get("reason", "")
                logger.debug(f"[{chat_id}] Judge耗时 {elapsed:.2f}s | Action: {plan.action} | 理由: {reason}")
                
                return plan
            
            except Exception as e:
                logger.warning(f"[{chat_id}] Judge LLM 失败，默认放行: {e}")
                plan.action = "REPLY" 
                plan.meta["retrieve_keys"] = []
                return plan
            
        finally:
            self.active_sys1_groups.discard(chat_id)