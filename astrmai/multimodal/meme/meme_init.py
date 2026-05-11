from __future__ import annotations

import logging
import shutil

from .meme_config import DEFAULT_MEMES_SOURCE_DIR, MEMES_DIR

logger = logging.getLogger("astrbot")


def init_meme_storage():
    try:
        if not MEMES_DIR.exists():
            MEMES_DIR.mkdir(parents=True, exist_ok=True)
        is_empty = not any(MEMES_DIR.iterdir())
        if is_empty and DEFAULT_MEMES_SOURCE_DIR.exists() and DEFAULT_MEMES_SOURCE_DIR.is_dir():
            shutil.copytree(DEFAULT_MEMES_SOURCE_DIR, MEMES_DIR, dirs_exist_ok=True)
    except Exception as exc:
        logger.error(f"[AstrMai] meme storage init failed: {exc}")


__all__ = ["init_meme_storage"]
