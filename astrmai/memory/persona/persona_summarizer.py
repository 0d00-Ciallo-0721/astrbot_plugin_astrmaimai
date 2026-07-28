# astrmai/Brain/persona_summarizer.py
import hashlib
import asyncio
import copy
import json
import time
import uuid
from typing import Dict, Any, Tuple
from astrbot.api import logger
from ...infrastructure.context_economy import PromptTemplateId
from ...infrastructure.persistence.persistence_manager import PersistenceManager
from ...infrastructure.gateway.model_gateway import GlobalModelGateway
from ...infrastructure.runtime.lane_manager import LaneKey
from ...shared.helpers.plugin_helpers import safe_create_task

class PersonaSummarizer:
    """
    人设摘要/压缩管理器 (System 2)
    职责: 将冗长的 System Prompt 压缩为高密度的核心特征与风格指南，减少 Token 消耗。
    """
    REQUIRED_SHARDS = (
        "logic_style",
        "speech_style",
        "world_view",
        "timeline",
        "relations",
        "skills",
        "values",
        "secrets",
    )
    MANUAL_CORE_FIELDS = ("summary", "first_person_rewrite", "style")
    DERIVATION_VERSION = 2
    REGENERATION_CACHE_PREFIX = "__persona_regeneration__:"

    def __init__(self, persistence: PersistenceManager, gateway: GlobalModelGateway, config=None, memory_engine=None):
        self.persistence = persistence
        self.gateway = gateway
        self.config = config if config else gateway.config
        self.memory_engine = memory_engine
        # 加载持久化缓存
        self.cache = self.persistence.load_persona_cache()
        # 运行时任务锁
        self.pending_tasks: Dict[str, asyncio.Task] = {}
        self.pending_core_tasks: Dict[str, asyncio.Task] = {}
        self.regeneration_tasks: Dict[str, asyncio.Task] = {}
        self.regeneration_jobs: Dict[str, Dict[str, Any]] = {}
        self._background_tasks: set[asyncio.Task] = set()
        self._cache_generations: Dict[str, int] = {}
        self._verified_core_hashes: Dict[str, str] = {}
        self._closed = False
        self._lock = asyncio.Lock()
        self._manual_update_lock = asyncio.Lock()
        self.prompt_registry = getattr(getattr(gateway, "context_economy", None), "templates", None)

    def refresh_config(self, config) -> None:
        self.config = config

    def _handle_background_task_result(self, task: asyncio.Task) -> None:
        self.pending_tasks = {k: v for k, v in self.pending_tasks.items() if v is not task}
        try:
            exc = task.exception()
            if exc:
                logger.error(f"[PersonaSummarizer] 后台切片任务异常: {exc}", exc_info=exc)
        except asyncio.CancelledError:
            pass

    def _generation_is_current(self, cache_key: str, generation: int) -> bool:
        return not self._closed and self._cache_generations.get(cache_key, 0) == generation

    def _start_shard_task(self, original_prompt: str, cache_key: str) -> asyncio.Task | None:
        if self._closed:
            return None
        generation = self._cache_generations.setdefault(cache_key, 0)
        task = safe_create_task(
            self._run_enrichment_until_complete(original_prompt, cache_key),
            name=f"persona-shards:{cache_key}",
            track_set=self._background_tasks,
        )
        setattr(task, "_astrmai_persona_generation", generation)
        task.add_done_callback(self._handle_background_task_result)
        self.pending_tasks[cache_key] = task
        return task

    async def _run_enrichment_until_complete(self, original_prompt: str, cache_key: str) -> None:
        initial_delay, max_delay = self._retry_delay_bounds()
        delay = initial_delay
        while not self._closed:
            try:
                try:
                    await self._generate_all_shards_background(
                        original_prompt,
                        cache_key,
                        raise_on_failure=True,
                    )
                except TypeError as exc:
                    if "raise_on_failure" not in str(exc):
                        raise
                    await self._generate_all_shards_background(original_prompt, cache_key)
                return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.warning(
                    f"[PersonaSummarizer] enrichment retry scheduled [{cache_key}] "
                    f"in {delay:.1f}s: {exc}"
                )
                await asyncio.sleep(delay)
                delay = min(max_delay, delay * 2)

    async def _invalidate_if_prompt_changed(self, cache_key: str, original_prompt: str) -> bool:
        expected_hash = self._compute_hash(original_prompt)
        stale_task = None
        stale_core_task = None
        async with self._lock:
            cached = self.cache.get(cache_key)
            if not isinstance(cached, dict):
                return False
            cached_raw = str(cached.get("raw", "") or "")
            cached_hash = str(cached.get("raw_hash", "") or "") or self._compute_hash(cached_raw)
            if cached_hash == expected_hash:
                if not cached.get("raw_hash"):
                    cached["raw_hash"] = expected_hash
                return False
            self._cache_generations[cache_key] = self._cache_generations.get(cache_key, 0) + 1
            self.cache.pop(cache_key, None)
            self._verified_core_hashes.pop(cache_key, None)
            stale_task = self.pending_tasks.pop(cache_key, None)
            stale_core_task = self.pending_core_tasks.pop(cache_key, None)
        if stale_task and not stale_task.done():
            stale_task.cancel()
            await asyncio.gather(stale_task, return_exceptions=True)
        if stale_core_task and stale_core_task is not asyncio.current_task() and not stale_core_task.done():
            stale_core_task.cancel()
            await asyncio.gather(stale_core_task, return_exceptions=True)
        memory_engine = getattr(self, "memory_engine", None)
        if memory_engine and hasattr(memory_engine, "clear_persona_lore"):
            try:
                await memory_engine.clear_persona_lore(cache_key)
            except Exception as exc:
                logger.warning(f"[PersonaSummarizer] self-lore invalidation degraded [{cache_key}]: {exc}")
        return True

    async def stop(self) -> None:
        self._closed = True
        for cache_key in list(self._cache_generations):
            self._cache_generations[cache_key] += 1
        tasks = list(
            {
                *self._background_tasks,
                *self.pending_tasks.values(),
                *self.pending_core_tasks.values(),
                *self.regeneration_tasks.values(),
            }
        )
        for task in tasks:
            if task and not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.pending_tasks.clear()
        self.pending_core_tasks.clear()
        self.regeneration_tasks.clear()
        self._background_tasks.clear()

    def reopen(self) -> None:
        """Allow the same summarizer instance to serve an explicit plugin reload."""
        self._closed = False

    def _compute_hash(self, text: str) -> str:
        """计算人设内容的 Hash 值，用于缓存 Key"""
        return hashlib.sha256(text.encode("utf-8")).hexdigest()

    def _persona_lane_key(self, cache_key: str) -> LaneKey:
        return LaneKey(subsystem="sys2", task_family="persona", scope_id=cache_key, scope_kind="global")

    async def _call_persona_lane(
        self,
        prompt: str,
        cache_key: str,
        system_prompt: str = "",
        is_json: bool = False,
        prefix_hash: str = "",
        template_envelope=None,
    ):
        return await self.gateway.call_persona_task(
            prompt=prompt,
            system_prompt=system_prompt,
            is_json=is_json,
            lane_key=self._persona_lane_key(cache_key),
            base_origin="",
            prefix_hash=prefix_hash or self._compute_hash(system_prompt or prompt[:400]),
            persona_id=cache_key,
            template_envelope=template_envelope,
        )

    def _render_persona_template(self, template_id: PromptTemplateId, *, original_prompt: str, cache_key: str):
        if self.prompt_registry is None:
            return None
        return self.prompt_registry.render_template(
            template_id,
            {
                "original_prompt": original_prompt,
                "cache_key": cache_key,
            },
        )

    async def _call_persona_template(
        self,
        template_id: PromptTemplateId,
        *,
        original_prompt: str,
        cache_key: str,
        is_json: bool = False,
        fallback_prompt: str = "",
        fallback_system_prompt: str = "",
    ):
        envelope = self._render_persona_template(
            template_id,
            original_prompt=original_prompt,
            cache_key=cache_key,
        )
        if envelope is None:
            return await self._call_persona_lane(
                fallback_prompt or original_prompt,
                cache_key,
                system_prompt=fallback_system_prompt,
                is_json=is_json,
            )
        return await self._call_persona_lane(
            envelope.prompt,
            cache_key,
            system_prompt=envelope.system_prompt,
            is_json=is_json,
            template_envelope=envelope,
        )

    def _component_max_retries(self) -> int:
        persona_config = getattr(self.config, "persona", None)
        return max(1, int(getattr(persona_config, "component_max_retries", 3) or 3))

    def _retry_delay_bounds(self) -> tuple[float, float]:
        persona_config = getattr(self.config, "persona", None)
        initial = max(0.1, float(getattr(persona_config, "retry_interval_sec", 15.0) or 15.0))
        maximum = max(initial, float(getattr(persona_config, "retry_max_interval_sec", 300.0) or 300.0))
        return initial, maximum

    async def _persist_cache(self, *, strict: bool = False) -> None:
        if self._closed:
            return
        snapshot = copy.deepcopy(self.cache)
        if strict and hasattr(self.persistence, "save_persona_cache_strict_async"):
            await self.persistence.save_persona_cache_strict_async(snapshot)
        elif strict and hasattr(self.persistence, "save_persona_cache_strict"):
            await asyncio.to_thread(self.persistence.save_persona_cache_strict, snapshot)
        elif hasattr(self.persistence, "save_persona_cache_async"):
            result = await self.persistence.save_persona_cache_async(snapshot)
            if strict and result is False:
                raise OSError("persona cache persistence failed")
        else:
            result = self.persistence.save_persona_cache(snapshot)
            if strict and result is False:
                raise OSError("persona cache persistence failed")

    def _cache_key(self, persona_id: str, session_id: str) -> str:
        normalized = str(persona_id or "").strip()
        return normalized or f"session_{session_id}"

    @classmethod
    def _manual_generated_snapshot(cls, payload: Dict[str, Any]) -> Dict[str, Any]:
        shards = payload.get("shards", {}) if isinstance(payload.get("shards", {}), dict) else {}
        return {
            **{field: str(payload.get(field, "") or "") for field in cls.MANUAL_CORE_FIELDS},
            "shards": {name: str(shards.get(name, "") or "") for name in cls.REQUIRED_SHARDS},
        }

    def _refresh_manual_readiness(self, payload: Dict[str, Any]) -> bool:
        core_components = payload.get("core_components", {})
        if not isinstance(core_components, dict):
            core_components = {}
            payload["core_components"] = core_components
        core_ready = all(
            core_components.get(field) == "completed" and bool(str(payload.get(field, "") or "").strip())
            for field in self.MANUAL_CORE_FIELDS
        )
        payload["core_ready"] = core_ready

        shards = payload.get("shards", {})
        shard_status = payload.get("shard_status", {})
        if not isinstance(shards, dict):
            shards = {}
            payload["shards"] = shards
        if not isinstance(shard_status, dict):
            shard_status = {}
            payload["shard_status"] = shard_status
        shards_ready = bool(payload.get("shards_not_required", False)) or all(
            shard_status.get(name) == "completed" and bool(str(shards.get(name, "") or "").strip())
            for name in self.REQUIRED_SHARDS
        )
        include_self_lore = bool(
            getattr(getattr(self.config, "persona", None), "include_self_lore_in_prompt", False)
        )
        self_lore_ready = not include_self_lore or bool(payload.get("self_lore_ready", False))
        payload["is_full_ready"] = bool(core_ready and shards_ready and self_lore_ready)
        payload["persona_state"] = "full_ready" if payload["is_full_ready"] else ("core_ready" if core_ready else "enriching")
        return bool(core_ready and not payload["is_full_ready"] and not payload.get("shards_not_required", False))

    async def _cancel_shard_task_for_manual_update(self, cache_key: str) -> None:
        stale_task = None
        async with self._lock:
            core_task = self.pending_core_tasks.get(cache_key)
            if core_task is not None and not core_task.done():
                raise RuntimeError("persona core is still being generated")
            self._cache_generations[cache_key] = self._cache_generations.get(cache_key, 0) + 1
            stale_task = self.pending_tasks.pop(cache_key, None)
        if stale_task is not None and not stale_task.done():
            stale_task.cancel()
            await asyncio.gather(stale_task, return_exceptions=True)

    @staticmethod
    def _check_manual_timestamp(payload: Dict[str, Any], expected_timestamp: float | None) -> None:
        if expected_timestamp is None:
            return
        current = float(payload.get("timestamp", 0.0) or 0.0)
        if abs(current - float(expected_timestamp)) > 1e-6:
            raise RuntimeError("persona cache changed; reload before saving")

    async def apply_manual_overrides(
        self,
        cache_key: str,
        changes: Dict[str, Any],
        *,
        expected_timestamp: float | None = None,
    ) -> Dict[str, Any]:
        clean_key = str(cache_key or "").strip()
        if not clean_key:
            raise ValueError("persona cache key is required")
        async with self._manual_update_lock:
            async with self._lock:
                payload = self.cache.get(clean_key)
                if not isinstance(payload, dict):
                    raise ValueError("persona cache was not found")
                self._check_manual_timestamp(payload, expected_timestamp)
            await self._cancel_shard_task_for_manual_update(clean_key)

            resume_enrichment = False
            original_prompt = ""
            async with self._lock:
                payload = self.cache.get(clean_key)
                if not isinstance(payload, dict):
                    raise ValueError("persona cache was not found")
                self._check_manual_timestamp(payload, expected_timestamp)
                payload.setdefault("generated_baseline", self._manual_generated_snapshot(payload))
                overrides = payload.get("manual_overrides", {})
                if not isinstance(overrides, dict):
                    overrides = {}
                now = time.time()
                core_components = payload.setdefault("core_components", {})
                for field in self.MANUAL_CORE_FIELDS:
                    if field not in changes:
                        continue
                    payload[field] = str(changes[field])
                    core_components[field] = "completed"
                    overrides[field] = {"source": "plugin_page", "updated_at": now}

                changed_shards = changes.get("shards", {})
                shards = payload.setdefault("shards", {})
                shard_status = payload.setdefault("shard_status", {})
                for name, value in changed_shards.items():
                    shards[name] = str(value)
                    shard_status[name] = "completed"
                    overrides[f"shards.{name}"] = {"source": "plugin_page", "updated_at": now}

                payload["manual_overrides"] = overrides
                payload["manual_revision"] = int(payload.get("manual_revision", 0) or 0) + 1
                payload["manual_updated_at"] = now
                payload["timestamp"] = now
                original_prompt = str(payload.get("raw", "") or "")
                resume_enrichment = self._refresh_manual_readiness(payload)
                await self._persist_cache(strict=True)
                result = copy.deepcopy(payload)

            if resume_enrichment and original_prompt and not self._closed:
                self._start_shard_task(original_prompt, clean_key)
            return result

    async def restore_manual_overrides(
        self,
        cache_key: str,
        fields: list[str] | None = None,
        *,
        expected_timestamp: float | None = None,
    ) -> Dict[str, Any]:
        clean_key = str(cache_key or "").strip()
        if not clean_key:
            raise ValueError("persona cache key is required")
        async with self._manual_update_lock:
            async with self._lock:
                payload = self.cache.get(clean_key)
                if not isinstance(payload, dict):
                    raise ValueError("persona cache was not found")
                self._check_manual_timestamp(payload, expected_timestamp)
            await self._cancel_shard_task_for_manual_update(clean_key)

            resume_enrichment = False
            original_prompt = ""
            async with self._lock:
                payload = self.cache.get(clean_key)
                if isinstance(payload, dict):
                    self._check_manual_timestamp(payload, expected_timestamp)
                baseline = payload.get("generated_baseline", {}) if isinstance(payload, dict) else {}
                overrides = payload.get("manual_overrides", {}) if isinstance(payload, dict) else {}
                if not isinstance(payload, dict) or not isinstance(baseline, dict) or not isinstance(overrides, dict):
                    raise ValueError("no generated persona baseline is available")
                requested = [str(item or "").strip() for item in (fields or list(overrides)) if str(item or "").strip()]
                if not requested:
                    raise ValueError("no manual persona fields were selected")

                core_components = payload.setdefault("core_components", {})
                shards = payload.setdefault("shards", {})
                shard_status = payload.setdefault("shard_status", {})
                baseline_shards = baseline.get("shards", {}) if isinstance(baseline.get("shards", {}), dict) else {}
                for field in requested:
                    if field in self.MANUAL_CORE_FIELDS and field in baseline:
                        payload[field] = str(baseline.get(field, "") or "")
                        core_components[field] = "completed" if str(payload[field]).strip() else "pending"
                        overrides.pop(field, None)
                    elif field.startswith("shards."):
                        name = field.split(".", 1)[1]
                        if name not in self.REQUIRED_SHARDS:
                            continue
                        restored = str(baseline_shards.get(name, "") or "")
                        if restored:
                            shards[name] = restored
                            shard_status[name] = "completed"
                        else:
                            shards.pop(name, None)
                            shard_status.pop(name, None)
                        overrides.pop(field, None)

                now = time.time()
                payload["manual_overrides"] = overrides
                if not overrides:
                    payload.pop("generated_baseline", None)
                payload["manual_revision"] = int(payload.get("manual_revision", 0) or 0) + 1
                payload["manual_updated_at"] = now
                payload["timestamp"] = now
                original_prompt = str(payload.get("raw", "") or "")
                resume_enrichment = self._refresh_manual_readiness(payload)
                await self._persist_cache(strict=True)
                result = copy.deepcopy(payload)

            if resume_enrichment and original_prompt and not self._closed:
                self._start_shard_task(original_prompt, clean_key)
            return result

    def _core_cache_is_ready(self, payload: Dict[str, Any], original_prompt: str) -> bool:
        if not isinstance(payload, dict):
            return False
        if str(payload.get("raw_hash", "") or "") != self._compute_hash(original_prompt):
            return False
        components = payload.get("core_components", {})
        if not isinstance(components, dict) or any(
            components.get(name) != "completed" for name in ("summary", "style", "first_person_rewrite")
        ):
            return False
        if not original_prompt:
            return bool(str(payload.get("style", "") or "").strip())
        return all(
            str(payload.get(name, "") or "").strip()
            for name in ("summary", "style", "first_person_rewrite")
        ) and str(payload.get("style", "") or "").strip() != "数据解析中..."

    async def _verify_persisted_core(self, cache_key: str, original_prompt: str) -> None:
        loader = getattr(self.persistence, "load_persona_cache_async", None)
        if callable(loader):
            persisted = await loader()
        else:
            persisted = await asyncio.to_thread(self.persistence.load_persona_cache)
        payload = persisted.get(cache_key) if isinstance(persisted, dict) else None
        if not self._core_cache_is_ready(payload, original_prompt):
            raise OSError(f"persona core cache verification failed: {cache_key}")
        self._verified_core_hashes[cache_key] = self._compute_hash(original_prompt)

    def _full_cache_is_ready(
        self,
        payload: Dict[str, Any],
        original_prompt: str,
        *,
        include_self_lore: bool,
    ) -> bool:
        if not self._core_cache_is_ready(payload, original_prompt) or not payload.get("is_full_ready", False):
            return False
        if include_self_lore and not payload.get("self_lore_ready", False):
            return False
        if payload.get("shards_not_required", False):
            return True
        shards = payload.get("shards", {})
        shard_status = payload.get("shard_status", {})
        if not isinstance(shards, dict) or not isinstance(shard_status, dict):
            return False
        return all(
            shard_status.get(name) == "completed" and bool(str(shards.get(name, "") or "").strip())
            for name in self.REQUIRED_SHARDS
        )

    async def _verify_persisted_full(
        self,
        cache_key: str,
        original_prompt: str,
        *,
        include_self_lore: bool,
    ) -> None:
        loader = getattr(self.persistence, "load_persona_cache_async", None)
        if callable(loader):
            persisted = await loader()
        else:
            persisted = await asyncio.to_thread(self.persistence.load_persona_cache)
        payload = persisted.get(cache_key) if isinstance(persisted, dict) else None
        if not self._full_cache_is_ready(
            payload,
            original_prompt,
            include_self_lore=include_self_lore,
        ):
            raise OSError(f"persona full cache verification failed: {cache_key}")

    async def _build_first_person_rewrite(
        self,
        *,
        original_prompt: str,
        summary: str,
        style: str,
        cache_key: str,
        max_retries: int | None = None,
        raise_on_failure: bool = False,
    ) -> str:
        base_summary = str(summary or original_prompt or "").strip()
        if not base_summary:
            return ""
        prompt = f"""
Rewrite the persona summary below as a short first-person self-awareness note.

[Original Persona]
{original_prompt}

[Summary]
{summary}

[Style]
{style}

Rules:
- Use first person voice.
- Keep it natural and compact.
- Preserve the distinction between the default interlocutor address, conditional relationship addresses,
  and names used only for specific people; do not turn a conditional relationship into the identity of every user.
- Do not mention prompts, AI, tools, or system instructions.
- Output plain text only, within 120 characters if possible.
"""
        attempts = max(1, int(max_retries or self._component_max_retries()))
        last_error: Exception | None = None
        for attempt in range(attempts):
            try:
                if self.prompt_registry is not None:
                    envelope = self.prompt_registry.render_template(
                        PromptTemplateId.PERSONA_FIRST_PERSON_REWRITE,
                        {
                            "original_prompt": original_prompt,
                            "summary": summary,
                            "style": style,
                        },
                    )
                    res = await self._call_persona_lane(
                        envelope.prompt,
                        cache_key,
                        system_prompt=envelope.system_prompt,
                        is_json=False,
                        template_envelope=envelope,
                    )
                else:
                    res = await self._call_persona_lane(
                        prompt,
                        cache_key,
                        system_prompt="Rewrite persona summaries into concise first-person self-awareness text.",
                        is_json=False,
                    )
                rewritten = str(res or "").strip()
                if len(rewritten) >= 4:
                    return rewritten
                last_error = ValueError("first-person rewrite result is too short")
            except Exception as exc:
                last_error = exc
            logger.warning(
                f"[PersonaSummarizer] first-person rewrite failed [{cache_key}] "
                f"({attempt + 1}/{attempts}): {last_error}"
            )
            if attempt + 1 < attempts:
                await asyncio.sleep(1.5)
        if raise_on_failure:
            raise RuntimeError(f"first-person rewrite failed for {cache_key}") from last_error
        return ""

    async def _checkpoint_core_component(
        self,
        cache_key: str,
        original_prompt: str,
        component: str,
        value: str,
    ) -> None:
        async with self._lock:
            payload = self.cache.setdefault(cache_key, {})
            payload.update(
                {
                    "raw": original_prompt,
                    "raw_hash": self._compute_hash(original_prompt),
                    "timestamp": time.time(),
                    "core_ready": False,
                    "is_full_ready": False,
                }
            )
            payload.setdefault("summary", "")
            payload.setdefault("style", "数据解析中...")
            payload.setdefault("first_person_rewrite", "")
            payload.setdefault("shards", {})
            payload.setdefault("shard_status", {})
            payload.setdefault("core_components", {})
            payload[component] = value
            payload["core_components"][component] = "completed"
            await self._persist_cache(strict=True)

    async def _initialize_core(
        self,
        original_prompt: str,
        cache_key: str,
        *,
        force_compression: bool = False,
    ) -> Dict[str, Any]:
        threshold = int(getattr(getattr(self.config, "performance", None), "summary_threshold", 300) or 300)
        if not force_compression and (not original_prompt or len(original_prompt) < threshold):
            include_self_lore = bool(
                getattr(getattr(self.config, "persona", None), "include_self_lore_in_prompt", False)
            )
            async with self._lock:
                self.cache[cache_key] = {
                    "summary": original_prompt,
                    "first_person_rewrite": original_prompt,
                    "style": "保持原始风格",
                    "shards": {},
                    "shard_status": {},
                    "core_components": {
                        "summary": "completed",
                        "style": "completed",
                        "first_person_rewrite": "completed",
                    },
                    "core_ready": True,
                    "persona_state": "core_ready",
                    "is_full_ready": not include_self_lore,
                    "self_lore_ready": not include_self_lore,
                    "shards_not_required": True,
                    "raw": original_prompt,
                    "raw_hash": self._compute_hash(original_prompt),
                    "timestamp": time.time(),
                }
                await self._persist_cache(strict=True)
            await self._verify_persisted_core(cache_key, original_prompt)
            return dict(self.cache[cache_key])

        payload = self.cache.get(cache_key, {})
        components = payload.get("core_components", {}) if isinstance(payload, dict) else {}
        retries = self._component_max_retries()
        legacy_summary = str(payload.get("summary", "") or "").strip()
        if components.get("summary") == "completed" and legacy_summary:
            summary = str(payload["summary"])
        elif legacy_summary and not legacy_summary.startswith("[系统降级提取]"):
            summary = legacy_summary
            await self._checkpoint_core_component(cache_key, original_prompt, "summary", summary)
        else:
            try:
                summary = await self._summarize_core_identity_with_retry(
                    original_prompt,
                    cache_key,
                    max_retries=retries,
                    raise_on_failure=True,
                )
            except TypeError as exc:
                if "max_retries" not in str(exc) and "raise_on_failure" not in str(exc):
                    raise
                summary = await self._summarize_core_identity_with_retry(original_prompt, cache_key)
            await self._checkpoint_core_component(cache_key, original_prompt, "summary", summary)

        payload = self.cache.get(cache_key, {})
        components = payload.get("core_components", {}) if isinstance(payload, dict) else {}
        legacy_style = str(payload.get("style", "") or "").strip()
        if components.get("style") == "completed" and legacy_style not in {"", "数据解析中..."}:
            style = str(payload["style"])
        elif legacy_style not in {"", "数据解析中..."}:
            style = legacy_style
            await self._checkpoint_core_component(cache_key, original_prompt, "style", style)
        else:
            try:
                style = await self._summarize_style_with_retry(
                    original_prompt,
                    cache_key,
                    max_retries=retries,
                    raise_on_failure=True,
                )
            except TypeError as exc:
                if "max_retries" not in str(exc) and "raise_on_failure" not in str(exc):
                    raise
                style = await self._summarize_style_with_retry(original_prompt, cache_key)
            await self._checkpoint_core_component(cache_key, original_prompt, "style", style)

        payload = self.cache.get(cache_key, {})
        components = payload.get("core_components", {}) if isinstance(payload, dict) else {}
        legacy_rewrite = str(payload.get("first_person_rewrite", "") or "").strip()
        if components.get("first_person_rewrite") == "completed" and legacy_rewrite:
            first_person_rewrite = str(payload["first_person_rewrite"])
        elif legacy_rewrite and legacy_rewrite != summary:
            first_person_rewrite = legacy_rewrite
            await self._checkpoint_core_component(
                cache_key,
                original_prompt,
                "first_person_rewrite",
                first_person_rewrite,
            )
        else:
            first_person_rewrite = await self._build_first_person_rewrite(
                original_prompt=original_prompt,
                summary=summary,
                style=style,
                cache_key=cache_key,
                max_retries=retries,
                raise_on_failure=True,
            )
            await self._checkpoint_core_component(
                cache_key,
                original_prompt,
                "first_person_rewrite",
                first_person_rewrite,
            )

        async with self._lock:
            self.cache[cache_key]["core_ready"] = True
            self.cache[cache_key]["persona_state"] = "core_ready"
            self.cache[cache_key]["core_completed_at"] = time.time()
            await self._persist_cache(strict=True)
        await self._verify_persisted_core(cache_key, original_prompt)
        return dict(self.cache[cache_key])

    async def ensure_core_ready(
        self,
        original_prompt: str,
        persona_id: str = "",
        session_id: str = "global",
    ) -> Dict[str, Any]:
        if self._closed:
            raise RuntimeError("persona summarizer is closed")
        cache_key = self._cache_key(persona_id, session_id)
        await self._invalidate_if_prompt_changed(cache_key, original_prompt)
        cached = self.cache.get(cache_key)
        if self._core_cache_is_ready(cached, original_prompt):
            include_self_lore = bool(
                getattr(getattr(self.config, "persona", None), "include_self_lore_in_prompt", False)
            )
            if cached.get("is_full_ready", False) and not self._full_cache_is_ready(
                cached,
                original_prompt,
                include_self_lore=include_self_lore,
            ):
                async with self._lock:
                    cached["is_full_ready"] = False
                    cached["persona_state"] = "core_ready"
                    await self._persist_cache(strict=True)
            expected_hash = self._compute_hash(original_prompt)
            if self._verified_core_hashes.get(cache_key) != expected_hash:
                await self._verify_persisted_core(cache_key, original_prompt)
            return dict(cached)

        async with self._lock:
            task = self.pending_core_tasks.get(cache_key)
            if task is None or task.done():
                task = safe_create_task(
                    self._initialize_core(original_prompt, cache_key),
                    name=f"persona-core:{cache_key}",
                    track_set=self._background_tasks,
                )
                self.pending_core_tasks[cache_key] = task
        try:
            return await task
        finally:
            if self.pending_core_tasks.get(cache_key) is task and task.done():
                self.pending_core_tasks.pop(cache_key, None)

    async def get_summary(self, original_prompt: str, persona_id: str = "", session_id: str = "global") -> Dict[str, Any]:
        cache_key = self._cache_key(persona_id, session_id)
        try:
            payload = await self.ensure_core_ready(original_prompt, persona_id=persona_id, session_id=session_id)
        except Exception as exc:
            logger.warning(f"[PersonaSummarizer] core persona unavailable [{cache_key}]: {exc}")
            cached = self.cache.get(cache_key, {})
            return {
                "summary": str(cached.get("summary", "") or original_prompt or ""),
                "first_person_rewrite": str(
                    cached.get("first_person_rewrite", "") or cached.get("summary", "") or original_prompt or ""
                ),
                "style": str(cached.get("style", "") or "保持原始风格"),
                "shards": dict(cached.get("shards", {}) or {}),
                "is_full_ready": False,
                "core_ready": False,
                "raw": original_prompt,
                "raw_hash": self._compute_hash(original_prompt),
                "timestamp": time.time(),
            }

        if not payload.get("is_full_ready", False) and cache_key not in self.pending_tasks:
            self._start_shard_task(original_prompt, cache_key)
        return dict(payload)

