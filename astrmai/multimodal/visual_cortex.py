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

    async def analyze_image_path(self, picid: str, image_path: str, scope_id: str = "global") -> dict | None:
        scoped_picid = f"{scope_id}:{picid}"
        cached = None
        if self.db_service is not None:
            cached = await asyncio.to_thread(self._get_cached_memory, scoped_picid)
        cached_payload = self._memory_payload(cached)
        if cached_payload and cached_payload.get("description"):
            logger.info(f"[AstrMai-VisualCortex] cache hit for {picid}, skip duplicate analysis.")
            return cached_payload

        result_dict = await self.gateway.call_vision_task(
            image_data=image_path,
            prompt=(
                "请完整分析当前图片，先判断它是普通图片还是表情包，再按照系统要求提取画面信息。"
                "描述必须以图片中真实可见的内容为依据，供后续对话模型准确理解用户发来的图片。"
            ),
            system_prompt=(
                "你是负责为聊天系统转述图片的视觉分析助手。请仔细观察图片，并先将其分类为普通图片 "
                "image 或表情包 emoji。\n"
                "普通图片要求：用自然、完整的中文客观转述画面；说明主要主体及数量、外观特征、动作或互动、"
                "场景环境、重要物体、位置关系、显著颜色和可能影响理解的细节。图片中存在标题、对话、标牌、"
                "截图界面或其他可见文字时，应尽可能准确地提取并说明文字内容；无法确认的细节必须明确表示不确定。\n"
                "表情包要求：description 也必须先完整描述画面内容，包括人物或角色、表情、姿态、动作、道具、"
                "构图以及全部可见文字；然后结合这些视觉信息解释主要情绪、情绪强度、表达意图和语气，例如赞同、"
                "拒绝、无奈、抱怨、调侃、讽刺、敷衍、安慰或庆祝，并说明它在聊天中通常想表达的意思。存在多种合理"
                "解释时应保留不确定性，不要强行断言。\n"
                "通用约束：不得猜测图片中不可见的身份、关系、事件前因后果或敏感属性；不要向用户提问，不要代替"
                "聊天机器人回复，不要输出分析过程。emotion_tags 请给出 1 到 5 个简短中文标签，概括主要情绪和"
                "交流语气。\n"
                "只输出一个 JSON 对象，不要使用 Markdown 代码块，不要添加 JSON 以外的文字。JSON 键必须严格保持为："
                '{"type": "image or emoji", "description": "完整中文转述", "emotion_tags": ["标签"]}'
            ),
            lane_key=self._build_lane_key(scope_id),
        )
        if not isinstance(result_dict, dict):
            return None
        description = str(result_dict.get("description", "") or "").strip()
        if not description or description.lower() in {"none", "null"}:
            return None
        payload = {
            "type": str(result_dict.get("type", "image") or "image"),
            "description": description,
            "emotion_tags": result_dict.get("emotion_tags", []) if isinstance(result_dict.get("emotion_tags", []), list) else [],
        }
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
