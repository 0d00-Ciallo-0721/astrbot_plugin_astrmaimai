from __future__ import annotations

import base64

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
    logger.debug(f"[{gate.__class__.__name__}] remote image URLs are disabled: {url}")
    return ""


__all__ = [
    "extract_image_base64",
    "extract_image_base64_from_url",
]
