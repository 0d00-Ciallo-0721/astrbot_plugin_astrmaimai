from __future__ import annotations

import asyncio
import json
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


def default_benchmark_run_id() -> str:
    return time.strftime("benchrun-%Y%m%dT%H%M%SZ", time.gmtime())


@dataclass
class ContextEconomyBenchmarkSample:
    source_run_id: str
    created_at: float
    workload_family: str = ""
    template_id: str = ""
    template_version: str = ""
    template_key: str = ""
    schema_id: str = ""
    model_id: str = ""
    provider_family: str = ""
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    provider_session_id: str = ""
    lane_rotated: bool = False
    lane_rotate_reason: str = ""
    stable_prefix_length: int = 0
    dynamic_payload_length: int = 0
    primary_model: str = ""
    actual_model: str = ""
    primary_hit: bool = False
    fallback_used: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


class ContextEconomyBenchmarkSampleStore:
    def __init__(
        self,
        base_dir: Path,
        *,
        filename: str = "context_economy_benchmark_samples.jsonl",
        run_id: str | None = None,
    ) -> None:
        self.base_dir = Path(base_dir)
        self.base_dir.mkdir(parents=True, exist_ok=True)
        self.path = self.base_dir / filename
        self.run_id = str(run_id or default_benchmark_run_id())
        self._lock = asyncio.Lock()

    def _append_sync(self, sample: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(sample, ensure_ascii=False) + "\n")

    async def append(self, sample: dict[str, Any] | ContextEconomyBenchmarkSample) -> None:
        payload = sample.as_dict() if isinstance(sample, ContextEconomyBenchmarkSample) else dict(sample or {})
        if not payload:
            return
        payload.setdefault("source_run_id", self.run_id)
        payload.setdefault("created_at", time.time())
        async with self._lock:
            await asyncio.to_thread(self._append_sync, payload)

    def read_all_sync(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        items: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as handle:
            for line in handle:
                raw = line.strip()
                if not raw:
                    continue
                try:
                    payload = json.loads(raw)
                except Exception:
                    continue
                if isinstance(payload, dict):
                    items.append(payload)
        return items

    async def read_all(self) -> list[dict[str, Any]]:
        async with self._lock:
            return await asyncio.to_thread(self.read_all_sync)


__all__ = [
    "ContextEconomyBenchmarkSample",
    "ContextEconomyBenchmarkSampleStore",
    "default_benchmark_run_id",
]
