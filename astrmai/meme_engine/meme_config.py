import os
from pathlib import Path
from astrbot.core.utils.astrbot_path import get_astrbot_data_path

# 获取插件根目录 (astrmai/meme_engine/ -> astrmai/)
PLUGIN_DIR = Path(__file__).parent.parent.resolve()

# 获取 AstrBot data 目录
DATA_DIR = Path(get_astrbot_data_path())

# --- 表情包物理存储目录 ---
# 保持与 HeartFlow 一致：data/memes_data/memes/
MEMES_DIR = (DATA_DIR / "memes_data" / "memes").resolve()

# --- 默认表情包源目录 ---
# 假设在插件包内 astrmai/default_memes (需用户自行准备或后续添加)
DEFAULT_MEMES_SOURCE_DIR = (PLUGIN_DIR.parent / "default_memes").resolve()