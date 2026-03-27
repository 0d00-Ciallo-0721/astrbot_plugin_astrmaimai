import random
from pathlib import Path
from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent, MessageChain
import astrbot.api.message_components as Comp

async def send_meme(
    event: AstrMessageEvent,
    emotion_tag: str,
    probability: int,
    memes_dir: Path,
    context=None
):
    """
    根据情绪标签发送随机表情包 (带防静默熔断)
    """
    logger.info(f"[AstrMai-Meme] 准备发送表情包，情绪判定: '{emotion_tag}', 触发概率设定: {probability}%")
    
    if not emotion_tag or emotion_tag == "neutral" or emotion_tag == "none":
        logger.info(f"[AstrMai-Meme] 情绪平稳 ({emotion_tag})，跳过表情包投递。")
        return

    hit = random.randint(1, 100)
    if hit > probability:
        logger.info(f"[AstrMai-Meme] 🎲 概率未命中 (Roll: {hit} > {probability})，取消发送。")
        return

    try:
        emotion_path = Path(memes_dir) / emotion_tag
        if not emotion_path.is_dir():
            logger.warning(f"[AstrMai-Meme] ❌ 找不到对应的情绪表情包目录: {emotion_path.absolute()}")
            return

        valid_exts = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
        memes = [
            f for f in emotion_path.iterdir() 
            if f.is_file() and f.suffix.lower() in valid_exts
        ]

        if not memes:
            logger.warning(f"[AstrMai-Meme] ⚠️ 情绪目录为空或无合法图片。")
            return

        selected = random.choice(memes)
        logger.info(f"[AstrMai-Meme] 🎯 选中表情包: {selected.name}，准备发送...")
        
        image_comp = Comp.Image.fromFileSystem(str(selected.absolute()))
        
        # [新增] 免疫标记：防止主动发出的表情包触发旁路嗅探
        event.set_extra("astrmai_is_self_reply", True)
        
        if context:
            # 🟢 [核心修复] 放弃 .message() 快捷方法，直接向底层的 chain 列表 append 富媒体组件
            chain = MessageChain()
            chain.chain.append(image_comp)
            
            await context.send_message(event.unified_msg_origin, chain)
        else:
            # 兼容性兜底
            message_result = event.make_result()
            message_result.chain = [image_comp]
            await event.send(message_result)
            
        logger.info(f"[AstrMai-Meme] ✅ 成功发送表情包: {emotion_tag}/{selected.name}")

    except Exception as e:
        logger.error(f"[AstrMai-Meme] ❌ 表情包发送发生未捕获异常: {e}", exc_info=True)