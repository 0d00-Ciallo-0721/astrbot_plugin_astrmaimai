# astrmai/memory/dream_agent.py
"""
梦境整理 Agent (Dream Agent) — Phase 7.1
参考: MaiBot/dream/dream_agent.py

核心功能: 像人类睡眠中整合记忆一样，后台定时对存储的记忆进行
         合并、精简、删除冗余，保持记忆库高质量。

工作流:
1. 调度器触发（每 30 分钟，可配）
2. 随机选一个有效 session_id（记忆事件数 ≥ 5）  
3. 随机抽取一组 MemoryEvent 作为整理起点
4. ReAct 循环（最多 5 轮，超时 90s）
   - Think: 分析记忆质量、找出冗余/矛盾
   - Act:   选择工具（搜索/合并/精简/删除）
   - Observe: 操作结果
5. 返回整理日志供 DreamGenerator 生成梦境叙述

可用工具（5个）:
- search_memory:   语义检索相关记忆
- get_memory_detail: 读取记忆完整内容
- merge_memories:  合并多条冗余记忆为一条精华
- update_memory:   重写/精简某条记忆
- delete_memory:   删除噪声/过时记忆
- finish_dream:    主动结束整理循环
"""
import time
import asyncio
import json
import random
from typing import Optional, List, Dict, Any
from astrbot.api import logger
from ..infra.gateway import GlobalModelGateway
from ..infra.database import DatabaseService


