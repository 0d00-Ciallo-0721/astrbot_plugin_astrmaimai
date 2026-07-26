# astrmai/Heart/judge.py
from ...infrastructure.gateway import GlobalModelGateway
from ...infrastructure.runtime.lane_manager import LaneKey
from ...state.chat_state_service import StateEngine
import datetime
import inspect
import time
import json
from astrbot.api import logger
from .action_plan import BrainActionPlan


from .judge_prompt import JUDGE_STABLE_PREFIX


class Judge:
    """
    判官 (System 1: Fused 3-State Version)
    职责: 决定 System 2 的初步动作倾向 (REPLY, WAIT, IGNORE)
    """
    BASE_ACTIONS = ("REPLY", "WAIT", "IGNORE")
    TOOL_ACTION = "TOOL_CALL"
    ACTION_DESCRIPTIONS = {
        "REPLY": "需要立刻回复（包含问题、提及你、或顺着话题聊）",
        "WAIT": "话似乎没说完，稍微等等看",
        "IGNORE": "没兴趣理会的闲聊或刷屏",
        "TOOL_CALL": "明确的指令求助，需要调用外部工具",
    }
    TASK_KEYWORDS = ("帮我", "查", "写", "翻译", "分析", "搜索", "提醒")

    def __init__(self, gateway: GlobalModelGateway, state_engine: StateEngine, config=None):
        self.gateway = gateway
        self.state_engine = state_engine
        self.config = config if config else gateway.config
        # ponytail: plain set() for Sys1 group mutex — asyncio is single-threaded
        # within event loop, so no true concurrency hazard. Add asyncio.Lock if
        # groups share event loops.
        self.active_sys1_groups = set()
        self.DEFAULT_HISTORY_LIMIT = 8
        self.NORMAL_HISTORY_MAX_AGE_SECONDS = 900.0
        self.WAKEUP_HISTORY_MAX_AGE_SECONDS = 1800.0
        self.PRIMARY_MOOD_MICROADJUST_THRESHOLD = 0.15
        self.PRIMARY_MOOD_MICROADJUST_SCALE = 0.25

    def refresh_config(self, config):
        self.config = config

    @classmethod
    def _available_action_names(cls, message: str) -> tuple[str, ...]:
        actions = list(cls.BASE_ACTIONS)
        if any(keyword in str(message or "") for keyword in cls.TASK_KEYWORDS):
            actions.append(cls.TOOL_ACTION)
        return tuple(actions)

