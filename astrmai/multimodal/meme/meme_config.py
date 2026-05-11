from __future__ import annotations

from pathlib import Path

from astrbot.core.utils.astrbot_path import get_astrbot_data_path

PLUGIN_DIR = Path(__file__).parent.parent.parent.resolve()
DATA_DIR = Path(get_astrbot_data_path())
MEMES_DIR = (DATA_DIR / "memes_data" / "memes").resolve()
DEFAULT_MEMES_SOURCE_DIR = (PLUGIN_DIR.parent / "default_memes").resolve()

__all__ = ["DATA_DIR", "DEFAULT_MEMES_SOURCE_DIR", "MEMES_DIR", "PLUGIN_DIR"]
