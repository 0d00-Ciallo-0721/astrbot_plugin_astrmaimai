from __future__ import annotations

import asyncio

from astrbot.api import logger
from astrbot.api.event import MessageChain


class ReviewDispatcher:
    def __init__(self, context, reflect_tracker):
        self.context = context
        self.reflect_tracker = reflect_tracker

    async def dispatch_pending(self):
        if not self.reflect_tracker:
            return
        requests = await self.reflect_tracker.get_unsent_requests()
        for item in requests:
            try:
                umo = self._normalize_umo(item.get("umo") or item.get("group_id", ""))
                await self.context.send_message(umo, MessageChain().message(item["question"]))
                if hasattr(self.reflect_tracker, "mark_request_sent"):
                    await self.reflect_tracker.mark_request_sent(str(item.get("pattern_id", "") or ""))
            except Exception as exc:
                logger.warning(f"[Life] review dispatch degraded: {exc}")
                await asyncio.sleep(0.2)

    async def describe_status(self) -> dict:
        if not self.reflect_tracker:
            return {"ready": False, "pending": 0}
        try:
            requests = await self.reflect_tracker.get_unsent_requests()
        except Exception as exc:
            logger.debug(f"[Life] review dispatcher status degraded: {exc}")
            return {"ready": False, "pending": 0}
        return {"ready": True, "pending": len(requests)}

    @staticmethod
    def _normalize_umo(value: object) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        if ":" in text:
            return text
        return f"default:GroupMessage:{text}"


__all__ = ["ReviewDispatcher"]
