from __future__ import annotations

import asyncio
import base64
import hashlib
import io
import os
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from astrbot.api import logger
from PIL import Image


@dataclass(frozen=True, slots=True)
class ResolvedImage:
    index: int
    source_ref: str
    local_path: str
    strategy: str = "direct"


@dataclass(slots=True)
class ImageResolutionBatch:
    had_images: bool = False
    images: list[ResolvedImage] = field(default_factory=list)
    failures: list[str] = field(default_factory=list)
    failure_details: list[dict[str, Any]] = field(default_factory=list)


class NapCatImageResolver:
    """Resolve OneBot/NapCat image references into readable local files."""

    MAX_DOWNLOAD_BYTES = 20 * 1024 * 1024

    def __init__(self, cache_dir: str | Path, config: Any = None):
        self.cache_dir = Path(cache_dir)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.config = config

    def refresh_config(self, config: Any) -> None:
        self.config = config

    def _download_timeout_seconds(self) -> float:
        timing = getattr(self.config, "timing", None)
        private_config = getattr(self.config, "private_chat", None)
        configured = getattr(
            timing,
            "image_resolve_timeout_sec",
            getattr(private_config, "image_resolve_timeout_sec", 15.0),
        )
        try:
            return max(0.1, float(configured))
        except (TypeError, ValueError):
            return 15.0

    async def resolve_event_images(self, event: Any) -> ImageResolutionBatch:
        references = self._extract_image_references(event)
        return await self._resolve_references(event, references)

    async def resolve_message_payload(
        self,
        event: Any,
        payload: Any,
    ) -> ImageResolutionBatch:
        """Resolve images from a historical OneBot ``get_msg`` payload."""
        references = self._extract_payload_image_references(payload)
        return await self._resolve_references(event, references)

    async def resolve_candidate(self, event: Any, candidate: Any) -> ImageResolutionBatch:
        """Resolve exactly one final-reply candidate with NapCat refresh fallbacks."""

        values = candidate if isinstance(candidate, dict) else {}
        raw_references = values.get("candidate_refs") or values.get("refs") or []
        if isinstance(raw_references, str):
            raw_references = [raw_references]
        references = self._unique_refs(raw_references)
        source_message_id = str(
            values.get("reply_to_message_id")
            or values.get("message_id")
            or values.get("selected_from_message_id")
            or ""
        ).strip()
        result = ImageResolutionBatch(had_images=bool(references or source_message_id))
        if not references and not source_message_id:
            self._record_failure(result.failure_details, 0, "no_reference")
            result.failures.append("image-0")
            return result
        resolved = await self._resolve_candidates(
            event,
            0,
            references,
            source_message_id=source_message_id,
            failure_details=result.failure_details,
        )
        if resolved is None:
            result.failures.append(references[0] if references else source_message_id or "image-0")
        else:
            result.images.append(resolved)
        return result

    async def _resolve_references(
        self,
        event: Any,
        references: list[list[str]],
    ) -> ImageResolutionBatch:
        result = ImageResolutionBatch(had_images=bool(references))
        for index, candidates in enumerate(references):
            resolved = await self._resolve_candidates(
                event,
                index,
                candidates,
                failure_details=result.failure_details,
            )
            if resolved is None:
                result.failures.append(candidates[0] if candidates else f"image-{index}")
            else:
                result.images.append(resolved)
        return result

    @staticmethod
    def _record_failure(
        failure_details: list[dict[str, Any]],
        index: int,
        reason: str,
        detail: str = "",
    ) -> None:
        item = {
            "index": max(0, int(index or 0)),
            "reason": str(reason or "")[:64],
        }
        if detail:
            item["detail"] = str(detail or "")[:80]
        if item not in failure_details:
            failure_details.append(item)

    def _extract_payload_image_references(self, payload: Any) -> list[list[str]]:
        data = payload
        if isinstance(data, dict) and isinstance(data.get("data"), (dict, list)):
            data = data["data"]
        if isinstance(data, dict):
            segments = data.get("message")
            if not isinstance(segments, list):
                segments = data.get("raw_message")
        else:
            segments = data
        if not isinstance(segments, list):
            return []

        references: list[list[str]] = []
        for segment in segments:
            if isinstance(segment, dict):
                if str(segment.get("type", "")).lower() != "image":
                    continue
                values = segment.get("data") if isinstance(segment.get("data"), dict) else segment
                candidates = self._unique_refs(
                    values.get(key)
                    for key in (
                        "local_path",
                        "path",
                        "file",
                        "file_id",
                        "url",
                        "file_unique",
                    )
                )
            elif segment.__class__.__name__.lower() == "image":
                candidates = self._unique_refs(
                    getattr(segment, key, None)
                    for key in ("path", "file", "url", "image_url", "src")
                )
            else:
                continue
            if candidates:
                references.append(candidates)
        return references

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
        *,
        source_message_id: str = "",
        allow_message_refresh: bool = True,
        failure_details: list[dict[str, Any]] | None = None,
    ) -> ResolvedImage | None:
        details = failure_details if failure_details is not None else []
        if not candidates and not source_message_id:
            self._record_failure(details, index, "no_reference")
            return None
        for candidate in candidates:
            local_path = await self._materialize_reference(candidate, index)
            if local_path:
                return ResolvedImage(index=index, source_ref=candidate, local_path=local_path, strategy="direct")
            if candidate.startswith(("http://", "https://")):
                self._record_failure(details, index, "download_failed")

        api = getattr(getattr(getattr(event, "bot", None), "api", None), "call_action", None)
        if not callable(api):
            self._record_failure(details, index, "no_reference", "napcat_api_unavailable")
            return None
        for candidate in candidates:
            if candidate.startswith("onebot-message://"):
                continue
            for action in ("get_image", "get_file"):
                action_resolved = False
                for params in ({"file": candidate}, {"file_id": candidate}):
                    try:
                        response = await api(action, **params)
                    except Exception as exc:
                        logger.debug(
                            f"[AstrMai-Vision] NapCat {action} degraded "
                            f"error={type(exc).__name__}"
                        )
                        continue
                    for source in self._extract_response_sources(response):
                        local_path = await self._materialize_reference(source, index)
                        if local_path:
                            action_resolved = True
                            return ResolvedImage(
                                index=index,
                                source_ref=candidate,
                                local_path=local_path,
                                strategy=action,
                            )
                if not action_resolved:
                    self._record_failure(details, index, f"{action}_failed")
        if allow_message_refresh and source_message_id:
            try:
                response = await api("get_msg", message_id=self._coerce_api_id(source_message_id))
            except Exception as exc:
                self._record_failure(details, index, "get_msg_failed", type(exc).__name__)
                logger.debug(
                    "[AstrMai-Vision] NapCat get_msg degraded "
                    f"error={type(exc).__name__}"
                )
            else:
                refreshed_references = self._extract_payload_image_references(response)
                payload = response.get("data", response) if isinstance(response, dict) else response
                message_value = payload.get("message") if isinstance(payload, dict) else None
                if isinstance(message_value, str):
                    self._record_failure(
                        details,
                        index,
                        "get_msg_failed",
                        "unsupported_cq_string",
                    )
                elif not refreshed_references:
                    self._record_failure(details, index, "get_msg_failed", "no_image_segment")
                for refreshed in reversed(refreshed_references):
                    resolved = await self._resolve_candidates(
                        event,
                        index,
                        refreshed,
                        source_message_id="",
                        allow_message_refresh=False,
                        failure_details=details,
                    )
                    if resolved is not None:
                        return ResolvedImage(
                            index=index,
                            source_ref=resolved.source_ref,
                            local_path=resolved.local_path,
                            strategy="get_msg",
                        )
        elif allow_message_refresh:
            self._record_failure(details, index, "get_msg_failed", "missing_message_id")
        return None

    async def _materialize_reference(self, reference: str, index: int) -> str:
        if reference.startswith("onebot-message://"):
            return ""
        if reference.startswith("data:image"):
            try:
                header, encoded = reference.split(",", 1)
                suffix = ".png" if "png" in header.lower() else ".jpg"
                payload = base64.b64decode(encoded)
                return await asyncio.to_thread(
                    self._write_bytes,
                    payload,
                    reference,
                    index,
                    self._detect_suffix(payload, fallback=suffix),
                )
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
    def _extract_response_sources(response: Any) -> list[str]:
        payload = response
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
            payload = payload["data"]
        if not isinstance(payload, dict):
            return []
        sources: list[str] = []
        for key in ("file", "path", "url"):
            value = str(payload.get(key, "") or "").strip()
            if value and value not in sources:
                sources.append(value)
        encoded = str(payload.get("base64", "") or "").strip()
        if encoded:
            data_uri = f"data:image/jpeg;base64,{encoded}"
            if data_uri not in sources:
                sources.append(data_uri)
        return sources

    @classmethod
    def _extract_response_source(cls, response: Any) -> str:
        sources = cls._extract_response_sources(response)
        return sources[0] if sources else ""

    @staticmethod
    def _coerce_api_id(value: str) -> str | int:
        normalized = str(value or "").strip()
        if normalized and normalized.lstrip("-").isdigit():
            try:
                return int(normalized)
            except ValueError:
                pass
        return normalized

    def _cache_path(self, source: str, index: int, suffix: str) -> Path:
        digest = hashlib.sha256(source.encode("utf-8", errors="ignore")).hexdigest()[:20]
        safe_suffix = suffix if suffix and len(suffix) <= 10 else ".img"
        return self.cache_dir / f"{digest}_{index}{safe_suffix}"

    def _copy_to_cache(self, source_path: str, source_ref: str, index: int) -> str:
        payload = Path(source_path).read_bytes()
        declared_suffix = Path(source_path).suffix.lower() or ".img"
        detected_suffix = self._detect_suffix(payload, fallback=declared_suffix)
        if detected_suffix != declared_suffix:
            logger.info(
                "[AstrMai-Vision] image format corrected "
                f"declared={declared_suffix} detected={detected_suffix}"
            )
        return self._write_bytes(payload, source_ref, index, detected_suffix)

    @staticmethod
    def _detect_suffix(payload: bytes, *, fallback: str = ".img") -> str:
        try:
            with Image.open(io.BytesIO(payload)) as image:
                image_format = str(image.format or "").upper()
        except Exception:
            return fallback
        return {
            "GIF": ".gif",
            "WEBP": ".webp",
            "PNG": ".png",
            "JPEG": ".jpg",
            "JPG": ".jpg",
            "BMP": ".bmp",
        }.get(image_format, fallback)

    def _write_bytes(self, payload: bytes, source_ref: str, index: int, suffix: str) -> str:
        destination = self._cache_path(source_ref, index, suffix)
        destination.write_bytes(payload)
        return str(destination)

    def _download_to_cache(self, url: str, index: int) -> str:
        request = urllib.request.Request(url, headers={"User-Agent": "AstrMai/1.0"})
        with urllib.request.urlopen(request, timeout=self._download_timeout_seconds()) as response:
            payload = response.read(self.MAX_DOWNLOAD_BYTES + 1)
            content_type = str(response.headers.get("Content-Type", "") or "").lower()
        if len(payload) > self.MAX_DOWNLOAD_BYTES:
            raise ValueError("image payload exceeds 20MB")
        declared_suffix = ".png" if "png" in content_type else ".jpg"
        suffix = self._detect_suffix(payload, fallback=declared_suffix)
        return self._write_bytes(payload, url, index, suffix)


__all__ = ["ImageResolutionBatch", "NapCatImageResolver", "ResolvedImage"]
