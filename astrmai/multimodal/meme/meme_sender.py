from __future__ import annotations

import random
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import MessageChain


async def send_meme(event, emotion_tag: str, probability: int, memes_dir: Path, context=None):
    try:
        if not emotion_tag or emotion_tag.lower() in {"neutral", "none"}:
            return False
        if random.randint(1, 100) > probability:
            return False

        emotion_path = Path(memes_dir) / emotion_tag
        if not emotion_path.is_dir():
            return False

        valid_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        memes = [item for item in emotion_path.iterdir() if item.is_file() and item.suffix.lower() in valid_exts]
        if not memes:
            return False

        selected = random.choice(memes)
        image_comp = Comp.Image.fromFileSystem(str(selected.absolute()))
        event.set_extra("astrmai_is_self_reply", True)

        if context:
            chain = MessageChain()
            chain.chain.append(image_comp)
            await context.send_message(event.unified_msg_origin, chain)
        else:
            message_result = event.make_result()
            message_result.chain = [image_comp]
            await event.send(message_result)
        logger.info(f"[AstrMai-Meme] sent {emotion_tag}/{selected.name}")
        return True
    except Exception as exc:
        logger.warning(f"[AstrMai-Meme] optional meme send degraded: {exc}")
        try:
            event.set_extra("astrmai_meme_send_degraded", True)
        except Exception:
            pass
        return False


__all__ = ["send_meme"]
