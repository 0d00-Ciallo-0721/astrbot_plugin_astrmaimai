import asyncio
import json
import re
import time
from typing import Optional, List, Dict

from astrbot.api import logger

from ..contracts.memory_query import MemoryWriteRequest
from ...infrastructure.context_economy import PromptTemplateId
from ...infrastructure.runtime.lane_manager import LaneKey
from .memory_processor import MemoryProcessor
from .topic_summarizer import TopicSummarizer

class ChatHistorySummarizer:
    """
    历史摘要清道夫 (System 2 / Memory Lifecycle)
    阶段二重构：废弃旧版扁平陈述句提取，接入 Cognitive Processor 实现高密度知识提取。
    """
    _INSTANT_PATTERNS = [
        ("identity", re.compile(r"我(?:叫|是|名字(?:是|叫))\s*(\S{1,20})")),
        ("contact", re.compile(r"(?:手机|电话|微信|QQ|邮箱)[号码]*\s*[:：]?\s*(\S{5,30})")),
        ("preference", re.compile(r"我(?:喜欢|讨厌|最爱|不吃|不喜欢|偏好)\s*(.{2,40})")),
        ("relationship", re.compile(r"(?:男朋友|女朋友|老公|老婆|分手|结婚|离婚|恋爱)")),
        ("major_event", re.compile(r"(?:住院|去世|毕业|入职|辞职|搬家|怀孕|生了)")),
        ("explicit_cmd", re.compile(r"(?:记住|别忘了|记下来|帮我记|你要记得)")),
    ]

    def __init__(self, context, gateway, engine, config=None):
        self.context = context
        self.gateway = gateway
        self.engine = engine
        self.config = config if config else gateway.config
        self.prompt_registry = getattr(getattr(gateway, "context_economy", None), "templates", None)
        
        self._running = False
        self._periodic_task = None
        
        # 从配置读取阈值
        self.check_interval = getattr(self.config.memory, 'cleanup_interval', 3600)
        self.msg_threshold = getattr(self.config.memory, 'summary_threshold', 30)
        
        # 挂载认知处理器
        self.processor = MemoryProcessor(gateway)
        # Phase 3: 话题图谱概括器
        self.topic_summarizer = TopicSummarizer(gateway, config)
        
        # 🟢 [新增] 接管原本在 main.py 里的高内聚状态变量，消除在 main 中的松散耦合
        self._session_history_buffer = {}
        self._memory_locks = {}
        self._background_tasks = set()
        self._instant_llm_last_check: dict[str, float] = {}

    def _build_topic_messages(self, chat_history_text: str) -> List[Dict]:
        messages = []
        for index, raw_line in enumerate(chat_history_text.splitlines()):
            line = raw_line.strip()
            if not line:
                continue

            match = re.match(r"^\[(?P<time>[^\]]+)\]\s*(?P<sender>[^:]+):\s*(?P<content>.*)$", line)
            if match:
                sender = match.group("sender").strip()
                content = match.group("content").strip()
            else:
                sender = "unknown"
                content = line

            if content:
                messages.append(
                    {
                        "sender": sender,
                        "content": content,
                        "timestamp": float(index),
                    }
                )
        return messages

    async def start(self):
        """启动后台定期检查循环"""
        if self._running:
            return
        self._running = True
        self._periodic_task = asyncio.create_task(self._periodic_check_loop())
        logger.info(f"[Memory Summarizer] ♻️ 已启动结构化记忆清道夫循环 (Interval: {self.check_interval}s)")

    async def stop(self):
        """停止后台定期检查循环"""
        self._running = False
        if self._periodic_task and not self._periodic_task.done():
            self._periodic_task.cancel()

    async def describe_session_eligibility(self, chat_id: str) -> Dict:
        threshold_messages = int(getattr(self.config.memory, 'summary_threshold', 30) or 30) * 2
        session_data = self._session_history_buffer.get(chat_id) or {}
        buffer = list(session_data.get("buffer", []) or [])
        pending_messages = len(buffer)
        cooldown_until = float(session_data.get("cooldown_until", 0.0) or 0.0)
        now = time.time()
        candidate_present = pending_messages > 0
        eligible = candidate_present and pending_messages >= threshold_messages and now >= cooldown_until
        if not candidate_present:
            reason = "no_buffer"
        elif now < cooldown_until:
            reason = "cooldown"
        elif pending_messages < threshold_messages:
            reason = "below_threshold"
        else:
            reason = "eligible"
        return {
            "eligible": eligible,
            "candidate_present": candidate_present,
            "reason": reason,
            "pending_messages": pending_messages,
            "history_size": pending_messages,
            "threshold_messages": threshold_messages,
            "cooldown_until": cooldown_until,
            "last_memory_run_at": float(session_data.get("last_run_at", 0.0) or 0.0),
            "last_update": float(session_data.get("last_update", 0.0) or 0.0),
        }

    async def run_once_for_session(self, chat_id: str) -> Dict:
        threshold = int(getattr(self.config.memory, 'summary_threshold', 30) or 30)
        now = time.time()
        lock = self._get_memory_lock(chat_id)
        async with lock:
            if chat_id not in self._session_history_buffer:
                self._session_history_buffer[chat_id] = {"buffer": [], "last_update": now, "cooldown_until": 0, "failures": 0}
            session_data = self._session_history_buffer[chat_id]
            buffer = list(session_data.get("buffer", []) or [])
            cooldown_until = float(session_data.get("cooldown_until", 0.0) or 0.0)
            if not buffer:
                return {"performed": False, "reason": "no_buffer", "pending_messages": 0}
            if now < cooldown_until:
                return {
                    "performed": False,
                    "reason": "cooldown",
                    "pending_messages": len(buffer),
                    "cooldown_until": cooldown_until,
                }
            if len(buffer) < threshold * 2:
                return {
                    "performed": False,
                    "reason": "below_threshold",
                    "pending_messages": len(buffer),
                    "threshold_messages": threshold * 2,
                }
            messages_to_process = buffer.copy()
            session_data["buffer"] = []

        history_text = "\n".join(messages_to_process)
        try:
            await self.summarize_session(session_id=chat_id, chat_history_text=history_text)
            completed_at = time.time()
            async with lock:
                current_data = self._session_history_buffer.get(chat_id, {"buffer": [], "cooldown_until": 0, "failures": 0})
                current_data["failures"] = 0
                current_data["cooldown_until"] = 0
                current_data["last_run_at"] = completed_at
                current_data["last_update"] = completed_at
                self._session_history_buffer[chat_id] = current_data
            return {
                "performed": True,
                "reason": "summarized",
                "pending_messages_processed": len(messages_to_process),
                "last_memory_run_at": completed_at,
            }
        except asyncio.CancelledError:
            async with lock:
                current_data = self._session_history_buffer.get(chat_id, {"buffer": [], "cooldown_until": 0, "failures": 0})
                current_data["buffer"] = messages_to_process + list(current_data.get("buffer", []) or [])
                current_data["last_update"] = time.time()
                self._session_history_buffer[chat_id] = current_data
            raise
        except Exception as e:
            logger.error(f"[AstrMai-Memory] memory maintenance degraded for {chat_id}: {e}")
            async with lock:
                current_data = self._session_history_buffer.get(chat_id, {"buffer": [], "cooldown_until": 0, "failures": 0})
                merged_buffer = messages_to_process + list(current_data.get("buffer", []) or [])
                max_capacity = threshold * 3
                if len(merged_buffer) > max_capacity:
                    merged_buffer = merged_buffer[-max_capacity:]
                current_data["buffer"] = merged_buffer
                current_data["last_update"] = time.time()
                failures = int(current_data.get("failures", 0) or 0) + 1
                current_data["failures"] = failures
                current_data["cooldown_until"] = time.time() + min(3600, 300 * (2 ** (failures - 1)))
                self._session_history_buffer[chat_id] = current_data
                cooldown_until = float(current_data.get("cooldown_until", 0.0) or 0.0)
            return {
                "performed": False,
                "reason": "summary_failed",
                "pending_messages_restored": len(merged_buffer),
                "cooldown_until": cooldown_until,
            }

    async def ingest_committed_turn(
        self,
        chat_id: str,
        user_text: str,
        assistant_text: str,
        *,
        source: str,
        is_proactive: bool = False,
    ) -> Dict:
        normalized_user = str(user_text or "").strip()
        normalized_assistant = str(assistant_text or "").strip()
        if not normalized_user or not normalized_assistant:
            return {"performed": False, "reason": "empty_turn", "source": source}
        if is_proactive:
            return {"performed": False, "reason": "proactive_ignored", "source": source}

        await self.pump_memory_reflection(chat_id, normalized_user, normalized_assistant)
        return {
            "performed": True,
            "reason": "ingested",
            "source": source,
            "pending_messages": len((self._session_history_buffer.get(chat_id) or {}).get("buffer", []) or []),
        }

    async def extract_and_summarize_history(self, session_id: str, days: int = 1):
        """[新增] 从底层数据库批量拉取历史消息，格式化后进行摘要。完美整合 chat_history_extract 提取大段历史的逻辑"""
        import time
        
        plugin = getattr(self.context, 'astrmai_plugin', None) or getattr(self.gateway.context, 'astrmai', None)
        if not plugin or not hasattr(plugin, 'db_service'):
            return
            
        db = plugin.db_service
        try:
            from sqlmodel import select
            from ...infrastructure.persistence import MessageLog
            
            cutoff_time = time.time() - (days * 86400)
            
            def fetch_logs_sync():
                with db.get_session() as session:
                    statement = select(MessageLog).where(
                        MessageLog.group_id == session_id,
                        MessageLog.timestamp >= cutoff_time
                    ).order_by(MessageLog.timestamp.asc())
                    results = session.exec(statement).all()
                    return [MessageLog.model_validate(r.model_dump()) for r in results]
                    
            import asyncio
            logs = await asyncio.to_thread(fetch_logs_sync)
            if not logs:
                return
                
            history_lines = []
            topic_messages = []
            for index, log in enumerate(logs):
                content = log.content
                if not content: continue
                # 避免单条数据过长冲毁上下文
                if len(content) > 2000:
                    content = content[:2000] + "..."
                    
                time_str = time.strftime("%H:%M:%S", time.localtime(log.timestamp))
                history_lines.append(f"[{time_str}] {log.sender_name}: {content}")
                topic_messages.append(
                    {
                        "sender": log.sender_name,
                        "content": content,
                        "timestamp": log.timestamp if log.timestamp is not None else float(index),
                    }
                )
                
            full_history = "\n".join(history_lines)
            
            if full_history:
                await self.summarize_session(session_id, full_history, messages=topic_messages)
                
        except Exception as e:
            logger.error(f"[Memory Summarizer] 批量历史提取异常: {e}", exc_info=True)

    # 位置: astrmai/memory/summarizer.py -> ChatHistorySummarizer 类下
    async def _periodic_check_loop(self):
        """[修改] 定期轮询时使用批量提取合并记录 + Phase 7.2 遗忘机制"""
        import asyncio
        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
                active_sessions = list(self._session_history_buffer.keys())
                for session_id in active_sessions:
                    await self.extract_and_summarize_history(session_id, days=1)
                
                # Phase 7.2 遗忘机制：清理低权重垃圾记忆
                if hasattr(self.engine, 'prune_low_importance'):
                    threshold = getattr(self.config.memory, 'prune_threshold', 0.2) if self.config else 0.2
                    await self.engine.prune_low_importance(threshold=threshold)
                    
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Memory Summarizer] 后台循环异常: {e}")

    async def summarize_session(self, session_id: str, chat_history_text: str, persona_id: Optional[str] = None, messages: Optional[List[Dict]] = None):
        """[修改] 核心记忆提炼流水线，保存反思和记忆节点到多表数据库"""
        if not chat_history_text.strip():
            return
            
        logger.info(f"[Memory Summarizer] 🧠 启动后台任务: 正在对 Session {session_id} 的历史记录进行多维认知降维...")

        # ==================================================
        # Gap 4 修复: Phase 3 TopicSummarizer 正式接入流水线
        # 先进行话题分割，再分别进行认知降维，实现话题级记忆
        # ==================================================
        try:
            topic_messages = messages or self._build_topic_messages(chat_history_text)
            topic_segments = []
            if topic_messages:
                topic_segments = await self.topic_summarizer.process_history(
                    messages=topic_messages,
                    session_id=session_id
                )
            if topic_segments:
                logger.info(
                    f"[Memory Summarizer] 📊 话题分割完成: "
                    f"Session {session_id} → {len(topic_segments)} 个话题段"
                )
                
                # ==========================================
                # 🟢 [修改 P2-T3] 调用 engine 的话题合并去重写入方法
                # 替代原有的 for 循环逐个无脑 add_memory
                # ==========================================
                if hasattr(self.engine, 'store_topic_results'):
                    await self.engine.store_topic_results(
                        topic_results=topic_segments, 
                        session_id=session_id,
                        persona_id=persona_id
                    )
                else:
                    # 兼容性降级，防止报错
                    for seg in topic_segments:
                        seg_text = seg.get("summary", "")
                        seg_keywords = seg.get("topic_keywords", [])
                        seg_importance = seg.get("importance", 0.0)
                        if seg_text and seg_importance >= 0.2:
                            topic_content = f"【话题摘要】{seg_text}"
                            if seg_keywords:
                                topic_content += f"\n【关键词】{', '.join(seg_keywords[:5])}"
                            await self.engine.add_memory(
                                content=topic_content,
                                session_id=session_id,
                                importance=min(1.0, seg_importance)
                            )
                            
                logger.info(
                    f"[Memory Summarizer] ✅ 话题级记忆入库完成: "
                    f"{len([s for s in topic_segments if s.get('importance', 0) >= 0.2])} 条有效话题"
                )
        except Exception as e:
            logger.warning(f"[Memory Summarizer] ⚠️ TopicSummarizer 失败，降级到全局摘要: {e}")
            
        # 原有全局认知降维（兜底）
        try:
            memory_data = await self.processor.process_conversation(
                chat_history_text,
                session_id=session_id,
            )
        except TypeError:
            memory_data = await self.processor.process_conversation(chat_history_text)

        
        if not isinstance(memory_data, dict):
            logger.warning(f"[Memory Summarizer] ⚠️ Session {session_id} 认知处理返回异常格式，跳过提取。")
            return

        summary = memory_data.get("summary", "")
        key_facts = memory_data.get("key_facts", [])
        topics = memory_data.get("topics", [])
        sentiment = memory_data.get("sentiment", "neutral")
        reflection = memory_data.get("reflection", "无")
        nodes = memory_data.get("nodes", [])
        
        try:
            importance = float(memory_data.get("importance", 0.0))
        except (ValueError, TypeError):
            importance = 0.0
            
        if not isinstance(key_facts, list):
            key_facts = [str(key_facts)] if key_facts else []
        if not isinstance(topics, list):
            topics = [str(topics)] if topics else []
        
        if not key_facts and summary == "对话记录":
            logger.info(f"[Memory Summarizer] ⏭️ Session {session_id} 未提取到有效事实或信息，跳过入库。")
            return
            
        if importance < 0.2:
            logger.info(f"[Memory Summarizer] 📉 提取内容重要度过低 (importance={importance})，触发即时遗忘机制。")
            return

        # 🟢 分流一：保存记忆节点实体
        plugin = getattr(self.context, 'astrmai_plugin', None) or getattr(self.gateway.context, 'astrmai', None)
        if plugin and hasattr(plugin, 'db_service'):
            db = plugin.db_service
            from ...infrastructure.persistence import MemoryNode
            if nodes and hasattr(db, 'update_nodes_async'):
                node_objs = [MemoryNode(**n) for n in nodes if isinstance(n, dict)]
                await db.update_nodes_async(node_objs)

        # 🟢 分流二：富文本组装，喂给 Faiss Vector
        content_lines = [f"【摘要】{summary}"]
        
        valid_facts = [str(f) for f in key_facts if str(f).strip()]
        if valid_facts:
            content_lines.append("【核心事实】\n- " + "\n- ".join(valid_facts))
            
        if reflection and reflection != "无":
            content_lines.append(f"【深度反思】{reflection}")
            
        valid_topics = [str(t) for t in topics if str(t).strip()]
        if valid_topics:
            content_lines.append(f"【话题标签】{', '.join(valid_topics)}")
            
        final_content = "\n".join(content_lines)

        logger.info(f"[Memory Summarizer] ✨ Session {session_id} 记忆提炼成功 -> 摘要: {summary[:20]}... | 事实数: {len(valid_facts)} | 节点数: {len(nodes)}")

        try:
            # 存入引擎底层 (Vector + BM25)
            # 🟢 分流三：存入结构化的 Event 表扩充属性维度，便于回溯提取
            import time
            import uuid
            import datetime
            import json
            date_str = datetime.datetime.now().strftime("%Y-%m-%d")
            from ...infrastructure.persistence import MemoryEvent
            
            event_id = f"evt_{date_str.replace('-', '')}_{uuid.uuid4().hex[:8]}"
            canonical_id = ""
            if hasattr(self.engine, "write_service"):
                from ..contracts.memory_query import MemoryWriteRequest

                canonical_id = await self.engine.write_service.write(
                    MemoryWriteRequest(
                        source="memory_summary",
                        kind="topic" if valid_topics else "fact",
                        session_id=str(session_id),
                        persona_id=str(persona_id or ""),
                        content=final_content,
                        summary=str(summary or "")[:240],
                        tags=valid_topics,
                        importance=float(importance or 0.5),
                        confidence=0.8,
                        metadata={
                            "legacy_event_id": event_id,
                            "reflection": reflection,
                            "sentiment": sentiment,
                            "canonical_write": True,
                        },
                        dedup_key=f"memory_summary:{session_id}:{event_id}",
                        source_ref=f"MemoryEvent:{event_id}",
                    )
                )
            else:
                await self.engine.add_memory(
                    content=final_content,
                    session_id=str(session_id),
                    persona_id=persona_id,
                    importance=importance
                )
            event = MemoryEvent(
                event_id=event_id,
                session_id=str(session_id),
                date=date_str,
                narrative="\n".join(valid_facts),
                emotion=sentiment,
                importance=int(importance * 10),
                emotional_intensity=int(importance * 10),
                reflection=reflection,
                memory_kind="topic" if valid_topics else "fact",
                source_layer="topic" if valid_topics else "fact",
                tags=json.dumps([*valid_topics, f"canonical_id:{canonical_id}"] if canonical_id else valid_topics)
            )
            
            if plugin and hasattr(plugin, 'db_service') and hasattr(plugin.db_service, 'save_event_async'):
                await plugin.db_service.save_event_async(event)

            if hasattr(self.engine, "record_cognitive_feedback"):
                try:
                    feedback_parts = [str(summary or "")[:220]]
                    if valid_topics:
                        feedback_parts.append("topics: " + ", ".join(valid_topics[:6]))
                    if reflection and reflection != "无":
                        feedback_parts.append("reflection: " + str(reflection)[:180])
                    await self.engine.record_cognitive_feedback(
                        session_id=str(session_id),
                        source="memory_summary",
                        summary=" | ".join(part for part in feedback_parts if part.strip()),
                        guidance=(
                            "Use this consolidated memory only when it is directly relevant; "
                            "do not force older topics into the current reply."
                        ),
                        tags=valid_topics[:8],
                        importance=min(0.8, max(0.4, float(importance or 0.4))),
                    )
                except Exception as exc:
                    logger.debug(f"[Memory Summarizer] cognitive feedback write degraded: {exc}")

            logger.info(f"[Memory Summarizer] 💾 已将立体记忆成功压入 Faiss 向量数据库并落盘长期事件。")
        except Exception as e:
            logger.error(f"[Memory Summarizer] ❌ 记忆存储失败: {e}", exc_info=True)


