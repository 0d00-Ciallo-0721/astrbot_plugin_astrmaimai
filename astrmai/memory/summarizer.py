import asyncio
import re
from typing import Optional, List, Dict
from astrbot.api import logger
from .processor import MemoryProcessor
from .topic_summarizer import TopicSummarizer

class ChatHistorySummarizer:
    """
    历史摘要清道夫 (System 2 / Memory Lifecycle)
    阶段二重构：废弃旧版扁平陈述句提取，接入 Cognitive Processor 实现高密度知识提取。
    """
    def __init__(self, context, gateway, engine, config=None):
        self.context = context
        self.gateway = gateway
        self.engine = engine
        self.config = config if config else gateway.config
        
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

    async def extract_and_summarize_history(self, session_id: str, days: int = 1):
        """[新增] 从底层数据库批量拉取历史消息，格式化后进行摘要。完美整合 chat_history_extract 提取大段历史的逻辑"""
        import time
        
        plugin = getattr(self.context, 'astrmai_plugin', None) or getattr(self.gateway.context, 'astrmai', None)
        if not plugin or not hasattr(plugin, 'db_service'):
            return
            
        db = plugin.db_service
        try:
            from sqlmodel import select
            from ..infra.datamodels import MessageLog
            
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
            from ..infra.datamodels import MemoryNode
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
            await self.engine.add_memory(
                content=final_content,
                session_id=str(session_id),
                persona_id=persona_id,
                importance=importance
            )

            # 🟢 分流三：存入结构化的 Event 表扩充属性维度，便于回溯提取
            import time
            import uuid
            import datetime
            import json
            date_str = datetime.datetime.now().strftime("%Y-%m-%d")
            from ..infra.datamodels import MemoryEvent
            
            event_id = f"evt_{date_str.replace('-', '')}_{uuid.uuid4().hex[:8]}"
            event = MemoryEvent(
                event_id=event_id,
                session_id=str(session_id),
                date=date_str,
                narrative="\n".join(valid_facts),
                emotion=sentiment,
                importance=int(importance * 10),
                emotional_intensity=int(importance * 10),
                reflection=reflection,
                tags=json.dumps(valid_topics)
            )
            
            if plugin and hasattr(plugin, 'db_service') and hasattr(plugin.db_service, 'save_event_async'):
                await plugin.db_service.save_event_async(event)

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
        
        # 防止漏网之鱼的后台 JSON 流入
        if ai_msg.strip().startswith('{') or ai_msg.strip().startswith('```json'):
            return
            
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