# [新增] 核心后台调度器：全维度切片提取引擎
    async def _generate_all_shards_background(
        self,
        original_prompt: str,
        cache_key: str,
        generation: int | None = None,
        raise_on_failure: bool = False,
        skip_self_lore: bool = False,
    ):
        """
        后台静默提取 8 大维度切片任务。
        采用顺序 await 执行以保护 LLM API 并发配额，完成后自动更新挂起状态。
        """
        logger.info(f"[PersonaSummarizer] 🚀 开始后台静默提取 [{cache_key}] 的全维度人格切片...")
        current_task = asyncio.current_task()
        task_generation = getattr(current_task, "_astrmai_persona_generation", None)
        active_generation = (
            self._cache_generations.get(cache_key, 0)
            if generation is None and task_generation is None
            else int(task_generation if generation is None else generation)
        )
        if not self._generation_is_current(cache_key, active_generation):
            return
        
        # ==========================================
        # 🟢 [Phase 8] 触发原典清洗与向量化重铸
        # ==========================================
        try:
            include_self_lore = not skip_self_lore and bool(
                getattr(getattr(self.config, "persona", None), "include_self_lore_in_prompt", False)
            )
            cached_payload = self.cache.get(cache_key, {})
            if include_self_lore and not bool(cached_payload.get("self_lore_ready", False)):
                if getattr(self, 'memory_engine', None):
                    logger.info(f"[PersonaSummarizer] 🧹 检测到人设重建，准备清空旧版并重铸 {cache_key} 的潜意识原典...")
                    await self.memory_engine.clear_persona_lore(cache_key)
                    if not self._generation_is_current(cache_key, active_generation):
                        return
                    lore_id = await self.memory_engine.add_persona_lore(original_prompt, cache_key)
                    if not str(lore_id or "").strip():
                        raise RuntimeError("self-lore write returned an empty memory id")
                    async with self._lock:
                        if self._generation_is_current(cache_key, active_generation) and cache_key in self.cache:
                            self.cache[cache_key]["self_lore_ready"] = True
                            await self._persist_cache(strict=True)
                else:
                    raise RuntimeError("memory_engine is unavailable for self-lore initialization")
            elif not include_self_lore:
                async with self._lock:
                    if cache_key in self.cache:
                        self.cache[cache_key]["self_lore_ready"] = True
                        await self._persist_cache(strict=True)
        except Exception as e:
            logger.error(f"[PersonaSummarizer] ⚠️ 潜意识原典重铸失败 (防宕机隔离): {e}")
            if raise_on_failure:
                raise

        try:
            cached_payload = self.cache.get(cache_key, {})
            shards = dict(cached_payload.get("shards", {}) or {})
            shard_status = dict(cached_payload.get("shard_status", {}) or {})
            for shard_name in self.REQUIRED_SHARDS:
                if str(shards.get(shard_name, "") or "").strip():
                    shard_status.setdefault(shard_name, "completed")
            # 顺序调用 8 大维度切片提取 (依赖下方的具体子函数)
            shard_builders = () if bool(cached_payload.get("shards_not_required", False)) else (
                ("logic_style", self._summarize_logic_style),
                ("speech_style", self._summarize_speech_style),
                ("world_view", self._summarize_world_view),
                ("timeline", self._summarize_timeline),
                ("relations", self._summarize_relations),
                ("skills", self._summarize_skills),
                ("values", self._summarize_values),
                ("secrets", self._summarize_secrets),
            )
            for shard_name, builder in shard_builders:
                if not self._generation_is_current(cache_key, active_generation):
                    return
                if shard_status.get(shard_name) == "completed" and shard_name in shards:
                    continue
                try:
                    value = await builder(original_prompt, cache_key, raise_on_failure=True)
                except TypeError as exc:
                    if "raise_on_failure" not in str(exc):
                        raise
                    value = await builder(original_prompt, cache_key)
                normalized_value = str(value or "").strip()
                if not normalized_value:
                    raise ValueError(f"persona shard returned empty content: {shard_name}")
                shards[shard_name] = normalized_value
                shard_status[shard_name] = "completed"
                async with self._lock:
                    if not self._generation_is_current(cache_key, active_generation) or cache_key not in self.cache:
                        return
                    self.cache[cache_key]["shards"] = dict(shards)
                    self.cache[cache_key]["shard_status"] = dict(shard_status)
                    self.cache[cache_key]["is_full_ready"] = False
                    await self._persist_cache(strict=True)

            # 获取原子锁，安全写回内存并解除失忆状态
            async with self._lock:
                if self._generation_is_current(cache_key, active_generation) and cache_key in self.cache:
                    shards_not_required = bool(self.cache[cache_key].get("shards_not_required", False))
                    if not shards_not_required and any(
                        shard_status.get(name) != "completed" for name in self.REQUIRED_SHARDS
                    ):
                        raise RuntimeError("persona shard set is incomplete")
                    if include_self_lore and not bool(self.cache[cache_key].get("self_lore_ready", False)):
                        raise RuntimeError("persona self-lore is incomplete")
                    self.cache[cache_key]["shards"] = dict(shards)
                    self.cache[cache_key]["shard_status"] = dict(shard_status)
                    self.cache[cache_key]["is_full_ready"] = True
                    self.cache[cache_key]["persona_state"] = "full_ready"
                    self.cache[cache_key]["full_completed_at"] = time.time()
                    await self._persist_cache(strict=True)

            try:
                await self._verify_persisted_full(
                    cache_key,
                    original_prompt,
                    include_self_lore=include_self_lore,
                )
            except Exception:
                async with self._lock:
                    if cache_key in self.cache:
                        self.cache[cache_key]["is_full_ready"] = False
                        self.cache[cache_key]["persona_state"] = "enriching"
                raise

            logger.info(f"[PersonaSummarizer] ✅ [{cache_key}] 的 8 大维度人格切片已全部提取并组装完毕，角色完全降临！")
            
        except asyncio.CancelledError:
            logger.warning(f"[PersonaSummarizer] ⚠️ [{cache_key}] 的后台切片任务被系统强行终止。")
            raise
        except Exception as e:
            logger.error(f"[PersonaSummarizer] ❌ [{cache_key}] 的切片任务发生严重异常: {e}")
            if raise_on_failure:
                raise
        finally:
            # 无论成功失败，必须从任务挂起池中安全注销自己，防止内存泄漏和僵尸任务
            if not raise_on_failure:
                current_task = asyncio.current_task()
                pending_task = self.pending_tasks.get(cache_key)
                if pending_task is current_task or not isinstance(pending_task, asyncio.Task):
                    self.pending_tasks.pop(cache_key, None)

    def get_regeneration_status(self, cache_key: str) -> Dict[str, Any]:
        clean_key = str(cache_key or "").strip()
        job = self.regeneration_jobs.get(clean_key)
        if not isinstance(job, dict):
            return {
                "state": "idle",
                "cache_key": clean_key,
                "derivation_version": self.DERIVATION_VERSION,
                "completed_components": 0,
                "total_components": 11,
            }
        staging_key = str(job.get("_staging_key", "") or "")
        staging = self.cache.get(staging_key, {}) if staging_key else {}
        core_status = staging.get("core_components", {}) if isinstance(staging, dict) else {}
        shard_status = staging.get("shard_status", {}) if isinstance(staging, dict) else {}
        core_completed = sum(
            1 for name in self.MANUAL_CORE_FIELDS if core_status.get(name) == "completed"
        )
        shard_completed = sum(
            1 for name in self.REQUIRED_SHARDS if shard_status.get(name) == "completed"
        )
        completed_components = max(
            int(job.get("completed_components", 0) or 0),
            core_completed + shard_completed,
        )
        return {
            "job_id": str(job.get("job_id", "") or ""),
            "cache_key": clean_key,
            "state": str(job.get("state", "idle") or "idle"),
            "stage": str(job.get("stage", "") or ""),
            "completed_components": min(11, completed_components),
            "total_components": 11,
            "started_at": float(job.get("started_at", 0.0) or 0.0),
            "finished_at": float(job.get("finished_at", 0.0) or 0.0),
            "error": str(job.get("error", "") or ""),
            "failed_component": str(job.get("failed_component", "") or ""),
            "derivation_version": self.DERIVATION_VERSION,
            "clears_manual_overrides": bool(job.get("clear_manual_overrides", True)),
            "self_lore_preserved": bool(job.get("self_lore_preserved", True)),
        }

    async def start_regeneration(
        self,
        cache_key: str,
        *,
        expected_timestamp: float | None = None,
        clear_manual_overrides: bool = True,
        idempotency_key: str = "",
    ) -> Dict[str, Any]:
        clean_key = str(cache_key or "").strip()
        if not clean_key:
            raise ValueError("persona cache key is required")
        if self._closed:
            raise RuntimeError("persona summarizer is closed")

        async with self._manual_update_lock:
            async with self._lock:
                payload = self.cache.get(clean_key)
                if not isinstance(payload, dict):
                    raise ValueError("persona cache was not found")
                self._check_manual_timestamp(payload, expected_timestamp)
                original_prompt = str(payload.get("raw", "") or "").strip()
                if not original_prompt:
                    raise ValueError("original persona prompt is unavailable")
                previous_job = self.regeneration_jobs.get(clean_key)
                requested_job_id = str(idempotency_key or "").strip()
                if (
                    requested_job_id
                    and isinstance(previous_job, dict)
                    and str(previous_job.get("job_id", "") or "") == requested_job_id
                ):
                    return self.get_regeneration_status(clean_key)
                active_task = self.regeneration_tasks.get(clean_key)
                if active_task is not None and not active_task.done():
                    return self.get_regeneration_status(clean_key)
                original_timestamp = float(payload.get("timestamp", 0.0) or 0.0)

            await self._cancel_shard_task_for_manual_update(clean_key)
            job_id = str(idempotency_key or "").strip() or uuid.uuid4().hex
            staging_key = f"{self.REGENERATION_CACHE_PREFIX}{job_id}"
            self.regeneration_jobs[clean_key] = {
                "job_id": job_id,
                "state": "queued",
                "stage": "queued",
                "started_at": time.time(),
                "finished_at": 0.0,
                "error": "",
                "failed_component": "",
                "completed_components": 0,
                "clear_manual_overrides": bool(clear_manual_overrides),
                "self_lore_preserved": True,
                "_staging_key": staging_key,
                "_original_timestamp": original_timestamp,
            }
            task = safe_create_task(
                self._run_full_regeneration(
                    clean_key,
                    staging_key,
                    original_prompt,
                    original_timestamp=original_timestamp,
                    clear_manual_overrides=bool(clear_manual_overrides),
                ),
                name=f"persona-regeneration:{clean_key}",
                track_set=self._background_tasks,
            )
            self.regeneration_tasks[clean_key] = task
            task.add_done_callback(
                lambda completed, key=clean_key: self._handle_regeneration_task_result(key, completed)
            )
            return self.get_regeneration_status(clean_key)

    def _handle_regeneration_task_result(self, cache_key: str, task: asyncio.Task) -> None:
        if self.regeneration_tasks.get(cache_key) is task:
            self.regeneration_tasks.pop(cache_key, None)
        try:
            exc = task.exception()
            if exc:
                logger.error(
                    f"[PersonaSummarizer] full regeneration task failed [{cache_key}]: {exc}",
                    exc_info=exc,
                )
        except asyncio.CancelledError:
            pass

    async def _run_full_regeneration(
        self,
        cache_key: str,
        staging_key: str,
        original_prompt: str,
        *,
        original_timestamp: float,
        clear_manual_overrides: bool,
    ) -> None:
        job = self.regeneration_jobs[cache_key]
        job["state"] = "running"
        job["stage"] = "core"
        old_payload = copy.deepcopy(self.cache.get(cache_key, {}))
        try:
            self._cache_generations.setdefault(staging_key, 0)
            await self._initialize_core(
                original_prompt,
                staging_key,
                force_compression=True,
            )
            job["stage"] = "shards"
            await self._generate_all_shards_background(
                original_prompt,
                staging_key,
                raise_on_failure=True,
                skip_self_lore=True,
            )
            staging_payload = copy.deepcopy(self.cache.get(staging_key, {}))
            if not self._full_cache_is_ready(
                staging_payload,
                original_prompt,
                include_self_lore=False,
            ):
                raise RuntimeError("regenerated persona cache is incomplete")

            include_self_lore = bool(
                getattr(
                    getattr(self.config, "persona", None),
                    "include_self_lore_in_prompt",
                    False,
                )
            )
            self_lore_ready = bool(old_payload.get("self_lore_ready", False))
            if include_self_lore and not self_lore_ready:
                job["stage"] = "self_lore"
                if self.memory_engine is None or not hasattr(self.memory_engine, "add_persona_lore"):
                    raise RuntimeError("memory_engine is unavailable for self-lore initialization")
                lore_id = await self.memory_engine.add_persona_lore(original_prompt, cache_key)
                if not str(lore_id or "").strip():
                    raise RuntimeError("self-lore write returned an empty memory id")
                self_lore_ready = True

            job["stage"] = "commit"
            async with self._manual_update_lock:
                async with self._lock:
                    current = self.cache.get(cache_key)
                    if not isinstance(current, dict):
                        raise RuntimeError("persona cache disappeared during regeneration")
                    self._check_manual_timestamp(current, original_timestamp)
                    staging_payload["self_lore_ready"] = (
                        not include_self_lore or self_lore_ready
                    )
                    staging_payload["is_full_ready"] = bool(staging_payload["self_lore_ready"])
                    staging_payload["persona_state"] = (
                        "full_ready" if staging_payload["is_full_ready"] else "core_ready"
                    )
                    staging_payload["derivation_version"] = self.DERIVATION_VERSION
                    staging_payload["regenerated_at"] = time.time()
                    staging_payload["timestamp"] = time.time()
                    staging_payload["manual_revision"] = int(
                        current.get("manual_revision", 0) or 0
                    ) + (1 if clear_manual_overrides else 0)
                    if clear_manual_overrides:
                        staging_payload.pop("manual_overrides", None)
                        staging_payload.pop("generated_baseline", None)
                        staging_payload.pop("manual_updated_at", None)
                    else:
                        overrides = current.get("manual_overrides", {})
                        if isinstance(overrides, dict) and overrides:
                            staging_payload["generated_baseline"] = self._manual_generated_snapshot(
                                staging_payload
                            )
                            current_shards = current.get("shards", {})
                            for field in overrides:
                                if field in self.MANUAL_CORE_FIELDS:
                                    staging_payload[field] = str(current.get(field, "") or "")
                                elif field.startswith("shards.") and isinstance(current_shards, dict):
                                    shard_name = field.split(".", 1)[1]
                                    if shard_name in self.REQUIRED_SHARDS:
                                        staging_payload["shards"][shard_name] = str(
                                            current_shards.get(shard_name, "") or ""
                                        )
                            staging_payload["manual_overrides"] = copy.deepcopy(overrides)
                            if "manual_updated_at" in current:
                                staging_payload["manual_updated_at"] = current["manual_updated_at"]
                            self._refresh_manual_readiness(staging_payload)
                    self.cache[cache_key] = staging_payload
                    self.cache.pop(staging_key, None)
                    try:
                        await self._persist_cache(strict=True)
                    except Exception:
                        self.cache[cache_key] = old_payload
                        self.cache.pop(staging_key, None)
                        try:
                            await self._persist_cache(strict=False)
                        except Exception as rollback_exc:
                            logger.error(
                                f"[PersonaSummarizer] persona regeneration rollback persistence "
                                f"failed [{cache_key}]: {rollback_exc}"
                            )
                        raise

            job["state"] = "completed"
            job["stage"] = "completed"
            job["completed_components"] = 11
            job["finished_at"] = time.time()
            logger.info(
                f"[PersonaSummarizer] full persona regeneration completed "
                f"[{cache_key}] version={self.DERIVATION_VERSION}"
            )
        except asyncio.CancelledError:
            job["state"] = "cancelled"
            job["stage"] = "cancelled"
            job["finished_at"] = time.time()
            raise
        except Exception as exc:
            failed_stage = str(job.get("stage", "") or "unknown")
            job["state"] = "failed"
            job["stage"] = "failed"
            job["failed_component"] = failed_stage
            job["error"] = str(exc).replace(original_prompt, "[persona-redacted]")[:500]
            job["finished_at"] = time.time()
            logger.warning(f"[PersonaSummarizer] full persona regeneration failed [{cache_key}]: {exc}")
        finally:
            async with self._lock:
                if staging_key in self.cache:
                    self.cache.pop(staging_key, None)
                    try:
                        await self._persist_cache(strict=False)
                    except Exception as cleanup_exc:
                        logger.error(
                            f"[PersonaSummarizer] persona regeneration staging cleanup "
                            f"persistence failed [{cache_key}]: {cleanup_exc}"
                        )
            self._cache_generations.pop(staging_key, None)
            self._verified_core_hashes.pop(staging_key, None)

