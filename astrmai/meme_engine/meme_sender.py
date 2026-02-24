import random
import os
from pathlib import Path
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
from astrbot.api.message_components import Image

async def send_meme(
    event: AstrMessageEvent,
    emotion_tag: str,
    probability: int,
    memes_dir: Path
):
    """
    根据情绪标签发送随机表情包
    """
    # 1. 基础校验
    if not emotion_tag or emotion_tag == "neutral" or emotion_tag == "none":
        return

    # 2. 概率判定
    if random.randint(1, 100) > probability:
        return

    try:
        # 3. 定位情绪目录
        emotion_path = memes_dir / emotion_tag
        if not emotion_path.is_dir():
            # 降级：如果找不到具体情绪文件夹，不发送 (避免乱发)
            return

        # 4. 筛选图片
        valid_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        memes = [
            f for f in emotion_path.iterdir() 
            if f.is_file() and f.suffix.lower() in valid_exts
        ]

        if not memes:
            return

        # 5. 随机选择并发送
        selected = random.choice(memes)
        # 使用 MessageChain 发送图片
        chain = MessageChain([Image.fromFileSystem(str(selected))])
        
        await event.send(chain)
        logger.info(f"[AstrMai] 🖼️ Sent Meme: {emotion_tag}/{selected.name}")

    except Exception as e:
        logger.error(f"[AstrMai] Meme Send Failed: {e}")