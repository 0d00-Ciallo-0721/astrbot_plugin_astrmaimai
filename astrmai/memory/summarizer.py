import asyncio
from typing import Optional
from astrbot.api import logger
from .processor import MemoryProcessor

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
        
        # 🟢 [新增] 接管原本在 main.py 里的高内聚状态变量，消除在 main 中的松散耦合
        self._session_history_buffer = {}
        self._memory_locks = {}
        self._background_tasks = set()

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

    async def _periodic_check_loop(self):
        while self._running:
            try:
                await asyncio.sleep(self.check_interval)
                # 注：实际的扫描逻辑可在此结合 AstrBot/数据库 的 get_messages 进行批量处理
                # 此处保留循环框架，等待与阶段四的 Event Hook 结合实现即时/延时摘要
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"[Memory Summarizer] 后台循环异常: {e}")

    async def summarize_session(self, session_id: str, chat_history_text: str, persona_id: Optional[str] = None):
        """
        核心记忆提炼流水线
        调用时机：当特定会话消息积累达到阈值时触发
        [修改] 引入终极防御性编程，消除所有字典强取操作，彻底预防 LLM 漏字段导致的 KeyError 崩溃。
        """
        if not chat_history_text.strip():
            return
            
        logger.info(f"[Memory Summarizer] 🧠 启动后台任务: 正在对 Session {session_id} 的历史记录进行多维认知降维...")

        # 1. 调用认知大脑进行结构化解析
        memory_data = await self.processor.process_conversation(chat_history_text)
        
        # 🟢 [核心修复] 终极防御：如果 LLM 完全幻觉返回了非字典类型，直接拦截，保护后续流程
        if not isinstance(memory_data, dict):
            logger.warning(f"[Memory Summarizer] ⚠️ Session {session_id} 认知处理返回异常格式，跳过提取。")
            return

        # 🟢 [核心修复] 安全读取，告别 dict["key"]，使用 .get() 提供类型兜底
        summary = memory_data.get("summary", "")
        key_facts = memory_data.get("key_facts", [])
        topics = memory_data.get("topics", [])
        sentiment = memory_data.get("sentiment", "neutral")
        
        # 确保 importance 为数字类型防毒
        try:
            importance = float(memory_data.get("importance", 0.0))
        except (ValueError, TypeError):
            importance = 0.0
            
        # 防止 LLM 幻觉把数组输出成了单行字符串
        if not isinstance(key_facts, list):
            key_facts = [str(key_facts)] if key_facts else []
        if not isinstance(topics, list):
            topics = [str(topics)] if topics else []
        
        # 2. 空转检测：如果没有任何有价值的事实，或者完全是系统默认回复，直接抛弃
        if not key_facts and summary == "对话记录":
            logger.info(f"[Memory Summarizer] ⏭️ Session {session_id} 未提取到有效事实或信息，跳过入库。")
            # 👆【修改结束】
            return
            
        # 3. 极速遗忘机制：重要性过低的内容不占用数据库和后续召回算力
        if importance < 0.2:
            logger.info(f"[Memory Summarizer] 📉 提取内容重要度过低 (importance={importance})，触发即时遗忘机制。")
            # 👆【修改结束】
            return

        # 4. 富文本组装：将多维数据渲染为对 System 2 的 Prompt 友好的易读格式
        content_lines = [f"【摘要】{summary}"]
        
        # 清理数组中的空字符串或非法数据
        valid_facts = [str(f) for f in key_facts if str(f).strip()]
        if valid_facts:
            content_lines.append("【核心事实】\n- " + "\n- ".join(valid_facts))
            
        valid_topics = [str(t) for t in topics if str(t).strip()]
        if valid_topics:
            content_lines.append(f"【话题标签】{', '.join(valid_topics)}")
            
        final_content = "\n".join(content_lines)

        # 👇【新增】在压入数据库前，打印 LLM 究竟提炼了什么核心要素
        logger.info(f"[Memory Summarizer] ✨ Session {session_id} 记忆提炼成功 -> 摘要: {summary[:20]}... | 事实数: {len(valid_facts)} | 标签数: {len(valid_topics)} | 重要度: {importance}")
        # 👆【新增结束】

        # 5. 压入统一底层引擎
        try:
            await self.engine.add_memory(
                content=final_content,
                session_id=str(session_id),
                persona_id=persona_id,
                importance=importance
            )
            # 👇【修改】强化入库成功日志
            logger.info(f"[Memory Summarizer] 💾 已将立体记忆成功压入 Faiss 向量数据库 (Sentiment: {sentiment})。")
            # 👆【修改结束】
        except Exception as e:
            logger.error(f"[Memory Summarizer] ❌ 记忆向量库写入失败: {e}", exc_info=True)



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