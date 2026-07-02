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
            logger.debug("[VisionBinding] file_to_base64 failed", exc_info=True)
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
            logger.debug("[VisionBinding] local file base64 encode failed", exc_info=True)
            pass
    return ""


async def extract_image_base64_from_url(gate, url: str):
    """Download remote image via HTTP and return base64-encoded data."""
    if not url or not (url.startswith("http://") or url.startswith("https://")):
        logger.debug(f"[{gate.__class__.__name__}] unsafe image URL ignored: {str(url)[:80]}")
        return ""
    try:
        import aiohttp
        timeout = aiohttp.ClientTimeout(total=10)
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as resp:
                if resp.status != 200:
                    logger.warning(f"[{gate.__class__.__name__}] image download failed: HTTP {resp.status}")
                    return ""
                data = await resp.read()
                if len(data) > 10 * 1024 * 1024:  # ponytail: 10MB limit
                    logger.warning(f"[{gate.__class__.__name__}] image too large: {len(data)} bytes")
                    return ""
                import base64
                return base64.b64encode(data).decode("ascii")
    except Exception as exc:
        logger.warning(f"[{gate.__class__.__name__}] image download failed: {exc}")
        return ""


__all__ = [
    "extract_image_base64",
    "extract_image_base64_from_url",
]
