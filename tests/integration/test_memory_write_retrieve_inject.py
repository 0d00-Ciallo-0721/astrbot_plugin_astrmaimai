from __future__ import annotations

import asyncio
import os
import tempfile
import unittest

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _Event:
    def __init__(self, text: str):
        self.message_str = text
        self.unified_msg_origin = "chat-1"
        self._extra = {"astrmai_think_level": 2}

    def get_sender_id(self):
        return "user-1"

    def get_sender_name(self):
        return "Alice"

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value


class MemoryWriteRetrieveInjectIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _services(self):
        from astrmai.memory.services.memory_injection_service import MemoryInjectionService
        from astrmai.memory.services.memory_retrieval_service import MemoryRetrievalService
        from astrmai.memory.services.memory_write_service import MemoryWriteService
        from astrmai.memory.services.v2_store import MemoryV2Store

        db_path = os.path.join(self.temp_dir.name, "memory.db")
        store = MemoryV2Store(db_path, data_path=self.temp_dir.name)
        retrieval = MemoryRetrievalService(store)
        writer = MemoryWriteService(store)
        injection = MemoryInjectionService(retrieval)
        return store, writer, retrieval, injection

    def test_identity_turn_can_be_written_into_canonical_store(self):
        from astrmai.memory.contracts.memory_query import InstantGateResult, MemoryWriteRequest
        from astrmai.memory.services.memory_turn_pipeline import MemoryTurnPipeline

        async def run():
            store, writer, _retrieval, _injection = self._services()

            class _InstantGate:
                async def process_committed_turn(self, turn):
                    memory_id = await writer.write(
                        MemoryWriteRequest(
                            source="instant_gate",
                            kind="identity",
                            session_id=turn.chat_id,
                            sender_id=turn.sender_id,
                            content="用户叫小明，今年 25 岁",
                            summary="小明，25 岁",
                            dedup_key="identity:user-1:name-age",
                        )
                    )
                    return InstantGateResult(hit=bool(memory_id), memory_id=memory_id, category="identity")

            pipeline = MemoryTurnPipeline(
                context=object(),
                gateway=object(),
                engine=object(),
                session_summarizer=object(),
                instant_gate=_InstantGate(),
            )
            turn = pipeline.build_turn(
                chat_id="chat-1",
                sender_id="user-1",
                user_text="我叫小明，今年 25 岁",
                assistant_text="记住啦",
                source="integration",
            )
            await pipeline.record_turn(turn)
            result = await pipeline.process_instant_gate(turn)
            row = await store.get_canonical(result.memory_id, include_inactive=True)
            return row

        row = asyncio.run(run())
        self.assertIsNotNone(row)
        self.assertEqual(row.kind, "identity")
        self.assertIn("小明", row.content)

    def test_food_preferences_roundtrip_through_retrieval(self):
        from astrmai.memory.contracts.memory_query import MemoryQuery, MemoryWriteRequest

        async def run():
            _store, writer, retrieval, _injection = self._services()
            foods = ["小明喜欢吃火锅", "小明喜欢吃芒果", "小明不喜欢苦瓜"]
            for index, text in enumerate(foods):
                await writer.write(
                    MemoryWriteRequest(
                        source="summary",
                        kind="preference",
                        session_id="chat-1",
                        sender_id="user-1",
                        content=text,
                        summary=text,
                        dedup_key=f"food:{index}",
                    )
                )
            return await retrieval.retrieve(MemoryQuery(query="喜欢吃什么", session_id="chat-1", top_k=5))

        candidates = asyncio.run(run())
        rendered = "\n".join(item.content for item in candidates)
        self.assertIn("火锅", rendered)
        self.assertIn("芒果", rendered)

    def test_food_preference_query_ranks_food_above_weak_like_matches(self):
        from astrmai.memory.contracts.memory_query import MemoryQuery, MemoryWriteRequest

        async def run():
            _store, writer, retrieval, _injection = self._services()
            rows = [
                ("color", "用户喜欢蓝色", "preference"),
                ("sport", "用户讨厌跑步", "preference"),
                ("food", "用户喜欢吃火锅", "preference"),
            ]
            for key, text, kind in rows:
                await writer.write(
                    MemoryWriteRequest(
                        source="summary",
                        kind=kind,
                        session_id="chat-1",
                        sender_id="user-1",
                        content=text,
                        summary=text,
                        dedup_key=f"preference:{key}",
                    )
                )
            return await retrieval.retrieve(MemoryQuery(query="我喜欢吃什么", session_id="chat-1", top_k=5))

        candidates = asyncio.run(run())
        self.assertTrue(candidates)
        self.assertIn("火锅", candidates[0].content)
        self.assertNotIn("蓝色", candidates[0].content)

    def test_weak_memory_query_does_not_return_broad_like_matches(self):
        from astrmai.memory.contracts.memory_query import MemoryQuery, MemoryWriteRequest

        async def run():
            _store, writer, retrieval, _injection = self._services()
            await writer.write(
                MemoryWriteRequest(
                    source="summary",
                    kind="preference",
                    session_id="chat-1",
                    sender_id="user-1",
                    content="用户喜欢蓝色",
                    summary="用户喜欢蓝色",
                    dedup_key="preference:color",
                )
            )
            return await retrieval.retrieve(MemoryQuery(query="我喜欢什么", session_id="chat-1", top_k=5))

        candidates = asyncio.run(run())
        self.assertEqual(candidates, [])

    def test_retrieved_memory_is_rendered_into_prompt_bundle(self):
        from astrmai.memory.contracts.memory_query import MemoryWriteRequest

        async def run():
            _store, writer, _retrieval, injection = self._services()
            await writer.write(
                MemoryWriteRequest(
                    source="summary",
                    kind="preference",
                    session_id="chat-1",
                    sender_id="user-1",
                    content="小明喜欢吃火锅",
                    summary="小明喜欢吃火锅",
                    dedup_key="food:hotpot",
                )
            )
            return await injection.build_bundle(event=_Event("还记得火锅吗"), prompt="火锅")

        bundle = asyncio.run(run())
        self.assertTrue(bundle.rendered_prompt_block)
        self.assertIn("火锅", bundle.rendered_prompt_block)
        self.assertTrue(bundle.items)

    def test_revision_updates_are_visible_to_retrieval_and_injection(self):
        # OPT-13/TG-05: 记忆闭环的"修订"腿——写入→检索命中旧内容→修订→
        # 检索/注入必须立即反映新内容且旧表述不再命中。此前三层测试
        # （WebUI mock、store 单测、写检注集成）全部绕开该 wiring。
        from astrmai.memory.contracts.memory_query import MemoryQuery, MemoryWriteRequest

        async def run():
            store, writer, retrieval, injection = self._services()
            memory_id = await writer.write(
                MemoryWriteRequest(
                    source="summary",
                    kind="preference",
                    session_id="chat-1",
                    sender_id="user-1",
                    content="小明喜欢吃寿司",
                    summary="小明喜欢吃寿司",
                    dedup_key="food:revision-case",
                )
            )
            before = await retrieval.retrieve(
                MemoryQuery(query="寿司", session_id="chat-1", top_k=3)
            )
            await store.update_memory(
                memory_id,
                content="小明其实喜欢吃拉面（寿司过敏，已修正）",
                summary="小明喜欢拉面，寿司过敏",
            )
            after = await retrieval.retrieve(
                MemoryQuery(query="拉面", session_id="chat-1", top_k=3)
            )
            bundle = await injection.build_bundle(event=_Event("我喜欢吃什么来着"), prompt="拉面")
            return memory_id, before, after, bundle

        memory_id, before, after, bundle = asyncio.run(run())
        self.assertTrue(any(item.id == memory_id for item in before), "修订前应能按旧内容命中")
        self.assertTrue(any(item.id == memory_id for item in after), "修订后应能按新内容命中")
        revised = next(item for item in after if item.id == memory_id)
        self.assertIn("拉面", revised.content)
        self.assertNotIn("喜欢吃寿司", revised.content)
        self.assertIn("拉面", bundle.rendered_prompt_block)


if __name__ == "__main__":
    unittest.main()