# [修改] 替换 call_judge 为 call_persona_task
    async def _summarize_core_identity_with_retry(
        self,
        original_prompt: str,
        cache_key: str,
        max_retries: int = 3,
        raise_on_failure: bool = False,
    ) -> str:
        """核心身份提取：带重试机制与智能正则兜底"""
        logger.info(f"[PersonaSummarizer] 🧠 正在提取核心身份骨架 (最大重试: {max_retries}次)...")
        prompt = f"""
你的任务是将以下[原始人设]极致压缩为【核心身份骨架】，作为 AI 聊天机器人（System 1 直觉引擎）秒开回复的底层基石。
注意：这是一个二次元/动漫/游戏角色扮演场景，极度依赖角色与用户的“羁绊”设定。

[原始人设]
{original_prompt}

[深度压缩指令]
请在 200 字以内，用最高密度的陈述句提取以下三大核心要素（不要分点或写小标题，请融合成一段极具概括力的设定陈述）：
1. **核心身份与属性标签**：她/他是谁？最显著的二次元萌属性是什么？（如：病弱重度兄控妹妹、慵懒但杀伐果断的风纪委员长、表面毒舌实则自卑的女仆）。
2. **关系与称呼范围（最高优先级！）**：对话者（用户）的默认关系锚点和默认称呼是什么？如果原文只对某个特定人物或已确认关系使用特殊称呼，必须标记为条件关系，不能把它泛化给所有用户。
3. **初始互动底色（Attitude）**：她面对对话者时，默认的心理状态和态度是怎样的？（是满眼爱意的无条件服从、口是心非的傲娇掩饰、公事公办的冷漠、还是极具侵略性的病娇占有？）。

[输出纪律]
- 必须严格控制在 200 字以内！字字珠玑，彻底剥离所有生平背景、冗长故事和无关配角。
- 必须直接输出纯文本，绝对禁止包含“好的”、“根据设定”、“在这份人设中”、“该角色”等废话前缀或后缀。
"""
        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                res = await self._call_persona_template(
                    PromptTemplateId.PERSONA_CORE_IDENTITY,
                    original_prompt=original_prompt,
                    cache_key=cache_key,
                    is_json=False,
                    fallback_prompt=prompt,
                    fallback_system_prompt="你是一个资深的角色扮演设定提取专家。",
                )
                if res and len(str(res).strip()) > 10:
                    return str(res).strip()
                last_error = ValueError("core identity result is too short")
                logger.warning(f"[PersonaSummarizer] ⚠️ 核心身份提取结果过短，准备重试 ({attempt+1}/{max_retries})")
            except Exception as e:
                last_error = e
                logger.warning(f"[PersonaSummarizer] ❌ 核心身份提取请求失败 ({attempt+1}/{max_retries}): {e}")
            
            await asyncio.sleep(1.5) # 错峰重试，避免并发限流

        # ==========================================
        # 🛡️ 智能兜底：不再无脑截断，尝试正则抓取关键信息
        # ==========================================
        if raise_on_failure:
            raise RuntimeError(f"core identity extraction failed for {cache_key}") from last_error

        import re
        logger.error(f"[PersonaSummarizer] 🚨 核心身份提取彻底失败，触发智能降级兜底！")
        # 尝试抓取包含“姓名”、“身份”、“性格”的段落
        match = re.search(r'(.{0,50}(?:姓名|身份|性格|设定).*?)(?:\n\n|$)', original_prompt, re.IGNORECASE | re.DOTALL)
        fallback_text = match.group(0).strip()[:150] if match else original_prompt[:150]
        return f"[系统降级提取] {fallback_text}...\n(注：角色记忆正在缓慢恢复中)"

    async def _summarize_style_with_retry(
        self,
        original_prompt: str,
        cache_key: str,
        max_retries: int = 3,
        raise_on_failure: bool = False,
    ) -> str:
        """语言风格提取：带重试机制与安全兜底"""
        logger.info(f"[PersonaSummarizer] 🗣️ 正在提取语言风格与排版规范 (最大重试: {max_retries}次)...")
        prompt = f"""
你的任务是从以下[原始人设]中极致压缩出【语言与排版绝对规范】，作为驱动 AI 聊天机器人（System 1 & 2）的底层强制指令。
注意：这是二次元/动漫/游戏角色扮演，极度依赖特定的口癖和回复格式。

[原始人设]
{original_prompt}

[深度提取指令]
请在 200 字以内，用极其简练、带有强制性（“必须”、“严禁”）的祈使句，提炼出以下四大对话规则：
1. **专属称谓规则**：角色的第一人称自称是什么？默认如何称呼对话者（用户）？哪些称呼只有在特定关系明确成立时才能使用？必须区分默认称呼与条件关系称呼。
2. **标志性口癖与语调**：高频使用的句式、语气词（如：喵、……的说、哼）或特定标点偏好（如喜欢用波浪号~、大量使用省略号...）。
3. **排版与动作禁忌（最高优先级！）**：原设定中是否有明确的格式限制？（例如：单次回复不超过30字、严禁使用括号/星号进行动作描写、必须维持日常短消息风格）。
4. **情绪表达质感**：说话时的整体温度和节奏是怎样的？（如：毒舌但句句使用敬语、软糯连贯的撒娇、冰冷简短的无口陈述）。

[输出纪律]
- 必须直接输出纯文本指令，绝对禁止包含“好的”、“提取结果如下”等废话。
- 语言必须具有强烈的“约束感”（如：必须使用...，严禁输出...），确保能直接作为系统规则约束最终的对话模型。
- 将四点融合成一段高密度的规则说明，不要输出 JSON 或 markdown 代码块。
"""
        last_error: Exception | None = None
        for attempt in range(max_retries):
            try:
                res = await self._call_persona_template(
                    PromptTemplateId.PERSONA_STYLE,
                    original_prompt=original_prompt,
                    cache_key=cache_key,
                    is_json=False,
                    fallback_prompt=prompt,
                    fallback_system_prompt="你是一个资深的角色扮演设定提取专家。",
                )
                if res and len(str(res).strip()) > 5:
                    return str(res).strip()
                last_error = ValueError("persona style result is too short")
            except Exception as e:
                last_error = e
                logger.warning(f"[PersonaSummarizer] ❌ 语言风格提取请求失败 ({attempt+1}/{max_retries}): {e}")
            
            await asyncio.sleep(1.5)

        if raise_on_failure:
            raise RuntimeError(f"persona style extraction failed for {cache_key}") from last_error

        # 🛡️ 安全兜底：赋予基础的二次元扮演防护
        return "保持自然、简短的对话风格，拒绝使用AI助手的机械回复格式，严禁长篇大论，贴合人设原本的语气。"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_logic_style(self, original_prompt: str, cache_key: str, raise_on_failure: bool = False) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 性格逻辑 (logic_style)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【性格逻辑】维度的深度切片。
