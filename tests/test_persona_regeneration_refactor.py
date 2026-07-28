import asyncio
import copy
import hashlib
import unittest
from types import MethodType, SimpleNamespace

from astrmai.memory.persona.persona_summarizer import PersonaSummarizer
from astrmai.webui.backend.services.persona_ui_service import PersonaUiService


class _Persistence:
    def __init__(self, cache):
        self.cache = copy.deepcopy(cache)

    def load_persona_cache(self):
        return copy.deepcopy(self.cache)

    async def load_persona_cache_async(self):
        return self.load_persona_cache()

    async def save_persona_cache_async(self, cache):
        self.cache = copy.deepcopy(cache)


class _Gateway:
    def __init__(self):
        self.config = SimpleNamespace(
            persona=SimpleNamespace(include_self_lore_in_prompt=False),
            performance=SimpleNamespace(summary_threshold=10),
        )


def _ready_payload():
    raw = "long original persona"
    shards = {name: f"old-{name}" for name in PersonaSummarizer.REQUIRED_SHARDS}
    return {
        "summary": "old-summary",
        "first_person_rewrite": "old-first",
        "style": "old-style",
        "shards": shards,
        "shard_status": {name: "completed" for name in shards},
        "core_components": {name: "completed" for name in PersonaSummarizer.MANUAL_CORE_FIELDS},
        "core_ready": True,
        "is_full_ready": True,
        "self_lore_ready": True,
        "persona_state": "full_ready",
        "raw": raw,
        "raw_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "timestamp": 10.0,
        "manual_overrides": {"style": {"source": "plugin_page"}},
        "generated_baseline": {"style": "old-style"},
    }


async def _write_generated_core(self, original_prompt, cache_key, *, force_compression=False):
    assert force_compression is True
    self.cache[cache_key] = {
        "summary": "new-summary",
        "first_person_rewrite": "new-first",
        "style": "new-style",
        "shards": {},
        "shard_status": {},
        "core_components": {name: "completed" for name in self.MANUAL_CORE_FIELDS},
        "core_ready": True,
        "is_full_ready": False,
        "self_lore_ready": True,
        "persona_state": "core_ready",
        "raw": original_prompt,
        "raw_hash": self._compute_hash(original_prompt),
        "timestamp": 20.0,
    }
    await self._persist_cache(strict=True)
    return dict(self.cache[cache_key])


async def _write_generated_shards(
    self,
    original_prompt,
    cache_key,
    generation=None,
    raise_on_failure=False,
    skip_self_lore=False,
):
    assert raise_on_failure is True
    assert skip_self_lore is True
    payload = self.cache[cache_key]
    payload["shards"] = {name: f"new-{name}" for name in self.REQUIRED_SHARDS}
    payload["shard_status"] = {name: "completed" for name in self.REQUIRED_SHARDS}
    payload["is_full_ready"] = True
    payload["persona_state"] = "full_ready"
    await self._persist_cache(strict=True)


