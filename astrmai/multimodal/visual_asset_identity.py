from __future__ import annotations

import hashlib
import io
import mimetypes
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from PIL import Image, ImageOps, ImageSequence


@dataclass(frozen=True)
class VisualAssetIdentity:
    asset_id: str
    blob_hash: str
    pixel_hash: str
    perceptual_hash: str
    prompt_version: str
    mime_type: str
    width: int
    height: int
    frame_count: int
    byte_size: int


def _dhash_hex(image: Image.Image) -> str:
    grayscale = image.convert("L").resize((9, 8), Image.Resampling.LANCZOS)
    pixel_reader = getattr(grayscale, "get_flattened_data", grayscale.getdata)
    pixels = list(pixel_reader())
    value = 0
    for row in range(8):
        offset = row * 9
        for column in range(8):
            value = (value << 1) | int(
                pixels[offset + column] > pixels[offset + column + 1]
            )
    return f"{value:016x}"


def build_visual_asset_identity(
    image_path: str,
    *,
    prompt_version: str = "v1",
) -> VisualAssetIdentity:
    raw = Path(image_path).read_bytes()
    blob_hash = hashlib.sha256(raw).hexdigest()
    with Image.open(io.BytesIO(raw)) as source:
        frame_count = int(getattr(source, "n_frames", 1) or 1)
        pixel_digest = hashlib.sha256()
        first_frame = None
        width = 0
        height = 0
        for frame_index, frame in enumerate(ImageSequence.Iterator(source)):
            normalized = ImageOps.exif_transpose(frame).convert("RGBA")
            if first_frame is None:
                first_frame = normalized.copy()
                width, height = normalized.size
            frame_duration = int(frame.info.get("duration", 0) or 0)
            pixel_digest.update(
                f"{frame_index}:{normalized.width}x{normalized.height}:"
                f"{frame_duration}:RGBA:".encode("ascii")
            )
            pixel_digest.update(normalized.tobytes())
        if first_frame is None:
            raise ValueError("image contains no decodable frame")
        pixel_hash = pixel_digest.hexdigest()
        perceptual_hash = _dhash_hex(first_frame)
        mime_type = str(Image.MIME.get(source.format, "") or "")

    if not mime_type:
        mime_type = str(mimetypes.guess_type(image_path)[0] or "application/octet-stream")
    version = str(prompt_version or "v1").strip() or "v1"
    asset_id = hashlib.sha256(f"{version}:{pixel_hash}".encode("utf-8")).hexdigest()
    return VisualAssetIdentity(
        asset_id=asset_id,
        blob_hash=blob_hash,
        pixel_hash=pixel_hash,
        perceptual_hash=perceptual_hash,
        prompt_version=version,
        mime_type=mime_type,
        width=width,
        height=height,
        frame_count=frame_count,
        byte_size=len(raw),
    )


def store_normalized_visual_asset(
    image_path: str,
    *,
    asset_id: str,
    asset_dir: str | Path,
    max_edge_px: int = 1600,
) -> str:
    target_dir = Path(asset_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"{asset_id}.jpg"
    if target.is_file():
        return target.name

    with Image.open(image_path) as source:
        normalized = ImageOps.exif_transpose(source).convert("RGB")
        normalized.thumbnail(
            (max(1, int(max_edge_px)), max(1, int(max_edge_px))),
            Image.Resampling.LANCZOS,
        )
        fd, temporary_name = tempfile.mkstemp(
            prefix=f".{asset_id}.",
            suffix=".tmp",
            dir=str(target_dir),
        )
        os.close(fd)
        try:
            normalized.save(temporary_name, format="JPEG", quality=88, optimize=True)
            os.replace(temporary_name, target)
        finally:
            if os.path.exists(temporary_name):
                os.unlink(temporary_name)
    return target.name


__all__ = [
    "VisualAssetIdentity",
    "build_visual_asset_identity",
    "store_normalized_visual_asset",
]