这将被用于驱动一个在线社交聊天机器人，使其表现得像一个真实的二次元/动漫/游戏角色。

[深度扫描维度]
请你像心理分析师一样，仔细扫描文本并提取以下细节：
1. **基础性格底色**：她/他的日常默认状态是什么？（如：慵懒、冷酷、元气、三无、病娇、傲娇、温柔等）。
2. **状态切换与反差（Gap Moe）**：什么特定情境或人会触发她/他的“里性格”？（例如：表面冷酷但被夸奖时会慌乱掩饰；平时懂事但遇到特定事情会极度任性；工作时杀伐果断但私下极度依赖）。
3. **情绪反应机制**：
   - 极度喜悦时：会有什么生理或心理表现？
   - 愤怒或吃醋时：是冷暴力、毒舌、病娇化，还是委屈哭泣？
   - 焦虑与不安时：会有什么强迫性行为或逃避机制？
4. **行动驱动力**：她/他做出决定的第一直觉是基于感性（情感、羁绊）还是理性（规则、利益、效率）？

[输出纪律]
- 请输出一段高密度、结构化的文本，全面总结上述维度。
- **绝对禁止**自行捏造设定中不存在的性格标签。
- 不要出现“该角色……”、“在这个设定中……”等旁白废话，直接陈述性格事实。
- 如果人设中完全没有提到性格相关内容，请仅回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self._call_persona_template(
                PromptTemplateId.PERSONA_LOGIC_STYLE,
                original_prompt=original_prompt,
                cache_key=cache_key,
                is_json=False,
                fallback_prompt=prompt,
            )
        except Exception:
            logger.exception(f"[AstrMai-persona] logic_style slice failed for {cache_key}", exc_info=True)
            if raise_on_failure:
                raise
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_speech_style(self, original_prompt: str, cache_key: str, raise_on_failure: bool = False) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 语言风格 (speech_style)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【语言风格】维度的深度切片。
这是防止角色 OOC 的最关键一步，因为这决定了她/他打字聊天的语气。

