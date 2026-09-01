from typing import Any, Dict, List
from astrbot.api import logger
from ...infrastructure.gateway.json_utils import parse_json_contract
from ...infrastructure.context_economy import PromptTemplateId
from ...infrastructure.runtime.lane_manager import LaneKey
from ...infrastructure.runtime.background_task_budget import BackgroundTaskBudget

class MemoryProcessor:
    """
    认知结构化大脑 (System 2 - Cognitive Processor)
    负责将非结构化的对话历史，通过 LLM 降维压缩为高密度的多维记忆数据模型。
    """
# 位置: astrmai/memory/processor.py -> MemoryProcessor 类下
    def __init__(self, gateway, background_task_budget=None):
        self.gateway = gateway
        self.background_task_budget = background_task_budget or BackgroundTaskBudget()
        self.prompt_registry = getattr(getattr(gateway, "context_economy", None), "templates", None)
        # [修改] 更新内部提示词模板，确保第一阶段输出 reflection 维度
        self.prompt_template = """你是一个专业的对话分析智能体和记忆提炼大脑。
请阅读以下对话历史，提取其中具有长期记忆价值的信息（如用户的身份、偏好习惯、重要事件、约定、情感倾向等）。

⚠️ 提取规则：
1. 忽略无意义的过渡性闲聊（如“你好”、“在吗”、“哈哈”）。
2. 如果对话完全没有记忆价值，请降低 importance 评分（如 0.1）。
3. 请将事实转化为客观的第三人称陈述句。
4. 你必须严格返回以下格式的 JSON 对象，不要包含任何 markdown 代码块或解释性文字。

JSON 格式要求：
{
    "summary": "这段对话的核心总结（50字以内）",
    "topics": ["话题1", "话题2"],
    "key_facts": ["事实1（例：用户表示自己是程序员）", "事实2"],
    "reflection": "针对对话中事件的深度观察和感想。如果没有特别感想（如日常琐事），可以填'无'。",
    "sentiment": "positive", // (positive / neutral / negative)
    "importance": 0.8 // (0.0到1.0的浮点数，越重要分数越高)
}

以下是对话内容：
{history}
"""
        # [新增] 第二阶段节点提取提示词模板
        self.node_prompt_template = """你正在从一组你之前总结的事件中提取记忆节点。必须严格按照以下 JSON 格式输出：
{
    "nodes": [
        {
            "name": "节点名称（实体或概念，如：王小美、火锅、考研）",
            "type": "节点类型（如：人物、食物、活动、情感、地点、技术）",
            "description": "对该节点的定义或最新状态描述"
        }
    ],
    "deleted_nodes": ["需要删除或合并的冗余节点名称列表"]
}

【提取规则】
- 仅提取具有“长期记忆价值”的重要实体（人物、地点、核心物品）或反复出现的关键概念。
- 避免提取琐碎的、一次性的细节。

以下是已知事件与事实：
{facts}
"""

    # 位置: astrmai/memory/processor.py -> MemoryProcessor 类下
    @staticmethod
    def _memory_lane_key(session_id: str) -> LaneKey:
        normalized = str(session_id or "").strip()
        if normalized and normalized != "global":
            return LaneKey(subsystem="bg", task_family="memory", scope_id=normalized, scope_kind="chat")
        logger.warning("[MemoryProcessor] global scope fallback engaged; expected a concrete session_id for memory extraction")
        return LaneKey(subsystem="bg", task_family="memory", scope_id="global", scope_kind="global")

    async def process_conversation(self, chat_history_text: str, session_id: str = "global") -> Dict[str, Any]:
        """[修改] 将对话文本处理为结构化记忆，分为事件与感想提取、节点提取两个阶段"""
        if not chat_history_text or not chat_history_text.strip():
            return self._get_default_structured_data()

        summary_envelope = None
        summary_prompt = self.prompt_template.replace("{history}", chat_history_text)
        summary_system_prompt = ""
        if self.prompt_registry is not None:
            summary_envelope = self.prompt_registry.render_template(
                PromptTemplateId.MEMORY_STRUCTURED_EXTRACTION,
                {"history": chat_history_text},
            )
            summary_prompt = summary_envelope.prompt
            summary_system_prompt = summary_envelope.system_prompt
        
        try:
            lane_key = self._memory_lane_key(session_id)
            # 阶段一：提取事件与感想
            async def _summary_call():
                return await self.gateway.call_data_process_task(
                    prompt=summary_prompt,
                    system_prompt=summary_system_prompt,
                    is_json=True,
                    lane_key=lane_key,
                    base_origin=session_id,
                    template_envelope=summary_envelope,
                )

            result = await self.background_task_budget.run(
                _summary_call,
                task_name="memory.summary",
                scope_id=session_id,
                defer_release_on_timeout=True,
            )
            
            data1 = self._parse_json(
                result,
                required_keys=("summary", "topics", "key_facts", "reflection", "sentiment", "importance"),
            )
                
            validated_data = self._validate_and_fill(data1)

            # 阶段二：提取记忆节点
            if validated_data.get("key_facts"):
                facts_text = "\n".join(validated_data["key_facts"])
                node_envelope = None
                node_prompt = self.node_prompt_template.replace("{facts}", facts_text)
                node_system_prompt = ""
                if self.prompt_registry is not None:
                    node_envelope = self.prompt_registry.render_template(
                        PromptTemplateId.MEMORY_NODE_EXTRACTION,
                        {"facts": facts_text},
                    )
                    node_prompt = node_envelope.prompt
                    node_system_prompt = node_envelope.system_prompt
                async def _node_call():
                    return await self.gateway.call_data_process_task(
                        prompt=node_prompt,
                        system_prompt=node_system_prompt,
                        is_json=True,
                        lane_key=lane_key,
                        base_origin=session_id,
                        template_envelope=node_envelope,
                    )

                node_result = await self.background_task_budget.run(
                    _node_call,
                    task_name="memory.summary_nodes",
                    scope_id=session_id,
                    defer_release_on_timeout=True,
                )
                
                node_data = self._parse_json(
                    node_result,
                    required_keys=("nodes", "deleted_nodes"),
                )
                    
                validated_data["nodes"] = node_data.get("nodes", [])
                validated_data["deleted_nodes"] = node_data.get("deleted_nodes", [])
            else:
                validated_data["nodes"] = []
                validated_data["deleted_nodes"] = []

            return validated_data
            
        except Exception as e:
            logger.error(f"[MemoryProcessor] 结构化处理失败: {e}", exc_info=True)
            return self._get_default_structured_data()

    # 位置: astrmai/memory/processor.py -> MemoryProcessor 类下
    def _validate_and_fill(self, data: Dict[str, Any]) -> Dict[str, Any]:
        """[修改] 校验并补全默认字段，确保契约结构完整（新增 reflection 维度）"""
        def ensure_list(val: Any) -> List[str]:
            if isinstance(val, list):
                return [str(i) for i in val]
            return [str(val)] if val else []

        sentiment = str(data.get("sentiment", "neutral")).lower()
        if sentiment not in ["positive", "neutral", "negative"]:
            sentiment = "neutral"

        try:
            importance = float(data.get("importance", 0.5))
            importance = max(0.0, min(1.0, importance))
        except (ValueError, TypeError):
            importance = 0.5

        return {
            "summary": str(data.get("summary", "对话记录")),
            "topics": ensure_list(data.get("topics")),
            "key_facts": ensure_list(data.get("key_facts")),
            "reflection": str(data.get("reflection", "无")),
            "sentiment": sentiment,
            "importance": importance
        }

    # 位置: astrmai/memory/processor.py -> MemoryProcessor 类下
    def _get_default_structured_data(self) -> Dict[str, Any]:
        """[修改] 提供包含节点和反思空列表的默认数据结构"""
        return {
            "summary": "对话记录",
            "topics": [],
            "key_facts": [],
            "reflection": "无",
            "sentiment": "neutral",
            "importance": 0.5,
            "nodes": [],
            "deleted_nodes": []
        }

    def _parse_json(
        self,
        text: Any,
        *,
        required_keys: tuple[str, ...] = (),
    ) -> Dict[str, Any]:
        result = parse_json_contract(
            text,
            required_keys=required_keys,
            optional_keys=(
                "summary", "topics", "key_facts", "reflection", "sentiment",
                "importance", "nodes", "deleted_nodes",
            ),
            field_types={
                "summary": str,
                "topics": list,
                "key_facts": list,
                "reflection": str,
                "sentiment": str,
                "importance": (int, float),
                "nodes": list,
                "deleted_nodes": list,
            },
            allow_extra_keys=False,
            allow_naked_members=True,
        )
        if result.schema_valid:
            return dict(result.value)
        logger.warning(
            "[MemoryProcessor] structured output rejected "
            f"status={result.terminal_status} missing={list(result.missing_keys)} "
            f"unexpected={list(result.unexpected_keys)} invalid_types={list(result.invalid_type_keys)}"
        )
        return {}
