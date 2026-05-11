from __future__ import annotations

import base64
import io
import json
import os
import tempfile
from dataclasses import dataclass
from typing import Optional

import numpy as np
from PIL import Image
from astrbot.api import logger


@dataclass(slots=True)
class PreparedImage:
    file_path: str
    image_bytes: bytes
    image_format: str


class ImagePipeline:
    @staticmethod
    def prepare_image(base64_data: str) -> Optional[PreparedImage]:
        image_bytes = base64.b64decode(base64_data)
        image_format = Image.open(io.BytesIO(image_bytes)).format.lower()
        if image_format in ["gif", "webp"]:
            transformed_b64 = ImagePipeline.transform_gif(base64_data)
            if not transformed_b64:
                return None
            image_bytes = base64.b64decode(transformed_b64)
            image_format = "jpeg"

        fd, temp_file_path = tempfile.mkstemp(suffix=f".{image_format}")
        with os.fdopen(fd, "wb") as handle:
            handle.write(image_bytes)
        return PreparedImage(
            file_path=temp_file_path,
            image_bytes=image_bytes,
            image_format=image_format,
        )

    @staticmethod
    def cleanup(prepared: Optional[PreparedImage]) -> None:
        if not prepared:
            return
        if os.path.exists(prepared.file_path):
            try:
                os.remove(prepared.file_path)
            except Exception as exc:
                logger.error(f"[VisualPipeline] failed to remove temp file {prepared.file_path}: {exc}")

    @staticmethod
    def serialize_tags(emotion_tags) -> str:
        return json.dumps(emotion_tags, ensure_ascii=False) if isinstance(emotion_tags, list) else "[]"

    @staticmethod
    def transform_gif(gif_base64: str, similarity_threshold: float = 1000.0, max_frames: int = 15) -> Optional[str]:
        try:
            gif_data = base64.b64decode(gif_base64.encode("ascii", errors="ignore").decode("ascii"))
            gif = Image.open(io.BytesIO(gif_data))

            all_frames = []
            try:
                while True:
                    gif.seek(len(all_frames))
                    all_frames.append(gif.convert("RGB").copy())
            except EOFError:
                pass
            if not all_frames:
                return None

            selected_frames = []
            last_selected_frame_np = None
            for index, current_frame in enumerate(all_frames):
                current_frame_np = np.array(current_frame)
                if index == 0:
                    selected_frames.append(current_frame)
                    last_selected_frame_np = current_frame_np
                    continue
                mse = np.mean((current_frame_np - last_selected_frame_np) ** 2)
                if mse > similarity_threshold:
                    selected_frames.append(current_frame)
                    last_selected_frame_np = current_frame_np
                    if len(selected_frames) >= max_frames:
                        break

            if not selected_frames:
                return None

            frame_width, frame_height = selected_frames[0].size
            target_height = 200
            target_width = max(int((target_height / frame_height) * frame_width), 1)
            resized_frames = [
                frame.resize((target_width, target_height), Image.Resampling.LANCZOS)
                for frame in selected_frames
            ]

            combined_image = Image.new("RGB", (target_width * len(resized_frames), target_height))
            for idx, frame in enumerate(resized_frames):
                combined_image.paste(frame, (idx * target_width, 0))

            buffer = io.BytesIO()
            combined_image.save(buffer, format="JPEG", quality=85)
            return base64.b64encode(buffer.getvalue()).decode("utf-8")
        except Exception as exc:
            logger.error(f"[VisualPipeline] GIF transform failed: {exc}")
            return None


__all__ = ["ImagePipeline", "PreparedImage"]