# 文件位置: astrmai/memory/summarizer.py
# 新增函数: _get_memory_lock

    def _get_memory_lock(self, chat_id: str) -> asyncio.Lock:
        """[新增] 安全获取记忆缓冲区的原子操作锁"""
        lock = self._memory_locks.get(chat_id)
        if lock is None:
            import asyncio
            lock = asyncio.Lock()
            self._memory_locks[chat_id] = lock
        return lock

# 文件位置: astrmai/memory/summarizer.py
# 新增函数: _fire_and_forget

    async def _try_instant_memorize(self, chat_id: str, user_msg: str, ai_msg: str):
        text = str(user_msg or "").strip()
        if not hasattr(self.engine, "write_service"):
            return
        if len(text) < 4:
            return
        matched = self._rule_gate_match(text)
        if matched:
            category, extracted = matched
            content = f"[即时记忆|{category}] 用户说：{text}"
            await self.engine.write_service.write(
                MemoryWriteRequest(
                    source="instant_gate",
                    kind="fact",
                    session_id=str(chat_id),
                    content=content,
                    summary=extracted[:240],
                    importance=0.85,
                    confidence=0.9,
                    metadata={"gate_category": category, "instant_write": True},
                    dedup_key=f"instant_gate:{chat_id}:{category}:{extracted[:60]}",
                )
            )
            return

        if await self._should_run_instant_llm_backfill(chat_id):
            await self._try_instant_memorize_with_llm_v2(chat_id, text, ai_msg)

    def _rule_gate_match(self, user_msg: str):
        text = str(user_msg or "").strip()
        if len(text) < 4:
            return None
        for category, pattern in self._INSTANT_PATTERNS:
            m = pattern.search(text)
            if m:
                extracted = m.group(1) if m.lastindex else m.group(0)
                return category, extracted.strip()
        return None

    async def _should_run_instant_llm_backfill(self, chat_id: str) -> bool:
        gateway = getattr(self, "gateway", None)
        if not gateway or not hasattr(gateway, "call_data_process_task"):
            return False
        think_level = self._resolve_runtime_think_level()
        session_rounds = len((self._session_history_buffer.get(chat_id) or {}).get("buffer", [])) // 2
        if think_level < 2 and session_rounds < 5:
            return False
        now = asyncio.get_running_loop().time()
        last_check = float(self._instant_llm_last_check.get(chat_id, 0.0) or 0.0)
        if now - last_check < 120:
            return False
        self._instant_llm_last_check[chat_id] = now
        return True

    def _resolve_runtime_think_level(self) -> int:
        candidates = [
            getattr(getattr(self.gateway, "context", None), "event", None),
            getattr(self.gateway, "event", None),
            getattr(self.context, "event", None),
            getattr(self.context, "current_event", None),
        ]
        for event in candidates:
            if event is None:
                continue
            value = None
            if hasattr(event, "get_extra"):
                value = event.get_extra("astrmai_think_level", None)
            if value is None:
                try:
                    from ...conversation.contracts.turn_context import get_turn_context

                    turn_context = get_turn_context(event)
                    if turn_context is not None:
                        value = getattr(turn_context.cognitive, "think_level", None)
                except Exception:
                    value = None
            try:
                if value is not None:
                    return int(value or 0)
            except (TypeError, ValueError):
                continue
        return 0

    @staticmethod
    def _memory_lane_key(chat_id: str):
        lane_scope = str(chat_id or "").strip()
        if lane_scope and lane_scope != "global":
            return LaneKey(subsystem="bg", task_family="memory", scope_id=lane_scope, scope_kind="chat")
        logger.warning("[Memory Summarizer] global scope fallback engaged; expected a concrete chat/session id for memory backfill")
        return LaneKey(subsystem="bg", task_family="memory", scope_id="global", scope_kind="global")

    async def _try_instant_memorize_with_llm(self, chat_id: str, user_msg: str, ai_msg: str) -> None:
        gateway = getattr(self, "gateway", None)
        if not gateway or not hasattr(gateway, "call_data_process_task"):
            return
        prompt = (
            "这轮对话是否有值得长期记住的1条关键事实？返回JSON "
            '{"worth": bool, "fact": "..."}。\n'
            f"用户消息：{user_msg}\n"
            f"助手回复：{str(ai_msg or '')[:200]}"
        )
        try:
            response = await gateway.call_data_process_task(prompt=prompt, is_json=True)
        except TypeError:
            response = await gateway.call_data_process_task(prompt)
        except Exception as exc:
            logger.debug(f"[Memory Summarizer] instant llm backfill degraded: {exc}")
            return

        try:
            if isinstance(response, str):
                response = json.loads(response)
        except Exception:
            return
        if not isinstance(response, dict):
            return
        if not bool(response.get("worth")):
            return
        fact = str(response.get("fact") or "").strip()
        if len(fact) < 4:
            return
        await self.engine.write_service.write(
            MemoryWriteRequest(
                source="instant_gate_llm",
                kind="fact",
                session_id=str(chat_id),
                content=f"[即时记忆|llm_backfill] 用户说：{user_msg}",
                summary=fact[:240],
                importance=0.8,
                confidence=0.72,
                metadata={"gate_category": "llm_backfill", "instant_write": True},
                dedup_key=f"instant_gate_llm:{chat_id}:{fact[:60]}",
            )
        )

    async def _try_instant_memorize_with_llm_v2(self, chat_id: str, user_msg: str, ai_msg: str) -> None:
        gateway = getattr(self, "gateway", None)
        if not gateway or not hasattr(gateway, "call_data_process_task"):
            return
        if self.prompt_registry is None:
            await self._try_instant_memorize_with_llm(chat_id, user_msg, ai_msg)
            return
        envelope = self.prompt_registry.render_template(
            PromptTemplateId.MEMORY_INSTANT_BACKFILL,
            {
                "user_msg": user_msg,
                "ai_msg": str(ai_msg or "")[:200],
            },
        )
        try:
            response = await gateway.call_data_process_task(
                prompt=envelope.prompt,
                system_prompt=envelope.system_prompt,
                is_json=True,
                lane_key=self._memory_lane_key(chat_id),
                base_origin="",
                template_envelope=envelope,
            )
        except TypeError:
            response = await gateway.call_data_process_task(
                envelope.prompt,
                system_prompt=envelope.system_prompt,
                lane_key=self._memory_lane_key(chat_id),
                base_origin="",
            )
        except Exception as exc:
            logger.debug(f"[Memory Summarizer] instant llm backfill degraded: {exc}")
            return

        try:
            if isinstance(response, str):
                response = json.loads(response)
        except Exception:
            return
        if not isinstance(response, dict):
            return
        if not bool(response.get("worth")):
            return
        fact = str(response.get("fact") or "").strip()
        if len(fact) < 4:
            return
        await self.engine.write_service.write(
            MemoryWriteRequest(
                source="instant_gate_llm",
                kind="fact",
                session_id=str(chat_id),
                content=f"[鍗虫椂璁板繂|llm_backfill] 鐢ㄦ埛璇达細{user_msg}",
                summary=fact[:240],
                importance=0.8,
                confidence=0.72,
                metadata={"gate_category": "llm_backfill", "instant_write": True},
                dedup_key=f"instant_gate_llm:{chat_id}:{fact[:60]}",
            )
        )

    def _fire_and_forget(self, coro):
        """[新增] 安全触发后台任务的通用封装，防止被 GC 和吞噬异常"""
        import asyncio
        task = asyncio.create_task(coro)
        self._background_tasks.add(task)
        task.add_done_callback(self._handle_task_result)

