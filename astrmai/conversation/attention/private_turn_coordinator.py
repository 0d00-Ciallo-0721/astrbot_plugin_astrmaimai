from __future__ import annotations

import asyncio
import hashlib
import re
import time
from dataclasses import dataclass, field
from typing import Any

from astrbot.api import logger

from ...infrastructure.runtime.trace_runtime import debug_trace
from ...infrastructure.runtime.turn_call_ledger import record_vision_observation
from ...multimodal.vision_prompt import normalize_vision_result, render_vision_record
from ..vision_state import vision_analysis_observation_facts


@dataclass
class _PendingPrivateBatch:
    events: list[Any] = field(default_factory=list)
    revision: int = 0
    updated_at: float = field(default_factory=time.time)


@dataclass(frozen=True)
class VisionBarrierOutcome:
    policy: str = "timeout_fallback"
    outcome: str = "not_applicable"
    image_count: int = 0
    resolved_count: int = 0
    analyzed_count: int = 0
    failed_count: int = 0
    timeout_count: int = 0
    cache_hit_count: int = 0
    attempt_count: int = 0
    elapsed_ms: int = 0
    downstream_action: str = "continue"

    @property
    def should_abort(self) -> bool:
        return self.downstream_action == "abort_required_vision"


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

    def _timing_config(self) -> Any:
        return getattr(self.config, "timing", None)

    def _vision_config(self) -> Any:
        return getattr(self.config, "vision", None)

    def _vision_policy(self) -> str:
        raw = str(
            getattr(self._vision_config(), "vision_reply_policy", "超时后忽略图片并继续回复")
            or ""
        ).strip()
        if raw in {"必须识别成功后再回复", "require_analysis", "strict"}:
            return "require_analysis"
        return "timeout_fallback"

    def _vision_total_timeout(self) -> float:
        return max(
            0.1,
            float(
                getattr(self._timing_config(), "vision_barrier_total_timeout_sec", 300.0)
                or 300.0
            ),
        )

    def _vision_analysis_timeout(self) -> float:
        return max(
            0.1,
            float(
                getattr(
                    self._timing_config(),
                    "image_analysis_timeout_sec",
                    getattr(self._private_config(), "image_barrier_timeout_sec", 90.0),
                )
                or 90.0
            ),
        )

    def _vision_resolve_timeout(self) -> float:
        return max(
            0.1,
            float(
                getattr(
                    self._timing_config(),
                    "image_resolve_timeout_sec",
                    getattr(self._private_config(), "image_resolve_timeout_sec", 15.0),
                )
                or 15.0
            ),
        )

    def _vision_retries(self) -> int:
        return max(
            1,
            int(
                getattr(
                    self._vision_config(),
                    "image_analysis_retries",
                    getattr(self._private_config(), "image_analysis_retries", 2),
                )
                or 2
            ),
        )

    def settle_seconds(self) -> float:
        return max(0.0, float(getattr(self._private_config(), "input_settle_sec", 1.5) or 0.0))

    def turn_merge_enabled(self) -> bool:
        # OPT-10/PL-03: UI 开关在 timing 分节，优先读之（运行时实证旧代码该开关无效）；
        # 未设置时回退 private_chat 侧默认
        timing = getattr(getattr(self, "config", None), "timing", None)
        timing_value = getattr(timing, "turn_merge_enabled", None)
        if isinstance(timing_value, bool):
            return timing_value
        return bool(getattr(self._private_config(), "turn_merge_enabled", True))

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
        if not self.turn_merge_enabled():
            return
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
        if not self.turn_merge_enabled():
            return list(events)
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
        if not self.turn_merge_enabled():
            return 0
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

    def vision_total_budget_sec(self) -> float:
        """OPT-07/RT-05: 供 gate 合并循环持久化 per-burst 截止时间使用。"""
        return float(self._vision_total_timeout())

    async def prepare_batch(
        self,
        events: list[Any],
        chat_id: str,
        *,
        deadline: float | None = None,
    ) -> VisionBarrierOutcome:
        if not bool(getattr(getattr(self.config, "vision", None), "enable_vision", True)):
            return VisionBarrierOutcome(outcome="disabled")
        if self.image_resolver is None:
            outcomes = [
                self._apply_failed_policy(
                    event,
                    image_count=self._event_image_count(event),
                    failed_count=self._event_image_count(event),
                    timeout_count=0,
                    elapsed_ms=0,
                    outcome="resolver_unavailable",
                )
                for event in events
                if self._event_image_count(event)
            ]
            return self._aggregate_outcomes(outcomes, elapsed_ms=0)
        started_at = time.monotonic()
        # OPT-07/RT-05: 调用方（gate 合并循环）可传入跨迭代持久的 burst deadline，
        # 未传时保持每批独立总额的旧语义
        deadline = float(deadline) if deadline else (started_at + self._vision_total_timeout())
        outcomes: list[VisionBarrierOutcome] = []
        for event in events:
            if bool(event.get_extra("astrmai_vision_barrier_complete", False)):
                continue
            try:
                outcomes.append(await self._prepare_event(event, chat_id, deadline=deadline))
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    f"[AstrMai-Vision] unexpected private vision failure for {chat_id}: {exc}",
                    exc_info=True,
                )
                outcomes.append(
                    self._apply_failed_policy(
                        event,
                        image_count=1,
                        failed_count=1,
                        timeout_count=0,
                        elapsed_ms=int((time.monotonic() - started_at) * 1000),
                        outcome="unexpected_failure",
                    )
                )
        return self._aggregate_outcomes(outcomes, elapsed_ms=int((time.monotonic() - started_at) * 1000))

    def bind_batch_context(self, events: list[Any], focus_event: Any) -> None:
        """Attach all visual facts from a private input burst to its final focus event."""
        if self.turn_merge_enabled():
            self._bind_batch_text_context(events, focus_event)
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
                failed_count += max(
                    1,
                    int(event.get_extra("astrmai_vision_failed_count", 1) or 1),
                )

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
        self._bind_batch_vision_observation(events, focus_event)
        if not str(focus_event.get_extra("astrmai_rich_text", "") or "").strip():
            focus_event.set_extra(
                "astrmai_rich_text",
                str(getattr(focus_event, "message_str", "") or "").strip(),
            )
        self._append_vision_context(focus_event, records, failed_count)

    @staticmethod
    def _bind_batch_vision_observation(events: list[Any], focus_event: Any) -> None:
        observations = [
            dict(event.get_extra("astrmai_vision_observability", {}) or {})
            for event in events
            if isinstance(event.get_extra("astrmai_vision_observability", {}), dict)
        ]
        if not observations:
            return
        sources: list[str] = []
        memory_ids: list[str] = []
        asset_ids: list[str] = []
        model_ids: list[str] = []
        for item in observations:
            for source in list(item.get("image_source", []) or []):
                if source not in sources:
                    sources.append(str(source))
            for memory_id in list(item.get("visual_memory_ids", []) or []):
                if memory_id not in memory_ids:
                    memory_ids.append(str(memory_id))
            for asset_id in list(item.get("asset_ids", []) or []):
                if asset_id not in asset_ids:
                    asset_ids.append(str(asset_id))
            for model_id in list(item.get("model_ids", []) or []):
                if model_id not in model_ids:
                    model_ids.append(str(model_id))
        payload = {
            "policy": str(observations[-1].get("policy", "") or ""),
            "outcome": "failed" if any(item.get("outcome") == "failed" for item in observations) else "success",
            "image_count": sum(int(item.get("image_count", 0) or 0) for item in observations),
            "resolved_count": sum(int(item.get("resolved_count", 0) or 0) for item in observations),
            "analyzed_count": sum(int(item.get("analyzed_count", 0) or 0) for item in observations),
            "failed_count": sum(int(item.get("failed_count", 0) or 0) for item in observations),
            "timeout_count": sum(int(item.get("timeout_count", 0) or 0) for item in observations),
            "attempt_count": sum(int(item.get("attempt_count", 0) or 0) for item in observations),
            "image_source": sources,
            "image_resolve_status": "failed" if any(
                item.get("image_resolve_status") == "failed" for item in observations
            ) else "success",
            "vision_barrier_status": "failed" if any(
                item.get("vision_barrier_status") == "failed" for item in observations
            ) else "completed",
            "vision_wait_ms": sum(int(item.get("vision_wait_ms", 0) or 0) for item in observations),
            "vision_timeout_ms": sum(int(item.get("vision_timeout_ms", 0) or 0) for item in observations),
            "vision_fallback": any(bool(item.get("vision_fallback", False)) for item in observations),
            "visual_memory_id": memory_ids[0] if memory_ids else "",
            "visual_memory_ids": memory_ids,
            "scope": "private",
            "cache_hit_count": sum(
                int(item.get("cache_hit_count", 0) or 0) for item in observations
            ),
            "cache_miss_count": sum(
                int(item.get("cache_miss_count", 0) or 0) for item in observations
            ),
            "singleflight_wait_count": sum(
                int(item.get("singleflight_wait_count", 0) or 0)
                for item in observations
            ),
            "asset_ids": asset_ids,
            "binding_count": sum(
                int(item.get("binding_count", 0) or 0) for item in observations
            ),
            "model_ids": model_ids,
            "analysis_prompt_version": str(
                observations[-1].get("analysis_prompt_version", "") or ""
            ),
            "asset_storage_status": str(
                observations[-1].get("asset_storage_status", "") or ""
            ),
            "final_status": (
                "failed"
                if any(item.get("final_status") == "failed" for item in observations)
                else "success"
            ),
        }
        focus_event.set_extra("astrmai_vision_observability", payload)
        record_vision_observation(focus_event, payload)

    @classmethod
    def _bind_batch_text_context(cls, events: list[Any], focus_event: Any) -> None:
        lines: list[str] = []
        message_ids: list[str] = []
        seen_keys: set[str] = set()
        for event in cls._deduplicate_events(list(events))[-cls.PENDING_MAX_EVENTS :]:
            event_key = cls._event_key(event)
            if event_key in seen_keys:
                continue
            seen_keys.add(event_key)
            text = str(getattr(event, "message_str", "") or "").strip()
            if text:
                lines.append(text)
            if event_key.startswith("id:"):
                message_ids.append(event_key[3:])
        if not lines:
            return
        merged_text = "\n".join(lines).strip()
        focus_event.set_extra("astrmai_rich_text", merged_text)
        focus_event.set_extra("astrmai_private_batch_text", merged_text)
        focus_event.set_extra("astrmai_private_batch_message_ids", message_ids)
        focus_event.set_extra("astrmai_private_batch_message_count", len(seen_keys))
        focus_event.set_extra("astrmai_private_batch_merged", len(seen_keys) > 1)

    async def prepare_direct_event(self, event: Any, chat_id: str) -> VisionBarrierOutcome:
        """Resolve a directly addressed image before group decision/dispatch."""
        event.set_extra("astrmai_vision_image_sources", self._image_source_categories(event))
        if self._event_image_count(event):
            event.set_extra("astrmai_vision_owner", "barrier")
        if not bool(getattr(getattr(self.config, "vision", None), "enable_vision", True)):
            outcome = VisionBarrierOutcome(
                policy=self._vision_policy(),
                outcome="disabled",
            )
            self._record_outcome(event, outcome, chat_id)
            return outcome
        if self.image_resolver is None:
            image_count = self._event_image_count(event)
            if image_count:
                return self._apply_failed_policy(
                    event,
                    image_count=image_count,
                    failed_count=image_count,
                    timeout_count=0,
                    elapsed_ms=0,
                    outcome="resolver_unavailable",
                )
            outcome = VisionBarrierOutcome(
                policy=self._vision_policy(),
                outcome="resolver_unavailable",
            )
            self._record_outcome(event, outcome, chat_id)
            return outcome
        if bool(event.get_extra("astrmai_vision_barrier_complete", False)):
            return VisionBarrierOutcome(outcome="already_complete")
        started_at = time.monotonic()
        outcome = await self._prepare_event(
            event,
            chat_id,
            deadline=started_at + self._vision_total_timeout(),
        )
        return outcome

    async def _prepare_event(
        self,
        event: Any,
        chat_id: str,
        *,
        deadline: float | None = None,
    ) -> VisionBarrierOutcome:
        started_at = time.monotonic()
        deadline = deadline or (started_at + self._vision_total_timeout())
        resolve_timeout = min(self._vision_resolve_timeout(), self._remaining_seconds(deadline))
        analysis_timeout = self._vision_analysis_timeout()
        retries = self._vision_retries()
        event.set_extra("astrmai_vision_image_sources", self._image_source_categories(event))
        try:
            resolution = await asyncio.wait_for(
                self.image_resolver.resolve_event_images(event),
                timeout=resolve_timeout,
            )
        except asyncio.TimeoutError:
            return self._apply_failed_policy(
                event,
                image_count=1,
                failed_count=1,
                timeout_count=1,
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
                outcome="resolve_timeout",
            )
        except Exception as exc:
            logger.warning(f"[AstrMai-Vision] image resolution failed for {chat_id}: {exc}")
            return self._apply_failed_policy(
                event,
                image_count=1,
                failed_count=1,
                timeout_count=0,
                elapsed_ms=int((time.monotonic() - started_at) * 1000),
                outcome="resolve_failed",
            )
        if not resolution.had_images:
            outcome = VisionBarrierOutcome(
                policy=self._vision_policy(),
                outcome="no_images",
            )
            self._record_outcome(event, outcome, chat_id)
            return outcome

        local_paths = [item.local_path for item in resolution.images]
        picids = [self._build_picid(event, item.index, item.source_ref) for item in resolution.images]
        await self._persist_image_metadata(event, chat_id, picids)
        event.set_extra("extracted_image_urls", local_paths)
        event.set_extra("extracted_image_refs", local_paths)
        event.set_extra("direct_vision_urls", local_paths)
        event.set_extra("direct_image_refs", local_paths)
        records: list[dict[str, Any]] = []
        failed_count = len(resolution.failures)
        timeout_count = 0
        cache_hit_count = 0
        attempt_count = 0
        for image in resolution.images:
            result = None
            image_timed_out = False
            picid = self._build_picid(event, image.index, image.source_ref)
            for attempt in range(retries):
                remaining = self._remaining_seconds(deadline)
                if remaining <= 0:
                    image_timed_out = True
                    break
                try:
                    if self.visual_cortex is None or not hasattr(self.visual_cortex, "analyze_image_path"):
                        break
                    call_timeout = min(analysis_timeout, remaining)
                    attempt_count += 1
                    result = await self._analyze_with_timeout(
                        picid,
                        image.local_path,
                        chat_id,
                        call_timeout,
                        event=event,
                        image_index=image.index,
                        source_ref=image.source_ref,
                    )
                    if result and str(result.get("description", "") or "").strip():
                        break
                except asyncio.TimeoutError:
                    image_timed_out = True
                    if self._remaining_seconds(deadline) <= 0:
                        break
                except Exception as exc:
                    if attempt + 1 >= retries:
                        logger.warning(f"[AstrMai-Vision] vision barrier failed for {chat_id}: {exc}")
                if attempt + 1 < retries:
                    remaining = self._remaining_seconds(deadline)
                    if remaining <= 0:
                        image_timed_out = True
                        break
                    await asyncio.sleep(min(0.5 * (attempt + 1), 1.0, remaining))
            payload, _invalid_reason = normalize_vision_result(result or {})
            description = str((payload or {}).get("description", "") or "").strip()
            if payload is not None and description:
                if bool((result or {}).get("_cache_hit", False)):
                    cache_hit_count += 1
                records.append(
                    {
                        "picid": picid,
                        "source_ref": image.source_ref,
                        "type": str(payload.get("type") or "image"),
                        "description": description,
                        "emotion_tags": list(payload.get("emotion_tags") or []),
                        "asset_id": str((result or {}).get("_asset_id", "") or ""),
                        "visual_memory_id": str(
                            (result or {}).get("_visual_memory_id", "") or ""
                        ),
                        "cache_kind": str((result or {}).get("_cache_kind", "miss") or "miss"),
                        "singleflight_join": bool(
                            (result or {}).get("_singleflight_join", False)
                        ),
                        "binding_status": str(
                            (result or {}).get("_binding_status", "") or ""
                        ),
                        "legacy_write_status": str(
                            (result or {}).get("_legacy_write_status", "") or ""
                        ),
                        "asset_storage_status": str(
                            (result or {}).get("_asset_storage_status", "") or ""
                        ),
                        "model_id": str((result or {}).get("_model_id", "") or ""),
                        "prompt_version": str(
                            (result or {}).get("_prompt_version", "") or ""
                        ),
                        **vision_analysis_observation_facts(result or {}),
                    }
                )
            else:
                failed_count += 1
                if image_timed_out:
                    timeout_count += 1

        descriptions = [str(record.get("description", "") or "") for record in records]
        event.set_extra("astrmai_vision_records", records)
        event.set_extra("astrmai_visual_context", records)
        event.set_extra("astrmai_image_context", records)
        event.set_extra("astrmai_vision_picids", [record.get("picid") for record in records])
        event.set_extra("astrmai_vision_descriptions", descriptions)
        event.set_extra("astrmai_vision_barrier_complete", True)
        event.set_extra("astrmai_vision_barrier_failed", bool(failed_count))
        event.set_extra("astrmai_vision_failed_count", failed_count)
        elapsed_ms = int((time.monotonic() - started_at) * 1000)
        if failed_count:
            return self._apply_failed_policy(
                event,
                image_count=len(resolution.images) + len(resolution.failures),
                resolved_count=len(resolution.images),
                analyzed_count=len(records),
                failed_count=failed_count,
                timeout_count=timeout_count,
                cache_hit_count=cache_hit_count,
                attempt_count=attempt_count,
                elapsed_ms=elapsed_ms,
                outcome="partial_failure" if records else "analysis_failed",
                records=records,
            )
        self._append_vision_context(event, records, 0)
        await self._mark_vision_executed(event, chat_id)
        outcome = VisionBarrierOutcome(
            policy=self._vision_policy(),
            outcome="success",
            image_count=len(resolution.images),
            resolved_count=len(resolution.images),
            analyzed_count=len(records),
            cache_hit_count=cache_hit_count,
            attempt_count=attempt_count,
            elapsed_ms=elapsed_ms,
        )
        self._record_outcome(event, outcome, chat_id)
        return outcome

    @staticmethod
    def _remaining_seconds(deadline: float) -> float:
        return max(0.0, deadline - time.monotonic())

    async def _analyze_with_timeout(
        self,
        picid: str,
        image_path: str,
        chat_id: str,
        timeout_sec: float,
        *,
        event: Any | None = None,
        image_index: int = 0,
        source_ref: str = "",
    ) -> dict | None:
        analyze = self.visual_cortex.analyze_image_path
        message_obj = getattr(event, "message_obj", None)
        message_id = str(
            getattr(message_obj, "message_id", "")
            or getattr(message_obj, "id", "")
            or getattr(event, "message_id", "")
            or ""
        )
        sender_id = ""
        if event is not None and hasattr(event, "get_sender_id"):
            try:
                sender_id = str(event.get_sender_id() or "")
            except Exception:
                sender_id = ""
        binding_context = {
            "chat_id": chat_id,
            "message_id": message_id,
            "sender_id": sender_id,
            "image_index": image_index,
            "source_ref": source_ref,
        }
        try:
            coroutine = analyze(
                picid,
                image_path,
                scope_id=chat_id,
                timeout_override=timeout_sec,
                binding_context=binding_context,
            )
        except TypeError:
            try:
                coroutine = analyze(
                    picid,
                    image_path,
                    scope_id=chat_id,
                    timeout_override=timeout_sec,
                )
            except TypeError:
                coroutine = analyze(picid, image_path, scope_id=chat_id)
        return await asyncio.wait_for(coroutine, timeout=timeout_sec)

    def _apply_failed_policy(
        self,
        event: Any,
        *,
        image_count: int,
        failed_count: int,
        timeout_count: int,
        elapsed_ms: int,
        outcome: str,
        resolved_count: int = 0,
        analyzed_count: int = 0,
        cache_hit_count: int = 0,
        attempt_count: int = 0,
        records: list[dict[str, Any]] | None = None,
    ) -> VisionBarrierOutcome:
        policy = self._vision_policy()
        records = list(records or [])
        event.set_extra("astrmai_vision_barrier_complete", True)
        event.set_extra("astrmai_vision_barrier_failed", True)
        event.set_extra("astrmai_vision_failed_count", failed_count)
        event.set_extra("astrmai_vision_unavailable", True)
        event.set_extra("astrmai_vision_no_guess", True)
        if policy == "require_analysis":
            event.set_extra("astrmai_vision_required_failed", True)
            downstream_action = "abort_required_vision"
        else:
            self._clear_raw_image_refs(event)
            self._append_vision_context(event, records, failed_count)
            if not records:
                media_only_failure = self._is_media_only_event(event)
                event.set_extra("astrmai_media_status_nonsemantic", True)
                event.set_extra("astrmai_media_only_failure", media_only_failure)
                event.set_extra(
                    "astrmai_media_status",
                    {
                        "status": "unavailable",
                        "owner": "barrier",
                        "reason": str(outcome or "analysis_failed"),
                        "image_count": max(image_count, failed_count),
                        "image_only": media_only_failure,
                    },
                )
            downstream_action = "continue_with_placeholder"
        barrier_outcome = VisionBarrierOutcome(
            policy=policy,
            outcome=outcome,
            image_count=max(image_count, failed_count),
            resolved_count=resolved_count,
            analyzed_count=analyzed_count,
            failed_count=failed_count,
            timeout_count=timeout_count,
            cache_hit_count=cache_hit_count,
            attempt_count=attempt_count,
            elapsed_ms=elapsed_ms,
            downstream_action=downstream_action,
        )
        self._record_outcome(event, barrier_outcome, "")
        return barrier_outcome

    @staticmethod
    def _is_media_only_event(event: Any) -> bool:
        text = str(getattr(event, "message_str", "") or "").strip()
        text = re.sub(r"obj_len_\d+", "", text, flags=re.IGNORECASE)
        text = re.sub(
            r"\[(?:图片|图像|表情包?|image)(?:[^\]]*)\]",
            "",
            text,
            flags=re.IGNORECASE,
        )
        return not text.strip()

    @staticmethod
    def _clear_raw_image_refs(event: Any) -> None:
        for key in (
            "direct_vision_urls",
            "direct_image_refs",
            "extracted_image_urls",
            "extracted_image_refs",
        ):
            event.set_extra(key, [])

    @staticmethod
    def _event_image_count(event: Any) -> int:
        refs: list[str] = []
        for key in (
            "direct_vision_urls",
            "direct_image_refs",
            "extracted_image_urls",
            "extracted_image_refs",
        ):
            for ref in list(event.get_extra(key, []) or []):
                normalized = str(ref or "").strip()
                if normalized and normalized not in refs:
                    refs.append(normalized)
        return len(refs)

    @staticmethod
    def _image_source_categories(event: Any) -> list[str]:
        categories: list[str] = []
        for key in (
            "direct_vision_urls",
            "direct_image_refs",
            "extracted_image_urls",
            "extracted_image_refs",
        ):
            for ref in list(event.get_extra(key, []) or []):
                value = str(ref or "").strip().lower()
                if not value:
                    continue
                if value.startswith(("http://", "https://")):
                    category = "url"
                elif value.startswith(("file://", "/", "\\")) or ":\\" in value:
                    category = "local_file"
                elif value.startswith("data:"):
                    category = "data_uri"
                else:
                    category = "opaque_ref"
                if category not in categories:
                    categories.append(category)
        return categories or ["unknown"]

    def _aggregate_outcomes(
        self,
        outcomes: list[VisionBarrierOutcome],
        *,
        elapsed_ms: int,
    ) -> VisionBarrierOutcome:
        outcomes = [outcome for outcome in outcomes if outcome is not None]
        if not outcomes:
            return VisionBarrierOutcome(policy=self._vision_policy(), elapsed_ms=elapsed_ms)
        should_abort = any(outcome.should_abort for outcome in outcomes)
        failed_count = sum(outcome.failed_count for outcome in outcomes)
        if should_abort:
            downstream_action = "abort_required_vision"
        elif failed_count:
            downstream_action = "continue_with_placeholder"
        else:
            downstream_action = "continue"
        return VisionBarrierOutcome(
            policy=self._vision_policy(),
            outcome="failed" if failed_count else "success",
            image_count=sum(outcome.image_count for outcome in outcomes),
            resolved_count=sum(outcome.resolved_count for outcome in outcomes),
            analyzed_count=sum(outcome.analyzed_count for outcome in outcomes),
            failed_count=failed_count,
            timeout_count=sum(outcome.timeout_count for outcome in outcomes),
            cache_hit_count=sum(outcome.cache_hit_count for outcome in outcomes),
            attempt_count=sum(outcome.attempt_count for outcome in outcomes),
            elapsed_ms=elapsed_ms,
            downstream_action=downstream_action,
        )

    @staticmethod
    def _record_outcome(event: Any, outcome: VisionBarrierOutcome, chat_id: str) -> None:
        effective_chat_id = str(
            chat_id
            or getattr(event, "unified_msg_origin", "")
            or getattr(event, "session_id", "")
            or ""
        )
        payload = {
            "policy": outcome.policy,
            "outcome": outcome.outcome,
            "image_count": outcome.image_count,
            "resolved_count": outcome.resolved_count,
            "analyzed_count": outcome.analyzed_count,
            "failed_count": outcome.failed_count,
            "timeout_count": outcome.timeout_count,
            "cache_hit_count": outcome.cache_hit_count,
            "attempt_count": outcome.attempt_count,
            "elapsed_ms": outcome.elapsed_ms,
            "downstream_action": outcome.downstream_action,
            "scope": "private" if "FriendMessage" in effective_chat_id else "group",
        }
        source_categories = list(event.get_extra("astrmai_vision_image_sources", []) or [])
        records = list(event.get_extra("astrmai_vision_records", []) or [])
        memory_ids = [
            str(record.get("visual_memory_id", "") or record.get("picid", "") or "")[:80]
            for record in records
            if str(record.get("visual_memory_id", "") or record.get("picid", "") or "").strip()
        ]
        asset_ids = [
            str(record.get("asset_id", "") or "")[:80]
            for record in records
            if str(record.get("asset_id", "") or "").strip()
        ]
        model_ids = list(
            dict.fromkeys(
                str(record.get("model_id", "") or "")[:80]
                for record in records
                if str(record.get("model_id", "") or "").strip()
            )
        )
        prompt_versions = list(
            dict.fromkeys(
                str(record.get("prompt_version", "") or "")[:64]
                for record in records
                if str(record.get("prompt_version", "") or "").strip()
            )
        )
        cache_miss_count = sum(
            1 for record in records if str(record.get("cache_kind", "miss")) == "miss"
        )
        singleflight_wait_count = sum(
            1 for record in records if bool(record.get("singleflight_join", False))
        )
        binding_count = sum(
            1 for record in records if str(record.get("binding_status", "")) == "persisted"
        )
        storage_statuses = list(
            dict.fromkeys(
                str(record.get("asset_storage_status", "") or "")[:80]
                for record in records
                if str(record.get("asset_storage_status", "") or "").strip()
            )
        )
        analysis_facts = vision_analysis_observation_facts(records[-1]) if records else {}
        if outcome.outcome in {"success", "partial_failure"}:
            resolve_status = "success"
        elif outcome.outcome in {"resolve_failed", "resolver_unavailable", "resolve_timeout"}:
            resolve_status = "timeout" if outcome.outcome == "resolve_timeout" else "failed"
        else:
            resolve_status = "unknown"
        skipped = outcome.outcome in {
            "disabled",
            "no_images",
            "already_complete",
            "not_applicable",
        }
        observation = {
            **payload,
            **analysis_facts,
            "image_source": source_categories,
            "image_resolve_status": resolve_status,
            "vision_barrier_status": (
                "skipped" if skipped else "failed" if outcome.failed_count else "completed"
            ),
            "vision_wait_ms": outcome.elapsed_ms,
            "vision_timeout_ms": outcome.elapsed_ms if outcome.timeout_count else 0,
            "vision_fallback": outcome.downstream_action == "continue_with_placeholder",
            "visual_memory_id": memory_ids[0] if memory_ids else "",
            "visual_memory_ids": memory_ids,
            "vision_path": "barrier",
            "vision_call_status": (
                "skipped" if skipped else "failed" if outcome.failed_count else "success"
            ),
            "visual_memory_write_status": (
                "persisted_or_cache_hit"
                if memory_ids
                else "not_available"
            ),
            "prompt_injected": (
                False
                if skipped
                else bool(records) or outcome.downstream_action == "continue_with_placeholder"
            ),
            "fallback_reason": outcome.outcome if outcome.failed_count else "",
            "cache_hit_count": outcome.cache_hit_count,
            "cache_miss_count": cache_miss_count,
            "singleflight_wait_count": singleflight_wait_count,
            "asset_ids": asset_ids,
            "binding_count": binding_count,
            "failure_stage": (
                "resolve"
                if outcome.outcome in {"resolve_failed", "resolver_unavailable", "resolve_timeout"}
                else "analysis" if outcome.failed_count else ""
            ),
            "skip_reason": outcome.outcome if skipped else "",
            "model_ids": model_ids,
            "analysis_prompt_version": prompt_versions[0] if prompt_versions else "",
            "asset_storage_status": ",".join(storage_statuses),
            "final_status": outcome.outcome,
        }
        event.set_extra("astrmai_vision_barrier_outcome", payload)
        event.set_extra("astrmai_vision_observability", observation)
        record_vision_observation(event, observation)
        debug_trace(event, "vision.barrier", chat_id=effective_chat_id, **payload)

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
            if "[图片]" not in base_text:
                lines.append("[图片]")
        event.set_extra("astrmai_rich_text", "\n".join(lines).strip())

    @classmethod
    def _append_failure_context(cls, event: Any, failed_count: int) -> None:
        event.set_extra("astrmai_vision_records", [])
        event.set_extra("astrmai_visual_context", [])
        event.set_extra("astrmai_image_context", [])
        event.set_extra("astrmai_vision_picids", [])
        event.set_extra("astrmai_vision_descriptions", [])
        cls._append_vision_context(event, [], failed_count)


__all__ = ["PrivateTurnCoordinator", "VisionBarrierOutcome"]
