from __future__ import annotations

import asyncio
import time

from astrbot.api import logger

from ..infrastructure.persistence.orm_models import VisualMemory
from ..infrastructure.runtime.lane_manager import LaneKey
from ..shared.helpers.plugin_helpers import safe_create_task

from .image_pipeline import ImagePipeline
from .vision_prompt import VISION_SYSTEM_PROMPT, VISION_USER_PROMPT, normalize_vision_result


class VisualCortex:
    """Refactoring-side multimodal worker owning image queue and vision analysis."""

    def __init__(self, gateway, db_service):
        self.gateway = gateway
        self.db_service = db_service
        self.queue = asyncio.Queue(maxsize=100)  # ponytail: R11 — cap queue to prevent OOM
        self._worker_task = None

    def start(self):
        if self._worker_task is None:
            self._worker_task = safe_create_task(self._worker())
            logger.info("[AstrMai-VisualCortex] async multimodal worker started.")

    def stop(self):
        if self._worker_task:
            self._worker_task.cancel()

    def submit_task(self, picid: str, base64_data: str) -> bool:
        try:
            self.queue.put_nowait((picid, base64_data))
            return True
        except asyncio.QueueFull:
            logger.warning(f"[AstrMai-VisualCortex] queue full, dropped task for {picid}")
            return False

    def describe_status(self) -> dict:
        return {
            "worker_running": self._worker_task is not None and not self._worker_task.done(),
            "queue_size": self.queue.qsize(),
            "db_bound": self.db_service is not None,
        }

    async def _worker(self):
        while True:
            try:
                picid, base64_data = await self.queue.get()
            except asyncio.CancelledError:
                break
            try:
                await self.process_image_async(picid, base64_data)
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[AstrMai-VisualCortex] queue worker degraded: {exc}", exc_info=True)
            finally:
                self.queue.task_done()

    # ponytail: sync DB session in async worker — acceptable for SQLite (fast, no network).
    # If this becomes a bottleneck, wrap in asyncio.to_thread().

    def _build_lane_key(self, scope_id: str) -> LaneKey:
        return LaneKey(subsystem="sys1", task_family="vision", scope_id=scope_id or "global")

    def _get_cached_memory(self, scoped_picid: str):
        with self.db_service.get_session() as session:
            return session.get(VisualMemory, scoped_picid)

    def _upsert_visual_memory(self, scoped_picid: str, img_type: str, description: str, tags_json_str: str) -> None:
        with self.db_service.get_session() as session:
            memory = session.get(VisualMemory, scoped_picid)
            if not memory:
                memory = VisualMemory(
                    picid=scoped_picid,
                    type=img_type,
                    description=description,
                    emotion_tags=tags_json_str,
                    timestamp=time.time(),
                )
                session.add(memory)
            else:
                memory.type = img_type
                memory.description = description
                memory.emotion_tags = tags_json_str
                memory.timestamp = time.time()
            session.commit()

    @staticmethod
    def _memory_payload(memory) -> dict | None:
        if memory is None:
            return None
        return {
            "type": str(getattr(memory, "type", "image") or "image"),
            "description": str(getattr(memory, "description", "") or ""),
            "emotion_tags": ImagePipeline._safe_json_tags(getattr(memory, "emotion_tags", "[]")),
        }

    async def analyze_image_path(
        self,
        picid: str,
        image_path: str,
        scope_id: str = "global",
        timeout_override: float | None = None,
    ) -> dict | None:
        scoped_picid = f"{scope_id}:{picid}"
        cached = None
        if self.db_service is not None:
            cached = await asyncio.to_thread(self._get_cached_memory, scoped_picid)
        cached_payload = self._memory_payload(cached)
        if cached_payload and cached_payload.get("description"):
            logger.info(f"[AstrMai-VisualCortex] cache hit for {picid}, skip duplicate analysis.")
            cached_payload["_cache_hit"] = True
            return cached_payload

        result_dict = await self.gateway.call_vision_task(
            image_data=image_path,
            prompt=VISION_USER_PROMPT,
            system_prompt=VISION_SYSTEM_PROMPT,
            lane_key=self._build_lane_key(scope_id),
            timeout_override=timeout_override,
        )
        payload, _invalid_reason = normalize_vision_result(result_dict)
        if payload is None:
            return None
        payload["_cache_hit"] = False
        if self.db_service is not None:
            await asyncio.to_thread(
                self._upsert_visual_memory,
                scoped_picid,
                payload["type"],
                payload["description"],
                ImagePipeline.serialize_tags(payload["emotion_tags"]),
            )
        return payload

    async def process_image_async(self, picid: str, base64_data: str, scope_id: str = "global"):
        prepared = None
        try:
            prepared = await asyncio.to_thread(ImagePipeline.prepare_image, base64_data)
            if not prepared:
                logger.warning(f"[AstrMai-VisualCortex] failed to prepare image payload: {picid}")
                return
            return await self.analyze_image_path(picid, prepared.file_path, scope_id=scope_id)
        except Exception as exc:
            logger.error(f"[AstrMai-VisualCortex] process image failed for {picid}: {exc}", exc_info=True)
        finally:
            ImagePipeline.cleanup(prepared)


__all__ = ["VisualCortex"]
