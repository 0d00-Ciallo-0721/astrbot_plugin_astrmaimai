import asyncio
import copy
import hashlib
import unittest
from types import SimpleNamespace

from astrmai.memory.persona.persona_summarizer import PersonaSummarizer
from astrmai.webui.backend.services.persona_ui_service import PersonaUiService


class _Persistence:
    def __init__(self, cache):
        self.cache = copy.deepcopy(cache)

    def load_persona_cache(self):
        return copy.deepcopy(self.cache)

    async def load_persona_cache_async(self):
        return self.load_persona_cache()

    def save_persona_cache(self, cache):
        self.cache = copy.deepcopy(cache)

    async def save_persona_cache_async(self, cache):
        self.save_persona_cache(cache)


class _Gateway:
    def __init__(self):
        self.config = SimpleNamespace(
            persona=SimpleNamespace(include_self_lore_in_prompt=False),
            performance=SimpleNamespace(summary_threshold=10),
        )


def _ready_payload():
    raw = "long original persona"
    shards = {name: f"ai-{name}" for name in PersonaSummarizer.REQUIRED_SHARDS}
    return {
        "summary": "ai-summary",
        "first_person_rewrite": "ai-first-person",
        "style": "ai-style",
        "shards": shards,
        "shard_status": {name: "completed" for name in shards},
        "core_components": {
            "summary": "completed",
            "first_person_rewrite": "completed",
            "style": "completed",
        },
        "core_ready": True,
        "is_full_ready": True,
        "self_lore_ready": True,
        "persona_state": "full_ready",
        "raw": raw,
        "raw_hash": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
        "timestamp": 10.0,
    }


class PersonaManualEditRefactorTests(unittest.TestCase):
    def test_manual_overrides_cancel_old_shard_task_and_persist(self):
        async def _run():
            persistence = _Persistence({"persona-1": _ready_payload()})
            summarizer = PersonaSummarizer(persistence, _Gateway())
            stale_task = asyncio.create_task(asyncio.sleep(60))
            summarizer.pending_tasks["persona-1"] = stale_task

            result = await summarizer.apply_manual_overrides(
                "persona-1",
                {
                    "summary": "manual-summary",
                    "style": "manual-style",
                    "shards": {"speech_style": "manual-speech"},
                },
                expected_timestamp=10.0,
            )

            self.assertTrue(stale_task.cancelled())
            self.assertEqual(result["summary"], "manual-summary")
            self.assertEqual(result["style"], "manual-style")
            self.assertEqual(result["shards"]["speech_style"], "manual-speech")
            self.assertTrue(result["is_full_ready"])
            self.assertIn("summary", result["manual_overrides"])
            self.assertIn("shards.speech_style", result["manual_overrides"])
            self.assertEqual(result["generated_baseline"]["summary"], "ai-summary")
            self.assertEqual(persistence.cache["persona-1"]["summary"], "manual-summary")
            self.assertNotIn("persona-1", summarizer.pending_tasks)
            live = await summarizer.get_summary(
                original_prompt="long original persona",
                persona_id="persona-1",
            )
            self.assertEqual(live["summary"], "manual-summary")
            self.assertEqual(live["style"], "manual-style")
            self.assertEqual(live["shards"]["speech_style"], "manual-speech")

        asyncio.run(_run())

    def test_manual_restore_uses_generated_baseline_and_rejects_stale_save(self):
        async def _run():
            persistence = _Persistence({"persona-1": _ready_payload()})
            summarizer = PersonaSummarizer(persistence, _Gateway())
            updated = await summarizer.apply_manual_overrides(
                "persona-1",
                {"first_person_rewrite": "manual-first", "shards": {"values": "manual-values"}},
                expected_timestamp=10.0,
            )
            with self.assertRaisesRegex(RuntimeError, "reload before saving"):
                await summarizer.apply_manual_overrides(
                    "persona-1",
                    {"style": "stale-style"},
                    expected_timestamp=10.0,
                )

            restored = await summarizer.restore_manual_overrides(
                "persona-1",
                ["first_person_rewrite", "shards.values"],
                expected_timestamp=updated["timestamp"],
            )
            self.assertEqual(restored["first_person_rewrite"], "ai-first-person")
            self.assertEqual(restored["shards"]["values"], "ai-values")
            self.assertEqual(restored["manual_overrides"], {})
            self.assertNotIn("generated_baseline", restored)

        asyncio.run(_run())

    def test_persona_ui_service_whitelists_fields_and_returns_live_result(self):
        class _Summarizer:
            pending_tasks = {}

            def __init__(self, persistence):
                self.persistence = persistence
                self.calls = []

            async def apply_manual_overrides(self, cache_key, changes, expected_timestamp=None):
                self.calls.append((cache_key, changes, expected_timestamp))
                payload = self.persistence.cache[cache_key]
                payload.update({key: value for key, value in changes.items() if key != "shards"})
                payload["shards"].update(changes.get("shards", {}))
                payload["manual_overrides"] = {"style": {"source": "plugin_page"}}
                payload["timestamp"] = 11.0

        class _Adapter:
            def __init__(self):
                self.persistence = _Persistence({"persona-1": _ready_payload()})
                self.summarizer = _Summarizer(self.persistence)

            async def read_persona_cache(self):
                return self.persistence.load_persona_cache()

            def get_runtime_config(self):
                return SimpleNamespace(persona=SimpleNamespace(persona_id="persona-1"))

            def get_persona_summarizer(self):
                return self.summarizer

            def get_memory_engine(self):
                return object()

        async def _run():
            adapter = _Adapter()
            service = PersonaUiService(adapter)
            rejected = await service.update_persona_slices(
                {"cache_key": "persona-1", "raw": "must-not-change"}
            )
            self.assertEqual(rejected["status"], "error")

            result = await service.update_persona_slices(
                {
                    "cache_key": "persona-1",
                    "expected_timestamp": 10.0,
                    "style": "更自然地使用短句",
                    "shards": {"speech_style": "短句、少解释、先回应情绪"},
                }
            )
            self.assertEqual(result["status"], "ok")
            self.assertEqual(adapter.summarizer.calls[0][0], "persona-1")
            self.assertEqual(adapter.summarizer.calls[0][2], 10.0)
            self.assertEqual(result["data"]["style"], "更自然地使用短句")
            self.assertIn("style", result["data"]["manual_overrides"])

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
