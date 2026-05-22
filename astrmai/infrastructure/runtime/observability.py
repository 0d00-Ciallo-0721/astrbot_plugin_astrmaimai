from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

from astrbot.api import logger


GLOBAL_CHAT_ID = "__observability_global__"
DEFAULT_GLOBAL_LIMIT = 2000
DEFAULT_CHAT_LIMIT = 200


@dataclass(slots=True)
class RuntimeObservabilityEvent:
    event_id: str
    timestamp: float
    domain: str
    kind: str
    level: str
    chat_id: str = ""
    title: str = ""
    summary: str = ""
    tags: dict[str, Any] = field(default_factory=dict)
    facets: dict[str, Any] = field(default_factory=dict)
    detail: dict[str, Any] = field(default_factory=dict)
    raw: dict[str, Any] = field(default_factory=dict)


class RuntimeObservabilityHub:
    def __init__(
        self,
        raw_trace_store: Any = None,
        *,
        max_recent_events: int = DEFAULT_GLOBAL_LIMIT,
        max_events_per_chat: int = DEFAULT_CHAT_LIMIT,
    ) -> None:
        self.raw_trace_store = raw_trace_store
        self.max_recent_events = max(1, int(max_recent_events or DEFAULT_GLOBAL_LIMIT))
        self.max_events_per_chat = max(1, int(max_events_per_chat or DEFAULT_CHAT_LIMIT))
        self._lock = asyncio.Lock()
        self._recent_events: list[dict[str, Any]] = []
        self._events_by_chat: dict[str, list[dict[str, Any]]] = {}

    @staticmethod
    def normalize_level(level: str) -> str:
        clean = str(level or "info").strip().lower()
        return clean if clean in {"info", "warning", "error"} else "info"

    @staticmethod
    def _copy_mapping(value: Any) -> dict[str, Any]:
        if isinstance(value, dict):
            return dict(value)
        return {}

    @classmethod
    def _normalize_event(cls, payload: dict[str, Any]) -> RuntimeObservabilityEvent:
        domain = str(payload.get("domain", "") or "").strip().lower()
        kind = str(payload.get("kind", "") or "").strip().lower()
        if domain not in {"scheduler", "heartflow", "cognition", "memory"}:
            raise ValueError(f"invalid domain: {domain}")
        if not kind:
            raise ValueError("kind is required")
        timestamp = float(payload.get("timestamp", 0.0) or 0.0)
        if timestamp <= 0:
            timestamp = time.time()
        return RuntimeObservabilityEvent(
            event_id=str(payload.get("event_id", "") or f"obs_{uuid.uuid4().hex[:12]}"),
            timestamp=timestamp,
            domain=domain,
            kind=kind,
            level=cls.normalize_level(str(payload.get("level", "info") or "info")),
            chat_id=str(payload.get("chat_id", "") or ""),
            title=str(payload.get("title", "") or ""),
            summary=str(payload.get("summary", "") or ""),
            tags=cls._copy_mapping(payload.get("tags")),
            facets=cls._copy_mapping(payload.get("facets")),
            detail=cls._copy_mapping(payload.get("detail")),
            raw=cls._copy_mapping(payload.get("raw")),
        )

    async def record(self, **payload: Any) -> dict[str, Any]:
        event = self._normalize_event(payload)
        data = asdict(event)
        async with self._lock:
            self._recent_events.append(data)
            self._recent_events = self._recent_events[-self.max_recent_events :]
            if event.chat_id:
                chat_items = list(self._events_by_chat.get(event.chat_id, []) or [])
                chat_items.append(data)
                self._events_by_chat[event.chat_id] = chat_items[-self.max_events_per_chat :]
        await self._append_trace_event(data)
        return data

    async def recent(
        self,
        *,
        chat_id: str | None = None,
        domains: set[str] | None = None,
        levels: set[str] | None = None,
        kinds: set[str] | None = None,
        limit: int = 80,
    ) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 80), self.max_recent_events))
        async with self._lock:
            source = (
                list(self._events_by_chat.get(str(chat_id or ""), []) or [])
                if chat_id
                else list(self._recent_events)
            )
        items = list(reversed(source))
        if domains:
            items = [item for item in items if str(item.get("domain", "") or "") in domains]
        if levels:
            items = [item for item in items if self.normalize_level(str(item.get("level", "") or "")) in levels]
        if kinds:
            items = [item for item in items if str(item.get("kind", "") or "") in kinds]
        return items[:safe_limit]

    async def recent_errors(self, *, chat_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 50), self.max_recent_events))
        events = await self.recent(chat_id=chat_id, limit=safe_limit * 2)
        return [item for item in events if self.normalize_level(str(item.get("level", "") or "")) in {"warning", "error"}][:safe_limit]

    async def global_snapshot(self) -> dict[str, Any]:
        async with self._lock:
            items = list(self._recent_events)
            by_chat = dict(self._events_by_chat)
        domain_counts: dict[str, int] = {}
        kind_counts: dict[str, int] = {}
        level_counts = {"info": 0, "warning": 0, "error": 0}
        for item in items:
            domain = str(item.get("domain", "") or "")
            kind = str(item.get("kind", "") or "")
            level = self.normalize_level(str(item.get("level", "") or "info"))
            if domain:
                domain_counts[domain] = int(domain_counts.get(domain, 0) or 0) + 1
            if kind:
                kind_counts[kind] = int(kind_counts.get(kind, 0) or 0) + 1
            level_counts[level] = int(level_counts.get(level, 0) or 0) + 1
        latest_event_at = max((float(item.get("timestamp", 0.0) or 0.0) for item in items), default=0.0)
        return {
            "retained_events": len(items),
            "retained_chats": len(by_chat),
            "domain_counts": domain_counts,
            "kind_counts": kind_counts,
            "level_counts": level_counts,
            "latest_event_at": latest_event_at,
            "recent_error_count": level_counts["error"],
            "recent_warning_count": level_counts["warning"],
        }

    async def chat_snapshot(self, chat_id: str) -> dict[str, Any]:
        normalized_chat_id = str(chat_id or "")
        async with self._lock:
            items = list(self._events_by_chat.get(normalized_chat_id, []) or [])
        domain_counts: dict[str, int] = {}
        kind_counts: dict[str, int] = {}
        latest_event_at = 0.0
        for item in items:
            domain = str(item.get("domain", "") or "")
            kind = str(item.get("kind", "") or "")
            latest_event_at = max(latest_event_at, float(item.get("timestamp", 0.0) or 0.0))
            if domain:
                domain_counts[domain] = int(domain_counts.get(domain, 0) or 0) + 1
            if kind:
                kind_counts[kind] = int(kind_counts.get(kind, 0) or 0) + 1
        return {
            "chat_id": normalized_chat_id,
            "retained_events": len(items),
            "latest_event_at": latest_event_at,
            "domain_counts": domain_counts,
            "kind_counts": kind_counts,
        }

    @staticmethod
    def format_timeline_item(item: dict[str, Any]) -> dict[str, Any]:
        payload = dict(item or {})
        payload["domain"] = str(payload.get("domain", "") or "")
        payload["kind"] = str(payload.get("kind", "") or "")
        payload["level"] = RuntimeObservabilityHub.normalize_level(str(payload.get("level", "info") or "info"))
        payload["display_title"] = str(payload.get("title", "") or payload.get("kind", "") or payload.get("domain", ""))
        payload["display_badge"] = payload["domain"]
        payload["display_group"] = "observability"
        payload["display_kind"] = payload["kind"]
        payload["is_error_like"] = payload["level"] in {"warning", "error"}
        payload["payload_preview"] = str(payload.get("summary", "") or "")[:240]
        return payload

    async def search(
        self,
        *,
        q: str = "",
        chat_id: str = "",
        domains: set[str] | None = None,
        kinds: set[str] | None = None,
        levels: set[str] | None = None,
        tags: set[str] | None = None,
        limit: int = 80,
    ) -> list[dict[str, Any]]:
        items = await self.recent(
            chat_id=chat_id or None,
            domains=domains,
            kinds=kinds,
            levels=levels,
            limit=limit if not q and not tags else self.max_recent_events,
        )
        query = str(q or "").strip().lower()
        tag_filters = {str(item or "").strip().lower() for item in tags or set() if str(item or "").strip()}
        if tag_filters:
            filtered = []
            for item in items:
                values = self._search_values(item)
                joined = " ".join(values)
                if all(tag in joined for tag in tag_filters):
                    filtered.append(item)
            items = filtered
        if query:
            query_tokens = [token for token in query.split() if token]
            filtered = []
            for item in items:
                values = self._search_values(item)
                joined = " ".join(values)
                if all(token in joined for token in query_tokens):
                    filtered.append(item)
            items = filtered
        return items[: max(1, min(int(limit or 80), self.max_recent_events))]

    @classmethod
    def _search_values(cls, item: dict[str, Any]) -> list[str]:
        values = [
            str(item.get("title", "") or "").lower(),
            str(item.get("summary", "") or "").lower(),
        ]
        for bucket_name in ("tags", "facets", "detail", "raw"):
            bucket = item.get(bucket_name, {})
            if isinstance(bucket, dict):
                for key, value in bucket.items():
                    values.append(str(key or "").lower())
                    values.append(str(value or "").lower())
        return values

    async def reset(self) -> None:
        async with self._lock:
            self._recent_events = []
            self._events_by_chat = {}

    async def _append_trace_event(self, event: dict[str, Any]) -> None:
        store = self.raw_trace_store
        if store is None or not hasattr(store, "append"):
            return
        try:
            payload = {
                "chat_id": str(event.get("chat_id", "") or GLOBAL_CHAT_ID),
                "created_at": float(event.get("timestamp", 0.0) or 0.0),
                "trace_id": str(event.get("event_id", "") or ""),
                "stage": f"observability.{event.get('domain', '')}.{event.get('kind', '')}",
                "level": event.get("level", "info"),
                "summary": event.get("summary", ""),
                "payload": {
                    "domain": event.get("domain", ""),
                    "kind": event.get("kind", ""),
                    "title": event.get("title", ""),
                    "tags": dict(event.get("tags", {}) or {}),
                    "facets": dict(event.get("facets", {}) or {}),
                },
                "observability_domain": event.get("domain", ""),
                "observability_kind": event.get("kind", ""),
                "observability_event": event,
            }
            await store.append(payload)
        except Exception as exc:
            logger.debug(f"[RuntimeObservabilityHub] raw trace append degraded: {exc}")


__all__ = [
    "DEFAULT_CHAT_LIMIT",
    "DEFAULT_GLOBAL_LIMIT",
    "GLOBAL_CHAT_ID",
    "RuntimeObservabilityEvent",
    "RuntimeObservabilityHub",
]
