from __future__ import annotations

import asyncio
import hashlib
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from ...multimodal.vision_prompt import normalize_vision_result, render_vision_record


@dataclass
class _PendingPrivateBatch:
    events: list[Any] = field(default_factory=list)
    revision: int = 0
    updated_at: float = field(default_factory=time.time)


class PrivateTurnCoordinator:
    """Private-chat quiet-window plus direct-message pre-decision vision barrier."""

    PENDING_MAX_AGE_SECONDS = 900.0
    PENDING_MAX_EVENTS = 12

    def __init__(self, config: Any, image_resolver: Any, visual_cortex: Any, persistence: Any = None):
        self.config = config
        self.image_resolver = image_resolver
        self.visual_cortex = visual_cortex
        self.persistence = persistence
        self._pending_batches: dict[str, _PendingPrivateBatch] = {}

    def refresh_config(self, config: Any) -> None:
        self.config = config
        refresh_resolver = getattr(self.image_resolver, "refresh_config", None)
        if callable(refresh_resolver):
            refresh_resolver(config)

    def _private_config(self) -> Any:
        return getattr(self.config, "private_chat", None)

    def settle_seconds(self) -> float:
        return max(0.0, float(getattr(self._private_config(), "input_settle_sec", 1.5) or 0.0))

    @staticmethod
    def _event_key(event: Any) -> str:
        message_obj = getattr(event, "message_obj", None)
        message_id = str(
            getattr(message_obj, "message_id", None)
            or getattr(message_obj, "id", None)
            or getattr(event, "message_id", None)
            or ""
        ).strip()
        if message_id:
            return f"id:{message_id}"
        sender_id = ""
        try:
            sender_id = str(event.get_sender_id() or "").strip()
        except Exception:
            pass
        text = str(getattr(event, "message_str", "") or "").strip()
        timestamp = str(getattr(event, "timestamp", "") or "")
        return f"fallback:{sender_id}:{timestamp}:{text}"

    @classmethod
    def _deduplicate_events(cls, events: list[Any]) -> list[Any]:
        merged: list[Any] = []
        seen: set[str] = set()
        for event in events:
            key = cls._event_key(event)
            if key in seen:
                continue
            seen.add(key)
            merged.append(event)
        return merged

    def note_new_message(self, chat_id: str) -> None:
        """Keep an in-flight private batch alive when a newer message arrives."""
        pending = self._pending_batches.get(str(chat_id or ""))
        if pending is not None:
            pending.revision += 1
            pending.updated_at = time.time()

    def merge_pending_batch(self, chat_id: str, events: list[Any]) -> list[Any]:
        """Prepend an unresolved private batch to the next input batch.

        This state is deliberately private-chat-only. It bridges a slow model
        response or a stale reply decision without changing the group window.
        """
        normalized_chat_id = str(chat_id or "")
        pending = self._pending_batches.get(normalized_chat_id)
        if pending is None:
            return list(events)
        if time.time() - pending.updated_at > self.PENDING_MAX_AGE_SECONDS:
            self._pending_batches.pop(normalized_chat_id, None)
            return list(events)

        for event in pending.events:
            if hasattr(event, "set_extra"):
                event.set_extra("astrmai_private_pending_context", True)
        return self._deduplicate_events([*pending.events, *events])[-self.PENDING_MAX_EVENTS :]

    def begin_pending_batch(self, chat_id: str, events: list[Any]) -> int:
        """Record a private batch until its reply completes successfully."""
        normalized_chat_id = str(chat_id or "")
        previous = self._pending_batches.get(normalized_chat_id)
        revision = (previous.revision if previous is not None else 0) + 1
        self._pending_batches[normalized_chat_id] = _PendingPrivateBatch(
            events=self._deduplicate_events(list(events))[-self.PENDING_MAX_EVENTS :],
            revision=revision,
            updated_at=time.time(),
        )
        return revision

    def finish_pending_batch(self, chat_id: str, revision: int, reply_sent: bool) -> None:
        """Clear only the exact batch that produced a successful reply."""
        if not reply_sent:
            return
        normalized_chat_id = str(chat_id or "")
        pending = self._pending_batches.get(normalized_chat_id)
        if pending is not None and pending.revision == int(revision or 0):
            self._pending_batches.pop(normalized_chat_id, None)

    def clear_pending_batch(self, chat_id: str) -> bool:
        return self._pending_batches.pop(str(chat_id or ""), None) is not None

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
            try:
                await self._prepare_event(event, chat_id)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    f"[AstrMai-Vision] unexpected private vision failure for {chat_id}: {exc}",
                    exc_info=True,
                )
                event.set_extra("astrmai_vision_barrier_complete", True)
                event.set_extra("astrmai_vision_barrier_failed", True)
                self._append_failure_context(event, 1)

    def bind_batch_context(self, events: list[Any], focus_event: Any) -> None:
        """Attach all visual facts from a private input burst to its final focus event."""
        image_refs: list[str] = []
        records: list[dict[str, Any]] = []
        seen_records: set[str] = set()
        image_events: list[Any] = []
        failed_count = 0

        for event in events:
            direct_refs = list(
                event.get_extra("direct_image_refs", event.get_extra("direct_vision_urls", []))
                or []
            )
            extracted_refs = list(
                event.get_extra("extracted_image_refs", event.get_extra("extracted_image_urls", []))
                or []
            )
            event_records = list(event.get_extra("astrmai_vision_records", []) or [])
            if direct_refs or extracted_refs or event_records or event.get_extra("astrmai_vision_barrier_complete", False):
                image_events.append(event)
            for ref in [*direct_refs, *extracted_refs]:
                normalized_ref = str(ref or "").strip()
                if normalized_ref and normalized_ref not in image_refs:
                    image_refs.append(normalized_ref)
            for record in event_records:
                copied = dict(record or {})
                record_key = str(
                    copied.get("picid")
                    or copied.get("source_ref")
                    or copied.get("description")
                    or ""
                )
                if not record_key or record_key in seen_records:
                    continue
                seen_records.add(record_key)
                records.append(copied)
            if bool(event.get_extra("astrmai_vision_barrier_failed", False)):
                failed_count += 1

        if not image_events:
            return

        descriptions = [str(record.get("description", "") or "") for record in records]
        focus_event.set_extra("extracted_image_urls", list(image_refs))
        focus_event.set_extra("extracted_image_refs", list(image_refs))
        focus_event.set_extra("direct_vision_urls", list(image_refs))
        focus_event.set_extra("direct_image_refs", list(image_refs))
        focus_event.set_extra("astrmai_vision_records", records)
        focus_event.set_extra("astrmai_visual_context", records)
        focus_event.set_extra("astrmai_image_context", records)
        focus_event.set_extra("astrmai_vision_picids", [record.get("picid") for record in records])
        focus_event.set_extra("astrmai_vision_descriptions", descriptions)
        focus_event.set_extra(
            "astrmai_vision_barrier_complete",
            all(bool(event.get_extra("astrmai_vision_barrier_complete", False)) for event in image_events),
        )
        focus_event.set_extra("astrmai_vision_barrier_failed", bool(failed_count))
        focus_event.set_extra(
            "astrmai_rich_text",
            str(getattr(focus_event, "message_str", "") or "").strip(),
        )
        self._append_vision_context(focus_event, records, failed_count)

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
        records: list[dict[str, Any]] = []
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
            payload, _invalid_reason = normalize_vision_result(result or {})
            description = str((payload or {}).get("description", "") or "").strip()
            if payload is not None and description:
                records.append(
                    {
                        "picid": picid,
                        "source_ref": image.source_ref,
                        "type": str(payload.get("type") or "image"),
                        "description": description,
                        "emotion_tags": list(payload.get("emotion_tags") or []),
                    }
                )
            else:
                failed_count += 1

        descriptions = [str(record.get("description", "") or "") for record in records]
        event.set_extra("astrmai_vision_records", records)
        event.set_extra("astrmai_visual_context", records)
        event.set_extra("astrmai_image_context", records)
        event.set_extra("astrmai_vision_picids", [record.get("picid") for record in records])
        event.set_extra("astrmai_vision_descriptions", descriptions)
        event.set_extra("astrmai_vision_barrier_complete", True)
        event.set_extra("astrmai_vision_barrier_failed", bool(failed_count))
        self._append_vision_context(event, records, failed_count)
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
    def _append_vision_context(event: Any, records: list[dict[str, Any]], failed_count: int) -> None:
        base_text = str(event.get_extra("astrmai_rich_text", getattr(event, "message_str", "")) or "").strip()
        lines = [base_text] if base_text else []
        lines.extend(line for record in records if (line := render_vision_record(record)))
        if failed_count:
            lines.append(f"[有 {failed_count} 张图片读取失败；禁止猜测其内容。]")
        event.set_extra("astrmai_rich_text", "\n".join(lines).strip())

    @classmethod
    def _append_failure_context(cls, event: Any, failed_count: int) -> None:
        event.set_extra("astrmai_vision_records", [])
        event.set_extra("astrmai_visual_context", [])
        event.set_extra("astrmai_image_context", [])
        event.set_extra("astrmai_vision_picids", [])
        event.set_extra("astrmai_vision_descriptions", [])
        cls._append_vision_context(event, [], failed_count)


__all__ = ["PrivateTurnCoordinator"]
