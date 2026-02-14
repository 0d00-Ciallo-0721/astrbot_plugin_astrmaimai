# heartflow/meme_engine/meme_init.py
# (v18.3 修复 - 优化目录创建逻辑与空目录提示)
import os
import shutil
import logging
from pathlib import Path

# (v4.0) 使用相对路径从同级目录导入
from .meme_config import MEMES_DIR, DEFAULT_MEMES_SOURCE_DIR

logger = logging.getLogger(__name__)

def init_meme_storage():
    """
    初始化表情包存储
    1. 确保存储目录存在。
    2. 如果目录为空，尝试复制默认表情包。
    3. 如果无法复制，提示用户手动添加。
    """
    try:
        # 1. 确保目标表情包目录存在
        if not MEMES_DIR.exists():
            MEMES_DIR.mkdir(parents=True, exist_ok=True)
            logger.info(f"💖 表情引擎：已创建表情包存储目录: {MEMES_DIR}")
        
        # 2. 检查目录是否为空
        # use any() is efficient as it returns True on first item
        is_empty = not any(MEMES_DIR.iterdir())

        if is_empty:
            logger.info(f"💖 表情引擎：检测到目录 '{MEMES_DIR}' 为空。")

            # 3. 尝试从插件包复制默认表情
            if DEFAULT_MEMES_SOURCE_DIR.exists() and DEFAULT_MEMES_SOURCE_DIR.is_dir():
                logger.info(f"正在从 '{DEFAULT_MEMES_SOURCE_DIR}' 复制默认表情包...")
                try:
                    # dirs_exist_ok=True 允许目录已存在的情况下覆盖/合并
                    shutil.copytree(DEFAULT_MEMES_SOURCE_DIR, MEMES_DIR, dirs_exist_ok=True)
                    logger.info(f"✅ 默认表情包已成功初始化到: {MEMES_DIR}")
                except Exception as cp_e:
                    logger.error(f"复制默认表情包失败: {cp_e}")
            else:
                # 4. 无默认表情包，强提示用户
                logger.warning(f"⚠️ 表情引擎：未在插件内找到默认表情源 '{DEFAULT_MEMES_SOURCE_DIR}'。")
                logger.warning(f"⚠️ [重要提示] 请手动在 '{MEMES_DIR}' 目录下：")
                logger.warning(f"   1. 创建情绪分类文件夹 (例如: happy, sad, angry, like)")
                logger.warning(f"   2. 在文件夹中放入对应的表情包图片 (.jpg/.png/.gif)")
        else:
            # 目录非空，视为正常
            logger.debug(f"表情包目录 '{MEMES_DIR}' 校验通过 (非空)。")

    except Exception as e:
        logger.error(f"初始化表情包目录失败: {e}")
        import traceback
        logger.error(traceback.format_exc())