class DreamAgent:
    """梦境整理 Agent"""

    MAX_ITERATIONS = 5
    TIMEOUT_SEC = 90.0
    MIN_EVENTS_TO_DREAM = 5  # 至少有这么多记忆才触发整理

    # 工具名称
    TOOLS = {
        "search_memory":    "语义搜索记忆库，参数: {query: str, limit: int=5}",
        "get_memory_detail":"读取某条记忆的完整内容，参数: {event_id: str}",
        "merge_memories":   "将多条冗余记忆合并为一条精华记忆，参数: {event_ids: [str], new_narrative: str}",
        "update_memory":    "重写/精简某条记忆内容，参数: {event_id: str, new_narrative: str}",
        "delete_memory":    "删除噪声或过时记忆，参数: {event_id: str, reason: str}",
        "finish_dream":     "结束本次整理循环，参数: {summary: str}",
    }

    def __init__(
        self,
        gateway: GlobalModelGateway,
        db_service: DatabaseService,
        memory_engine=None,
        config=None,
    ):
        self.gateway = gateway
        self.db = db_service
        self.memory_engine = memory_engine
        self.config = config if config else gateway.config

    async def run_dream_cycle(self, session_id: str = None) -> Optional[str]:
        """
        执行一次梦境整理循环。

        Args:
            session_id: 指定整理的会话 ID；None 时随机选择

        Returns:
            整理日志文本（供 DreamGenerator 使用），失败时返回 None
        """
        # 1. 选择整理目标
        if not session_id:
            session_id = await self._pick_random_session()
        if not session_id:
            logger.info("[DreamAgent] 💤 无满足条件的记忆库，本次梦境跳过")
            return None

        logger.info(f"[DreamAgent] 💤 开始梦境整理 → session: {session_id}")

        # 2. 获取整理起点（随机抽取一批事件摘要）
        seed_events = await self._get_seed_events(session_id)
        if not seed_events:
            return None

        # 3. ReAct 主循环
        dream_log = []
        iteration = 0
        start_time = time.time()

        # 初始思考 prompt
        events_desc = "\n".join(
            [f"- [{e.get('event_id', '?')}] {e.get('narrative', '')[:80]}..."
             for e in seed_events]
        )
        system_prompt = (
            "你是一个记忆整理者，正在处理下列记忆片段。\n"
            "你的目标是：合并重复内容、精简冗长叙述、删除无价值噪声，保留最精华的记忆。\n"
            "每次只执行一个工具操作，逐步完成整理。\n\n"
            f"当前待整理记忆：\n{events_desc}\n\n"
            f"可用工具：\n" + 
            "\n".join([f"- {k}: {v}" for k, v in self.TOOLS.items()])
        )

        messages = [{"role": "user", "content": system_prompt}]

        while iteration < self.MAX_ITERATIONS:
            if time.time() - start_time > self.TIMEOUT_SEC:
                logger.warning(f"[DreamAgent] ⏰ 梦境整理超时 ({self.TIMEOUT_SEC}s)")
                break

            iteration += 1
            logger.debug(f"[DreamAgent] 🔄 第{iteration}轮思考...")

            # LLM 思考
            try:
                response = await self.gateway.call_data_process_task(
                    prompt=json.dumps(messages, ensure_ascii=False),
                    is_json=True,
                )
            except Exception as e:
                logger.error(f"[DreamAgent] LLM 调用失败: {e}")
                break

            # 解析响应
            action = self._parse_action(response)
            if not action:
                logger.debug("[DreamAgent] 无法解析动作，结束")
                break

            tool_name = action.get("tool", "")
            params = action.get("params", {})
            thought = action.get("thought", "")
            dream_log.append(f"[思考] {thought}")

            # 执行工具
            observation = await self._execute_tool(tool_name, params, session_id)
            dream_log.append(f"[行动] {tool_name}({params}) → {observation}")

            logger.debug(f"[DreamAgent] 工具: {tool_name} | 结果: {observation[:50]}")

            # finish_dream 终止
            if tool_name == "finish_dream":
                summary = params.get("summary", "整理完成")
                dream_log.append(f"[结束] {summary}")
                break

            # 更新对话历史
            messages.append({"role": "assistant", "content": json.dumps(action, ensure_ascii=False)})
            messages.append({"role": "user", "content": f"工具执行结果：{observation}\n请继续整理，或调用 finish_dream 结束。"})

        log_text = "\n".join(dream_log)
        logger.info(f"[DreamAgent] ✅ 梦境整理完成 ({iteration}轮) | session: {session_id}")
        return log_text

    # ==========================================
    # 工具执行层
    # ==========================================

    async def _execute_tool(self, tool_name: str, params: Dict, session_id: str) -> str:
        """分发并执行工具调用"""
        try:
            if tool_name == "search_memory":
                return await self._tool_search_memory(params, session_id)
            elif tool_name == "get_memory_detail":
                return await self._tool_get_detail(params)
            elif tool_name == "merge_memories":
                return await self._tool_merge(params, session_id)
            elif tool_name == "update_memory":
                return await self._tool_update(params)
            elif tool_name == "delete_memory":
                return await self._tool_delete(params)
            elif tool_name == "finish_dream":
                return "整理循环结束"
            else:
                return f"未知工具: {tool_name}"
        except Exception as e:
            return f"工具执行失败: {e}"

    async def _tool_search_memory(self, params: Dict, session_id: str) -> str:
        query = params.get("query", "")
        limit = int(params.get("limit", 5))
        if not query:
            return "参数缺失: query"
        if self.memory_engine:
            result = await self.memory_engine.recall(query, session_id=session_id, top_k=limit)
            return result or "未找到相关记忆"
        return "记忆引擎不可用"

    async def _tool_get_detail(self, params: Dict) -> str:
        event_id = params.get("event_id", "")
        if not event_id:
            return "参数缺失: event_id"
        try:
            event = await asyncio.to_thread(self._get_event_by_id, event_id)
            if event:
                return f"叙事: {event.get('narrative', '')}\n情感: {event.get('emotion', '')}\n重要度: {event.get('importance', 0)}"
            return "未找到该记忆"
        except Exception as e:
            return f"读取失败: {e}"

    async def _tool_merge(self, params: Dict, session_id: str) -> str:
        event_ids = params.get("event_ids", [])
        new_narrative = params.get("new_narrative", "")
        if not event_ids or not new_narrative:
            return "参数缺失: event_ids 或 new_narrative"
        try:
            # 创建新的精华记忆
            if self.memory_engine:
                await self.memory_engine.add_memory(
                    content=new_narrative,
                    session_id=session_id,
                    importance=0.7
                )
            # 删除旧的冗余记忆
            for eid in event_ids:
                await asyncio.to_thread(self._delete_event, eid)
            return f"已合并 {len(event_ids)} 条记忆为精华：{new_narrative[:50]}..."
        except Exception as e:
            return f"合并失败: {e}"

    async def _tool_update(self, params: Dict) -> str:
        event_id = params.get("event_id", "")
        new_narrative = params.get("new_narrative", "")
        if not event_id or not new_narrative:
            return "参数缺失"
        try:
            await asyncio.to_thread(self._update_event_narrative, event_id, new_narrative)
            return f"已更新记忆 {event_id}"
        except Exception as e:
            return f"更新失败: {e}"

    async def _tool_delete(self, params: Dict) -> str:
        event_id = params.get("event_id", "")
        reason = params.get("reason", "冗余/过时")
        if not event_id:
            return "参数缺失: event_id"
        try:
            await asyncio.to_thread(self._delete_event, event_id)
            return f"已删除记忆 {event_id} (原因: {reason})"
        except Exception as e:
            return f"删除失败: {e}"

    # ==========================================
    # DB 辅助
    # ==========================================

    def _get_event_by_id(self, event_id: str) -> Optional[Dict]:
        from ..infra.datamodels import MemoryEvent
        from sqlmodel import select
        try:
            with self.db.get_session() as session:
                stmt = select(MemoryEvent).where(MemoryEvent.event_id == event_id)
                ev = session.exec(stmt).first()
                if ev:
                    return {"event_id": ev.event_id, "narrative": ev.narrative,
                            "emotion": ev.emotion, "importance": ev.importance}
        except Exception:
            pass
        return None

    def _delete_event(self, event_id: str):
        from ..infra.datamodels import MemoryEvent
        from sqlmodel import select
        try:
            with self.db.get_session() as session:
                stmt = select(MemoryEvent).where(MemoryEvent.event_id == event_id)
                ev = session.exec(stmt).first()
                if ev:
                    session.delete(ev)
                    session.commit()
                    logger.debug(f"[DreamAgent] 🗑️ 删除记忆事件: {event_id}")
        except Exception as e:
            logger.warning(f"[DreamAgent] 删除事件失败: {e}")

    def _update_event_narrative(self, event_id: str, new_narrative: str):
        from ..infra.datamodels import MemoryEvent
        from sqlmodel import select
        try:
            with self.db.get_session() as session:
                stmt = select(MemoryEvent).where(MemoryEvent.event_id == event_id)
                ev = session.exec(stmt).first()
                if ev:
                    ev.narrative = new_narrative
                    session.add(ev)
                    session.commit()
        except Exception as e:
            logger.warning(f"[DreamAgent] 更新事件失败: {e}")

    async def _pick_random_session(self) -> Optional[str]:
        """随机选择有足够记忆的 session"""
        from ..infra.datamodels import MemoryEvent
        from sqlmodel import select, func
        try:
            def _query():
                with self.db.get_session() as session:
                    # 按 date 分组，统计各 session 的记忆数
                    stmt = (
                        select(MemoryEvent.date)
                        .group_by(MemoryEvent.date)
                        .having(func.count(MemoryEvent.id) >= self.MIN_EVENTS_TO_DREAM)
                    )
                    results = session.exec(stmt).all()
                    return results
            sessions = await asyncio.to_thread(_query)
            if sessions:
                return random.choice(sessions)
        except Exception as e:
            logger.warning(f"[DreamAgent] 选取 session 失败: {e}")
        return None

    async def _get_seed_events(self, session_id: str) -> List[Dict]:
        """获取指定 session 的随机种子记忆"""
        from ..infra.datamodels import MemoryEvent
        from sqlmodel import select
        try:
            def _query():
                with self.db.get_session() as session:
                    stmt = select(MemoryEvent).where(
                        MemoryEvent.date == session_id
                    ).limit(10)
                    events = session.exec(stmt).all()
                    return [
                        {"event_id": e.event_id, "narrative": e.narrative,
                         "emotion": e.emotion, "importance": e.importance}
                        for e in events
                    ]
            events = await asyncio.to_thread(_query)
            # 随机抽取 5 条
            return random.sample(events, min(5, len(events))) if events else []
        except Exception as e:
            logger.warning(f"[DreamAgent] 获取种子记忆失败: {e}")
            return []

    @staticmethod
    def _parse_action(response) -> Optional[Dict]:
        """解析 LLM 返回的行动 JSON"""
        try:
            if isinstance(response, dict):
                return response
            import re
            raw = str(response)
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                return json.loads(match.group(0))
        except Exception:
            pass
        return None
