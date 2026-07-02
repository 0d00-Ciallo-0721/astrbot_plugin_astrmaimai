from __future__ import annotations

import asyncio
import json
from typing import Any, Dict

from astrbot.api import logger


class PersonaCacheMixin:
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
        import os
        try:
            tmp_path = str(self.persona_cache_path) + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(cache_data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, str(self.persona_cache_path))
        except Exception as e:
            logger.error(f"[Persistence] : {e}")


    # ==========================================
    # State & Profile  I/O
    # ==========================================

    async def load_persona_cache_async(self) -> Dict[str, Any]:
        """Load persona cache asynchronously."""
        import asyncio
        return await asyncio.to_thread(self.load_persona_cache)

    async def save_persona_cache_async(self, cache_data: Dict[str, Any]):
        """Save persona cache asynchronously."""
        import asyncio
        return await asyncio.to_thread(self.save_persona_cache, cache_data)
