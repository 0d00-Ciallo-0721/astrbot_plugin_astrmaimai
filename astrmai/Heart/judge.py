# astrmai/Heart/judge.py
from ..infra.gateway import GlobalModelGateway
from ..infra.lane_manager import LaneKey
from .state_engine import StateEngine
import time
import json
from astrbot.api import logger
from ..infra.datamodels import BrainActionPlan


JUDGE_STABLE_PREFIX = """
你是 AstrMai 的 System1 Judge。
你的职责不是直接聊天，而是快速判断当前消息最适合采取的动作。
必须严格输出 JSON，不要输出额外说明。
规则：
1. 优先判断是否需要立即回复、等待、忽略，或进入工具/知识/目标重想路径。
2. 只有在确实需要回复类动作时，thought 才允许非空。
3. retrieve_keys 只在 REPLY 类动作下使用，否则必须为空数组。
4. 不要编造系统、工具、模型等底层信息。
"""


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

# ==========================================
    # [新增 P1-T1] 动态行为修饰器
    # ==========================================
    def _build_dynamic_actions(self, state, message: str, window_events_count: int) -> str:
        """
        动态行为修饰器：根据状态 + 概率 + 关键词条件，
        构建当前可用的动作列表文本，注入 Judge Prompt。
        """
        actions = [
            "REPLY: 需要立刻回复（包含问题、提及你、或顺着话题聊）",
            "WAIT: 话似乎没说完，稍微等等看",
            "IGNORE: 没兴趣理会的闲聊或刷屏",
        ]
        
        # TOOL_CALL: 仅当消息含有任务性关键词时激活
        task_keywords = ["帮我", "查", "写", "翻译", "分析", "搜索", "提醒"]
        if any(kw in message for kw in task_keywords):
            actions.append("TOOL_CALL: 明确的指令求助，需要调用外部工具")
        
        # FETCH_KNOWLEDGE: 20% 概率随机激活 或 消息含问号时必定激活
        if "?" in message or "？" in message or any(kw in message for kw in ["为什么", "怎么", "啥", "什么", "解释", "查", "搜索", "资料"]):
            actions.append("FETCH_KNOWLEDGE: 你突然想去翻翻记忆/知识库确认一下（灵光一闪）")
        
        # RETHINK_GOAL: 窗口内事件 >= 5 条时以 30% 概率激活
        if window_events_count >= 5:
            actions.append("RETHINK_GOAL: 聊了一阵子了，你想重新审视一下自己的对话目标")
            
        return "\n".join(f"- {a}" for a in actions)

    # ==========================================
    # [修改 P1-T1, P1-T2] 主判决函数
    # ==========================================
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
            # 【架构级升级】: 提取与 System 2 对齐的历史记忆
            # =====================================================================
            def _flatten_content(raw_val: any) -> str:
                if not raw_val: return ""
                if isinstance(raw_val, str):
                    try:
                        parsed = json.loads(raw_val)
                        if isinstance(parsed, list): raw_val = parsed
                        else: return raw_val
                    except Exception:
                        return raw_val 
                
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
                history_limit = 8
                history_records = []
                persistence = getattr(self.state_engine, "persistence", None)
                
                if persistence:
                    if hasattr(persistence, "get_recent_messages"):
                        history_records = await persistence.get_recent_messages(chat_id, limit=history_limit)
                    elif hasattr(persistence, "get_chat_history"):
                        history_records = await persistence.get_chat_history(chat_id, limit=history_limit)
                        
                if history_records:
                    history_context = f"【近期对话 ({len(history_records)} 条)】\n"
                    for record in history_records:
                        if isinstance(record, dict):
                            sender = record.get("sender_name") or record.get("role") or "User"
                            raw_content = record.get("content") or record.get("message") or ""
                        else:
                            sender = getattr(record, "sender_name", getattr(record, "role", "User"))
                            raw_content = getattr(record, "content", getattr(record, "message", ""))
                            
                        clean_content = _flatten_content(raw_content)
                        if clean_content:
                            history_context += f"{sender}: {clean_content[:60]}\n"
                    history_context += "\n"
            except Exception as e:
                logger.debug(f"[{chat_id}] ⚠️ 提取 Sys1 历史上下文失败，安全回落: {e}")

            # =====================================================================
            # 🟢 [新增 P1-T2] 关键词反应注入
            # =====================================================================
            keyword_reaction_block = ""
            kw_reactions = getattr(self.config.system1, 'keyword_reactions', [])
            if kw_reactions:
                matched = []
                for rule in kw_reactions:
                    parts = rule.split(":", 1) if ":" in rule else rule.split("：", 1)
                    if len(parts) == 2:
                        kw, reaction = parts[0].strip(), parts[1].strip()
                        if kw.lower() in msg_lower:
                            matched.append(reaction)
                if matched:
                    keyword_reaction_block = "\n【关键词触发的特殊心理状态】\n" + "\n".join(f"- {r}" for r in matched) + "\n"

            # =====================================================================
            # 🟢 [新增 P1-T1] 动态可用动作构建
            # =====================================================================
            available_actions = self._build_dynamic_actions(state, message, window_events_count)

            # =====================================================================
            # 【正常执行 System 1 唤醒大模型判决】
            # =====================================================================
            prompt = f"""
            你是群聊中的这个角色的潜意识大脑，请完全沉浸于以下设定中：
            [你的核心人设]: {persona_summary if persona_summary else '保持你原本的性格特征'}
            {keyword_reaction_block}
            当前群聊情绪: {state.mood:.2f} (-1.0 到 1.0)。
            
            {history_context}
            【近期发生的连续对话 (请重点基于以上历史语境和以下近期对话进行最终裁决)】:
            {message}
            
            【思考与决策流】
            1. 意图判决 (action): 请从以下【当前可用动作】中选择一个：
            {available_actions}
            
            2. 潜意识生成 (thought): **仅当 action 为 REPLY、TOOL_CALL、FETCH_KNOWLEDGE 或 RETHINK_GOAL 时**，你需要以第一人称和角色语气，生成一段你此刻脑海中一闪而过的内心戏。如果决定 WAIT 或 IGNORE，请严格留空。
            
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
            
            并且，请评估【上述近期对话】对你产生的【情绪影响】。
            可用情绪标签 (mood_tag)：happy(积极/开心), sad(悲伤/遗憾), angry(生气/抱怨), neutral(平静/客观), curious(好奇/困惑), surprise(惊讶)
            
            请严格按照以下 JSON 格式输出（必须先输出 reason 进行极简逻辑推理）：
            {{
                "reason": "极简的判定理由，例如：'有人在提问' 或 '顺着刚才的话题在聊'（限20字内）",
                "action": "REPLY"|"WAIT"|"IGNORE"|"TOOL_CALL"|"FETCH_KNOWLEDGE"|"RETHINK_GOAL",
                "thought": "【仅当 action 选中需要回复的类型时生成】第一人称的真实内心戏。不回复请严格输出空字符串 \"\"",
                "relevance": int(1-10),
                "necessity": float(1.0-10.0),
                "retrieve_keys": ["key1"],
                "mood_tag": "happy/sad/angry/neutral/curious/surprise",
                "mood_delta": 0.0
            }}
            说明：mood_delta 为情绪变化值（范围 -0.5 到 0.5）。受到夸奖/喜爱时为正数，受到辱骂/指责时为负数，平常对话为 0.0。
            """
            
            plan = BrainActionPlan()
            try:
                if getattr(self.config.global_settings, 'debug_mode', True):
                    logger.info(
                        f"\n{'='*60}\n"
                        f"⚡ [System 1 意图判决探针] 目标: {chat_id}\n"
                        f"{'='*60}\n"
                        f"{prompt}\n"
                        f"{'='*60}"
                    )

                lane_key = LaneKey(subsystem="sys1", task_family="judge", scope_id=chat_id)
                task_models = getattr(self.config.provider, 'task_models', [])
                llm_result = await self.gateway.chat_in_lane_result(
                    lane_key=lane_key,
                    base_origin=chat_id,
                    prompt=prompt,
                    system_prompt=JUDGE_STABLE_PREFIX,
                    models=task_models,
                    is_json=True,
                    use_fallback=False,
                )
                result = llm_result.parsed_json or {}
                
                plan.action = result.get("action", "IGNORE").upper()
                if plan.action in ["REPLY", "TOOL_CALL", "FETCH_KNOWLEDGE", "RETHINK_GOAL"]:
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
                
                # 🟢 [修改 P1-T1] 校验扩展的合法动作并执行降级处理
                valid_actions = ["REPLY", "WAIT", "IGNORE", "TOOL_CALL", "FETCH_KNOWLEDGE", "RETHINK_GOAL"]
                if plan.action not in valid_actions:
                    plan.action = "IGNORE"
                
                # 动作降级 (保护下游 Planner 路由不被破坏)
                if plan.action == "FETCH_KNOWLEDGE":
                    plan.action = "REPLY"
                    plan.meta["retrieve_keys"] = plan.meta.get("retrieve_keys", []) or ["ALL"]
                elif plan.action == "RETHINK_GOAL":
                    plan.action = "REPLY"
                    plan.meta["force_rethink_goal"] = True

                mood_tag = result.get("mood_tag", "neutral")
                try:
                    mood_delta = float(result.get("mood_delta", 0.0))
                except (ValueError, TypeError):
                    mood_delta = 0.0
                    
                if mood_delta != 0.0:
                    new_mood = await self.state_engine.atomic_update_mood(chat_id, delta=mood_delta)
                    logger.debug(f"[{chat_id}] 😃 接收消息后情绪更新: {mood_tag} (变动 {mood_delta:+.2f} -> {new_mood:.2f})")
                
                if "FriendMessage" in chat_id and plan.action in ["WAIT", "IGNORE"]:
                    plan.action = "REPLY"
                    plan.thought = "私聊模式强制兜底：无论多无聊的消息都必须给予反馈。"
                    logger.debug(f"[{chat_id}] 🛡️ [私聊特权] 判定为忽略或等待，已强行覆写为 REPLY 以保证绝对专注。")
                    
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
