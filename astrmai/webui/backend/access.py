from __future__ import annotations

from typing import Optional

from fastapi import Header


ASTRBOT_USER_HEADER = "X-AstrBot-User"


async def get_current_user(
    astrbot_user: Optional[str] = Header(None, alias=ASTRBOT_USER_HEADER),
) -> str:
    return str(astrbot_user or "astrbot-plugin-page")


__all__ = ["ASTRBOT_USER_HEADER", "get_current_user"]
