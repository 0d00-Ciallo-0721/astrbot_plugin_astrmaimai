import shutil
import logging
from .meme_config import MEMES_DIR, DEFAULT_MEMES_SOURCE_DIR

logger = logging.getLogger("astrbot")

def init_meme_storage():
    """
    初始化表情包存储
    """
    try:
        # 1. 确保目标目录存在
        if not MEMES_DIR.exists():
            MEMES_DIR.mkdir(parents=True, exist_ok=True)
            logger.info(f"[AstrMai] 📁 表情引擎：已创建存储目录: {MEMES_DIR}")
        
        # 2. 检查是否为空
        is_empty = not any(MEMES_DIR.iterdir())

        if is_empty:
            # 3. 尝试复制默认表情
            if DEFAULT_MEMES_SOURCE_DIR.exists() and DEFAULT_MEMES_SOURCE_DIR.is_dir():
                logger.info(f"[AstrMai] 正在部署默认表情包...")
                try:
                    shutil.copytree(DEFAULT_MEMES_SOURCE_DIR, MEMES_DIR, dirs_exist_ok=True)
                    logger.info(f"[AstrMai] ✅ 默认表情包部署成功。")
                except Exception as e:
                    logger.error(f"[AstrMai] ❌ 部署默认表情包失败: {e}")
            else:
                # 4. 提示用户
                logger.warning(f"[AstrMai] ⚠️ 表情目录为空且无默认源。请手动在 '{MEMES_DIR}' 下创建 happy, sad, angry 等文件夹并放入图片。")
    
    except Exception as e:
        logger.error(f"[AstrMai] 表情引擎初始化异常: {e}")