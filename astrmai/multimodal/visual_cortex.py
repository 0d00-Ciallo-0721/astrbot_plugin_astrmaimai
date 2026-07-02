from __future__ import annotations

import asyncio
import time

from astrbot.api import logger

from ..infrastructure.persistence.orm_models import VisualMemory
from ..infrastructure.runtime.lane_manager import LaneKey
from ..shared.helpers.plugin_helpers import safe_create_task

from .image_pipeline import ImagePipeline


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
                await self.process_image_async(picid, base64_data)
                self.queue.task_done()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.error(f"[AstrMai-VisualCortex] queue worker degraded: {exc}", exc_info=True)

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

    async def process_image_async(self, picid: str, base64_data: str, scope_id: str = "global"):
        prepared = None
        scoped_picid = f"{scope_id}:{picid}"
        try:
            cached = await asyncio.to_thread(self._get_cached_memory, scoped_picid)
            if cached:
                logger.info(f"[AstrMai-VisualCortex] cache hit for {picid}, skip duplicate analysis.")
                return

            prepared = ImagePipeline.prepare_image(base64_data)
            if not prepared:
                logger.warning(f"[AstrMai-VisualCortex] failed to prepare image payload: {picid}")
                return

            system_prompt = (
                "你是一个群聊视觉分析助手。请分析图片内容，并严格使用 JSON 返回: "
                '{"type": "image" or "emoji", "description": "...", "emotion_tags": ["..."]}'
            )

            result_dict = await self.gateway.call_vision_task(
                image_data=prepared.file_path,
                prompt="请分析这幅图片/表情包。",
                system_prompt=system_prompt,
                lane_key=self._build_lane_key(scope_id),
            )
            if not result_dict:
                return

            img_type = result_dict.get("type", "image")
            description = result_dict.get("description", "无法识别内容的图片")
            tags_json_str = ImagePipeline.serialize_tags(result_dict.get("emotion_tags", []))

            await asyncio.to_thread(
                self._upsert_visual_memory,
                scoped_picid,
                img_type,
                description,
                tags_json_str,
            )
        except Exception as exc:
            logger.error(f"[AstrMai-VisualCortex] process image failed for {picid}: {exc}", exc_info=True)
        finally:
            ImagePipeline.cleanup(prepared)


__all__ = ["VisualCortex"]
