from __future__ import annotations

import asyncio
import json
import os
import tempfile
from typing import Any, Dict

from astrbot.api import logger


class PersonaCacheMixin:
    def _write_persona_cache_atomic(self, cache_data: Dict[str, Any]) -> None:
        """Write the cache atomically and propagate failures to strict callers."""
        self.persona_cache_path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(
            prefix=f"{self.persona_cache_path.name}.",
            suffix=".tmp",
            dir=str(self.persona_cache_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, str(self.persona_cache_path))
        except Exception:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise

    def load_persona_cache(self) -> Dict[str, Any]:
        """Load persona cache from disk."""
        if not self.persona_cache_path.exists():
            return {}
        try:
            with open(self.persona_cache_path, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"[Persistence] : {e}")
            return {}

    def save_persona_cache(self, cache_data: Dict[str, Any]):
        """Persist persona cache to disk (atomic write via tempfile)."""
        try:
            self._write_persona_cache_atomic(cache_data)
            return True
        except Exception as e:
            logger.error(f"[Persistence] : {e}")
            return False

    def save_persona_cache_strict(self, cache_data: Dict[str, Any]) -> None:
        """Persist persona cache and let the caller handle write failures."""
        self._write_persona_cache_atomic(cache_data)


    # ==========================================
    # State & Profile  I/O
    # ==========================================

    async def load_persona_cache_async(self) -> Dict[str, Any]:
        """Load persona cache asynchronously."""
        import asyncio
        return await asyncio.to_thread(self.load_persona_cache)

    async def save_persona_cache_async(self, cache_data: Dict[str, Any]):
        """Save persona cache asynchronously."""
        return await asyncio.to_thread(self.save_persona_cache, cache_data)

    async def save_persona_cache_strict_async(self, cache_data: Dict[str, Any]) -> None:
        """Strict asynchronous cache persistence used by startup readiness."""
        await asyncio.to_thread(self.save_persona_cache_strict, cache_data)