[深度扫描维度]
请极度细致地扫描并提取以下语言特征：
1. **第一人称自称**：平时自称什么？（如：我、吾、人家、妾身、老朽、自己的名字等）。情绪激动时自称是否会改变？
2. **第二人称与专属称谓**：如何称呼默认对话者/用户？哪些称呼只适用于特定关系或原文中的特定人物？（如：你、汝、欧尼酱、Sensei、前辈、杂修、主人等）。必须标注范围，不能从聊天记录推导关系。
3. **标志性口癖（Catchphrase）**：句子开头或结尾是否有高频词？（如：……的说、喵、啦、哼、hiyohiyo、哎呀）。
4. **文本排版与符号偏好**：
   - 是否喜欢用特定符号？（如：波浪线“~”、音符“♪”、颜文字）。
   - 沉默或无口属性的表达：（是否大量使用“……”或简短的单字）。
   - 语速与句式：（是喋喋不休的长篇大论，还是惜字如金的短句？是否经常使用倒装句或反问句？）。
5. **社交语气**：是敬语拉满（礼貌但疏离）、粗口/毒舌、还是软糯撒娇？

[输出纪律]
- 必须列出具体的称呼、口癖示例，并用“默认对话者称呼/条件关系称呼/特定人物称呼”区分适用范围。
- **绝对禁止**捏造原设定中没有的口癖和颜文字。
- 如果人设中完全没有提到相关内容，请仅回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self._call_persona_template(
                PromptTemplateId.PERSONA_SPEECH_STYLE,
                original_prompt=original_prompt,
                cache_key=cache_key,
                is_json=False,
                fallback_prompt=prompt,
            )
        except Exception:
            logger.exception(f"[AstrMai-persona] speech_style slice failed for {cache_key}", exc_info=True)
            if raise_on_failure:
                raise
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_world_view(self, original_prompt: str, cache_key: str, raise_on_failure: bool = False) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 世界观 (world_view)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【世界观】维度的深度切片。

