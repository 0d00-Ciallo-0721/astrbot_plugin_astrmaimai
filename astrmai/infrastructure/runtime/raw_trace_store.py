from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from collections import deque
from pathlib import Path
from typing import Any

from astrbot.api import logger


class RawTraceEventStore:
    _SENSITIVE_KEYS = frozenset(
        {"authorization", "cookie", "api_key", "apikey", "token", "access_token", "secret", "password"}
    )
    _MAX_STRING_CHARS = 4096
    _MAX_DEPTH = 6
    _MAX_MAPPING_KEYS = 64
    _MAX_SEQUENCE_ITEMS = 100

    def __init__(
        self,
        base_dir: Path,
        *,
        max_per_chat: int = 200,
        max_global: int = 10000,
        filename: str = "raw_trace_events.json",
        queue_capacity: int = 2048,
        max_queue_bytes: int = 8 * 1024 * 1024,
        max_event_bytes: int = 32 * 1024,
        compact_size_bytes: int = 64 * 1024 * 1024,
        owner_registry: Any = None,
        generation: int = 0,
    ):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / filename
        self.jsonl_path = self.base_dir / f"{self.path.stem}.jsonl"
        self.max_per_chat = max(1, int(max_per_chat or 200))
        self.max_global = max(self.max_per_chat, int(max_global or 10000))
        self.queue_capacity = max(1, int(queue_capacity or 2048))
        self.max_queue_bytes = max(1024, int(max_queue_bytes or 8 * 1024 * 1024))
        self.max_event_bytes = max(1024, int(max_event_bytes or 32 * 1024))
        self.compact_size_bytes = max(self.max_event_bytes * 4, int(compact_size_bytes or 64 * 1024 * 1024))
        self.owner_registry = owner_registry
        self.generation = int(generation or 0)
        self._queue: deque[tuple[dict[str, Any], int, float]] = deque()
        self._queue_bytes = 0
        self._queue_lock = asyncio.Lock()
        self._wake = asyncio.Event()
        self._writer_task: asyncio.Task[Any] | None = None
        self._physical_write_inflight = False
        self._accepting = True
        self._recent_memory: deque[dict[str, Any]] = deque(maxlen=self.max_global)
        self._seen_event_ids: deque[str] = deque(maxlen=max(self.max_global * 2, 256))
        self._seen_event_id_set: set[str] = set()
        self._stats = {
            "appended_total": 0,
            "appended_batches": 0,
            "duplicate_dropped_total": 0,
            "queue_dropped_total": 0,
            "payload_truncated_total": 0,
            "write_failure_total": 0,
            "compaction_total": 0,
            "compaction_failure_total": 0,
            "last_write_latency_ms": 0.0,
            "last_compaction_latency_ms": 0.0,
        }
        self._last_error = ""
        self._line_count = 0

    def _read_sync(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 1, "by_chat": {}}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 1, "by_chat": {}}
        if not isinstance(payload, dict):
            return {"version": 1, "by_chat": {}}
        by_chat = payload.get("by_chat", {})
        if not isinstance(by_chat, dict):
            by_chat = {}
        return {"version": 1, "by_chat": by_chat}

    def _write_sync(self, payload: dict[str, Any]) -> None:
        """Legacy compatibility hook; active writes never call this method."""
        normalized = {"version": 1, "by_chat": dict(payload.get("by_chat", {}) or {})}
        self.path.write_text(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    @classmethod
    def _bounded_value(cls, value: Any, *, key: str = "", depth: int = 0) -> Any:
        if str(key or "").strip().lower() in cls._SENSITIVE_KEYS:
            return "[REDACTED]"
        if depth >= cls._MAX_DEPTH:
            return "[MAX_DEPTH]"
        if value is None or isinstance(value, (bool, int, float)):
            return value
        if isinstance(value, bytes):
            return f"[BINARY_OMITTED:{len(value)}]"
        if isinstance(value, str):
            lowered = value[:64].lower()
            if lowered.startswith("data:image/") or lowered.startswith("data:application/octet-stream"):
                return f"[BINARY_OMITTED:{len(value)}]"
            return value[: cls._MAX_STRING_CHARS]
        if isinstance(value, dict):
            return {
                str(item_key)[:120]: cls._bounded_value(item, key=str(item_key), depth=depth + 1)
                for item_key, item in list(value.items())[: cls._MAX_MAPPING_KEYS]
            }
        if isinstance(value, (list, tuple, set, frozenset)):
            return [
                cls._bounded_value(item, depth=depth + 1)
                for item in list(value)[: cls._MAX_SEQUENCE_ITEMS]
            ]
        return str(value)[: cls._MAX_STRING_CHARS]

    def _normalize_event(self, event: dict[str, Any], chat_id: str | None = None) -> dict[str, Any] | None:
        copied = self._bounded_value(dict(event or {}))
        if not isinstance(copied, dict):
            return None
        normalized_chat_id = str(chat_id if chat_id is not None else copied.get("chat_id", "") or "")
        if not normalized_chat_id:
            return None
        copied["chat_id"] = normalized_chat_id
        copied["schema_version"] = 2
        copied["event_id"] = str(
            copied.get("event_id", "") or copied.get("trace_id", "") or f"raw_{uuid.uuid4().hex[:16]}"
        )
        try:
            copied["created_at"] = float(copied.get("created_at", time.time()) or time.time())
        except (TypeError, ValueError):
            copied["created_at"] = time.time()
        serialized = json.dumps(copied, ensure_ascii=False, separators=(",", ":"), default=str)
        serialized_bytes = len(serialized.encode("utf-8"))
        if serialized_bytes > self.max_event_bytes:
            copied = {
                key: copied[key]
                for key in (
                    "schema_version",
                    "event_id",
                    "chat_id",
                    "trace_id",
                    "turn_id",
                    "stage",
                    "level",
                    "created_at",
                    "reason",
                    "summary",
                )
                if key in copied
            }
            copied["payload"] = {
                "payload_truncated": True,
                "original_serialized_bytes": serialized_bytes,
            }
            copied["payload_truncated"] = True
            for key in ("summary", "reason", "stage"):
                if key in copied:
                    copied[key] = str(copied[key] or "")[:512]
            self._stats["payload_truncated_total"] += 1
        return copied

    def _remember_event_id(self, event_id: str) -> None:
        if event_id in self._seen_event_id_set:
            return
        if len(self._seen_event_ids) >= self._seen_event_ids.maxlen:
            expired = self._seen_event_ids.popleft()
            self._seen_event_id_set.discard(expired)
        self._seen_event_ids.append(event_id)
        self._seen_event_id_set.add(event_id)

    async def _enqueue(self, events: list[dict[str, Any]]) -> None:
        if not events or not self._accepting:
            return
        async with self._queue_lock:
            for event in events:
                event_id = str(event.get("event_id", "") or "")
                if event_id and event_id in self._seen_event_id_set:
                    self._stats["duplicate_dropped_total"] += 1
                    continue
                line_size = len(json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")) + 1
                if line_size > self.max_queue_bytes:
                    self._stats["queue_dropped_total"] += 1
                    continue
                while self._queue and (
                    len(self._queue) >= self.queue_capacity
                    or self._queue_bytes + line_size > self.max_queue_bytes
                ):
                    _, dropped_size, _ = self._queue.popleft()
                    self._queue_bytes -= dropped_size
                    self._stats["queue_dropped_total"] += 1
                self._queue.append((event, line_size, time.monotonic()))
                self._queue_bytes += line_size
                self._recent_memory.append(event)
                if event_id:
                    self._remember_event_id(event_id)
            if self._queue:
                self._wake.set()
        self._ensure_writer()

    def _ensure_writer(self) -> None:
        if self._writer_task is not None and not self._writer_task.done():
            return
        coroutine = None
        try:
            coroutine = self._writer_loop()
            registry = self.owner_registry
            if registry is not None and hasattr(registry, "track"):
                self._writer_task = registry.track(
                    coroutine,
                    task_family="observability.raw_trace_writer",
                    scope_id="GLOBAL",
                    run_id=f"raw-trace-{self.generation}",
                    owner="RawTraceEventStore",
                    generation=self.generation,
                    name="astrmai-raw-trace-writer",
                )
            else:
                self._writer_task = asyncio.create_task(coroutine, name="astrmai-raw-trace-writer")
            self._writer_task._astrmai_raw_trace_writer = True
        except Exception as exc:
            close = getattr(coroutine, "close", None)
            if callable(close):
                try:
                    close()
                except RuntimeError:
                    pass
            self._writer_task = None
            self._stats["write_failure_total"] += 1
            self._last_error = f"writer_start_failed:{type(exc).__name__}: {exc}"[:500]

    def _append_lines_sync(self, lines: list[str]) -> None:
        if not lines:
            return
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write("".join(lines))
            handle.flush()
        self._line_count += len(lines)
        try:
            should_compact = (
                self._line_count > self.max_global * 2
                or self.jsonl_path.stat().st_size > self.compact_size_bytes
            )
        except OSError:
            should_compact = False
        if should_compact:
            self._compact_sync()

    def _compact_sync(self) -> None:
        started = time.perf_counter()
        tmp_path = self.jsonl_path.with_suffix(self.jsonl_path.suffix + ".compact.tmp")
        try:
            samples = self._read_jsonl_sync(None)
            by_id: dict[str, dict[str, Any]] = {}
            anonymous: list[dict[str, Any]] = []
            for item in samples:
                event_id = str(item.get("event_id", "") or "")
                if event_id:
                    by_id[event_id] = item
                else:
                    anonymous.append(item)
            ordered = [*anonymous, *by_id.values()]
            ordered.sort(key=lambda item: float(item.get("created_at", 0.0) or 0.0))
            per_chat: dict[str, list[dict[str, Any]]] = {}
            for item in ordered:
                per_chat.setdefault(str(item.get("chat_id", "") or ""), []).append(item)
            keep_ids: set[int] = set()
            for items in per_chat.values():
                keep_ids.update(id(item) for item in items[-self.max_per_chat :])
            kept = [item for item in ordered if id(item) in keep_ids][-self.max_global :]
            with tmp_path.open("w", encoding="utf-8") as handle:
                for item in kept:
                    handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str) + "\n")
                handle.flush()
            os.replace(tmp_path, self.jsonl_path)
            self._line_count = len(kept)
            self._stats["compaction_total"] += 1
            self._stats["last_compaction_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        except Exception as exc:
            self._stats["compaction_failure_total"] += 1
            self._last_error = f"{type(exc).__name__}: {exc}"[:500]
            if self._stats["compaction_failure_total"] == 1:
                logger.warning("[RawTrace] compaction degraded: %s", self._last_error)
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except OSError:
                pass

    async def _writer_loop(self) -> None:
        try:
            while True:
                await self._wake.wait()
                while True:
                    async with self._queue_lock:
                        if not self._queue:
                            self._wake.clear()
                            should_stop = not self._accepting
                            batch: list[tuple[dict[str, Any], int, float]] = []
                        else:
                            batch = []
                            batch_bytes = 0
                            while self._queue and len(batch) < 128:
                                item, size, queued_at = self._queue[0]
                                if batch and batch_bytes + size > self.max_queue_bytes:
                                    break
                                self._queue.popleft()
                                self._queue_bytes -= size
                                batch.append((item, size, queued_at))
                                batch_bytes += size
                            self._physical_write_inflight = True
                            should_stop = False
                    if batch:
                        started = time.perf_counter()
                        try:
                            lines = [
                                json.dumps(item, ensure_ascii=False, separators=(",", ":"), default=str) + "\n"
                                for item, _, _ in batch
                            ]
                            await asyncio.to_thread(self._append_lines_sync, lines)
                            self._stats["appended_total"] += len(batch)
                            self._stats["appended_batches"] += 1
                            self._stats["last_write_latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
                        except Exception as exc:
                            self._stats["write_failure_total"] += 1
                            self._last_error = f"{type(exc).__name__}: {exc}"[:500]
                            if self._stats["write_failure_total"] == 1:
                                logger.warning("[RawTrace] append degraded: %s", self._last_error)
                            await asyncio.sleep(0.1)
                        finally:
                            self._physical_write_inflight = False
                        continue
                    if should_stop:
                        return
                    break
        except asyncio.CancelledError:
            raise

    async def append(self, event: dict[str, Any]) -> None:
        normalized = self._normalize_event(event)
        if normalized is not None:
            await self._enqueue([normalized])

    async def append_many(self, chat_id: str, events: list[dict[str, Any]]) -> None:
        normalized_chat_id = str(chat_id or "")
        if not normalized_chat_id or not events:
            return
        normalized: list[dict[str, Any]] = []
        for index, event in enumerate(events):
            copied = dict(event or {})
            if not str(copied.get("event_id", "") or ""):
                trace_id = str(copied.get("trace_id", "") or "")
                stage = str(copied.get("stage", "") or "")
                if trace_id:
                    copied["event_id"] = f"{trace_id}:{index}:{stage}"
            item = self._normalize_event(copied, normalized_chat_id)
            if item is not None:
                normalized.append(item)
        await self._enqueue(normalized)

    def _read_jsonl_sync(self, max_lines: int | None) -> list[dict[str, Any]]:
        if not self.jsonl_path.exists():
            return []
        if max_lines is None:
            with self.jsonl_path.open("r", encoding="utf-8") as handle:
                lines = list(handle)
        else:
            wanted = max(1, int(max_lines))
            block_size = 64 * 1024
            with self.jsonl_path.open("rb") as handle:
                handle.seek(0, os.SEEK_END)
                position = handle.tell()
                buffer = b""
                while position > 0 and buffer.count(b"\n") <= wanted:
                    read_size = min(block_size, position)
                    position -= read_size
                    handle.seek(position)
                    buffer = handle.read(read_size) + buffer
            lines = [line.decode("utf-8", errors="replace") for line in buffer.splitlines()[-wanted:]]
        items: list[dict[str, Any]] = []
        for line in lines:
            try:
                item = json.loads(line)
            except Exception:
                continue
            if isinstance(item, dict):
                items.append(item)
        return items

    def _read_legacy_recent_sync(self) -> list[dict[str, Any]]:
        payload = self._read_sync()
        merged: list[dict[str, Any]] = []
        for chat_id, items in dict(payload.get("by_chat", {}) or {}).items():
            for item in list(items or []):
                if not isinstance(item, dict):
                    continue
                copied = dict(item)
                copied.setdefault("chat_id", str(chat_id))
                merged.append(copied)
        return merged

    async def recent(self, *, chat_id: str | None = None, limit: int = 80) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 80), 500))
        disk_items = await asyncio.to_thread(self._read_jsonl_sync, max(self.max_global * 2, safe_limit * 4))
        if not disk_items and not self.jsonl_path.exists() and self.path.exists():
            disk_items = await asyncio.to_thread(self._read_legacy_recent_sync)
        async with self._queue_lock:
            memory_items = list(self._recent_memory)
        merged: dict[str, dict[str, Any]] = {}
        for item in [*disk_items, *memory_items]:
            if not isinstance(item, dict):
                continue
            key = str(item.get("event_id", "") or f"{item.get('chat_id', '')}:{item.get('created_at', '')}:{item.get('stage', '')}")
            merged[key] = dict(item)
        items = list(merged.values())
        if chat_id:
            wanted = str(chat_id)
            items = [item for item in items if str(item.get("chat_id", "") or "") == wanted]
            items.sort(key=lambda item: float(item.get("created_at", 0.0) or 0.0), reverse=True)
            return items[: min(safe_limit, self.max_per_chat)]
        items.sort(key=lambda item: float(item.get("created_at", 0.0) or 0.0), reverse=True)
        return items[:safe_limit]

    async def flush(self, timeout_sec: float = 1.0) -> bool:
        deadline = time.monotonic() + max(0.0, float(timeout_sec or 0.0))
        self._ensure_writer()
        while time.monotonic() < deadline:
            async with self._queue_lock:
                if not self._queue and not self._physical_write_inflight:
                    return True
            await asyncio.sleep(0.005)
        async with self._queue_lock:
            return not self._queue and not self._physical_write_inflight

    def begin_shutdown(self) -> None:
        self._accepting = False
        self._wake.set()

    async def close(self, timeout_sec: float = 1.0) -> bool:
        self.begin_shutdown()
        task = self._writer_task
        if task is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=max(0.0, float(timeout_sec or 0.0)))
        except asyncio.TimeoutError:
            return False
        except asyncio.CancelledError:
            return False
        finally:
            if task.done():
                self._writer_task = None
        return not self._queue and not self._physical_write_inflight

    async def close_background_resources(self, timeout_sec: float = 1.0) -> bool:
        return await self.close(timeout_sec=timeout_sec)

    @property
    def tasks(self) -> set[asyncio.Task[Any]]:
        task = self._writer_task
        return {task} if task is not None and not task.done() else set()

    def describe_status(self) -> dict[str, Any]:
        try:
            file_size = self.jsonl_path.stat().st_size if self.jsonl_path.exists() else 0
        except OSError:
            file_size = 0
        oldest_queued_age_ms = (
            round(max(0.0, time.monotonic() - self._queue[0][2]) * 1000, 3)
            if self._queue
            else 0.0
        )
        return {
            "format": "jsonl",
            "accepting": self._accepting,
            "generation": self.generation,
            "writer_running": bool(self._writer_task is not None and not self._writer_task.done()),
            "queue_depth": len(self._queue),
            "queue_capacity": self.queue_capacity,
            "queued_bytes": self._queue_bytes,
            "oldest_queued_age_ms": oldest_queued_age_ms,
            "file_size_bytes": file_size,
            "legacy_present": self.path.exists(),
            "physical_write_inflight": self._physical_write_inflight,
            "last_error": self._last_error,
            **self._stats,
        }


__all__ = ["RawTraceEventStore"]
