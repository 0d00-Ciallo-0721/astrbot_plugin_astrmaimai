from __future__ import annotations

import base64
import io
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import numpy as np
from PIL import Image, ImageDraw, ImageOps
from astrbot.api import logger


@dataclass(slots=True)
class PreparedImage:
    file_path: str
    image_bytes: bytes
    image_format: str
    source_format: str = ""
    declared_suffix: str = ""
    is_animated: bool = False
    source_frame_count: int = 1
    duration_ms: int = 0
    sampled_indices: tuple[int, ...] = ()
    sampled_timestamps_ms: tuple[int, ...] = ()
    preprocess_version: str = "static-v1"
    preprocess_status: str = "ready"
    preprocess_elapsed_ms: float = 0.0
    fallback_reason: str = ""
    contact_sheet_size: tuple[int, int] = (0, 0)
    owns_temp_file: bool = True


class ImagePipeline:
    ANIMATION_PREPROCESS_VERSION = "animated-grid-v1"

    @staticmethod
    def _safe_json_tags(value) -> list:
        if isinstance(value, list):
            return value
        try:
            parsed = json.loads(str(value or "[]"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        return parsed if isinstance(parsed, list) else []

    @staticmethod
    def prepare_image(base64_data: str) -> Optional[PreparedImage]:
        try:
            image_bytes = base64.b64decode(base64_data)
        except Exception as exc:
            logger.error(f"[VisualPipeline] image prepare failed: {exc}")
            return None
        try:
            return ImagePipeline._prepare_bytes(image_bytes)
        except Exception as exc:
            logger.error(f"[VisualPipeline] image prepare failed: {exc}")
            return None

    @staticmethod
    def materialize_image(base64_data: str) -> Optional[PreparedImage]:
        """Decode an image payload without consuming its animation frames.

        Queue callers use this method to hand the original image to
        ``analyze_image_path``. The production analysis path then performs the
        same format detection, animation sampling and cache identity work as
        file-based callers.
        """
        started_at = time.monotonic()
        try:
            image_bytes = base64.b64decode(base64_data)
            with Image.open(io.BytesIO(image_bytes)) as source:
                source_format = str(source.format or "").lower()
                frame_count = max(1, int(getattr(source, "n_frames", 1) or 1))
                is_animated = bool(
                    getattr(source, "is_animated", False) and frame_count > 1
                )
            if not source_format:
                raise ValueError("image format is unavailable")
            suffix = ".jpeg" if source_format in {"jpg", "jpeg"} else f".{source_format}"
            return PreparedImage(
                file_path=ImagePipeline._write_temp_bytes(image_bytes, suffix=suffix),
                image_bytes=image_bytes,
                image_format=source_format,
                source_format=source_format,
                is_animated=is_animated,
                source_frame_count=frame_count,
                duration_ms=0,
                preprocess_version="materialized-v1",
                preprocess_status="materialized_original",
                preprocess_elapsed_ms=round(
                    (time.monotonic() - started_at) * 1000,
                    1,
                ),
            )
        except Exception as exc:
            logger.error(f"[VisualPipeline] image materialization failed: {exc}")
            return None

    @staticmethod
    def prepare_image_path(
        image_path: str,
        *,
        max_frames: int = 12,
        max_edge_px: int = 1600,
        max_decode_frames: int = 500,
        timeout_sec: float = 8.0,
    ) -> Optional[PreparedImage]:
        try:
            image_bytes = Path(image_path).read_bytes()
            return ImagePipeline._prepare_bytes(
                image_bytes,
                declared_suffix=Path(image_path).suffix.lower(),
                static_source_path=image_path,
                max_frames=max_frames,
                max_edge_px=max_edge_px,
                max_decode_frames=max_decode_frames,
                timeout_sec=timeout_sec,
            )
        except Exception as exc:
            logger.error(f"[VisualPipeline] image path prepare failed: {exc}")
            return None

    @staticmethod
    def _prepare_bytes(
        image_bytes: bytes,
        *,
        declared_suffix: str = "",
        static_source_path: str = "",
        max_frames: int = 12,
        max_edge_px: int = 1600,
        max_decode_frames: int = 500,
        timeout_sec: float = 8.0,
    ) -> PreparedImage:
        started_at = time.monotonic()
        with Image.open(io.BytesIO(image_bytes)) as source:
            source_format = str(source.format or "").lower()
            frame_count = max(1, int(getattr(source, "n_frames", 1) or 1))
            is_animated = bool(getattr(source, "is_animated", False) and frame_count > 1)
        if not source_format:
            raise ValueError("image format is unavailable")

        if not is_animated:
            file_path = static_source_path or ImagePipeline._write_temp_bytes(
                image_bytes,
                suffix=f".{source_format}",
            )
            return PreparedImage(
                file_path=file_path,
                image_bytes=image_bytes,
                image_format=source_format,
                source_format=source_format,
                declared_suffix=declared_suffix,
                source_frame_count=frame_count,
                sampled_indices=(0,),
                sampled_timestamps_ms=(0,),
                preprocess_elapsed_ms=round((time.monotonic() - started_at) * 1000, 1),
                owns_temp_file=not bool(static_source_path),
            )

        try:
            frames, durations = ImagePipeline._decode_animation_frames(
                image_bytes,
                max_decode_frames=max_decode_frames,
                deadline=(time.monotonic() + max(0.1, float(timeout_sec))),
            )
            normalized_frames = ImagePipeline._normalize_animation_frames(frames)
            selected_indices = ImagePipeline._select_animation_indices(
                normalized_frames,
                max_frames=max_frames,
            )
            timestamps = ImagePipeline._frame_timestamps(durations)
            contact_sheet = ImagePipeline._render_contact_sheet(
                [normalized_frames[index] for index in selected_indices],
                indices=selected_indices,
                timestamps_ms=[timestamps[index] for index in selected_indices],
                max_edge_px=max_edge_px,
            )
            output = io.BytesIO()
            contact_sheet.save(output, format="JPEG", quality=88, optimize=True)
            prepared_bytes = output.getvalue()
            return PreparedImage(
                file_path=ImagePipeline._write_temp_bytes(prepared_bytes, suffix=".jpeg"),
                image_bytes=prepared_bytes,
                image_format="jpeg",
                source_format=source_format,
                declared_suffix=declared_suffix,
                is_animated=True,
                source_frame_count=frame_count,
                duration_ms=sum(durations),
                sampled_indices=tuple(selected_indices),
                sampled_timestamps_ms=tuple(timestamps[index] for index in selected_indices),
                preprocess_version=ImagePipeline.ANIMATION_PREPROCESS_VERSION,
                preprocess_status="contact_sheet",
                preprocess_elapsed_ms=round((time.monotonic() - started_at) * 1000, 1),
                contact_sheet_size=contact_sheet.size,
            )
        except Exception as exc:
            logger.warning(
                "[VisualPipeline] animation preprocessing degraded to first frame "
                f"format={source_format} frames={frame_count} error={type(exc).__name__}"
            )
            return ImagePipeline._first_frame_fallback(
                image_bytes,
                source_format=source_format,
                declared_suffix=declared_suffix,
                frame_count=frame_count,
                started_at=started_at,
                reason=type(exc).__name__,
                max_edge_px=max_edge_px,
            )

    @staticmethod
    def _write_temp_bytes(payload: bytes, *, suffix: str) -> str:
        fd, temp_file_path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as handle:
            handle.write(payload)
        return temp_file_path

    @staticmethod
    def _decode_animation_frames(
        image_bytes: bytes,
        *,
        max_decode_frames: int,
        deadline: float,
    ) -> tuple[list[Image.Image], list[int]]:
        frames: list[Image.Image] = []
        durations: list[int] = []
        with Image.open(io.BytesIO(image_bytes)) as source:
            available = max(1, int(getattr(source, "n_frames", 1) or 1))
            limit = min(available, max(1, int(max_decode_frames)))
            for index in range(limit):
                if time.monotonic() > deadline:
                    raise TimeoutError("animation preprocessing deadline exceeded")
                source.seek(index)
                frames.append(ImageOps.exif_transpose(source.convert("RGBA")).copy())
                durations.append(max(1, int(source.info.get("duration", 100) or 100)))
        if not frames:
            raise ValueError("animation contains no decodable frames")
        return frames, durations

    @staticmethod
    def _normalize_animation_frames(frames: list[Image.Image]) -> list[Image.Image]:
        if not frames:
            return []
        canvas_width = max(frame.width for frame in frames)
        canvas_height = max(frame.height for frame in frames)
        normalized: list[Image.Image] = []
        for frame in frames:
            rgba = frame.convert("RGBA")
            canvas = Image.new("RGBA", (canvas_width, canvas_height), (255, 255, 255, 0))
            canvas.alpha_composite(rgba, (0, 0))
            background = Image.new("RGBA", canvas.size, (255, 255, 255, 255))
            background.alpha_composite(canvas)
            normalized.append(background.convert("RGB"))
        return normalized

    @staticmethod
    def _frame_difference(first: Image.Image, second: Image.Image) -> float:
        size = (64, 64)
        first_array = np.asarray(first.convert("RGB").resize(size)).astype(np.float32)
        second_array = np.asarray(second.convert("RGB").resize(size)).astype(np.float32)
        return float(np.mean(np.square(first_array - second_array, dtype=np.float32)))

    @staticmethod
    def _select_animation_indices(
        frames: list[Image.Image],
        *,
        max_frames: int,
        similarity_threshold: float = 16.0,
    ) -> list[int]:
        frame_count = len(frames)
        if frame_count <= 1:
            return [0] if frame_count else []
        limit = max(2, min(int(max_frames), frame_count))
        if frame_count <= limit:
            selected = [0]
            for index in range(1, frame_count - 1):
                if ImagePipeline._frame_difference(frames[selected[-1]], frames[index]) > similarity_threshold:
                    selected.append(index)
            if selected[-1] != frame_count - 1:
                selected.append(frame_count - 1)
            return selected

        uniform_count = max(2, min(limit, int(math.ceil(limit * 0.65))))
        selected = {
            int(round(index * (frame_count - 1) / (uniform_count - 1)))
            for index in range(uniform_count)
        }
        change_scores = [
            (ImagePipeline._frame_difference(frames[index - 1], frames[index]), index)
            for index in range(1, frame_count)
        ]
        for _score, index in sorted(change_scores, reverse=True):
            if len(selected) >= limit:
                break
            selected.add(index)
        if len(selected) < limit:
            for index in range(frame_count):
                if len(selected) >= limit:
                    break
                selected.add(index)
        selected.add(0)
        selected.add(frame_count - 1)
        ordered = sorted(selected)
        if len(ordered) > limit:
            removable = [index for index in ordered if index not in {0, frame_count - 1}]
            while len(ordered) > limit and removable:
                candidate = min(
                    removable,
                    key=lambda value: min(
                        abs(value - other) for other in ordered if other != value
                    ),
                )
                ordered.remove(candidate)
                removable.remove(candidate)
        return ordered

    @staticmethod
    def _frame_timestamps(durations: list[int]) -> list[int]:
        timestamps: list[int] = []
        elapsed = 0
        for duration in durations:
            timestamps.append(elapsed)
            elapsed += max(1, int(duration))
        return timestamps

    @staticmethod
    def _render_contact_sheet(
        frames: list[Image.Image],
        *,
        indices: list[int],
        timestamps_ms: list[int],
        max_edge_px: int,
    ) -> Image.Image:
        if not frames:
            raise ValueError("no animation frames selected")
        frame_count = len(frames)
        columns = max(1, int(math.ceil(math.sqrt(frame_count))))
        rows = int(math.ceil(frame_count / columns))
        edge = max(256, int(max_edge_px))
        cell_width = max(1, edge // columns)
        cell_height = max(1, edge // rows)
        label_height = min(24, max(14, cell_height // 8))
        image_height = max(1, cell_height - label_height)
        sheet = Image.new("RGB", (cell_width * columns, cell_height * rows), (238, 238, 238))
        draw = ImageDraw.Draw(sheet)
        for position, frame in enumerate(frames):
            thumbnail = frame.convert("RGB").copy()
            thumbnail.thumbnail((cell_width, image_height), Image.Resampling.LANCZOS)
            column = position % columns
            row = position // columns
            x = column * cell_width + (cell_width - thumbnail.width) // 2
            y = row * cell_height + (image_height - thumbnail.height) // 2
            sheet.paste(thumbnail, (x, y))
            timestamp = timestamps_ms[position] if position < len(timestamps_ms) else 0
            frame_index = indices[position] if position < len(indices) else position
            draw.text(
                (column * cell_width + 5, row * cell_height + image_height + 2),
                f"frame {frame_index + 1}  {timestamp} ms",
                fill=(32, 32, 32),
            )
        return sheet

    @staticmethod
    def _first_frame_fallback(
        image_bytes: bytes,
        *,
        source_format: str,
        declared_suffix: str,
        frame_count: int,
        started_at: float,
        reason: str,
        max_edge_px: int,
    ) -> PreparedImage:
        with Image.open(io.BytesIO(image_bytes)) as source:
            source.seek(0)
            first_frame = ImageOps.exif_transpose(source.convert("RGBA"))
            background = Image.new("RGBA", first_frame.size, (255, 255, 255, 255))
            background.alpha_composite(first_frame)
            normalized = background.convert("RGB")
            normalized.thumbnail((max_edge_px, max_edge_px), Image.Resampling.LANCZOS)
        output = io.BytesIO()
        normalized.save(output, format="JPEG", quality=88, optimize=True)
        payload = output.getvalue()
        return PreparedImage(
            file_path=ImagePipeline._write_temp_bytes(payload, suffix=".jpeg"),
            image_bytes=payload,
            image_format="jpeg",
            source_format=source_format,
            declared_suffix=declared_suffix,
            is_animated=True,
            source_frame_count=frame_count,
            sampled_indices=(0,),
            sampled_timestamps_ms=(0,),
            preprocess_version=ImagePipeline.ANIMATION_PREPROCESS_VERSION,
            preprocess_status="fallback_first_frame",
            preprocess_elapsed_ms=round((time.monotonic() - started_at) * 1000, 1),
            fallback_reason=reason,
            contact_sheet_size=normalized.size,
        )

    @staticmethod
    def cleanup(prepared: Optional[PreparedImage]) -> None:
        if not prepared:
            return
        if prepared.owns_temp_file and os.path.exists(prepared.file_path):
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
            gif_data = base64.b64decode(gif_base64)
            frames, durations = ImagePipeline._decode_animation_frames(
                gif_data,
                max_decode_frames=500,
                deadline=time.monotonic() + 8.0,
            )
            normalized = ImagePipeline._normalize_animation_frames(frames)
            selected_indices = ImagePipeline._select_animation_indices(
                normalized,
                max_frames=max_frames,
                similarity_threshold=max(0.0, float(similarity_threshold)),
            )
            timestamps = ImagePipeline._frame_timestamps(durations)
            combined_image = ImagePipeline._render_contact_sheet(
                [normalized[index] for index in selected_indices],
                indices=selected_indices,
                timestamps_ms=[timestamps[index] for index in selected_indices],
                max_edge_px=1600,
            )
            buffer = io.BytesIO()
            combined_image.save(buffer, format="JPEG", quality=88, optimize=True)
            return base64.b64encode(buffer.getvalue()).decode("ascii")
        except Exception as exc:
            logger.error(f"[VisualPipeline] GIF transform failed: {exc}")
            return None


__all__ = ["ImagePipeline", "PreparedImage"]
