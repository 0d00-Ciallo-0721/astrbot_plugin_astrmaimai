from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any


class TurnTraceSampleStore:
    def __init__(
        self,
        base_dir: Path,
        *,
        max_per_chat: int = 50,
        max_global: int = 2000,
        filename: str = "turn_trace_samples.json",
    ):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / filename
        self.max_per_chat = max(1, int(max_per_chat or 50))
        self.max_global = max(self.max_per_chat, int(max_global or 2000))
        self._lock = asyncio.Lock()

    def _read_sync(self) -> dict[str, Any]:
        if not self.path.exists():
            return {"version": 2, "capture_started_at": time.time(), "by_chat": {}, "recent": []}
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            return {"version": 2, "capture_started_at": time.time(), "by_chat": {}, "recent": []}
        if not isinstance(payload, dict):
            return {"version": 2, "capture_started_at": time.time(), "by_chat": {}, "recent": []}
        by_chat = payload.get("by_chat", {})
        if not isinstance(by_chat, dict):
            by_chat = {}
        recent = payload.get("recent", [])
        if not isinstance(recent, list):
            recent = []
        return {
            "version": 2,
            "capture_started_at": float(payload.get("capture_started_at", 0.0) or time.time()),
            "by_chat": by_chat,
            "recent": [dict(item) for item in recent if isinstance(item, dict)],
        }

    def _write_sync(self, payload: dict[str, Any]) -> None:
        normalized = {
            "version": 2,
            "capture_started_at": float(payload.get("capture_started_at", 0.0) or time.time()),
            "by_chat": dict(payload.get("by_chat", {}) or {}),
            "recent": [
                dict(item)
                for item in list(payload.get("recent", []) or [])[-self.max_global :]
                if isinstance(item, dict)
            ],
        }
        serialized = json.dumps(normalized, ensure_ascii=False, indent=2)
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.base_dir),
            delete=False,
            prefix=f"{self.path.stem}.",
            suffix=".tmp",
        ) as tmp_file:
            tmp_file.write(serialized)
            tmp_path = Path(tmp_file.name)
        for attempt in range(3):
            try:
                os.replace(tmp_path, self.path)
                return
            except PermissionError:
                if attempt >= 2:
                    break
                time.sleep(0.05 * (attempt + 1))
        try:
            self.path.write_text(serialized, encoding="utf-8")
        finally:
            try:
                tmp_path.unlink(missing_ok=True)
            except Exception:
                pass

    async def append(self, sample: dict[str, Any]) -> None:
        chat_id = str(sample.get("chat_id", "") or "")
        if not chat_id:
            return
        async with self._lock:
            payload = await asyncio.to_thread(self._read_sync)
            by_chat = payload.setdefault("by_chat", {})
            items = list(by_chat.get(chat_id, []) or [])
            items.append(dict(sample))
            by_chat[chat_id] = items[-self.max_per_chat :]
            recent = list(payload.get("recent", []) or [])
            recent.append(dict(sample))
            payload["recent"] = recent[-self.max_global :]
            await asyncio.to_thread(self._write_sync, payload)

    async def recent(self, *, chat_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 50), 300))
        async with self._lock:
            payload = await asyncio.to_thread(self._read_sync)
        by_chat = payload.get("by_chat", {}) or {}
        if chat_id:
            items = list(by_chat.get(str(chat_id), []) or [])
            return items[-safe_limit:][::-1]
        recent = [dict(item) for item in list(payload.get("recent", []) or []) if isinstance(item, dict)]
        if recent:
            recent.sort(key=lambda item: float(item.get("created_at", 0.0) or 0.0), reverse=True)
            return recent[:safe_limit]
        merged: list[dict[str, Any]] = []
        for items in by_chat.values():
            merged.extend(list(items or []))
        merged.sort(key=lambda item: float(item.get("created_at", 0.0) or 0.0), reverse=True)
        return merged[:safe_limit]


__all__ = ["TurnTraceSampleStore"]
