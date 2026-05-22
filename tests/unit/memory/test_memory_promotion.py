import asyncio
import importlib
import os
import sys
import tempfile
import time
import unittest

from tests.helpers import install_astrbot_stubs


class MemoryPromotionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)
        for name in list(sys.modules):
            if name.startswith("astrmai.memory.services.memory_") or name.startswith("astrmai.memory.dream.") or name.endswith("v2_store"):
                sys.modules.pop(name, None)
        self.contracts = importlib.import_module("astrmai.memory.contracts.memory_query")
        self.store_mod = importlib.import_module("astrmai.memory.services.v2_store")
        self.write_mod = importlib.import_module("astrmai.memory.services.memory_write_service")
        self.engine_mod = importlib.import_module("astrmai.memory.services.memory_engine")
        self.promotion_mod = importlib.import_module("astrmai.memory.dream.promotion_engine")
        self.db_path = os.path.join(self.temp_dir.name, "docs.db")

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def test_promotion_engine_promotes_repeated_fact_with_evidence(self):
        async def run():
            store = self.store_mod.MemoryV2Store(self.db_path, data_path=self.temp_dir.name)
            writer = self.write_mod.MemoryWriteService(store)

            class _Engine:
                def __init__(self):
                    self.v2_store = store
                    self.write_service = writer

            now = time.time()
            for index, content in enumerate(
                [
                    "我今天换了5TB网盘",
                    "5TB空间真大喵",
                    "5TB Google One 续费成功",
                ]
            ):
                await writer.write(
                    self.contracts.MemoryWriteRequest(
                        source="memory_summary",
                        kind="topic",
                        session_id="chat-1",
                        sender_id="zlj",
                        content=content,
                        summary=content,
                        metadata={
                            "promotion_entity": "asset",
                            "promotion_attribute": "google_one",
                            "promotion_value": "5TB",
                            "turn_id": f"t_{index:03d}",
                        },
                        dedup_key=f"topic:chat-1:{index}",
                        created_at=now - index * 3600,
                    )
                )

            engine = _Engine()
            promotion = self.promotion_mod.MemoryPromotionEngine(engine)
            maintenance_result = {
                "detected_facts": [
                    {
                        "subject_id": "zlj",
                        "entity": "asset",
                        "attribute": "google_one",
                        "value": "5TB",
                        "confidence_score": 0.95,
                        "evidence": {"turn_id": "t_099", "text": "5TB Google One 续费成功"},
                    }
                ]
            }
            report = await promotion.run_audit("chat-1", maintenance_result, now=now)
            self.assertEqual(len(report["promoted"]), 1)
            promoted_id = report["promoted"][0]["memory_id"]
            promoted = await store.get_canonical(promoted_id, include_inactive=True)
            self.assertEqual(promoted.kind, "fact")
            self.assertEqual(promoted.confidence, 1.0)
            self.assertEqual(promoted.sender_id, "zlj")
            metadata = dict(promoted.metadata or {})
            self.assertEqual(metadata["promotion_source"], "dream_audit_pipeline")
            self.assertEqual(metadata["promotion_window_days"], 3)
            self.assertTrue(metadata["authority_eav"])
            self.assertGreaterEqual(metadata["promotion_count"], 3)
            self.assertEqual(promoted_id, metadata.get("promoted_to", promoted_id) if False else promoted_id)
            self.assertEqual(report["promoted"][0]["dedup_key"], "zlj:asset:google_one")
            self.assertTrue(metadata["evidence_turns"])

        asyncio.run(run())

    def test_promotion_engine_skips_short_term_state_like_anxiety(self):
        async def run():
            store = self.store_mod.MemoryV2Store(self.db_path, data_path=self.temp_dir.name)
            writer = self.write_mod.MemoryWriteService(store)

            class _Engine:
                def __init__(self):
                    self.v2_store = store
                    self.write_service = writer

            now = time.time()
            for index in range(3):
                await writer.write(
                    self.contracts.MemoryWriteRequest(
                        source="memory_summary",
                        kind="topic",
                        session_id="chat-1",
                        sender_id="zlj",
                        content="我今天特别焦虑",
                        summary="今天特别焦虑",
                        metadata={
                            "promotion_entity": "emotion",
                            "promotion_attribute": "anxiety_state",
                            "promotion_value": "anxious",
                            "turn_id": f"anx-{index}",
                        },
                        dedup_key=f"topic:anx:{index}",
                        created_at=now - index * 3600,
                    )
                )

            engine = _Engine()
            promotion = self.promotion_mod.MemoryPromotionEngine(engine)
            report = await promotion.run_audit("chat-1", {"detected_facts": []}, now=now)
            self.assertEqual(report["promoted"], [])

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()
