from __future__ import annotations

import asyncio
import hashlib
import time
from typing import Any

from astrbot.api import logger


class PrivateTurnCoordinator:
    """Private-chat quiet-window plus direct-message pre-decision vision barrier."""

    def __init__(self, config: Any, image_resolver: Any, visual_cortex: Any, persistence: Any = None):
        self.config = config
        self.image_resolver = image_resolver
        self.visual_cortex = visual_cortex
        self.persistence = persistence

    def refresh_config(self, config: Any) -> None:
        self.config = config

    def _private_config(self) -> Any:
        return getattr(self.config, "private_chat", None)

    def settle_seconds(self) -> float:
        return max(0.0, float(getattr(self._private_config(), "input_settle_sec", 1.5) or 0.0))

    async def wait_for_input_stability(self, session: Any) -> None:
        delay = self.settle_seconds()
        if delay <= 0:
            return
        while True:
            async with session.lock:
                remaining = delay - max(0.0, time.time() - float(session.last_active_time or 0.0))
            if remaining <= 0:
                return
            await asyncio.sleep(remaining)

    async def prepare_batch(self, events: list[Any], chat_id: str) -> None:
        if not bool(getattr(getattr(self.config, "vision", None), "enable_vision", True)):
            return
        if self.image_resolver is None:
            return
        for event in events:
            if bool(event.get_extra("astrmai_vision_barrier_complete", False)):
                continue
            await self._prepare_event(event, chat_id)

    async def prepare_direct_event(self, event: Any, chat_id: str) -> None:
        """Resolve a directly addressed image before group decision/dispatch."""
        if not bool(getattr(getattr(self.config, "vision", None), "enable_vision", True)):
            return
        if self.image_resolver is None:
            return
        if bool(event.get_extra("astrmai_vision_barrier_complete", False)):
            return
        await self._prepare_event(event, chat_id)

    async def _prepare_event(self, event: Any, chat_id: str) -> None:
        private_cfg = self._private_config()
        resolve_timeout = max(0.1, float(getattr(private_cfg, "image_resolve_timeout_sec", 15.0) or 15.0))
        analysis_timeout = max(0.1, float(getattr(private_cfg, "image_barrier_timeout_sec", 45.0) or 45.0))
        retries = max(1, int(getattr(private_cfg, "image_analysis_retries", 2) or 2))
        try:
            resolution = await asyncio.wait_for(
                self.image_resolver.resolve_event_images(event),
                timeout=resolve_timeout,
            )
        except Exception as exc:
            logger.warning(f"[AstrMai-Vision] image resolution failed for {chat_id}: {exc}")
            event.set_extra("astrmai_vision_barrier_complete", True)
            event.set_extra("astrmai_vision_barrier_failed", True)
            self._append_failure_context(event, 1)
            return
        if not resolution.had_images:
            return

        local_paths = [item.local_path for item in resolution.images]
        picids = [self._build_picid(event, item.index, item.source_ref) for item in resolution.images]
        await self._persist_image_metadata(event, chat_id, picids)
        event.set_extra("extracted_image_urls", local_paths)
        event.set_extra("extracted_image_refs", local_paths)
        event.set_extra("direct_vision_urls", local_paths)
        event.set_extra("direct_image_refs", local_paths)
        descriptions: list[str] = []
        failed_count = len(resolution.failures)
        for image in resolution.images:
            result = None
            picid = self._build_picid(event, image.index, image.source_ref)
            for attempt in range(retries):
                try:
                    if self.visual_cortex is None or not hasattr(self.visual_cortex, "analyze_image_path"):
                        break
                    result = await asyncio.wait_for(
                        self.visual_cortex.analyze_image_path(picid, image.local_path, scope_id=chat_id),
                        timeout=analysis_timeout,
                    )
                    if result and str(result.get("description", "") or "").strip():
                        break
                except Exception as exc:
                    if attempt + 1 >= retries:
                        logger.warning(f"[AstrMai-Vision] vision barrier failed for {chat_id}: {exc}")
                    else:
                        await asyncio.sleep(min(0.5 * (attempt + 1), 1.0))
            description = str((result or {}).get("description", "") or "").strip()
            if description:
                descriptions.append(description)
            else:
                failed_count += 1

        event.set_extra("astrmai_vision_descriptions", descriptions)
        event.set_extra("astrmai_vision_barrier_complete", True)
        event.set_extra("astrmai_vision_barrier_failed", bool(failed_count))
        self._append_vision_context(event, descriptions, failed_count)
        await self._mark_vision_executed(event, chat_id)

    @staticmethod
    def _build_picid(event: Any, index: int, source_ref: str) -> str:
        message_obj = getattr(event, "message_obj", None)
        message_id = str(getattr(message_obj, "message_id", "") or getattr(message_obj, "id", "") or "")
        raw = f"{message_id}:{index}:{source_ref}"
        return hashlib.sha256(raw.encode("utf-8", errors="ignore")).hexdigest()

    async def _persist_image_metadata(self, event: Any, chat_id: str, picids: list[str]) -> None:
        writer = getattr(self.persistence, "add_last_message_meta", None)
        if not callable(writer):
            return
        try:
            sender_id = str(event.get_sender_id() if hasattr(event, "get_sender_id") else "")
            await writer(chat_id, sender_id, True, picids)
        except Exception as exc:
            logger.warning(f"[AstrMai-Vision] image metadata persistence degraded for {chat_id}: {exc}")

    async def _mark_vision_executed(self, event: Any, chat_id: str) -> None:
        marker = getattr(self.persistence, "mark_last_message_vision_executed", None)
        if not callable(marker):
            return
        try:
            sender_id = str(event.get_sender_id() if hasattr(event, "get_sender_id") else "")
            await marker(chat_id, sender_id)
        except Exception as exc:
            logger.warning(f"[AstrMai-Vision] vision metadata update degraded for {chat_id}: {exc}")

    @staticmethod
    def _append_vision_context(event: Any, descriptions: list[str], failed_count: int) -> None:
        base_text = str(event.get_extra("astrmai_rich_text", getattr(event, "message_str", "")) or "").strip()
        lines = [base_text] if base_text else []
        lines.extend(f"[图片转述：{description}]" for description in descriptions)
        if failed_count:
            lines.append(f"[有 {failed_count} 张图片读取失败；禁止猜测其内容。]")
        event.set_extra("astrmai_rich_text", "\n".join(lines).strip())

    @classmethod
    def _append_failure_context(cls, event: Any, failed_count: int) -> None:
        event.set_extra("astrmai_vision_descriptions", [])
        cls._append_vision_context(event, [], failed_count)


__all__ = ["PrivateTurnCoordinator"]
