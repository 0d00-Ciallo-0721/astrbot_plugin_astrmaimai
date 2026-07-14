import asyncio
import json
import re
import time
from typing import Dict, List, Optional

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from ...infrastructure.persistence import DatabaseService
from ...infrastructure.persistence import ExpressionPattern
from ...infrastructure.gateway import GlobalModelGateway
from ...infrastructure.runtime.lane_manager import LaneKey


class ReflectTracker:
    """人工反馈追踪器。"""

    def __init__(self, db_service: DatabaseService, gateway: GlobalModelGateway, config=None):
        self.db = db_service
        self.gateway = gateway
        self.config = config if config else gateway.config
        self._pending: Dict[str, Dict] = {}
        self._lock = asyncio.Lock()

    def _pattern_service(self):
        return getattr(getattr(self.db, "memory_engine", None), "expression_pattern_service", None)

    def queue_review_request(self, pattern: ExpressionPattern, reason: str = "", replacement: str = ""):
        if not getattr(pattern, "id", None):
            return
        pattern_id = str(pattern.id)
        group_id = str(getattr(pattern, "group_id", "") or "")
        umo = self._normalize_umo(getattr(pattern, "umo", "") or getattr(pattern, "unified_msg_origin", "") or group_id)
        existing = self._pending.get(pattern_id)
        if existing:
            existing["question"] = self._build_question(pattern, reason=reason, replacement=replacement)
            existing["group_id"] = group_id
            existing["umo"] = umo
            return
        self._pending[pattern_id] = {
            "pattern_id": pattern_id,
            "group_id": group_id,
            "umo": umo,
            "question": self._build_question(pattern, reason=reason, replacement=replacement),
            "created_at": time.time(),
            "sent": False,
            "processing": False,
        }

    async def requeue_request(self, pattern_id: str) -> bool:
        async with self._lock:
            item = self._pending.get(str(pattern_id or ""))
            if not item:
                return False
            item["sent"] = False
            item["processing"] = False
            return True

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
            return [item.copy() for item in self._pending.values() if not item.get("sent")]

    async def mark_request_sent(self, pattern_id: str) -> None:
        async with self._lock:
            pid = str(pattern_id or "")
            if pid and pid in self._pending:
                self._pending[pid]["sent"] = True

    @staticmethod
    def _normalize_umo(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if ":" in text:
            return text
        return f"default:GroupMessage:{text}"

    async def try_consume_feedback(self, event: AstrMessageEvent) -> Optional[str]:
        admin_ids = set(getattr(self.config.global_settings, "admin_ids", []) or [])
        sender_id = str(event.get_sender_id())
        if admin_ids and sender_id not in admin_ids:
            return None

        text = (event.message_str or "").strip()
        if not text:
            return None

        async with self._lock:
            event_umo = str(event.unified_msg_origin or "")
            normalized_event_umo = self._normalize_umo(event_umo)
            candidates = [
                item for item in self._pending.values()
                if item.get("group_id") == event_umo
                or item.get("umo") == event_umo
                or item.get("umo") == normalized_event_umo
            ]
            candidates = [item for item in candidates if not item.get("processing")]
        if not candidates:
            return None

        pattern_id = self._extract_pattern_id(text)
        if pattern_id is None and len(candidates) == 1:
            pattern_id = candidates[0]["pattern_id"]
        if pattern_id is None:
            return None

        async with self._lock:
            pending = self._pending.get(str(pattern_id))
            if not pending or pending.get("processing"):
                return None
            pending["processing"] = True

        decision = await self._parse_feedback(event.unified_msg_origin, text)
        if not decision:
            await self._release_claim(str(pattern_id))
            return f"表达审核 #{pattern_id} 暂未处理，请稍后重试。"

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
            await self._release_claim(str(pattern_id))
            return None

        try:
            service = self._pattern_service()
            if service and hasattr(service, "update_review"):
                updated = await service.update_review(str(pattern_id), **kwargs)
            else:
                legacy_id = int(pattern_id) if str(pattern_id).isdigit() else pattern_id
                updated = await self.db.update_pattern_review_async(legacy_id, **kwargs)
        except Exception as exc:
            logger.warning(f"[ReflectTracker] 人工审核持久化失败 #{pattern_id}: {exc}")
            await self._release_claim(str(pattern_id))
            return f"表达审核 #{pattern_id} 暂未处理，请稍后重试。"
        if not updated:
            await self._release_claim(str(pattern_id))
            return f"表达审核 #{pattern_id} 暂未处理，请稍后重试。"
        async with self._lock:
            self._pending.pop(str(pattern_id), None)
        return f"已处理表达审核 #{pattern_id}：{action}"

    async def _release_claim(self, pattern_id: str) -> None:
        async with self._lock:
            item = self._pending.get(str(pattern_id or ""))
            if item:
                item["processing"] = False

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
def _extract_canonical_pattern_id(text: str) -> Optional[str]:
    match = re.search(r"#([A-Za-z0-9_-]+)", str(text or ""))
    if match:
        return str(match.group(1))
    match = re.search(r"expression\s+review\s+([A-Za-z0-9_-]+)", str(text or ""), re.IGNORECASE)
    if match:
        return str(match.group(1))
    return None


# ponytail: replaces int-return with str-return for canonical IDs. Callers use str() so compatible.
ReflectTracker._extract_pattern_id = staticmethod(_extract_canonical_pattern_id)