# ==========================================
    # [新增 P1-T1] 动态行为修饰器
    # ==========================================
    def _build_dynamic_actions(self, state, message: str, window_events_count: int) -> str:
        """
        动态行为修饰器：根据状态 + 概率 + 关键词条件，
        构建当前可用的动作列表文本，注入 Judge Prompt。
        """
        return "\n".join(
            f"- {action}: {self.ACTION_DESCRIPTIONS[action]}"
            for action in self._available_action_names(message)
        )

    # ==========================================
    # [修改 P1-T1, P1-T2] 主判决函数
    # ==========================================
    @staticmethod
    def _flatten_history_content(raw_val: any) -> str:
        if not raw_val:
            return ""
        if isinstance(raw_val, str):
            try:
                parsed = json.loads(raw_val)
                if isinstance(parsed, list):
                    raw_val = parsed
                else:
                    return raw_val
            except Exception:
                return raw_val

        if isinstance(raw_val, list):
            text_parts = []
            for item in raw_val:
                if isinstance(item, dict):
                    item_type = item.get("type", item.get("component", "")).lower()
                    if item_type in ["text", "plain"]:
                        text_parts.append(str(item.get("text", "")))
                    elif item_type in ["image"]:
                        text_parts.append("[image]")
                    elif item_type in ["at"]:
                        text_parts.append("[@mention]")
                elif hasattr(item, "type") or hasattr(item, "__class__"):
                    item_type = getattr(item, "type", item.__class__.__name__).lower()
                    if item_type in ["text", "plain"]:
                        text_parts.append(str(getattr(item, "text", "")))
                    elif item_type in ["image"]:
                        text_parts.append("[image]")
                    elif item_type in ["at"]:
                        text_parts.append("[@mention]")
            return "".join(text_parts)
        return str(raw_val)

    @staticmethod
    def _extract_history_timestamp(record) -> float:
        timestamp = None
        if isinstance(record, dict):
            timestamp = (
                record.get("timestamp")
                or record.get("created_at")
                or record.get("updated_at")
            )
        else:
            timestamp = (
                getattr(record, "timestamp", None)
                or getattr(record, "created_at", None)
                or getattr(record, "updated_at", None)
            )
        if timestamp is None:
            return 0.0
        if isinstance(timestamp, (int, float)):
            return float(timestamp) if float(timestamp) > 0 else 0.0
        if hasattr(timestamp, "timestamp") and callable(timestamp.timestamp):
            try:
                converted = float(timestamp.timestamp())
                return converted if converted > 0 else 0.0
            except Exception:
                return 0.0
        if isinstance(timestamp, str):
            stripped = timestamp.strip()
            if not stripped:
                return 0.0
            try:
                converted = float(stripped)
                return converted if converted > 0 else 0.0
            except Exception:
                pass
            try:
                normalized = stripped.replace("Z", "+00:00")
                converted = datetime.datetime.fromisoformat(normalized).timestamp()
                return converted if converted > 0 else 0.0
            except Exception:
                return 0.0
        return 0.0

    @staticmethod
    async def _maybe_await(result):
        if inspect.isawaitable(result):
            return await result
        return result

    async def _load_recent_history_records(
        self,
        chat_id: str,
        *,
        max_age_seconds: float,
        limit: int,
    ) -> list:
        persistence = getattr(self.state_engine, "persistence", None)
        if not persistence:
            return []

        database_service = (
            getattr(persistence, "database_service", None)
            or getattr(persistence, "db_service", None)
            or getattr(self.gateway, "db_service", None)
        )
        chat_repository = getattr(database_service, "chat_repository", None) if database_service else None
        sources = []

        if hasattr(persistence, "get_recent_messages"):
            sources.append(lambda: persistence.get_recent_messages(chat_id, limit=limit))
        if hasattr(persistence, "get_chat_history"):
            sources.append(lambda: persistence.get_chat_history(chat_id, limit=limit))
        if database_service and hasattr(database_service, "get_recent_message_logs"):
            sources.append(
                lambda: database_service.get_recent_message_logs(
                    chat_id,
                    limit=limit,
                    max_age_seconds=max_age_seconds,
                    include_processed=True,
                )
            )
        if chat_repository and hasattr(chat_repository, "get_recent_message_logs"):
            sources.append(
                lambda: chat_repository.get_recent_message_logs(
                    chat_id,
                    limit=limit,
                    max_age_seconds=max_age_seconds,
                    include_processed=True,
                )
            )

        now = time.time()
        for loader in sources:
            try:
                raw_records = await self._maybe_await(loader())
            except Exception as exc:
                logger.debug(f"[{chat_id}] Judge history source failed: {exc}")
                continue
            if not raw_records:
                continue
            filtered = []
            for record in list(raw_records):
                timestamp = self._extract_history_timestamp(record)
                if timestamp <= 0:
                    continue
                delta = now - timestamp
                if delta < 0:  # ponytail: NTP guard
                    logger.warning(f"[AstrMai-judge] clock skew: msg ts {timestamp} > now {now}")
                    delta = 0
                if delta > max_age_seconds:
                    continue
                filtered.append(record)
            if not filtered:
                continue
            filtered.sort(key=self._extract_history_timestamp)
            return filtered[-limit:]
        return []

    def _render_history_context(self, records: list) -> str:
        if not records:
            return ""
        lines = []
        for record in records:
            if isinstance(record, dict):
                sender = record.get("sender_name") or record.get("role") or "User"
                raw_content = record.get("content") or record.get("message") or ""
            else:
                sender = getattr(record, "sender_name", getattr(record, "role", "User"))
                raw_content = getattr(record, "content", getattr(record, "message", ""))
            clean_content = self._flatten_history_content(raw_content).strip()
            timestamp = self._extract_history_timestamp(record)
            if not clean_content or timestamp <= 0:
                continue
            timestamp_text = datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
            lines.append(f"[{timestamp_text}] {sender}: {clean_content[:60]}")
        if not lines:
            return ""
        return f"[Recent dialogue snippets ({len(lines)})]\n" + "\n".join(lines) + "\n\n"

    @staticmethod
    def _extract_event_timestamp(event) -> float:
        timestamp = 0.0
        if hasattr(event, "get_extra"):
            timestamp = event.get_extra("astrmai_timestamp", 0.0)
        if not timestamp:
            timestamp = getattr(event, "timestamp", 0.0)
        try:
            converted = float(timestamp or 0.0)
            return converted if converted > 0 else 0.0
        except (TypeError, ValueError):
            return 0.0

    def _render_window_events_context(self, events: list | None, *, focus_event=None) -> str:
        if not events:
            return ""
        lines = []
        for event in events:
            if focus_event is not None and event is focus_event:
                continue
            timestamp = self._extract_event_timestamp(event)
            if timestamp <= 0:
                continue
            sender = event.get_sender_name() if hasattr(event, "get_sender_name") else "User"
            if hasattr(event, "get_extra"):
                raw_content = event.get_extra("astrmai_rich_text", getattr(event, "message_str", ""))
            else:
                raw_content = getattr(event, "message_str", "")
            clean_content = self._flatten_history_content(raw_content).strip()
            if not clean_content:
                continue
            timestamp_text = datetime.datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d %H:%M")
            lines.append(f"[{timestamp_text}] {sender or 'User'}: {clean_content[:60]}")
        if not lines:
            return ""
        return f"[Attention window snippets ({len(lines)})]\n" + "\n".join(lines) + "\n\n"

    async def evaluate(
        self,
        chat_id: str,
        message: str,
        is_force_wakeup: bool,
        persona_summary: str = "",
        window_events_count: int = 1,
        is_first_event_wakeup: bool = False,
        *,
        focus_thread=None,
        window_events: list | None = None,
        focus_event=None,
    ) -> BrainActionPlan:
        """
        输出结构化的 BrainActionPlan，融合了 HeartFlow 的评分机制和 3 态决策。
        [修改]: 注入系统级共享配置的短期历史对话，并进行 JSON 组件树的纯文本扁平化，提供极致的沉浸式语境。
        """
        import time
        from astrbot.api import logger
        from .action_plan import BrainActionPlan

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
                # ponytail: time.time() for cold-chat check; NTP risk accepted (last_reply_time stored as time.time())
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
            history_context = ""
            try:
                history_context = self._render_window_events_context(
                    window_events,
                    focus_event=focus_event,
                )
                if not history_context:
                    history_max_age = (
                        self.WAKEUP_HISTORY_MAX_AGE_SECONDS
                        if (is_force_wakeup or is_keyword_wakeup)
                        else self.NORMAL_HISTORY_MAX_AGE_SECONDS
                    )
                    history_records = await self._load_recent_history_records(
                        chat_id,
                        max_age_seconds=history_max_age,
                        limit=self.DEFAULT_HISTORY_LIMIT,
                    )
                    
                    history_context = self._render_history_context(history_records)
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
            available_action_names = self._available_action_names(message)
            action_schema = '"|"'.join(available_action_names)

            # =====================================================================
            # 【正常执行 System 1 唤醒大模型判决】
            # =====================================================================
            # OPT-08/RT-09: 固定 rubric 已迁入 JUDGE_STABLE_PREFIX（system，可被前缀
            # 缓存），此处只留动态段并按"半稳定在前、易变在后"排序
            prompt = f"""
            你是群聊中的这个角色的潜意识大脑，请完全沉浸于以下设定中：
            [你的核心人设]: {persona_summary if persona_summary else '保持你原本的性格特征'}
            {keyword_reaction_block}
            【当前可用动作】(action 只能从中选择，取值: {action_schema}):
            {available_actions}

            当前群聊情绪: {state.mood:.2f} (-1.0 到 1.0)。

            {history_context}
            【近期发生的连续对话 (请重点基于以上历史语境和以下近期对话进行最终裁决)】:
            {message}
            """
            
            plan = BrainActionPlan()
            try:
                lane_key = LaneKey(subsystem="sys1", task_family="judge", scope_id=chat_id)
                task_models = getattr(self.config.provider, 'task_models', [])
                timing = getattr(self.config, "timing", None)
                judge_timeout = float(getattr(timing, "attention_judge_timeout_sec", 3.0) or 3.0)
                llm_result = await self.gateway.chat_in_lane_result(
                    lane_key=lane_key,
                    base_origin=chat_id,
                    prompt=prompt,
                    system_prompt=JUDGE_STABLE_PREFIX,
                    models=task_models,
                    is_json=True,
                    use_fallback=False,
                    event=focus_event,
                    timeout_override=judge_timeout,
                    max_retries_override=0,
                    max_models_override=1,
                    allow_cooldown_override=False,
                    reserve_for_reply=True,
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
                if plan.action not in available_action_names:
                    plan.action = "IGNORE"
                # ponytail: M4 — FETCH_KNOWLEDGE/RETHINK_GOAL downgrade removed; actions no longer offered to LLM

                mood_tag = result.get("mood_tag", "neutral")
                try:
                    mood_delta = float(result.get("mood_delta", 0.0))
                except (ValueError, TypeError):
                    mood_delta = 0.0
                    
                effective_mood_delta = mood_delta
                primary_mood_applied = bool(
                    focus_event is not None
                    and hasattr(focus_event, "get_extra")
                    and focus_event.get_extra("astrmai_primary_mood_applied", False)
                )
                if primary_mood_applied:
                    if abs(effective_mood_delta) < self.PRIMARY_MOOD_MICROADJUST_THRESHOLD:
                        effective_mood_delta = 0.0
                    else:
                        effective_mood_delta *= self.PRIMARY_MOOD_MICROADJUST_SCALE
                if effective_mood_delta != 0.0:
                    new_mood = await self.state_engine.atomic_update_mood(chat_id, delta=effective_mood_delta)
                    if focus_event is not None and hasattr(focus_event, "set_extra"):
                        focus_event.set_extra("astrmai_judge_mood_delta", effective_mood_delta)
                    logger.debug(
                        f"[{chat_id}] 😃 接收消息后情绪更新: {mood_tag} "
                        f"(judge_delta {mood_delta:+.2f} -> applied {effective_mood_delta:+.2f} -> {new_mood:.2f})"
                    )
                
                if "FriendMessage" in chat_id and plan.action in ["WAIT", "IGNORE"]:
                    plan.action = "REPLY"
                    plan.thought = "私聊模式强制兜底：无论多无聊的消息都必须给予反馈。"
                    logger.debug(f"[{chat_id}] 🛡️ [私聊特权] 判定为忽略或等待，已强行覆写为 REPLY 以保证绝对专注。")
                    
                elapsed = time.perf_counter() - start_time
                reason = result.get("reason", "")
                logger.debug(f"[{chat_id}] Judge耗时 {elapsed:.2f}s | Action: {plan.action} | 理由: {reason}")
                
                return plan
            
            except Exception as e:
                logger.exception(f"[{chat_id}] Judge LLM failed, defaulting to REPLY: {e}")
                plan.action = "REPLY" 
                plan.meta["retrieve_keys"] = []
                return plan
            
        finally:
            self.active_sys1_groups.discard(chat_id)
