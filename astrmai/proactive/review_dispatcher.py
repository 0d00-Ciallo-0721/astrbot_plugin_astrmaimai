from __future__ import annotations

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
                await self.context.send_message(item["group_id"], MessageChain().message(item["question"]))
            except Exception as exc:
                logger.warning(f"[Life] review dispatch degraded: {exc}")

    async def describe_status(self) -> dict:
        if not self.reflect_tracker:
            return {"ready": False, "pending": 0}
        try:
            requests = await self.reflect_tracker.get_unsent_requests()
        except Exception as exc:
            logger.debug(f"[Life] review dispatcher status degraded: {exc}")
            return {"ready": False, "pending": 0}
        return {"ready": True, "pending": len(requests)}


__all__ = ["ReviewDispatcher"]
