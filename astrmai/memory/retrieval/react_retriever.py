from __future__ import annotations

import json
import re
import time
import uuid
from typing import Dict, List, Optional

from astrbot.api import logger

from ...infrastructure.runtime.lane_manager import LaneKey
from ...infrastructure.runtime.turn_call_ledger import clamp_timeout_to_turn_budget
from ..contracts.memory_query import MemoryQuery
from ..contracts.retrieval_trace import RetrievalTrace


class ReActRetriever:
    """ReAct-based memory retriever implemented inside the refactor tree."""

    MAX_ITERATIONS = 3

    def __init__(self, memory_engine=None, db_service=None, gateway=None, config=None):
        self.memory_engine = memory_engine
        self.db_service = db_service
        self.gateway = gateway
        self.config = config
        self._tools = {
            "query_memory": self._tool_query_memory,
            "query_person": self._tool_query_person,
            "query_jargon": self._tool_query_jargon,
            "query_nodes": self._tool_query_nodes,
            "found_answer": None,
        }

    def _question_timeout_seconds(self) -> float:
        timing = getattr(self.config, "timing", None)
        try:
            configured = float(getattr(timing, "memory_react_timeout_sec", 45.0) or 45.0)
        except (TypeError, ValueError):
            configured = 45.0
        return max(0.1, configured)

    def _retrieval_lane(self, chat_id: str) -> LaneKey:
        return LaneKey(subsystem="sys2", task_family="retrieval", scope_id=chat_id)

    async def retrieve(
        self,
        query: str,
        chat_id: str,
        chat_context: str = "",
        sender_name: str = "",
        retrieve_keys: list | None = None,
    ) -> str:
        if not self.gateway:
            return ""

        memory_cfg = getattr(self.config, "memory", None)
        if memory_cfg and hasattr(memory_cfg, "enable_react_agent") and not memory_cfg.enable_react_agent:
            return ""

        if retrieve_keys:
            valid_keys = [key for key in retrieve_keys if key not in ["ALL", "CORE_ONLY"]]
            question = f"{query} (请优先回忆这些维度：{', '.join(valid_keys)})" if valid_keys else query
        else:
            question = await self._generate_question(query, chat_id, chat_context, sender_name)
            if not question:
                return ""

        collected_info: List[Dict[str, str]] = []
        final_answer = ""

        # OPT-06/ML-02(RT-12): 逐步无超时的 react 循环是 memory.injection 尾延迟 92s 的
        # 来源之一；整个循环受 turn 预算约束，预算耗尽即收束到已收集信息
        react_budget_sec = clamp_timeout_to_turn_budget(None, 20.0, reserve_for_reply=True)
        if react_budget_sec <= 0.5:
            return ""
        react_deadline = time.monotonic() + react_budget_sec

        for iteration in range(self.MAX_ITERATIONS):
            if time.monotonic() >= react_deadline:
                logger.debug("[ReAct] iteration budget exhausted; finalizing with collected info")
                break
            action = await self._react_step(
                question,
                chat_id,
                collected_info,
                is_last_round=(iteration == self.MAX_ITERATIONS - 1),
                step_timeout_sec=max(0.5, react_deadline - time.monotonic()),
            )
            if not action:
                break

            tool_name = action.get("tool", "")
            tool_args = action.get("args", {})
            if not isinstance(tool_args, dict):
                tool_args = {}

            if tool_name == "found_answer":
                answer = str(tool_args.get("answer", "")).strip()
                if answer:
                    final_answer = f"记忆检索结果: {answer}"
                break

            tool_func = self._tools.get(tool_name)
            if tool_func:
                try:
                    result = await tool_func(chat_id=chat_id, **tool_args)
                except Exception as exc:  # pragma: no cover - defensive fallback
                    result = f"工具调用失败: {exc}"
                collected_info.append(
                    {
                        "tool": tool_name,
                        "query": json.dumps(tool_args, ensure_ascii=False),
                        "result": result or "未找到相关信息",
                    }
                )

        if not final_answer and collected_info:
            summary = "\n".join(f"- [{item['tool']}] {item['result'][:200]}" for item in collected_info)
            final_answer = f"记忆检索参考信息可能不完整:\n{summary}"

        if final_answer and collected_info:
            readable_layers, confidence = self._format_trace_meta(collected_info)
            if readable_layers and "[记忆元信息]" not in final_answer:
                final_answer = (
                    f"{final_answer}\n[记忆元信息]\n"
                    f"- 记忆类型: {'、'.join(readable_layers)}\n"
                    f"- 来源层: {', '.join(readable_layers)}\n"
                    f"- 置信度: {confidence}"
                )

        await self._save_trace(
            chat_id=chat_id,
            sender_name=sender_name,
            query=query,
            planner_question=question,
            collected_info=collected_info,
            final_answer=final_answer,
        )
        return final_answer

    async def _generate_question(
        self,
        query: str,
        chat_id: str,
        chat_context: str,
        sender_name: str,
    ) -> Optional[str]:
        prompt = (
            "You are deciding whether memory retrieval is needed. "
            "Return JSON with need_search and question.\n\n"
            f"chat_context:\n{chat_context[-1500:]}\n\n"
            f"sender_name: {sender_name}\n"
            f"query: {query}"
        )
        try:
            result = await asyncio.wait_for(
                self.gateway.call_data_process_task(
                    prompt,
                    is_json=True,
                    lane_key=self._retrieval_lane(chat_id),
                    base_origin=chat_id,
                ),
                timeout=self._question_timeout_seconds(),
            )
            data = self._safe_parse_json(result)
            if data.get("need_search") and data.get("question"):
                return str(data["question"])
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.debug(f"[ReAct] question generation failed: {exc}")
        return None

    async def _react_step(
        self,
        question: str,
        chat_id: str,
        collected_info: List[Dict],
        is_last_round: bool = False,
        step_timeout_sec: float | None = None,
    ) -> Optional[Dict]:
        info_text = "no facts collected yet" if not collected_info else "\n".join(
            f"[{item['tool']}] query: {item['query']} -> result: {item['result'][:300]}"
            for item in collected_info
        )
        force_end = "\nThis is the last round. You must call found_answer." if is_last_round else ""
        prompt = (
            f"question: {question}\n\n"
            f"collected_info:\n{info_text}\n\n"
            "tools:\n"
            "- query_memory(query)\n"
            "- query_person(name)\n"
            "- query_jargon(word)\n"
            "- query_nodes(keyword)\n"
            "- found_answer(answer)\n"
            f"{force_end}\n\n"
            'Return strict JSON: {"thinking":"...", "tool":"...", "args":{}}'
        )
        try:
            step_kwargs = {}
            if step_timeout_sec is not None:
                step_kwargs["timeout_override"] = max(0.5, min(8.0, float(step_timeout_sec)))
            result = await self.gateway.call_data_process_task(
                prompt,
                is_json=True,
                lane_key=self._retrieval_lane(chat_id),
                base_origin=chat_id,
                **step_kwargs,
            )
            return self._safe_parse_json(result)
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.debug(f"[ReAct] step failed: {exc}")
        return None

    async def _tool_query_memory(self, chat_id: str, query: str = "", **_: dict) -> str:
        if not self.memory_engine or not query:
            return "记忆模块离线或查询为空"
        try:
            retrieval = getattr(self.memory_engine, "retrieval_service", None)
            if retrieval and hasattr(retrieval, "retrieve_deep"):
                memory_query = MemoryQuery(
                    query=query,
                    session_id=chat_id,
                    top_k=3,
                    policy="deep",
                    allow_stale=False,
                    metadata={"visibility_mode": "tool"},
                )
                candidates = await retrieval.retrieve_deep(memory_query)
                if candidates:
                    return retrieval.render_recall(memory_query, candidates)
            return "记忆检索服务不可用"
        except Exception as exc:  # pragma: no cover - defensive fallback
            return f"记忆检索失败: {exc}"

    async def _tool_query_person(self, chat_id: str, name: str = "", **_: dict) -> str:
        if not self.db_service or not name:
            return "档案模块离线或姓名为空"
        try:
            profiles = {}
            persistence = getattr(self.db_service, "persistence", None)
            if persistence and hasattr(persistence, "load_all_user_profiles"):
                profiles = persistence.load_all_user_profiles() or {}

            query_name = name.strip().lower()
            for uid, data in profiles.items():
                if not isinstance(data, dict):
                    continue
                profile_name = str(data.get("name", "") or "").strip()
                nickname = str(data.get("nickname", "") or "").strip()
                candidates = [candidate for candidate in (profile_name, nickname) if candidate]
                if not any(query_name in candidate.lower() or candidate.lower() in query_name for candidate in candidates):
                    continue

                analysis = data.get("persona_analysis", "暂无")
                tags = data.get("tags", [])
                score = data.get("social_score", 0)
                identity_points = data.get("identity_points", []) or []
                preference_points = data.get("preference_points", []) or []
                relationship_points = data.get("relationship_points", []) or []
                speech_style_points = data.get("speech_style_points", []) or []
                if profile_name and nickname and nickname != profile_name:
                    display_name = f"{profile_name} ({nickname})"
                else:
                    display_name = nickname or profile_name or uid
                sections = [
                    f"姓名: {display_name}, 好感度: {score}, 标签: {', '.join(tags) if tags else '-'}, 侧写: {analysis}"
                ]
                if identity_points:
                    sections.append(f"身份记忆: {' / '.join(identity_points[:3])}")
                if preference_points:
                    sections.append(f"偏好记忆: {' / '.join(preference_points[:3])}")
                if relationship_points:
                    sections.append(f"关系记忆: {' / '.join(relationship_points[:3])}")
                if speech_style_points:
                    sections.append(f"说话风格: {' / '.join(speech_style_points[:3])}")
                return "\n".join(sections)
            return f"未找到关于 '{name}' 的档案"
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.debug(f"[ReAct] query_person failed: {exc}")
            return f"查询人物档案失败: {exc}"

    async def _tool_query_jargon(self, chat_id: str, word: str = "", **_: dict) -> str:
        if not self.memory_engine or not word:
            return "黑话模块离线或查询词为空"
        try:
            retrieval = getattr(self.memory_engine, "retrieval_service", None)
            if not retrieval:
                return "黑话检索服务不可用"
            query = MemoryQuery(
                query=word,
                session_id=chat_id,
                layers=["jargon"],
                top_k=3,
                intent="jargon",
                allow_stale=False,
                metadata={"visibility_mode": "tool"},
            )
            results = await retrieval.retrieve(query)
            if results:
                rendered = []
                for item in results:
                    term = str(getattr(item, "content", "") or "").strip()
                    meaning = str(getattr(item, "summary", "") or item.metadata.get("meaning", "")).strip()
                    if term and meaning:
                        rendered.append(f"'{term}': {meaning}")
                if rendered:
                    return "\n".join(rendered)
            return f"词典中未收录 '{word}'"
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.debug(f"[ReAct] query_jargon failed: {exc}")
            return f"查询黑话失败: {exc}"

    async def _tool_query_nodes(self, chat_id: str, keyword: str = "", **_: dict) -> str:
        if not self.db_service or not keyword:
            return "节点模块离线或关键词为空"
        try:
            if hasattr(self.db_service, "search_nodes_async"):
                nodes = await self.db_service.search_nodes_async(keyword, limit=3)
                if nodes:
                    return "\n".join(
                        f"节点 {node.name} ({node.type}): {node.description}" for node in nodes
                    )
            return f"未找到与 '{keyword}' 相关的知识节点"
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.debug(f"[ReAct] query_nodes failed: {exc}")
            return f"查询知识节点失败: {exc}"

    @staticmethod
    def _safe_parse_json(raw) -> Dict:
        if isinstance(raw, dict):
            return raw
        if isinstance(raw, str):
            chunk = ReActRetriever._extract_braced_json(raw)
            if chunk:
                try:
                    return json.loads(chunk)
                except json.JSONDecodeError:
                    pass
        return {}

    @staticmethod
    def _extract_braced_json(text: str):
        """Extract the first complete JSON object from text by counting braces.

        Braces inside JSON string literals are ignored so that values like
        ``{"key": "text with { in it}"}`` are not miscounted.
        """
        start = text.find('{')
        if start == -1:
            return None
        depth = 0
        in_string = False
        escape = False
        for i in range(start, len(text)):
            ch = text[i]
            if escape:
                escape = False
                continue
            if ch == '\\':
                escape = True
                continue
            if ch == '"':
                in_string = not in_string
                continue
            if in_string:
                continue
            if ch == '{':
                depth += 1
            elif ch == '}':
                depth -= 1
                if depth == 0:
                    return text[start:i + 1]
        return None

    async def _save_trace(
        self,
        chat_id: str,
        sender_name: str,
        query: str,
        planner_question: str,
        collected_info: List[Dict[str, str]],
        final_answer: str,
    ) -> None:
        if not self.db_service or not hasattr(self.db_service, "save_retrieval_trace_async"):
            return
        try:
            selected_memory_ids: List[str] = []
            source_layers: List[str] = []
            for item in collected_info:
                tool_name = str(item.get("tool", ""))
                if tool_name:
                    source_layers.append(tool_name.replace("query_", ""))
                result_text = str(item.get("result", ""))
                selected_memory_ids.extend(re.findall(r"evt_[a-zA-Z0-9_]+", result_text))

            trace = RetrievalTrace(
                trace_id=f"trace_{uuid.uuid4().hex[:12]}",
                chat_id=chat_id,
                sender_name=sender_name,
                query=query,
                planner_question=planner_question,
                tool_calls=json.dumps(collected_info, ensure_ascii=False),
                selected_memory_ids=json.dumps(sorted(set(selected_memory_ids)), ensure_ascii=False),
                final_answer=final_answer[:800],
                source_layers=json.dumps(sorted(set(source_layers)), ensure_ascii=False),
                confidence=0.8 if final_answer else 0.0,
            )
            await self.db_service.save_retrieval_trace_async(trace.to_orm_model())
        except Exception as exc:  # pragma: no cover - defensive fallback
            logger.debug(f"[ReAct] trace save failed: {exc}")

    @staticmethod
    def _format_trace_meta(collected_info: List[Dict[str, str]]) -> tuple[List[str], str]:
        layers = sorted(
            {
                str(item.get("tool", "")).replace("query_", "")
                for item in collected_info
                if item.get("tool")
            }
        )
        if not layers:
            return [], "低"

        layer_alias = {
            "memory": "长期记忆",
            "person": "人物记忆",
            "jargon": "黑话记忆",
            "nodes": "节点记忆",
        }
        readable_layers = [layer_alias.get(layer, layer) for layer in layers]
        if len(layers) >= 3:
            confidence = "高"
        elif len(layers) == 2:
            confidence = "中"
        else:
            confidence = "中低"
        return readable_layers, confidence
