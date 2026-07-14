from __future__ import annotations

import asyncio
import base64
import hashlib
import os
import shutil
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger


@dataclass(frozen=True, slots=True)
class ResolvedImage:
    index: int
    source_ref: str
    local_path: str


@dataclass(slots=True)
class ImageResolutionBatch:
    had_images: bool = False
    images: list[ResolvedImage] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)


class NapCatImageResolver:
    """Resolve OneBot/NapCat image references into readable local files."""

    MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

    def __init__(self, cache_dir: str | Path):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)

    async def resolve_event_images(self, event: Any) -> ImageResolutionBatch:
        references = self._extract_image_references(event)
        result = ImageResolutionBatch(had_images=bool(references))
        for index, candidates in enumerate(references):
            resolved = await self._resolve_candidates(event, index, candidates)
            if resolved is None:
                result.failures.append(candidates[0] if candidates else f"image-{index}")
            else:
                result.images.append(resolved)
        return result

    def _extract_image_references(self, event: Any) -> list[list[str]]:
        raw_event = getattr(getattr(event, "message_obj", None), "raw_message", None)
        raw_message = raw_event.get("message") if isinstance(raw_event, dict) else None
        references: list[list[str]] = []
        if isinstance(raw_message, list):
            for segment in raw_message:
                if not isinstance(segment, dict) or str(segment.get("type", "")).lower() != "image":
                    continue
                data = segment.get("data") if isinstance(segment.get("data"), dict) else {}
                candidates = self._unique_refs(
                    data.get(key) for key in ("local_path", "path", "file", "file_id", "url", "file_unique")
                )
                if candidates:
                    references.append(candidates)
        if references:
            return references

        components = list(getattr(getattr(event, "message_obj", None), "message", None) or [])
        for component in components:
            if component.__class__.__name__.lower() != "image":
                continue
            candidates = self._unique_refs(
                getattr(component, key, None) for key in ("path", "file", "url", "image_url", "src")
            )
            if candidates:
                references.append(candidates)
        if references:
            return references

        get_extra = getattr(event, "get_extra", None)
        if callable(get_extra):
            refs = list(get_extra("direct_image_refs", get_extra("direct_vision_urls", [])) or [])
            refs += list(get_extra("extracted_image_refs", get_extra("extracted_image_urls", [])) or [])
            references.extend([[ref] for ref in self._unique_refs(refs)])
        return references

    @staticmethod
    def _unique_refs(values) -> list[str]:
        unique: list[str] = []
        for value in values:
            normalized = str(value or "").strip()
            if normalized and normalized not in unique:
                unique.append(normalized)
        return unique

    async def _resolve_candidates(
        self,
        event: Any,
        index: int,
        candidates: list[str],
    ) -> ResolvedImage | None:
        for candidate in candidates:
            local_path = await self._materialize_reference(candidate, index)
            if local_path:
                return ResolvedImage(index=index, source_ref=candidate, local_path=local_path)

        api = getattr(getattr(getattr(event, "bot", None), "api", None), "call_action", None)
        if not callable(api):
            return None
        for candidate in candidates:
            for params in ({"file": candidate}, {"file_id": candidate}):
                try:
                    response = await api("get_image", **params)
                except Exception as exc:
                    logger.debug(f"[AstrMai-Vision] NapCat get_image degraded for {candidate}: {exc}")
                    continue
                source = self._extract_response_source(response)
                if not source:
                    continue
                local_path = await self._materialize_reference(source, index)
                if local_path:
                    return ResolvedImage(index=index, source_ref=candidate, local_path=local_path)
        return None

    async def _materialize_reference(self, reference: str, index: int) -> str:
        if reference.startswith("data:image"):
            try:
                header, encoded = reference.split(",", 1)
                suffix = ".png" if "png" in header.lower() else ".jpg"
                payload = base64.b64decode(encoded)
                return await asyncio.to_thread(self._write_bytes, payload, reference, index, suffix)
            except Exception as exc:
                logger.debug(f"[AstrMai-Vision] data URI decode degraded: {exc}")
                return ""

        path = self._normalize_local_path(reference)
        if path and os.path.isfile(path):
            return await asyncio.to_thread(self._copy_to_cache, path, reference, index)
        if reference.startswith(("http://", "https://")):
            try:
                return await asyncio.to_thread(self._download_to_cache, reference, index)
            except Exception as exc:
                logger.debug(f"[AstrMai-Vision] image URL download degraded: {exc}")
        return ""

    @staticmethod
    def _normalize_local_path(reference: str) -> str:
        value = str(reference or "").strip()
        if value.startswith("file:///"):
            value = value[8:]
        elif value.startswith("file://"):
            value = value[7:]
        return os.path.abspath(value) if value else ""

    @staticmethod
    def _extract_response_source(response: Any) -> str:
        payload = response
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        if not isinstance(payload, dict):
            return ""
        for key in ("file", "path", "url"):
            value = str(payload.get(key, "") or "").strip()
            if value:
                return value
        return ""

    def _cache_path(self, source: str, index: int, suffix: str) -> Path:
        digest = hashlib.sha256(source.encode("utf-8", errors="ignore")).hexdigest()[:20]
        safe_suffix = suffix if suffix and len(suffix) <= 10 else ".img"
        return self.cache_dir / f"{digest}_{index}{safe_suffix}"

    def _copy_to_cache(self, source_path: str, source_ref: str, index: int) -> str:
        suffix = Path(source_path).suffix or ".img"
        destination = self._cache_path(source_ref, index, suffix)
        if Path(source_path).resolve() != destination.resolve():
            shutil.copy2(source_path, destination)
        return str(destination)

    def _write_bytes(self, payload: bytes, source_ref: str, index: int, suffix: str) -> str:
        destination = self._cache_path(source_ref, index, suffix)
        destination.write_bytes(payload)
        return str(destination)

    def _download_to_cache(self, url: str, index: int) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": "AstrMai/1.0"})
        with urllib.request.urlopen(request, timeout=10) as response:
            payload = response.read(self.MAX_DOWNLOAD_BYTES + 1)
            content_type = str(response.headers.get("Content-Type", "") or "").lower()
        if len(payload) > self.MAX_DOWNLOAD_BYTES:
            raise ValueError("image payload exceeds 20MB")
        suffix = ".png" if "png" in content_type else ".jpg"
        return self._write_bytes(payload, url, index, suffix)


__all__ = ["ImageResolutionBatch", "NapCatImageResolver", "ResolvedImage"]