[深度扫描维度]
请提取支撑该角色生存的虚拟世界背景：
1. **时代与舞台特征**：故事发生在哪里？（如：赛博朋克都市、剑与魔法异世界、封闭的乡下小镇、末日废土、日常校园等）。
2. **角色社会阶层与阵营**：她/他在这个世界中处于什么位置？（如：权贵、反叛军、学生会、风纪委员、神明、平民、被通缉者等）。
3. **专属黑话与专有名词**：文本中出现的特定组织名称、地名、魔法系统、科技名词（如：融合战士、基沃托斯、圣痕、异世界图书馆等）。简要标注其含义。
4. **世界法则对角色的限制**：这个世界的什么规则在压迫或约束着她/他？

[输出纪律]
- 重点提取名词及其解释，为角色提供聊天时的“常识库”。
- **绝对禁止**引入原设定之外的任何动漫或现实世界观。
- 如果人设中完全没有提到相关内容，请仅回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self._call_persona_template(
                PromptTemplateId.PERSONA_WORLD_VIEW,
                original_prompt=original_prompt,
                cache_key=cache_key,
                is_json=False,
                fallback_prompt=prompt,
            )
        except Exception:
            logger.exception(f"[AstrMai-persona] world_view slice failed for {cache_key}", exc_info=True)
            if raise_on_failure:
                raise
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_timeline(self, original_prompt: str, cache_key: str, raise_on_failure: bool = False) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 生平经历 (timeline)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【生平经历】维度的深度切片。

