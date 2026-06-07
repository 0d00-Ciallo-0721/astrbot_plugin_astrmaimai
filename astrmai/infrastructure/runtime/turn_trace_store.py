from __future__ import annotations

import asyncio
import json
import os
import tempfile
from pathlib import Path
from typing import Any


class TurnTraceSampleStore:
    def __init__(self, base_dir: Path, *, max_per_chat: int = 50, filename: str = "turn_trace_samples.json"):
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / filename
        self.max_per_chat = max(1, int(max_per_chat or 50))
        self._lock = asyncio.Lock()

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
        normalized = {
            "version": 1,
            "by_chat": dict(payload.get("by_chat", {}) or {}),
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
        os.replace(tmp_path, self.path)

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
            await asyncio.to_thread(self._write_sync, payload)

    async def recent(self, *, chat_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 50), 300))
        async with self._lock:
            payload = await asyncio.to_thread(self._read_sync)
        by_chat = payload.get("by_chat", {}) or {}
        if chat_id:
            items = list(by_chat.get(str(chat_id), []) or [])
            return items[-safe_limit:][::-1]
        merged: list[dict[str, Any]] = []
        for items in by_chat.values():
            merged.extend(list(items or []))
        merged.sort(key=lambda item: float(item.get("created_at", 0.0) or 0.0), reverse=True)
        return merged[:safe_limit]


__all__ = ["TurnTraceSampleStore"]
