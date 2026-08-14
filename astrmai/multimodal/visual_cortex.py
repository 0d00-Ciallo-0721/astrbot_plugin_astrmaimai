from __future__ import annotations

import asyncio
import hashlib
import time
from pathlib import Path
from typing import Any

from astrbot.api import logger
from sqlmodel import select

from ..infrastructure.persistence.orm_models import (
    VisualAsset,
    VisualMemory,
    VisualMessageBinding,
)
from ..infrastructure.runtime.lane_manager import LaneKey
from ..shared.helpers.plugin_helpers import safe_create_task

from .image_pipeline import ImagePipeline
from .visual_asset_identity import (
    VisualAssetIdentity,
    build_visual_asset_identity,
    store_normalized_visual_asset,
)
from .vision_prompt import normalize_vision_result, vision_prompts_for_animation


class VisionAnalysisCoolingDown(RuntimeError):
    def __init__(self, retry_after_sec: float):
        self.retry_after_sec = max(0.0, float(retry_after_sec or 0.0))
        super().__init__(f"vision_analysis_cooling_down:{self.retry_after_sec:.1f}s")


class VisualCortex:
    """Refactoring-side multimodal worker owning image queue and vision analysis."""

    def __init__(self, gateway, db_service, *, config=None, asset_dir: str | Path | None = None):
        self.gateway = gateway
        self.db_service = db_service
        self.config = config
        self.asset_dir = Path(asset_dir) if asset_dir else self._default_asset_dir()
        self.queue = asyncio.Queue(maxsize=100)  # ponytail: R11 — cap queue to prevent OOM
        self._worker_task = None
        self._inflight_lock = asyncio.Lock()
        self._inflight: dict[str, asyncio.Task] = {}
        self._failure_cooldowns: dict[str, tuple[float, str]] = {}
        self._last_cleanup_at = 0.0

    def _default_asset_dir(self) -> Path:
        persistence = getattr(self.db_service, "persistence", None)
        cache_dir = getattr(persistence, "cache_dir", None)
        if cache_dir:
            try:
                return Path(cache_dir).parent / "visual_assets"
            except TypeError:
                pass
        return Path("data") / "plugin_data" / "astrmai" / "visual_assets"

    def refresh_config(self, config) -> None:
        self.config = config

    def _vision_config(self):
        return getattr(self.config, "vision", None)

    def _prompt_version(self) -> str:
        value = str(getattr(self._vision_config(), "visual_prompt_version", "v1") or "v1")
        return value.strip() or "v1"

    def _cache_enabled(self) -> bool:
        return bool(getattr(self._vision_config(), "enable_visual_result_cache", True))

    def _file_storage_enabled(self) -> bool:
        return bool(getattr(self._vision_config(), "store_visual_asset_files", False))

    def _failure_cooldown_sec(self) -> float:
        return max(
            0.0,
            float(getattr(self._vision_config(), "visual_failure_cooldown_sec", 120) or 0),
        )

    def _gif_max_sample_frames(self) -> int:
        return max(
            2,
            min(24, int(getattr(self._vision_config(), "gif_max_sample_frames", 12) or 12)),
        )

    def _gif_contact_sheet_max_edge_px(self) -> int:
        return max(
            512,
            min(
                4096,
                int(
                    getattr(
                        self._vision_config(),
                        "gif_contact_sheet_max_edge_px",
                        1600,
                    )
                    or 1600
                ),
            ),
        )

    def _gif_preprocess_timeout_sec(self) -> float:
        return max(
            1.0,
            min(
                30.0,
                float(
                    getattr(self._vision_config(), "gif_preprocess_timeout_sec", 8.0)
                    or 8.0
                ),
            ),
        )

    def _gif_max_decode_frames(self) -> int:
        return max(
            10,
            min(
                2000,
                int(getattr(self._vision_config(), "gif_max_decode_frames", 500) or 500),
            ),
        )

    @staticmethod
    def _prepared_metadata(prepared) -> dict[str, Any]:
        return {
            "_source_format": str(prepared.source_format or ""),
            "_declared_suffix": str(prepared.declared_suffix or ""),
            "_is_animated": bool(prepared.is_animated),
            "_source_frame_count": int(prepared.source_frame_count or 1),
            "_duration_ms": int(prepared.duration_ms or 0),
            "_sampled_frame_count": len(prepared.sampled_indices),
            "_sampled_indices": list(prepared.sampled_indices),
            "_sampled_timestamps_ms": list(prepared.sampled_timestamps_ms),
            "_preprocess_version": str(prepared.preprocess_version or ""),
            "_preprocess_status": str(prepared.preprocess_status or ""),
            "_preprocess_elapsed_ms": float(prepared.preprocess_elapsed_ms or 0.0),
            "_preprocess_fallback_reason": str(prepared.fallback_reason or ""),
            "_model_input_format": str(prepared.image_format or ""),
            "_contact_sheet_size": list(prepared.contact_sheet_size),
        }

    @staticmethod
    def _cached_identity_metadata(
        identity: VisualAssetIdentity,
        image_path: str,
    ) -> dict[str, Any]:
        source_format = str(identity.mime_type or "").partition("/")[2].lower()
        is_animated = int(identity.frame_count or 1) > 1
        return {
            "_source_format": source_format,
            "_declared_suffix": Path(image_path).suffix.lower(),
            "_is_animated": is_animated,
            "_source_frame_count": int(identity.frame_count or 1),
            "_duration_ms": 0,
            "_sampled_frame_count": 0,
            "_sampled_indices": [],
            "_sampled_timestamps_ms": [],
            "_preprocess_version": (
                ImagePipeline.ANIMATION_PREPROCESS_VERSION if is_animated else "static-v1"
            ),
            "_preprocess_status": "cache_hit_no_preprocess",
            "_preprocess_elapsed_ms": 0.0,
            "_preprocess_fallback_reason": "",
            "_model_input_format": "",
            "_contact_sheet_size": [],
        }

    def start(self):
        if self._worker_task is None or self._worker_task.done():
            self._worker_task = safe_create_task(self._worker())
            logger.info("[AstrMai-VisualCortex] async multimodal worker started.")

    def stop(self):
        if self._worker_task:
            self._worker_task.cancel()
            self._worker_task = None
        for task in tuple(self._inflight.values()):
            task.cancel()
        self._inflight.clear()
        self._failure_cooldowns.clear()
        discarded = self._discard_pending_tasks()
        if discarded:
            logger.info(
                f"[AstrMai-VisualCortex] discarded {discarded} pending image task(s) during shutdown."
            )

    def _discard_pending_tasks(self) -> int:
        discarded = 0
        while True:
            try:
                self.queue.get_nowait()
            except asyncio.QueueEmpty:
                return discarded
            else:
                self.queue.task_done()
                discarded += 1

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
            "content_cache_enabled": self._cache_enabled(),
            "asset_file_storage_enabled": self._file_storage_enabled(),
            "inflight_count": len(self._inflight),
            "failure_cooldown_count": len(self._failure_cooldowns),
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

    def _upsert_visual_memory(
        self,
        scoped_picid: str,
        img_type: str,
        description: str,
        tags_json_str: str,
    ) -> str:
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
        return "persisted"

    def _get_visual_asset(self, asset_id: str):
        with self.db_service.get_session() as session:
            asset = session.get(VisualAsset, asset_id)
            if asset is not None:
                asset.hit_count = int(asset.hit_count or 0) + 1
                asset.last_access_at = time.time()
                session.add(asset)
                session.commit()
                session.refresh(asset)
            return asset

    def _upsert_visual_asset(
        self,
        identity: VisualAssetIdentity,
        *,
        img_type: str,
        description: str,
        tags_json_str: str,
        model_id: str,
        storage_path: str = "",
        status: str = "ready",
        last_error: str = "",
        initial_recognition_elapsed_ms: float = 0.0,
    ) -> str:
        now = time.time()
        with self.db_service.get_session() as session:
            asset = session.get(VisualAsset, identity.asset_id)
            if asset is None:
                asset = VisualAsset(
                    asset_id=identity.asset_id,
                    blob_hash=identity.blob_hash,
                    pixel_hash=identity.pixel_hash,
                    perceptual_hash=identity.perceptual_hash,
                    prompt_version=identity.prompt_version,
                    type=img_type,
                    description=description,
                    emotion_tags=tags_json_str,
                    model_id=model_id,
                    mime_type=identity.mime_type,
                    width=identity.width,
                    height=identity.height,
                    frame_count=identity.frame_count,
                    byte_size=identity.byte_size,
                    initial_recognition_elapsed_ms=max(
                        0.0, float(initial_recognition_elapsed_ms or 0.0)
                    ),
                    storage_path=storage_path,
                    status=status,
                    last_error=last_error,
                    created_at=now,
                    updated_at=now,
                    last_access_at=now,
                )
            else:
                asset.blob_hash = identity.blob_hash
                asset.pixel_hash = identity.pixel_hash
                asset.perceptual_hash = identity.perceptual_hash
                asset.prompt_version = identity.prompt_version
                asset.type = img_type
                asset.description = description
                asset.emotion_tags = tags_json_str
                asset.model_id = model_id
                asset.mime_type = identity.mime_type
                asset.width = identity.width
                asset.height = identity.height
                asset.frame_count = identity.frame_count
                asset.byte_size = identity.byte_size
                if (
                    float(initial_recognition_elapsed_ms or 0.0) > 0
                    and float(asset.initial_recognition_elapsed_ms or 0.0) <= 0
                ):
                    asset.initial_recognition_elapsed_ms = float(
                        initial_recognition_elapsed_ms
                    )
                if storage_path:
                    asset.storage_path = storage_path
                asset.status = status
                asset.last_error = last_error
                asset.updated_at = now
                asset.last_access_at = now
            session.add(asset)
            session.commit()
        return "persisted"

    def _upsert_message_binding(
        self,
        *,
        asset_id: str,
        legacy_picid: str,
        binding_context: dict[str, Any] | None,
    ) -> str:
        context = dict(binding_context or {})
        chat_id = str(context.get("chat_id", "") or "")
        message_id = str(context.get("message_id", "") or "")
        if not chat_id or not message_id:
            return "skipped_missing_message_identity"
        image_index = max(0, int(context.get("image_index", 0) or 0))
        source_ref_hash = str(context.get("source_ref_hash", "") or "")
        raw_source_ref = str(context.get("source_ref", "") or "")
        if not source_ref_hash and raw_source_ref:
            source_ref_hash = hashlib.sha256(raw_source_ref.encode("utf-8")).hexdigest()
        now = time.time()
        with self.db_service.get_session() as session:
            statement = select(VisualMessageBinding).where(
                VisualMessageBinding.chat_id == chat_id,
                VisualMessageBinding.message_id == message_id,
                VisualMessageBinding.image_index == image_index,
            )
            binding = session.exec(statement).first()
            if binding is None:
                binding = VisualMessageBinding(
                    chat_id=chat_id,
                    message_id=message_id,
                    sender_id=str(context.get("sender_id", "") or ""),
                    image_index=image_index,
                    asset_id=asset_id,
                    legacy_picid=legacy_picid,
                    source_ref_hash=source_ref_hash,
                    created_at=now,
                    updated_at=now,
                )
            else:
                binding.sender_id = str(context.get("sender_id", "") or binding.sender_id or "")
                binding.asset_id = asset_id
                binding.legacy_picid = legacy_picid
                binding.source_ref_hash = source_ref_hash or binding.source_ref_hash
                binding.updated_at = now
            session.add(binding)
            session.commit()
        return "persisted"

    def _cleanup_asset_files(self) -> dict[str, int]:
        if not self.asset_dir.is_dir() or self.db_service is None:
            return {"removed": 0, "bytes_removed": 0}
        config = self._vision_config()
        retention_sec = max(
            86400,
            int(getattr(config, "visual_asset_retention_days", 30) or 30) * 86400,
        )
        max_bytes = max(
            16 * 1024 * 1024,
            int(getattr(config, "visual_asset_max_disk_mb", 512) or 512) * 1024 * 1024,
        )
        now = time.time()
        with self.db_service.get_session() as session:
            assets = list(
                session.exec(
                    select(VisualAsset).where(VisualAsset.storage_path != "")
                ).all()
            )
            entries: list[tuple[VisualAsset, Path, int]] = []
            for asset in assets:
                path = self.asset_dir / Path(str(asset.storage_path or "")).name
                size = path.stat().st_size if path.is_file() else 0
                entries.append((asset, path, size))

            remove_ids = {
                asset.asset_id
                for asset, _path, _size in entries
                if now - float(asset.last_access_at or asset.updated_at or now) > retention_sec
            }
            remaining = sum(
                size for asset, _path, size in entries if asset.asset_id not in remove_ids
            )
            if remaining > max_bytes:
                for asset, _path, size in sorted(
                    entries,
                    key=lambda item: float(item[0].last_access_at or item[0].updated_at or 0),
                ):
                    if remaining <= max_bytes:
                        break
                    if asset.asset_id not in remove_ids:
                        remove_ids.add(asset.asset_id)
                        remaining -= size

            removed = 0
            bytes_removed = 0
            for asset, path, size in entries:
                if asset.asset_id not in remove_ids:
                    continue
                try:
                    path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning(
                        "[AstrMai-Vision] asset cleanup failed "
                        f"asset={asset.asset_id[:12]} error={type(exc).__name__}"
                    )
                    continue
                asset.storage_path = ""
                asset.updated_at = now
                session.add(asset)
                removed += 1
                bytes_removed += size
            if removed:
                session.commit()
        return {"removed": removed, "bytes_removed": bytes_removed}

    async def _maybe_cleanup_asset_files(self) -> None:
        now = time.time()
        if now - self._last_cleanup_at < 3600:
            return
        self._last_cleanup_at = now
        try:
            result = await asyncio.to_thread(self._cleanup_asset_files)
            if result["removed"]:
                logger.info(
                    "[AstrMai-Vision] asset cleanup completed "
                    f"removed={result['removed']} bytes_removed={result['bytes_removed']}"
                )
        except Exception as exc:
            logger.warning(
                f"[AstrMai-Vision] asset cleanup degraded error={type(exc).__name__}"
            )

    @staticmethod
    def _memory_payload(memory) -> dict | None:
        if memory is None:
            return None
        return {
            "type": str(getattr(memory, "type", "image") or "image"),
            "description": str(getattr(memory, "description", "") or ""),
            "emotion_tags": ImagePipeline._safe_json_tags(getattr(memory, "emotion_tags", "[]")),
        }

    @staticmethod
    def _asset_payload(asset) -> dict | None:
        if asset is None or str(getattr(asset, "status", "")) != "ready":
            return None
        return {
            "type": str(getattr(asset, "type", "image") or "image"),
            "description": str(getattr(asset, "description", "") or ""),
            "emotion_tags": ImagePipeline._safe_json_tags(
                getattr(asset, "emotion_tags", "[]")
            ),
            "_asset_id": str(getattr(asset, "asset_id", "") or ""),
            "_model_id": str(getattr(asset, "model_id", "") or ""),
            "_prompt_version": str(getattr(asset, "prompt_version", "") or ""),
            "_asset_storage_status": (
                "stored" if str(getattr(asset, "storage_path", "") or "") else "not_stored"
            ),
        }

    def _on_inflight_done(self, asset_id: str, task: asyncio.Task) -> None:
        if self._inflight.get(asset_id) is task:
            self._inflight.pop(asset_id, None)
        if task.cancelled():
            return
        try:
            error = task.exception()
        except Exception:
            return
        failure_kind = type(error).__name__ if error is not None else ""
        if error is None:
            try:
                if task.result() is None:
                    failure_kind = "EmptyVisionResult"
            except Exception as exc:
                error = exc
                failure_kind = type(exc).__name__
        if failure_kind:
            cooldown_sec = self._failure_cooldown_sec()
            if cooldown_sec > 0:
                self._failure_cooldowns[asset_id] = (
                    time.monotonic() + cooldown_sec,
                    failure_kind,
                )
            logger.warning(
                "[AstrMai-Vision] in-flight analysis failed "
                f"asset={asset_id[:12]} error={failure_kind}"
            )

    def _failure_cooldown_remaining(self, asset_id: str) -> float:
        entry = self._failure_cooldowns.get(asset_id)
        if entry is None:
            return 0.0
        retry_at, _reason = entry
        remaining = retry_at - time.monotonic()
        if remaining <= 0:
            self._failure_cooldowns.pop(asset_id, None)
            return 0.0
        return remaining

    async def _run_vision_analysis(
        self,
        identity: VisualAssetIdentity,
        image_path: str,
        scope_id: str,
        timeout_override: float | None,
    ) -> dict | None:
        started_at = time.monotonic()
        prepared = await asyncio.to_thread(
            ImagePipeline.prepare_image_path,
            image_path,
            max_frames=self._gif_max_sample_frames(),
            max_edge_px=self._gif_contact_sheet_max_edge_px(),
            max_decode_frames=self._gif_max_decode_frames(),
            timeout_sec=self._gif_preprocess_timeout_sec(),
        )
        if prepared is None:
            raise ValueError("vision image preprocessing failed")
        prompt, system_prompt = vision_prompts_for_animation(prepared.is_animated)
        prepared_metadata = self._prepared_metadata(prepared)
        logger.info(
            "[AstrMai-Vision] model call started "
            f"asset={identity.asset_id[:12]} prompt_version={identity.prompt_version} "
            f"source_format={prepared.source_format or 'unknown'} "
            f"animated={prepared.is_animated} frames={prepared.source_frame_count} "
            f"sampled={len(prepared.sampled_indices)} "
            f"preprocess={prepared.preprocess_status} "
            f"preprocess_ms={prepared.preprocess_elapsed_ms}"
        )
        try:
            result_dict = await self.gateway.call_vision_task(
                image_data=prepared.file_path,
                prompt=prompt,
                system_prompt=system_prompt,
                lane_key=self._build_lane_key(scope_id),
                timeout_override=timeout_override,
            )
        except asyncio.CancelledError:
            logger.warning(
                "[AstrMai-Vision] model call cancelled "
                f"asset={identity.asset_id[:12]} "
                f"elapsed_ms={round((time.monotonic() - started_at) * 1000, 1)}"
            )
            ImagePipeline.cleanup(prepared)
            raise
        except Exception as exc:
            logger.warning(
                "[AstrMai-Vision] model call failed "
                f"asset={identity.asset_id[:12]} error={type(exc).__name__} "
                f"elapsed_ms={round((time.monotonic() - started_at) * 1000, 1)}"
            )
            ImagePipeline.cleanup(prepared)
            raise
        payload, invalid_reason = normalize_vision_result(result_dict)
        elapsed_ms = round((time.monotonic() - started_at) * 1000, 1)
        if payload is None:
            logger.warning(
                "[AstrMai-Vision] model call returned unusable result "
                f"asset={identity.asset_id[:12]} reason={invalid_reason or 'invalid_result'} "
                f"elapsed_ms={elapsed_ms}"
            )
            ImagePipeline.cleanup(prepared)
            return None
        model_id = str(
            (result_dict or {}).get("_vision_model_id", "")
            or (result_dict or {}).get("_model_id", "")
        )
        storage_path = ""
        storage_status = "disabled"
        if self._file_storage_enabled():
            try:
                storage_path = await asyncio.to_thread(
                    store_normalized_visual_asset,
                    prepared.file_path,
                    asset_id=identity.asset_id,
                    asset_dir=self.asset_dir,
                    max_edge_px=int(
                        getattr(self._vision_config(), "visual_asset_max_edge_px", 1600)
                        or 1600
                    ),
                )
                storage_status = "stored"
            except asyncio.CancelledError:
                ImagePipeline.cleanup(prepared)
                raise
            except Exception as exc:
                storage_status = f"failed:{type(exc).__name__}"
                logger.warning(
                    "[AstrMai-Vision] asset file storage degraded "
                    f"asset={identity.asset_id[:12]} error={type(exc).__name__}"
                )
        ImagePipeline.cleanup(prepared)
        asset_write_status = "skipped_no_db"
        if self.db_service is not None:
            try:
                asset_write_status = await asyncio.to_thread(
                    self._upsert_visual_asset,
                    identity,
                    img_type=payload["type"],
                    description=payload["description"],
                    tags_json_str=ImagePipeline.serialize_tags(payload["emotion_tags"]),
                    model_id=model_id,
                    storage_path=storage_path,
                    initial_recognition_elapsed_ms=elapsed_ms,
                )
            except Exception as exc:
                asset_write_status = f"failed:{type(exc).__name__}"
                logger.warning(
                    "[AstrMai-Vision] asset persistence degraded "
                    f"asset={identity.asset_id[:12]} error={type(exc).__name__}"
                )
        payload.update(
            {
                "_asset_id": identity.asset_id,
                "_cache_hit": False,
                "_cache_kind": "miss",
                "_singleflight_join": False,
                "_model_id": model_id,
                "_prompt_version": identity.prompt_version,
                "_asset_write_status": asset_write_status,
                "_asset_storage_status": storage_status,
                **prepared_metadata,
            }
        )
        logger.info(
            "[AstrMai-Vision] model call completed "
            f"asset={identity.asset_id[:12]} model={model_id or 'unknown'} "
            f"elapsed_ms={elapsed_ms} asset_write={asset_write_status} "
            f"storage={storage_status}"
        )
        await self._maybe_cleanup_asset_files()
        return payload

    async def analyze_image_path(
        self,
        picid: str,
        image_path: str,
        scope_id: str = "global",
        timeout_override: float | None = None,
        binding_context: dict[str, Any] | None = None,
    ) -> dict | None:
        scoped_picid = f"{scope_id}:{picid}"
        identity_started = time.monotonic()
        try:
            identity = await asyncio.to_thread(
                build_visual_asset_identity,
                image_path,
                prompt_version=self._prompt_version(),
                animation_preprocess_version=ImagePipeline.ANIMATION_PREPROCESS_VERSION,
            )
        except Exception as exc:
            logger.warning(
                "[AstrMai-Vision] identity failed "
                f"legacy={hashlib.sha256(scoped_picid.encode('utf-8')).hexdigest()[:12]} "
                f"error={type(exc).__name__}"
            )
            raise
        logger.info(
            "[AstrMai-Vision] identity ready "
            f"asset={identity.asset_id[:12]} bytes={identity.byte_size} "
            f"size={identity.width}x{identity.height} frames={identity.frame_count} "
            f"elapsed_ms={round((time.monotonic() - identity_started) * 1000, 1)}"
        )

        payload = None
        if self.db_service is not None and self._cache_enabled():
            try:
                asset = await asyncio.to_thread(self._get_visual_asset, identity.asset_id)
                payload = self._asset_payload(asset)
            except Exception as exc:
                logger.warning(
                    "[AstrMai-Vision] content cache lookup degraded "
                    f"asset={identity.asset_id[:12]} error={type(exc).__name__}"
                )
            if payload and payload.get("description"):
                for key, value in self._cached_identity_metadata(
                    identity,
                    image_path,
                ).items():
                    payload.setdefault(key, value)
                payload["_cache_hit"] = True
                payload["_cache_kind"] = "content"
                payload["_singleflight_join"] = False
                logger.info(
                    "[AstrMai-Vision] content cache hit "
                    f"asset={identity.asset_id[:12]} prompt_version={identity.prompt_version}"
                )

        if (
            payload is None
            and self.db_service is not None
            and self._cache_enabled()
            and identity.prompt_version == "v1"
        ):
            legacy = await asyncio.to_thread(self._get_cached_memory, scoped_picid)
            payload = self._memory_payload(legacy)
            if payload and payload.get("description"):
                payload.update(
                    {
                        "_asset_id": identity.asset_id,
                        "_cache_hit": True,
                        "_cache_kind": "legacy",
                        "_singleflight_join": False,
                        "_model_id": "",
                        "_prompt_version": identity.prompt_version,
                        "_asset_storage_status": "not_stored",
                        **self._cached_identity_metadata(identity, image_path),
                    }
                )
                try:
                    await asyncio.to_thread(
                        self._upsert_visual_asset,
                        identity,
                        img_type=payload["type"],
                        description=payload["description"],
                        tags_json_str=ImagePipeline.serialize_tags(payload["emotion_tags"]),
                        model_id="",
                        status="ready",
                    )
                except Exception as exc:
                    logger.warning(
                        "[AstrMai-Vision] legacy cache migration degraded "
                        f"asset={identity.asset_id[:12]} error={type(exc).__name__}"
                    )
                logger.info(
                    "[AstrMai-Vision] legacy cache migrated "
                    f"asset={identity.asset_id[:12]}"
                )

        if payload is None:
            cooldown_remaining = self._failure_cooldown_remaining(identity.asset_id)
            if cooldown_remaining > 0:
                logger.info(
                    "[AstrMai-Vision] repeated failed image suppressed "
                    f"asset={identity.asset_id[:12]} retry_after_sec={cooldown_remaining:.1f}"
                )
                raise VisionAnalysisCoolingDown(cooldown_remaining)
            joined = False
            async with self._inflight_lock:
                task = self._inflight.get(identity.asset_id)
                if task is None or task.done():
                    task = asyncio.create_task(
                        self._run_vision_analysis(
                            identity,
                            image_path,
                            scope_id,
                            timeout_override,
                        )
                    )
                    self._inflight[identity.asset_id] = task
                    task.add_done_callback(
                        lambda done, asset_id=identity.asset_id: self._on_inflight_done(
                            asset_id, done
                        )
                    )
                    logger.info(
                        "[AstrMai-Vision] content cache miss "
                        f"asset={identity.asset_id[:12]}"
                    )
                else:
                    joined = True
                    logger.info(
                        "[AstrMai-Vision] joined in-flight analysis "
                        f"asset={identity.asset_id[:12]}"
                    )
            payload = await asyncio.shield(task)
            if payload is None:
                return None
            self._failure_cooldowns.pop(identity.asset_id, None)
            payload = dict(payload)
            if joined:
                payload["_cache_kind"] = "singleflight"
                payload["_singleflight_join"] = True

        tags_json = ImagePipeline.serialize_tags(payload.get("emotion_tags", []))
        legacy_write_status = "skipped_no_db"
        binding_status = "skipped_no_db"
        if self.db_service is not None:
            try:
                legacy_write_status = await asyncio.to_thread(
                    self._upsert_visual_memory,
                    scoped_picid,
                    str(payload.get("type", "image") or "image"),
                    str(payload.get("description", "") or ""),
                    tags_json,
                )
            except Exception as exc:
                legacy_write_status = f"failed:{type(exc).__name__}"
                logger.warning(
                    "[AstrMai-Vision] legacy projection failed "
                    f"asset={identity.asset_id[:12]} error={type(exc).__name__}"
                )
            try:
                binding_status = await asyncio.to_thread(
                    self._upsert_message_binding,
                    asset_id=identity.asset_id,
                    legacy_picid=scoped_picid,
                    binding_context=binding_context,
                )
            except Exception as exc:
                binding_status = f"failed:{type(exc).__name__}"
                logger.warning(
                    "[AstrMai-Vision] message binding failed "
                    f"asset={identity.asset_id[:12]} error={type(exc).__name__}"
                )
        payload["_asset_id"] = identity.asset_id
        payload["_visual_memory_id"] = scoped_picid
        payload["_legacy_write_status"] = legacy_write_status
        payload["_binding_status"] = binding_status
        logger.info(
            "[AstrMai-Vision] persistence finalized "
            f"asset={identity.asset_id[:12]} cache={payload.get('_cache_kind', 'miss')} "
            f"legacy={legacy_write_status} binding={binding_status}"
        )
        return payload

    async def process_image_async(self, picid: str, base64_data: str, scope_id: str = "global"):
        prepared = None
        try:
            prepared = await asyncio.to_thread(ImagePipeline.materialize_image, base64_data)
            if not prepared:
                logger.warning(f"[AstrMai-VisualCortex] failed to prepare image payload: {picid}")
                return
            return await self.analyze_image_path(picid, prepared.file_path, scope_id=scope_id)
        except Exception as exc:
            logger.error(f"[AstrMai-VisualCortex] process image failed for {picid}: {exc}", exc_info=True)
        finally:
            ImagePipeline.cleanup(prepared)


__all__ = ["VisualCortex"]
