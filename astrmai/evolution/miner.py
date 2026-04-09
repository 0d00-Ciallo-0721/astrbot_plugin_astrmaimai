import json
import time
from typing import List, Dict, Any
from astrbot.api import logger
from ..infra.database import ExpressionPattern, MessageLog
from ..infra.gateway import GlobalModelGateway
from ..infra.datamodels import ExpressionPattern, MessageLog, Jargon
from ..infra.lane_manager import LaneKey

class ExpressionMiner:
    """
    风格挖掘器 (System 2 Offline Task)
    职责: 从历史消息中提炼 Expression Patterns
    Reference: Self_Learning/expression_pattern_learner.py
    """
    def __init__(self, gateway: GlobalModelGateway, config=None):
        self.gateway = gateway
        self.config = config if config else gateway.config

    def _reflect_lane(self, group_id: str) -> LaneKey:
        return LaneKey(subsystem="bg", task_family="reflect", scope_id=group_id or "global", scope_kind="global")

    async def mine(self, group_id: str, messages: List[MessageLog]) -> List[ExpressionPattern]:
        """
        执行挖掘任务：融合句式与黑话的双重提取 (Reference: expression_learner.py & jargon_miner.py)
        """
        # 接入 Config 阈值
        min_mining_context = self.config.evolution.min_mining_context
        if len(messages) < min_mining_context:
            return []

        # 1. 构建 Context
        context_str = self._build_context(messages)
        
        logger.info(f"[Evolution-Miner] 🧠 启动后台任务: 开始挖掘群组 {group_id} 的表达风格 (分析上下文: {len(messages)}条)...")
        
        # 2. 构建融合 Prompt
        prompt = f"""
{context_str}

请从上面这段群聊记录中，分析并概括除了人名为"SELF"（也就是你自己）之外的用户的语言风格和专属黑话。
任务1（语言风格）：总结特定的表达规律，例如"当[某场景]时，喜欢说[某句话]"。
任务2（群组黑话）：提取群友发明的特殊词汇、简称或梗，并解释其场景。

严格返回 JSON 数组格式，每个对象包含 situation（场景/梗的含义）和 expression（表达方式/黑话词汇）：
[
    {{"situation": "打招呼或者赞同别人时", "expression": "确实"}},
    {{"situation": "群友发送好笑的事情时", "expression": "草"}},
    {{"situation": "用来指代游戏里的某个特定BOSS", "expression": "大鸟"}}
]
"""
        try:
            # [修改点] 调用数据处理接口，深度防御数据类型异常
            raw_result = await self.gateway.call_data_process_task(
                prompt=prompt,
                is_json=True,
                lane_key=self._reflect_lane(group_id),
                base_origin="",
            )
            
            patterns_data = []
            if isinstance(raw_result, list):
                patterns_data = raw_result
            elif isinstance(raw_result, str):
                patterns_data = self._parse_json(raw_result)
            
            # 确保最终一定是 list
            if not isinstance(patterns_data, list):
                patterns_data = []
            
            patterns = []
            import time

            for item in patterns_data:
                # 🟢 防御：严格检查 item 必须是字典，防止 LLM 在数组里混入字符串导致 TypeError
                if isinstance(item, dict):
                    situation = item.get("situation")
                    expression = item.get("expression")
                    if situation and expression:
                        patterns.append(ExpressionPattern(
                            situation=str(situation),
                            expression=str(expression),
                            group_id=group_id,
                            weight=1.0,
                            last_active_time=time.time(),
                            create_time=time.time()
                        ))

            logger.info(f"[Evolution-Miner] ✅ 风格挖掘完成: 群组 {group_id} 成功提取 {len(patterns)} 条表达习惯。")
                       
            return patterns
            
        except Exception as e:
            logger.error(f"[Evolution-Miner] ❌ 风格挖掘任务失败: {e}")
            return []

    def _build_context(self, messages: List[MessageLog]) -> str:
        lines = []
        for msg in messages:
            # 简单清洗
            content = msg.content.strip()
            if not content or content.startswith("[") or len(content) > 100:
                continue
            lines.append(f"{msg.sender_name}: {content}")
        return "\n".join(lines)

    def _parse_json(self, text: str) -> List[Dict]:
        """简易 JSON 提取"""
        import re
        try:
            match = re.search(r"```json\s*(.*?)\s*```", text, re.DOTALL)
            json_str = match.group(1) if match else text
            return json.loads(json_str)
        except:
            return []
        

    async def mine_jargons(self, group_id: str, messages: List[MessageLog]) -> List[Jargon]:
        """
        [修改] 从历史消息中挖掘群组黑话，并使用三步推断法尝试解析含义
        """
        min_mining_context = getattr(self.config.evolution, 'min_mining_context', 20)
        if len(messages) < min_mining_context:
            return []

        context_str = self._build_context(messages)
        
        logger.info(f"[Evolution-Miner] 🕵️ 启动后台任务: 开始挖掘群组 {group_id} 的潜藏黑话 (分析上下文: {len(messages)}条)...")
        
        # 👆【新增结束】
        # 步骤 1: 识别潜在黑话
        extract_prompt = f"""
{context_str}

请从上面这段群聊记录中，提取出群员频繁使用、具有特定小圈子含义的“黑话”、网络用语或特殊简称。
严格返回 JSON 格式的列表：
[
  {{
    "jargon": "黑话词汇",
    "raw_context": "包含该词汇的原话（用于后续推断）"
  }}
]
如果没有找到任何黑话，请返回 []。
"""
        try:
            # [修改点] 深度防御数据类型异常
            raw_result = await self.gateway.call_data_process_task(
                prompt=extract_prompt,
                is_json=True,
                lane_key=self._reflect_lane(group_id),
                base_origin="",
            )
            
            jargon_candidates = []
            if isinstance(raw_result, list):
                jargon_candidates = raw_result
            elif isinstance(raw_result, str):
                jargon_candidates = self._parse_json(raw_result)
            
            # 确保最终一定是 list
            if not isinstance(jargon_candidates, list):
                jargon_candidates = []
            
            # 🟢 [核心降频] 截断过多候选词，批量推断
            jargon_candidates = [item for item in jargon_candidates if isinstance(item, dict) and item.get("jargon") and item.get("raw_context")]
            jargon_candidates = jargon_candidates[:5]  # 硬上限限制

            jargons = []
            import time
            
            if jargon_candidates:
                inferred_batch = await self._infer_jargons_batch(group_id, jargon_candidates)
                for i, item in enumerate(jargon_candidates):
                    if i < len(inferred_batch):
                        inferred_data = inferred_batch[i]
                        if not isinstance(inferred_data, dict):
                            inferred_data = {}
                    else:
                        inferred_data = {}
                        
                    jargons.append(Jargon(
                        content=str(item.get("jargon")),
                        raw_content=str(item.get("raw_context")),
                        meaning=str(inferred_data.get("meaning", "")),
                        is_jargon=bool(inferred_data.get("is_jargon", False)),
                        is_complete=bool(inferred_data.get("is_complete", False)),
                        group_id=group_id,
                        created_at=time.time(),
                        updated_at=time.time()
                    ))
            
            # 👇【新增】在此处插入成功结果日志
            logger.info(f"[Evolution-Miner] ✅ 黑话挖掘完成: 群组 {group_id} 成功提取 {len(jargons)} 条特殊词汇。")
            # 👆【新增结束】
            
            return jargons
        except Exception as e:
            # 👇【修改】完善异常日志
            logger.error(f"[Evolution-Miner] ❌ 黑话挖掘任务异常: {e}")
            # 👆【修改结束】
            return []


    async def _infer_jargons_batch(self, group_id: str, jargon_candidates: List[dict]) -> List[dict]:
        """
        [修改] 批量推断法 (融合上下文与基础词义)，消除 N 次长级联 API 调用
        """
        logger.info(f"[Evolution-Miner] 🔍 正在批量调用 LLM 推断 {len(jargon_candidates)} 个词汇含义...")

        items_str = "\n".join(
            f"{i+1}. 词: {w['jargon']} | 上下文: {w['raw_context']}"
            for i, w in enumerate(jargon_candidates)
        )

        infer_prompt = f"""
请为以下词汇逐一执行推断分析：
1. 分析它在上下文中的实际意图。
2. 结合常规网络用语知识，补全它的具体含义。
3. 它是否是一个真正的“黑话”或“特殊用语”？

待推断词条：
{items_str}

严格返回 JSON 数组，顺序必须与给出的一致：
[
  {{
    "meaning": "详细含义说明（包含使用场景、具体解释）",
    "is_jargon": true/false (是否确认为黑话),
    "is_complete": true/false (信息是否足够推断出明确含义)
  }}
]
"""
        try:
            result = await self.gateway.call_data_process_task(
                prompt=infer_prompt,
                is_json=True,
                lane_key=self._reflect_lane(group_id),
                base_origin="",
            )
            
            data = []
            if isinstance(result, list):
                data = result
            elif isinstance(result, str):
                import re
                import json
                match = re.search(r'\[.*\]', result, re.DOTALL)
                if match:
                    try:
                        data = json.loads(match.group(0))
                    except json.JSONDecodeError:
                        data = []
                        
            if not isinstance(data, list):
                data = []
                
            return data
        except Exception as e:
            logger.debug(f"[Evolution] 黑话推断解析失败: {e}")
            
        return []

    def _build_joint_prompt(self, context_str: str) -> str:
        return f"""
{context_str}

请同时完成两项抽取：
1. expressions：用户的“情境 -> 表达习惯”
2. jargons：聊天中反复出现的黑话、简称、梗词和它们的含义

严格返回 JSON：
{{
  "expressions": [
    {{
      "situation": "什么情境下会这样说",
      "expression": "常见表达",
      "style": "风格标签",
      "content_samples": ["原句1", "原句2"],
      "think_level": 0
    }}
  ],
  "jargons": [
    {{
      "content": "词条",
      "raw_content": "上下文原句",
      "meaning": "词义解释",
      "is_jargon": true,
      "is_complete": true
    }}
  ]
}}
"""

    def _build_pattern(self, group_id: str, item: Dict[str, Any]) -> ExpressionPattern:
        samples = item.get("content_samples", [])
        if not isinstance(samples, list):
            samples = []
        return ExpressionPattern(
            situation=str(item.get("situation", "")).strip(),
            expression=str(item.get("expression", "")).strip(),
            style=str(item.get("style", "")).strip(),
            content_list=json.dumps([str(sample).strip() for sample in samples if str(sample).strip()][:8], ensure_ascii=False),
            count=max(len(samples), 1),
            checked=False,
            rejected=False,
            modified_by="ai",
            source="joint_mining",
            shared_scope=str(item.get("shared_scope", "")).strip(),
            think_level=int(item.get("think_level", 0) or 0),
            review_status="pending",
            weight=1.0,
            last_active_time=time.time(),
            create_time=time.time(),
            group_id=group_id,
        )

    def _build_jargon(self, group_id: str, item: Dict[str, Any]) -> Jargon:
        return Jargon(
            content=str(item.get("content", "")).strip(),
            raw_content=str(item.get("raw_content", "")).strip(),
            meaning=str(item.get("meaning", "")).strip(),
            is_jargon=bool(item.get("is_jargon", False)),
            is_complete=bool(item.get("is_complete", False)),
            group_id=group_id,
            created_at=time.time(),
            updated_at=time.time(),
        )

    async def mine_bundle(self, group_id: str, messages: List[MessageLog]) -> Dict[str, List[Any]]:
        min_mining_context = getattr(self.config.evolution, "min_mining_context", 10)
        if len(messages) < min_mining_context:
            return {"patterns": [], "jargons": []}

        context_str = self._build_context(messages)
        if not context_str.strip():
            return {"patterns": [], "jargons": []}

        result = await self.gateway.call_data_process_task(
            prompt=self._build_joint_prompt(context_str),
            is_json=True,
            lane_key=self._reflect_lane(group_id),
            base_origin="",
        )
        if isinstance(result, str):
            parsed = self._parse_json(result)
            result = parsed if isinstance(parsed, dict) else {}
        if not isinstance(result, dict):
            result = {}

        patterns: List[ExpressionPattern] = []
        for item in result.get("expressions", []):
            if not isinstance(item, dict):
                continue
            if not str(item.get("situation", "")).strip() or not str(item.get("expression", "")).strip():
                continue
            patterns.append(self._build_pattern(group_id, item))

        jargons: List[Jargon] = []
        for item in result.get("jargons", []):
            if not isinstance(item, dict):
                continue
            if not str(item.get("content", "")).strip() or not str(item.get("raw_content", "")).strip():
                continue
            jargons.append(self._build_jargon(group_id, item))

        logger.info(f"[Evolution-Miner] 联合抽取完成: {group_id} -> expressions={len(patterns)}, jargons={len(jargons)}")
        return {"patterns": patterns, "jargons": jargons}

    async def mine(self, group_id: str, messages: List[MessageLog]) -> List[ExpressionPattern]:
        bundle = await self.mine_bundle(group_id, messages)
        return bundle.get("patterns", [])

    async def mine_jargons(self, group_id: str, messages: List[MessageLog]) -> List[Jargon]:
        bundle = await self.mine_bundle(group_id, messages)
        return bundle.get("jargons", [])
