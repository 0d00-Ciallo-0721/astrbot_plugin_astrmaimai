# astrmai/memory/react_retriever.py
"""
ReAct Agent 记忆检索器 (Phase 2)
参考: MaiBot/memory_system/memory_retrieval.py (1289行, 56KB)

工作原理:
1. 分析对话上下文 → 生成 1 个最关键的问题
2. 进入 ReAct 循环 (最多 3 轮):
   - Think: 分析当前已收集的信息是否足够
   - Act: 选择并调用一个检索工具
   - Observe: 收集工具返回结果
3. 调用 found_answer 终止循环，输出最终答案

AstrBot API 遵循:
- 使用 GlobalModelGateway 进行 LLM 调用
- 无直接框架依赖，所有外部接口通过构造函数注入
"""
import asyncio
import json
import re
from typing import Dict, List, Optional, Any
from astrbot.api import logger


class ReActRetriever:
    """ReAct Agent 记忆检索器"""

    MAX_ITERATIONS = 3

    def __init__(self, memory_engine=None, db_service=None, gateway=None, config=None):
        self.memory_engine = memory_engine
        self.db_service = db_service
        self.gateway = gateway
        self.config = config

        # 工具注册表
        self._tools = {
            "query_memory": self._tool_query_memory,
            "query_person": self._tool_query_person,
            "query_jargon": self._tool_query_jargon,
            "query_nodes": self._tool_query_nodes,
            "found_answer": None,  # 特殊终止工具
        }

    async def retrieve(
        self, query: str, chat_id: str,
        chat_context: str = "", sender_name: str = "",
        retrieve_keys: list = None
    ) -> str:
        """
        主入口: 基于对话上下文进行 ReAct 记忆检索。
        返回: 检索结果摘要 (供注入 System Prompt)，未触发时返回空字符串。
        """
        if not self.gateway:
            return ""

        # guard: 配置开关
        enable = getattr(self.config, 'memory', None)
        if enable and hasattr(enable, 'enable_react_agent') and not enable.enable_react_agent:
            return ""

        # 阶段一: 问题生成 (如果传入了 retrieve_keys 则直接跳过 LLM 抽提)
        if retrieve_keys:
            # 过滤掉特殊的 ALL 或 CORE_ONLY 标签
            valid_keys = [k for k in retrieve_keys if k not in ["ALL", "CORE_ONLY"]]
            if valid_keys:
                question = f"{query} (请重点检索关于这些维度的档案：{', '.join(valid_keys)})"
            else:
                question = query
        else:
            question = await self._generate_question(query, chat_context, sender_name)
            if not question:
                return ""

        logger.info(f"[ReAct] 🧠 生成检索问题: {question}")

        # 阶段二: ReAct 循环
        collected_info: List[Dict[str, str]] = []

        for iteration in range(self.MAX_ITERATIONS):
            is_last = (iteration == self.MAX_ITERATIONS - 1)

            # 构建 ReAct 单步
            action = await self._react_step(
                question, collected_info, is_last_round=is_last
            )

            if not action:
                break

            tool_name = action.get("tool", "")
            tool_args = action.get("args", {})
            thinking = action.get("thinking", "")

            if not isinstance(tool_args, dict):
                tool_args = {}

            logger.info(
                f"[ReAct] 🔄 第{iteration + 1}轮 | 思考: {thinking[:60]}... | 动作: {tool_name}"
            )

            # 终止条件
            if tool_name == "found_answer":
                answer = tool_args.get("answer", "")
                if answer:
                    logger.info(f"[ReAct] ✅ 检索完成: {answer[:50]}...")
                    return f"记忆检索结果: {answer}"
                break

            # 执行工具
            tool_func = self._tools.get(tool_name)
            if tool_func:
                try:
                    result = await tool_func(
                        chat_id=chat_id,
                        **tool_args
                    )
                    collected_info.append({
                        "tool": tool_name,
                        "query": str(tool_args),
                        "result": result or "未找到相关信息"
                    })
                except Exception as e:
                    collected_info.append({
                        "tool": tool_name,
                        "query": str(tool_args),
                        "result": f"工具调用失败: {e}"
                    })
            else:
                logger.debug(f"[ReAct] 未知工具: {tool_name}，跳过")

        # 如果循环结束仍未 found_answer，汇总已收集信息
        if collected_info:
            summary = "\n".join(
                f"- [{c['tool']}] {c['result'][:200]}" for c in collected_info
            )
            return f"记忆检索参考(信息可能不完整):\n{summary}"

        return ""

    # ==========================================
    # 阶段一: 问题生成
    # ==========================================

    async def _generate_question(
        self, query: str, chat_context: str, sender_name: str
    ) -> Optional[str]:
        """分析对话上下文，生成最关键的检索问题。如不需检索则返回 None。"""
        prompt = f"""你正在参与聊天。分析以下内容，判断是否需要从记忆中检索信息。

聊天上下文:
{chat_context[-1500:]}

当前消息 ({sender_name}): {query}

考虑:
1. 对话中是否提到了过去发生的事情、人物、事件或信息？
2. 是否有"之前说过"、"上次"、"以前"、"记得吗"等回忆性词语？
3. 是否有你不认识的名词、黑话或简称？
4. 是否有需要查询档案的人物？

重要：如果当前对话只是普通闲聊、打招呼，不涉及任何历史信息，请返回 need_search=false。

返回 JSON: {{"need_search": true/false, "question": "问题文本 (如不需要则留空)"}}"""

        try:
            result = await self.gateway.call_data_process_task(prompt, is_json=True)
            data = self._safe_parse_json(result)
            if data.get("need_search") and data.get("question"):
                return str(data["question"])
        except Exception as e:
            logger.debug(f"[ReAct] 问题生成阶段异常: {e}")
        return None

    # ==========================================
    # 阶段二: ReAct 循环单步
    # ==========================================

    async def _react_step(
        self, question: str, collected_info: List[Dict],
        is_last_round: bool = False
    ) -> Optional[Dict]:
        """单步 ReAct: Think → Act"""

        info_text = "暂无已收集信息。" if not collected_info else "\n".join(
            f"[{c['tool']}] 查询: {c['query']} → 结果: {c['result'][:300]}"
            for c in collected_info
        )

        tools_desc = """可用工具:
- query_memory: 从向量记忆库检索（参数: query: str）— 适合查找过往事件和聊天记录
- query_person: 查询人物档案（参数: name: str）— 适合查找某人的信息和好感度
- query_jargon: 查询黑话词典（参数: word: str）— 适合查找不懂的词语或缩写
- query_nodes: 查询记忆节点（参数: keyword: str）— 适合查找特定实体或概念
- found_answer: 终止检索并给出答案（参数: answer: str）— 当信息足够时必须调用此工具"""

        force_end = ""
        if is_last_round:
            force_end = "\n⚠️ 这是最后一轮，你必须调用 found_answer 给出最终答案（即使信息不完整）。"

        prompt = f"""当前问题: {question}

已收集信息:
{info_text}

{tools_desc}
{force_end}

请先简短思考（一两句话），然后选择一个工具。
严格返回 JSON: {{"thinking": "思考内容", "tool": "工具名", "args": {{"参数名": "参数值"}}}}"""

        try:
            result = await self.gateway.call_data_process_task(prompt, is_json=True)
            return self._safe_parse_json(result)
        except Exception as e:
            logger.debug(f"[ReAct] ReAct step 失败: {e}")
        return None

    # ==========================================
    # 工具实现
    # ==========================================

    async def _tool_query_memory(self, chat_id: str, query: str = "", **kw) -> str:
        """工具: 向量+BM25 混合记忆检索"""
        if not self.memory_engine or not query:
            return "记忆模块离线或查询为空"
        try:
            result = await self.memory_engine.recall(query, session_id=chat_id)
            if result and "什么也没想起来" not in result:
                return result
            return "未找到相关记忆"
        except Exception as e:
            return f"记忆检索失败: {e}"

    async def _tool_query_person(self, chat_id: str, name: str = "", **kw) -> str:
        """工具: 人物档案查询"""
        if not self.db_service or not name:
            return "档案模块离线或姓名为空"
        try:
            # 尝试通过 persistence 加载用户画像
            if hasattr(self.db_service, 'persistence'):
                profiles = self.db_service.persistence.load_all_user_profiles()
                for uid, data in profiles.items():
                    if isinstance(data, dict):
                        pname = data.get("name", "")
                        if name.lower() in pname.lower() or pname.lower() in name.lower():
                            analysis = data.get("persona_analysis", "暂无")
                            tags = data.get("tags", [])
                            score = data.get("social_score", 0)
                            return (
                                f"姓名: {pname}, 好感度: {score}, "
                                f"标签: {', '.join(tags) if tags else '无'}, "
                                f"侧写: {analysis}"
                            )
            return f"未找到关于 '{name}' 的档案"
        except Exception as e:
            logger.debug(f"[ReAct] query_person 异常: {e}")
            return f"查询人物档案失败: {e}"

    async def _tool_query_jargon(self, chat_id: str, word: str = "", **kw) -> str:
        """工具: 黑话词典查询"""
        if not self.db_service or not word:
            return "词典模块离线或查询词为空"
        try:
            if hasattr(self.db_service, 'get_jargon'):
                result = self.db_service.get_jargon(chat_id, word)
                if result:
                    return f"'{word}' 的含义: {result}"
            # 兜底: 查全局
            if hasattr(self.db_service, 'search_jargons'):
                results = self.db_service.search_jargons(word, limit=3)
                if results:
                    return "\n".join(
                        f"'{j.content}': {j.meaning}" for j in results if j.meaning
                    )
            return f"词典中未收录 '{word}'"
        except Exception as e:
            logger.debug(f"[ReAct] query_jargon 异常: {e}")
            return f"查询黑话失败: {e}"

    async def _tool_query_nodes(self, chat_id: str, keyword: str = "", **kw) -> str:
        """工具: 记忆节点查询"""
        if not self.db_service or not keyword:
            return "节点模块离线或关键词为空"
        try:
            if hasattr(self.db_service, 'search_nodes_async'):
                nodes = await self.db_service.search_nodes_async(keyword, limit=3)
                if nodes:
                    return "\n".join(
                        f"📌 {n.name} ({n.type}): {n.description}" for n in nodes
                    )
            return f"未找到与 '{keyword}' 相关的知识节点"
        except Exception as e:
            logger.debug(f"[ReAct] query_nodes 异常: {e}")
            return f"查询知识节点失败: {e}"

    # ==========================================
    # 工具函数
    # ==========================================

    @staticmethod
    def _safe_parse_json(raw) -> Dict:
        """鲁棒的 JSON 解析，兼容 dict/str 类型"""
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            match = re.search(r'\{.*\}', raw, re.DOTALL)
            if match:
                try:
                    return json.loads(match.group(0))
                except json.JSONDecodeError:
                    pass
        return {}