[深度扫描维度]
不要写成流水账，请提取对角色当前性格产生决定性影响的“剧情锚点”：
1. **起源与童年**：她/他的出身背景，是否经历过重大创伤、失去亲人或被抛弃？
2. **核心转折事件**：哪一个事件彻底改变了她/他的命运？（如：获得力量的瞬间、犯下大错的时刻、被救赎的经历）。
3. **与对话者（用户）的历史渊源**：她/他与默认对话者是怎么相遇的？共同经历过什么关键事件？如果原文只针对特定人物或特定条件建立关系，必须明确标注适用范围，不能将其泛化为所有用户的身份或关系。
4. **当前的处境**：她/他现在正面临什么危机，或者正处于什么日常状态中？

[输出纪律]
- 提炼高密度的事件骨架，侧重于“事件如何塑造了她的心理”。
- **绝对禁止**发散或续写剧情。
- 如果人设中完全没有提到相关内容，请仅回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self._call_persona_template(
                PromptTemplateId.PERSONA_TIMELINE,
                original_prompt=original_prompt,
                cache_key=cache_key,
                is_json=False,
                fallback_prompt=prompt,
            )
        except Exception:
            logger.exception(f"[AstrMai-persona] timeline slice failed for {cache_key}", exc_info=True)
            if raise_on_failure:
                raise
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_relations(self, original_prompt: str, cache_key: str, raise_on_failure: bool = False) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 人际关系 (relations)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【人际关系】维度的深度切片。

