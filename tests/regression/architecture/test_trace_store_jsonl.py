"""G8 / WU-06 回归测试：trace 存储改 append-only JSONL。

旧实现每条入站消息（含被忽略的 317/585）都在聊天路径上「读整文件+解析+序列化+写整文件」，
15MB 实测 0.7s/条、封顶约 42MB，且与 WebUI 45s 轮询共用一把锁互相放大。

守护不变式：
1. 追加是 append-only（不重写既有内容），写入耗时不随文件增长线性劣化。
2. 语义等价：`recent()` 仍按新→旧返回、支持按 chat 过滤、同 turn_id 后写覆盖先写。
3. 容量护栏：超阈值触发压实，压实后遵守 per-chat 与 global 上限。
4. **历史兼容**：JSONL 不存在时回落读 legacy 整文件；首次写入自动迁移历史样本。
5. 分析脚本双格式可读（.agent/runtime-observability-* 的归档快照仍要能分析）。
"""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path

from astrmai.infrastructure.runtime.turn_trace_store import TurnTraceSampleStore


def _sample(turn_id: str, chat_id: str = "chat-1", created_at: float = 0.0, **extra):
    payload = {"turn_id": turn_id, "chat_id": chat_id, "created_at": created_at or float(len(turn_id))}
    payload.update(extra)
    return payload


class JsonlAppendTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _store(self, **kwargs):
        return TurnTraceSampleStore(self.base, **kwargs)

    def test_append_writes_one_line_per_sample(self):
        store = self._store()

        async def _run():
            for index in range(3):
                await store.append(_sample(f"t{index}", created_at=float(index)))

        asyncio.run(_run())

        lines = [line for line in store.jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertEqual(len(lines), 3)
        self.assertEqual(json.loads(lines[0])["turn_id"], "t0")

    def test_append_is_append_only(self):
        # 追加不得重写已有内容：前缀字节必须原样保留
        store = self._store()

        async def _first():
            await store.append(_sample("t0", created_at=1.0))

        asyncio.run(_first())
        prefix = store.jsonl_path.read_bytes()

        async def _second():
            await store.append(_sample("t1", created_at=2.0))

        asyncio.run(_second())
        after = store.jsonl_path.read_bytes()

        self.assertTrue(after.startswith(prefix), "既有内容必须原样保留（append-only）")
        self.assertGreater(len(after), len(prefix))

    def test_recent_returns_newest_first_and_dedupes_by_turn(self):
        store = self._store()

        async def _run():
            await store.append(_sample("t1", created_at=1.0, reply_preview="旧"))
            await store.append(_sample("t2", created_at=2.0))
            # 同 turn_id 重写：append-only 下旧行仍在文件里，读取端必须后写覆盖先写
            await store.append(_sample("t1", created_at=3.0, reply_preview="新"))
            return await store.recent(limit=10)

        items = asyncio.run(_run())

        self.assertEqual([item["turn_id"] for item in items], ["t1", "t2"])
        self.assertEqual(items[0]["reply_preview"], "新")

    def test_recent_filters_by_chat(self):
        store = self._store()

        async def _run():
            await store.append(_sample("a1", chat_id="chat-a", created_at=1.0))
            await store.append(_sample("b1", chat_id="chat-b", created_at=2.0))
            await store.append(_sample("a2", chat_id="chat-a", created_at=3.0))
            return await store.recent(chat_id="chat-a", limit=10)

        items = asyncio.run(_run())

        self.assertEqual([item["turn_id"] for item in items], ["a2", "a1"])

    def test_file_stays_bounded_under_sustained_appends(self):
        # 核心护栏：文件不得无界增长（旧实现靠每次全量重写维持上限，
        # append-only 改由周期性压实维持）
        store = self._store(max_per_chat=2, max_global=4)

        async def _run():
            for index in range(60):
                await store.append(_sample(f"t{index}", chat_id=f"chat-{index % 2}", created_at=float(index)))

        asyncio.run(_run())

        lines = [line for line in store.jsonl_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        self.assertLessEqual(len(lines), store.max_global * store.COMPACTION_FACTOR)

    def test_compaction_applies_per_chat_and_global_caps(self):
        # 语义变化记录：per-chat 上限是**压实后**的保留保证，不再是"每次写入即刻生效"
        # 的瞬时不变式——两次压实之间尾部可短暂超出（上限由文件总量护栏兜住）。
        store = self._store(max_per_chat=2, max_global=4)

        async def _run():
            for index in range(20):
                await store.append(_sample(f"t{index}", chat_id=f"chat-{index % 2}", created_at=float(index)))
            await asyncio.to_thread(store._compact_sync)
            return await store.recent(limit=50)

        items = asyncio.run(_run())

        self.assertLessEqual(len(items), store.max_global)
        for chat in ("chat-0", "chat-1"):
            per_chat = [item for item in items if item["chat_id"] == chat]
            self.assertLessEqual(len(per_chat), store.max_per_chat, "压实后每 chat 必须收敛到上限")
        # 保留的必须是最新的那批
        self.assertEqual(items[0]["turn_id"], "t19")

    def test_recent_honors_limit(self):
        store = self._store()

        async def _run():
            for index in range(10):
                await store.append(_sample(f"t{index}", created_at=float(index)))
            return await store.recent(limit=3)

        items = asyncio.run(_run())

        self.assertEqual([item["turn_id"] for item in items], ["t9", "t8", "t7"])

    def test_truncated_line_is_skipped_not_fatal(self):
        store = self._store()

        async def _run():
            await store.append(_sample("t1", created_at=1.0))

        asyncio.run(_run())
        with store.jsonl_path.open("a", encoding="utf-8") as handle:
            handle.write('{"turn_id": "broken", "chat_id"\n')  # 崩溃截断的半行

        items = asyncio.run(store.recent(limit=10))

        self.assertEqual([item["turn_id"] for item in items], ["t1"], "半行跳过，不得整份报废")


class LegacyCompatibilityTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _write_legacy(self):
        payload = {
            "version": 2,
            "capture_started_at": 1.0,
            "by_chat": {"chat-1": [_sample("legacy-1", created_at=1.0)]},
            "recent": [_sample("legacy-1", created_at=1.0), _sample("legacy-2", created_at=2.0)],
        }
        (self.base / "turn_trace_samples.json").write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8"
        )

    def test_reads_legacy_file_when_jsonl_absent(self):
        self._write_legacy()
        store = TurnTraceSampleStore(self.base)

        items = asyncio.run(store.recent(limit=10))

        self.assertEqual([item["turn_id"] for item in items], ["legacy-2", "legacy-1"])

    def test_first_append_migrates_legacy_history(self):
        self._write_legacy()
        store = TurnTraceSampleStore(self.base)

        asyncio.run(store.append(_sample("fresh", created_at=9.0)))

        turn_ids = {item["turn_id"] for item in asyncio.run(store.recent(limit=10))}
        self.assertEqual(turn_ids, {"legacy-1", "legacy-2", "fresh"}, "历史样本不得因迁移丢失")


class AnalyzeScriptDualFormatTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.base = Path(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    def _load(self, path):
        import importlib.util

        spec = importlib.util.spec_from_file_location(
            "analyze_turn_ledger", Path("scripts/analyze_turn_ledger.py")
        )
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.load_traces(path)

    def test_reads_jsonl(self):
        path = self.base / "samples.jsonl"
        with path.open("w", encoding="utf-8") as handle:
            for index in range(3):
                handle.write(json.dumps(_sample(f"t{index}", created_at=float(index))) + "\n")

        self.assertEqual(len(self._load(path)), 3)

    def test_still_reads_legacy_archive(self):
        # .agent/runtime-observability-* 的归档快照必须继续可分析
        path = self.base / "samples.json"
        path.write_text(
            json.dumps({"version": 2, "recent": [_sample("t1"), _sample("t2")], "by_chat": {}}),
            encoding="utf-8",
        )

        self.assertEqual(len(self._load(path)), 2)


if __name__ == "__main__":
    unittest.main()
