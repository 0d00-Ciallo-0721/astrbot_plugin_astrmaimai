from __future__ import annotations

import base64

import aiohttp
from astrbot.api import logger


async def extract_image_base64(gate, image_component):
    if hasattr(image_component, "file_to_base64"):
        try:
            res = await image_component.file_to_base64()
            if res:
                return res
        except Exception:
            pass

    url = getattr(image_component, "url", None)
    if url:
        return await extract_image_base64_from_url(gate, url)

    file_path = getattr(image_component, "file", None) or getattr(image_component, "path", None)
    if file_path:
        try:
            with open(file_path, "rb") as file_obj:
                return base64.b64encode(file_obj.read()).decode("utf-8")
        except Exception:
            pass
    return ""


async def extract_image_base64_from_url(gate, url: str):
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(url, timeout=5) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    return base64.b64encode(data).decode("utf-8")
    except Exception as exc:
        logger.debug(f"[{gate.__class__.__name__}] 获取图片 URL 失败: {exc}")
    return ""


__all__ = [
    "extract_image_base64",
    "extract_image_base64_from_url",
]