[深度扫描维度]
请清晰构建该角色的社交图谱：
1. **默认对话者关系**：她/他对“你（用户/对话者）”的默认感情定位和默认称呼是什么？（是病态的依赖、默默的暗恋、主从的绝对忠诚、还是傲娇的掩饰？）。
   如果某种关系或称呼只针对原文中的特定人物，必须标成条件关系或特定人物关系，不能默认套给所有用户。
2. **敌意与警惕对象**：谁是她/他的死对头？她/他会对接近用户的哪些人产生嫉妒或敌意？
3. **友方与NPC态度**：设定中提到的其他具体名字的角色，她/他怎么称呼他们？态度是怎样的？
4. **社交边界感**：对待完全不认识的陌生人，她是冷漠、警惕、毒舌还是热情礼貌？

[输出纪律]
- 必须明确“默认对话者”“条件关系”“特定人物/其他人”的边界，不能把一个人的关系事实写成所有人的身份。
- 必须明确“对待用户”和“对待其他人”的差异，但不能凭空扩大关系。
- **绝对禁止**提取或捏造原设定文本中未出现的名字。
- 如果人设中完全没有提到相关内容，请仅回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self._call_persona_template(
                PromptTemplateId.PERSONA_RELATIONS,
                original_prompt=original_prompt,
                cache_key=cache_key,
                is_json=False,
                fallback_prompt=prompt,
            )
        except Exception:
            logger.exception(f"[AstrMai-persona] relations slice failed for {cache_key}", exc_info=True)
            if raise_on_failure:
                raise
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_skills(self, original_prompt: str, cache_key: str, raise_on_failure: bool = False) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 技能能力 (skills)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【技能能力】维度的深度切片。

[深度扫描维度]
全方位评估角色的能力面板：
1. **超凡/战斗能力**：拥有的魔法、武技、武器、黑客技术或特殊天赋。战斗时的风格是怎样的？（如：狂暴、精准、毁灭性、治愈辅助）。
2. **日常与生活技能**：在非战斗状态下，她/他擅长什么？（如：家务全能、料理大师，或者是重度机械白痴、生活九级残废需要人照顾）。
3. **能力代价与致命弱点**：使用能力是否需要付出代价？（如：消耗寿命、失去记忆、身体退化）。她在生理或心理上有什么极度害怕的弱点？（如：怕鬼、怕虫子、怕孤单）。

[输出纪律]
- 既要提取“她能做什么”，更要提取“她不能做什么”或“她的软肋”，这有助于增加交互的脆弱感。
- 如果人设中完全没有提到相关内容，请仅回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self._call_persona_template(
                PromptTemplateId.PERSONA_SKILLS,
                original_prompt=original_prompt,
                cache_key=cache_key,
                is_json=False,
                fallback_prompt=prompt,
            )
        except Exception:
            logger.exception(f"[AstrMai-persona] skills slice failed for {cache_key}", exc_info=True)
            if raise_on_failure:
                raise
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_values(self, original_prompt: str, cache_key: str, raise_on_failure: bool = False) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 价值观 (values)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【价值观】维度的深度切片。

[深度扫描维度]
剖析角色的底层动机与喜恶法则：
1. **最高信仰与核心执念**：在这个世界上，对她/他来说最重要、绝对不能妥协的事物是什么？（如：不择手段追求进化、维护某人的安全、遵守风纪、对爱的纯粹渴求）。
2. **道德底线**：她是守序正义（不伤害无辜）、还是混沌邪恶（为了目的可以杀人/无视伦理）？
3. **极度的喜好**：最喜欢的食物、物品或消遣方式是什么？（这些通常是聊天中能让她开心起来的“道具”）。
4. **极度的厌恶**：绝对不能触碰的逆鳞或极其讨厌的事物是什么？（这些通常是触发她愤怒或黑化的“雷区”）。

[输出纪律]
- 重点突出极端偏好和底线，不要用模棱两可的词汇。
- 如果人设中完全没有提到相关内容，请仅回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self._call_persona_template(
                PromptTemplateId.PERSONA_VALUES,
                original_prompt=original_prompt,
                cache_key=cache_key,
                is_json=False,
                fallback_prompt=prompt,
            )
        except Exception:
            logger.exception(f"[AstrMai-persona] values slice failed for {cache_key}", exc_info=True)
            if raise_on_failure:
                raise
            return "无"

    # [修改] 替换 call_planner 为 call_persona_task
    async def _summarize_secrets(self, original_prompt: str, cache_key: str, raise_on_failure: bool = False) -> str:
        logger.info("[PersonaSummarizer] 🧠 正在后台提取切片: 深层秘密 (secrets)...")
        prompt = f"""
你的任务是从以下[原始人设]中提取出【深层秘密】维度的深度切片。
这是角色的“灵魂”，即表象之下的里设定。

[深度扫描维度]
请像心理医生一样，挖掘出文本中隐藏的里层信息：
1. **心理创伤与自卑感**：她/他内心深处最怕什么？（如：觉得自己只是替代品、害怕被抛弃、害怕被视为怪物、对过往罪行的负罪感）。
2. **伪装下的真心**：傲娇、毒舌、冷酷或过分元气的外表下，掩盖了怎样脆弱、渴望被爱或极度疲惫的真实想法？（她/他绝口不提，但在特定时刻会暴露的软肋）。
3. **剧情暗线事实**：设定中是否提到了某种隐藏的诅咒、寿命将近的倒计时、不可告人的黑历史或不为人知的牺牲？

[输出纪律]
- 重点提取那些“她自己不想承认，但确实存在”的矛盾点。
- 提取的结果将作为 AI 对话时的“潜意识指南”，请务必深刻。
- 如果人设中完全没有提到相关内容，请仅回复“无”。

[原始人设]
{original_prompt}
"""
        try:
            return await self._call_persona_template(
                PromptTemplateId.PERSONA_SECRETS,
                original_prompt=original_prompt,
                cache_key=cache_key,
                is_json=False,
                fallback_prompt=prompt,
            )
        except Exception:
            logger.exception(f"[AstrMai-persona] secrets slice failed for {cache_key}", exc_info=True)
            if raise_on_failure:
                raise
            return "无"
