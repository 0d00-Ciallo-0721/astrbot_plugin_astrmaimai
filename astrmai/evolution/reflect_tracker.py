import asyncio
import json
import re
import time
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ..infra.database import DatabaseService
from ..infra.datamodels import ExpressionPattern
from ..infra.gateway import GlobalModelGateway
from ..infra.lane_manager import LaneKey


class ReflectTracker:
    """人工反馈追踪器。"""

    def __init__(self, db_service: DatabaseService, gateway: GlobalModelGateway, config=None):
        self.db = db_service
        self.gateway = gateway
        self.config = config if config else gateway.config
        self._pending: Dict[int, Dict] = {}
        self._lock = asyncio.Lock()

    def queue_review_request(self, pattern: ExpressionPattern, reason: str = "", replacement: str = ""):
        if not getattr(pattern, "id", None):
            return
        self._pending[int(pattern.id)] = {
            "pattern_id": int(pattern.id),
            "group_id": pattern.group_id,
            "question": self._build_question(pattern, reason=reason, replacement=replacement),
            "created_at": time.time(),
            "sent": False,
        }

    def _build_question(self, pattern: ExpressionPattern, reason: str = "", replacement: str = "") -> str:
        suffix = f"\nAI 备注：{reason}" if reason else ""
        if replacement:
            suffix += f"\n建议改成：{replacement}"
        return (
            f"表达审核 #{pattern.id}\n"
            f"场景：{pattern.situation}\n"
            f"表达：{pattern.expression}\n"
            "请回复“通过”“拒绝”或“改成 xxx”。"
            f"{suffix}"
        )

    async def get_unsent_requests(self) -> List[Dict]:
        async with self._lock:
            requests = [item.copy() for item in self._pending.values() if not item.get("sent")]
            for item in self._pending.values():
                item["sent"] = True
            return requests

    async def try_consume_feedback(self, event: AstrMessageEvent) -> Optional[str]:
        admin_ids = set(getattr(self.config.global_settings, "admin_ids", []) or [])
        sender_id = str(event.get_sender_id())
        if admin_ids and sender_id not in admin_ids:
            return None

        text = (event.message_str or "").strip()
        if not text:
            return None

        async with self._lock:
            candidates = [
                item for item in self._pending.values()
                if item.get("group_id") == event.unified_msg_origin
            ]
        if not candidates:
            return None

        pattern_id = self._extract_pattern_id(text)
        if pattern_id is None and len(candidates) == 1:
            pattern_id = candidates[0]["pattern_id"]
        if pattern_id is None:
            return None

        decision = await self._parse_feedback(event.unified_msg_origin, text)
        if not decision:
            return None

        kwargs = {"modified_by": f"human:{sender_id}"}
        action = decision.get("decision")
        if action == "approved":
            kwargs.update(
                {
                    "checked": True,
                    "rejected": False,
                    "review_status": "approved",
                    "review_reason": str(decision.get("reason", "") or ""),
                    "review_suggestion": "",
                }
            )
        elif action == "rejected":
            kwargs.update(
                {
                    "checked": False,
                    "rejected": True,
                    "review_status": "rejected",
                    "weight_delta": -0.4,
                    "review_reason": str(decision.get("reason", "") or ""),
                    "review_suggestion": "",
                }
            )
        elif action == "revision_needed":
            kwargs.update(
                {
                    "checked": True,
                    "rejected": False,
                    "review_status": "approved",
                    "replacement_expression": decision.get("replacement_expression") or None,
                    "apply_replacement": True,
                    "review_reason": str(decision.get("reason", "") or ""),
                    "review_suggestion": "",
                }
            )
        else:
            return None

        updated = await self.db.update_pattern_review_async(pattern_id, **kwargs)
        async with self._lock:
            self._pending.pop(pattern_id, None)
        if not updated:
            return None
        return f"已处理表达审核 #{pattern_id}：{action}"

    async def _parse_feedback(self, chat_id: str, text: str) -> Optional[dict]:
        lowered = text.lower()
        if "通过" in text:
            return {"decision": "approved"}
        if "拒绝" in text or "否决" in text:
            return {"decision": "rejected"}
        match = re.search(r"改成[:： ]*(.+)$", text)
        if match:
            return {"decision": "revision_needed", "replacement_expression": match.group(1).strip()}

        prompt = (
            "请判断下列人工反馈属于哪种表达审核结果：approved / rejected / revision_needed。\n"
            f"反馈内容：{text}\n"
            "严格返回 JSON: "
            "{\"decision\":\"approved|rejected|revision_needed|unknown\","
            "\"replacement_expression\":\"可选替代表达\"}"
        )
        try:
            result = await self.gateway.call_data_process_task(
                prompt=prompt,
                is_json=True,
                lane_key=LaneKey(subsystem="bg", task_family="reflect", scope_id=chat_id or "global", scope_kind="global"),
                base_origin="",
            )
            if isinstance(result, dict):
                return result
            if isinstance(result, str):
                match = re.search(r"\{.*\}", result, re.DOTALL)
                if match:
                    return json.loads(match.group(0))
        except Exception as exc:
            logger.debug(f"[ReflectTracker] 解析人工反馈失败: {exc}")
        return None

    @staticmethod
    def _extract_pattern_id(text: str) -> Optional[int]:
        match = re.search(r"#(\d+)", text)
        if match:
            return int(match.group(1))
        match = re.search(r"表达审核\s*(\d+)", text)
        if match:
            return int(match.group(1))
        return None