# 文件位置: astrmai/memory/summarizer.py
# 新增函数: _handle_task_result

    def _handle_task_result(self, task: asyncio.Task):
        """[新增] 处理后台任务完成后的清理与异常捕获"""
        import asyncio
        self._background_tasks.discard(task)
        try:
            exc = task.exception()
            if exc:
                logger.error(f"[AstrMai-Memory] 后台摘要任务异常: {exc}", exc_info=exc)
        except asyncio.CancelledError:
            pass

# 文件位置: astrmai/memory/summarizer.py
# 新增函数: pump_memory_reflection

    async def pump_memory_reflection(self, chat_id: str, user_msg: str, ai_msg: str):
        """
        [新增] 显式闭环的记忆反思泵，接管原本飘忽不定的 main.py 全局拦截器。
        在此处将对话存入 Buffer，达到阈值后触发 summarize_session。
        """
        import time
        import asyncio
        if not ai_msg: return

        if ai_msg.strip().startswith('{') or ai_msg.strip().startswith('```json'):
            return

        await self._try_instant_memorize(chat_id, user_msg, ai_msg)
            
        lock = self._get_memory_lock(chat_id)
        async with lock:
            if chat_id not in self._session_history_buffer:
                self._session_history_buffer[chat_id] = {"buffer": [], "last_update": time.time(), "cooldown_until": 0, "failures": 0}
                
            session_data = self._session_history_buffer[chat_id]
            buffer = session_data["buffer"]
            session_data["last_update"] = time.time()
            
            if user_msg and user_msg.strip(): buffer.append(f"用户/旁白：{user_msg}")
            if ai_msg and ai_msg.strip(): buffer.append(f"Bot：{ai_msg}")
            
            threshold = getattr(self.config.memory, 'summary_threshold', 30)
            
            if time.time() < session_data.get("cooldown_until", 0):
                return
            
            if len(buffer) >= threshold * 2:
                messages_to_process = buffer.copy()
                self._session_history_buffer[chat_id]["buffer"] = []
                
                history_text = "\n".join(messages_to_process)
                
                async def safe_summarize_task():
                    try:
                        await self.summarize_session(
                            session_id=chat_id,
                            chat_history_text=history_text
                        )
                        async with self._get_memory_lock(chat_id):
                            if chat_id in self._session_history_buffer:
                                self._session_history_buffer[chat_id]["failures"] = 0
                                self._session_history_buffer[chat_id]["last_run_at"] = time.time()
                    except asyncio.CancelledError:
                        # 🟢 [核心修复] 当协程被外力终止时，强制触发安全回滚，避免记忆蒸发
                        logger.info(f"[{chat_id}] ⚠️ 记忆摘要任务被强行中断，执行安全回滚...")
                        async with self._get_memory_lock(chat_id):
                            current_data = self._session_history_buffer.get(chat_id, {"buffer": [], "cooldown_until": 0, "failures": 0})
                            current_data["buffer"] = messages_to_process + current_data["buffer"]
                            self._session_history_buffer[chat_id] = current_data
                        raise
                    except Exception as e:
                        logger.error(f"[AstrMai-Memory] 🚨 记忆摘要生成失败，进入指数退避: {e}")
                        async with self._get_memory_lock(chat_id):
                            current_data = self._session_history_buffer.get(chat_id, {"buffer": [], "cooldown_until": 0, "failures": 0})
                            merged_buffer = messages_to_process + current_data["buffer"]
                            
                            max_capacity = threshold * 3
                            if len(merged_buffer) > max_capacity:
                                logger.warning(f"[AstrMai-Memory] ⚠️ 触及硬截断上限，丢弃 {len(merged_buffer) - max_capacity} 条极旧记忆防雪崩。")
                                merged_buffer = merged_buffer[-max_capacity:]
                                
                            current_data["buffer"] = merged_buffer
                            current_data["last_update"] = time.time()
                            
                            failures = current_data.get("failures", 0) + 1
                            current_data["failures"] = failures
                            backoff_time = min(3600, 300 * (2 ** (failures - 1)))
                            current_data["cooldown_until"] = time.time() + backoff_time
                            
                            self._session_history_buffer[chat_id] = current_data

                self._fire_and_forget(safe_summarize_task())            