class PersonaRegenerationRefactorTests(unittest.TestCase):
    def test_successful_regeneration_atomically_replaces_payload_and_clears_manual_overrides(self):
        async def _run():
            persistence = _Persistence({"persona-1": _ready_payload()})
            summarizer = PersonaSummarizer(persistence, _Gateway())
            summarizer._initialize_core = MethodType(_write_generated_core, summarizer)
            summarizer._generate_all_shards_background = MethodType(
                _write_generated_shards,
                summarizer,
            )

            accepted = await summarizer.start_regeneration(
                "persona-1",
                expected_timestamp=10.0,
                idempotency_key="job-success",
            )
            self.assertEqual(accepted["state"], "queued")
            task = summarizer.regeneration_tasks["persona-1"]
            await task

            result = persistence.cache["persona-1"]
            self.assertEqual(result["summary"], "new-summary")
            self.assertEqual(result["shards"]["relations"], "new-relations")
            self.assertNotIn("manual_overrides", result)
            self.assertNotIn("generated_baseline", result)
            self.assertEqual(result["derivation_version"], PersonaSummarizer.DERIVATION_VERSION)
            self.assertFalse(
                any(key.startswith(PersonaSummarizer.REGENERATION_CACHE_PREFIX) for key in persistence.cache)
            )
            status = summarizer.get_regeneration_status("persona-1")
            self.assertEqual(status["state"], "completed")
            self.assertEqual(status["completed_components"], 11)
            self.assertNotIn("summary", status)
            self.assertNotIn("raw", status)

        asyncio.run(_run())

    def test_duplicate_start_reuses_running_job_and_old_persona_remains_live(self):
        async def _run():
            persistence = _Persistence({"persona-1": _ready_payload()})
            summarizer = PersonaSummarizer(persistence, _Gateway())
            gate = asyncio.Event()

            async def _blocked_core(instance, original_prompt, cache_key, *, force_compression=False):
                await gate.wait()
                return await _write_generated_core(
                    instance,
                    original_prompt,
                    cache_key,
                    force_compression=force_compression,
                )

            summarizer._initialize_core = MethodType(_blocked_core, summarizer)
            summarizer._generate_all_shards_background = MethodType(
                _write_generated_shards,
                summarizer,
            )
            first = await summarizer.start_regeneration("persona-1", expected_timestamp=10.0)
            await asyncio.sleep(0)
            second = await summarizer.start_regeneration("persona-1", expected_timestamp=10.0)
            self.assertEqual(first["job_id"], second["job_id"])
            self.assertEqual(summarizer.cache["persona-1"]["summary"], "old-summary")
            self.assertEqual(len(summarizer.regeneration_tasks), 1)

            task = summarizer.regeneration_tasks["persona-1"]
            gate.set()
            await task
            self.assertEqual(summarizer.cache["persona-1"]["summary"], "new-summary")

        asyncio.run(_run())

    def test_failed_regeneration_keeps_old_persona_and_reports_stage_without_content(self):
        async def _run():
            persistence = _Persistence({"persona-1": _ready_payload()})
            summarizer = PersonaSummarizer(persistence, _Gateway())
            summarizer._initialize_core = MethodType(_write_generated_core, summarizer)

            async def _fail_shards(*_args, **_kwargs):
                raise RuntimeError("provider unavailable")

            summarizer._generate_all_shards_background = _fail_shards
            await summarizer.start_regeneration("persona-1", expected_timestamp=10.0)
            task = summarizer.regeneration_tasks["persona-1"]
            await task

            self.assertEqual(persistence.cache["persona-1"]["summary"], "old-summary")
            status = summarizer.get_regeneration_status("persona-1")
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["failed_component"], "shards")
            self.assertIn("provider unavailable", status["error"])
            self.assertNotIn("old-summary", str(status))

        asyncio.run(_run())

    def test_manual_edit_during_regeneration_wins_and_blocks_atomic_swap(self):
        async def _run():
            persistence = _Persistence({"persona-1": _ready_payload()})
            summarizer = PersonaSummarizer(persistence, _Gateway())
            shards_started = asyncio.Event()
            release_shards = asyncio.Event()
            summarizer._initialize_core = MethodType(_write_generated_core, summarizer)

            async def _blocked_shards(instance, *args, **kwargs):
                shards_started.set()
                await release_shards.wait()
                await _write_generated_shards(instance, *args, **kwargs)

            summarizer._generate_all_shards_background = MethodType(
                _blocked_shards,
                summarizer,
            )
            await summarizer.start_regeneration("persona-1", expected_timestamp=10.0)
            task = summarizer.regeneration_tasks["persona-1"]
            await shards_started.wait()
            edited = await summarizer.apply_manual_overrides(
                "persona-1",
                {"summary": "manual-wins"},
                expected_timestamp=10.0,
            )
            release_shards.set()
            await task

            self.assertEqual(summarizer.cache["persona-1"]["summary"], "manual-wins")
            self.assertEqual(persistence.cache["persona-1"]["timestamp"], edited["timestamp"])
            status = summarizer.get_regeneration_status("persona-1")
            self.assertEqual(status["state"], "failed")
            self.assertEqual(status["failed_component"], "commit")
            self.assertIn("reload before saving", status["error"])

        asyncio.run(_run())

    def test_service_exposes_status_and_filters_internal_cache_keys(self):
        class _Adapter:
            def __init__(self):
                self.persistence = _Persistence({"persona-1": _ready_payload()})
                self.summarizer = PersonaSummarizer(self.persistence, _Gateway())
                self.summarizer.cache["__persona_regeneration__:hidden"] = {"summary": "hidden"}

            async def read_persona_cache(self):
                return copy.deepcopy(self.summarizer.cache)

            def get_runtime_config(self):
                return SimpleNamespace(persona=SimpleNamespace(persona_id="persona-1"))

            def get_persona_summarizer(self):
                return self.summarizer

            def get_memory_engine(self):
                return object()

        async def _run():
            adapter = _Adapter()
            service = PersonaUiService(adapter)
            result = await service.get_persona_slices()
            self.assertEqual(result["data"]["regeneration"]["state"], "idle")
            self.assertEqual(result["data"]["cache_keys"], ["persona-1"])
            rejected = await service.regenerate_persona_slices(
                {"cache_key": "persona-1", "unexpected": True}
            )
            self.assertEqual(rejected["code"], "invalid_fields")

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
