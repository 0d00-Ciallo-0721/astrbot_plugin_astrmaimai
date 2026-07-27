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
        # G8/WU-06: append-only JSONL 取代整文件 JSON 读改写。旧实现每条入站消息
        # （含被忽略的）都在聊天路径上「读整文件 + 解析 + 序列化 + 写整文件」，
        # 15MB 实测 0.7s/条，且与 WebUI 45s 轮询共用同一把锁互相放大。
        self.jsonl_path = self.base_dir / f"{self.path.stem}.jsonl"
        self.max_per_chat = max(1, int(max_per_chat or 50))
        self.max_global = max(self.max_per_chat, int(max_global or 2000))
        self._lock = asyncio.Lock()
        self._line_count: int | None = None

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
        # OPT-11/WU-06 短期缓解: 去掉 indent（15MB 实测 dumps 0.46s/条、封顶 ~42MB，
        # 每条入站消息都在聊天路径上整文件重写且与 WebUI 轮询共锁）；
        # 结构迁移（JSONL 分片/SQLite）另行专项，读取端需同步
        serialized = json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
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

    # ---- G8/WU-06: append-only JSONL ------------------------------------
    # 语义变化：追加不再重写整文件，因此**同一 turn_id 的旧行不会被物理删除**，
    # 由读取端「后写覆盖先写」去重（compact 时物理合并）。

    COMPACTION_FACTOR = 2

    def _migrate_legacy_sync(self) -> None:
        """首次写入前把 legacy 整文件 JSON 的历史样本转成 JSONL，避免丢历史。"""
        if self.jsonl_path.exists() or not self.path.exists():
            return
        payload = self._read_sync()
        samples: list[dict[str, Any]] = []
        for items in dict(payload.get("by_chat", {}) or {}).values():
            samples.extend(item for item in list(items or []) if isinstance(item, dict))
        samples.extend(
            item for item in list(payload.get("recent", []) or []) if isinstance(item, dict)
        )
        deduped = self._dedupe_by_turn(samples)
        if not deduped:
            return
        with self.jsonl_path.open("w", encoding="utf-8") as handle:
            for item in deduped[-self.max_global :]:
                handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")

    @staticmethod
    def _dedupe_by_turn(samples: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """后写覆盖先写；无 turn_id 的样本全部保留。"""
        by_turn: dict[str, int] = {}
        result: list[dict[str, Any]] = []
        for item in samples:
            if not isinstance(item, dict):
                continue
            turn_id = str(item.get("turn_id", "") or "")
            if not turn_id:
                result.append(item)
                continue
            if turn_id in by_turn:
                result[by_turn[turn_id]] = item
                continue
            by_turn[turn_id] = len(result)
            result.append(item)
        return result

    def _append_line_sync(self, sample: dict[str, Any]) -> None:
        self._migrate_legacy_sync()
        line = json.dumps(sample, ensure_ascii=False, separators=(",", ":"))
        with self.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")

    def _count_lines_sync(self) -> int:
        if not self.jsonl_path.exists():
            return 0
        with self.jsonl_path.open("r", encoding="utf-8") as handle:
            return sum(1 for _ in handle)

    def _read_samples_sync(self, max_lines: int | None = None) -> list[dict[str, Any]]:
        """读 JSONL 尾部；JSONL 不存在时回落 legacy 整文件（历史快照兼容）。"""
        if not self.jsonl_path.exists():
            payload = self._read_sync()
            legacy = [item for item in list(payload.get("recent", []) or []) if isinstance(item, dict)]
            if not legacy:
                for items in dict(payload.get("by_chat", {}) or {}).values():
                    legacy.extend(item for item in list(items or []) if isinstance(item, dict))
            return legacy
        with self.jsonl_path.open("r", encoding="utf-8") as handle:
            lines = handle.readlines()
        if max_lines is not None and max_lines > 0:
            lines = lines[-max_lines:]
        samples: list[dict[str, Any]] = []
        for line in lines:
            text = line.strip()
            if not text:
                continue
            try:
                item = json.loads(text)
            except Exception:
                continue  # 崩溃截断的半行：跳过而非整份报废
            if isinstance(item, dict):
                samples.append(item)
        return samples

    def _compact_sync(self) -> None:
        samples = self._dedupe_by_turn(self._read_samples_sync())
        per_chat: dict[str, list[dict[str, Any]]] = {}
        for item in samples:
            per_chat.setdefault(str(item.get("chat_id", "") or ""), []).append(item)
        keep_ids: set[int] = set()
        for items in per_chat.values():
            for item in items[-self.max_per_chat :]:
                keep_ids.add(id(item))
        kept = [item for item in samples if id(item) in keep_ids][-self.max_global :]
        tmp_path = self.jsonl_path.with_suffix(".jsonl.tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            for item in kept:
                handle.write(json.dumps(item, ensure_ascii=False, separators=(",", ":")) + "\n")
        os.replace(tmp_path, self.jsonl_path)
        self._line_count = len(kept)

    def _maybe_compact_sync(self) -> None:
        if self._line_count is None:
            self._line_count = self._count_lines_sync()
        self._line_count += 1
        if self._line_count > self.max_global * self.COMPACTION_FACTOR:
            self._compact_sync()

    async def append(self, sample: dict[str, Any]) -> None:
        chat_id = str(sample.get("chat_id", "") or "")
        if not chat_id:
            return
        normalized_sample = dict(sample)

        def _write() -> None:
            self._append_line_sync(normalized_sample)
            self._maybe_compact_sync()

        async with self._lock:
            await asyncio.to_thread(_write)

    async def recent(self, *, chat_id: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        safe_limit = max(1, min(int(limit or 50), 300))
        scan_lines = max(self.max_global, safe_limit * 4)
        async with self._lock:
            samples = await asyncio.to_thread(self._read_samples_sync, scan_lines)
        # 保留 max_global 保留量语义：压实是周期性的，读取端也要兜住上限，
        # 否则两次压实之间会返回超过配置保留量的样本
        samples = self._dedupe_by_turn(samples)[-self.max_global :]
        if chat_id:
            wanted = str(chat_id)
            samples = [item for item in samples if str(item.get("chat_id", "") or "") == wanted]
            return samples[-min(safe_limit, self.max_per_chat) :][::-1]
        samples.sort(key=lambda item: float(item.get("created_at", 0.0) or 0.0), reverse=True)
        return samples[:safe_limit]


__all__ = ["TurnTraceSampleStore"]
