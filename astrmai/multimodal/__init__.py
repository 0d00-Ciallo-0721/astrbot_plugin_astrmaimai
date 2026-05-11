from .image_pipeline import ImagePipeline, PreparedImage
from .visual_cortex import VisualCortex
from .meme.meme_config import DEFAULT_MEMES_SOURCE_DIR, MEMES_DIR
from .meme.meme_init import init_meme_storage
from .meme.meme_sender import send_meme


def describe_multimodal_capabilities(visual_cortex, *, vision_enabled: bool, meme_enabled: bool) -> dict:
    vision_status = {"worker_running": False, "queue_size": 0, "db_bound": False}
    if visual_cortex and hasattr(visual_cortex, "describe_status"):
        vision_status = visual_cortex.describe_status()
    return {
        "vision_enabled": vision_enabled,
        "meme_enabled": meme_enabled,
        "vision_status": vision_status,
        "meme_service": {
            "available": True,
            "memes_dir": str(MEMES_DIR),
        },
    }

__all__ = [
    "DEFAULT_MEMES_SOURCE_DIR",
    "ImagePipeline",
    "MEMES_DIR",
    "PreparedImage",
    "VisualCortex",
    "describe_multimodal_capabilities",
    "init_meme_storage",
    "send_meme",
]
