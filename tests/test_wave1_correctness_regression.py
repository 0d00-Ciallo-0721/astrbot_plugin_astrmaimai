import asyncio
import json
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


class _GatewayResponse:
    completion_text = "visible reply"
    usage = SimpleNamespace(input=3, input_cached=0, output=2)


class _GatewayContext:
    async def llm_generate(self, **kwargs):
        return _GatewayResponse()


class Wave1CorrectnessRegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        install_astrbot_stubs(self.temp_dir.name)

    def tearDown(self):
        try:
            self.temp_dir.cleanup()
        except Exception:
            pass

    @staticmethod
    def _gateway():
        from astrmai.infrastructure.gateway.model_gateway import GlobalModelGateway

        return GlobalModelGateway(
            _GatewayContext(),
            SimpleNamespace(
                infra=SimpleNamespace(
                    max_concurrent_llm_calls=1,
                    llm_retries=0,
                    backoff_factor=1.0,
                    api_timeout=10,
                ),
                provider=SimpleNamespace(fallback_models=[]),
                global_settings=SimpleNamespace(debug_mode=False),
                system1=SimpleNamespace(nicknames=[]),
            ),
        )

    def test_success_artifact_failures_do_not_turn_model_success_into_failure(self):
        async def _raise_async(**kwargs):
            raise RuntimeError("artifact store unavailable")

        for failure_point in ("usage", "economy", "benchmark"):
            with self.subTest(failure_point=failure_point):
                gateway = self._gateway()
                workload_policy = None
                if failure_point == "usage":
                    gateway._log_usage = lambda *args, **kwargs: (_ for _ in ()).throw(
                        RuntimeError("usage unavailable")
                    )
                elif failure_point == "economy":
                    from astrmai.infrastructure.context_economy import WorkloadFamily

                    workload_policy = gateway.context_economy.resolve_policy(
                        gateway.context_economy.build_request(
                            family=WorkloadFamily.MEMORY_GLOBAL_SUMMARY,
                            pool_name="task",
                            prompt="hello",
                            system_prompt="system",
                            models=["model-a"],
                            scope_id="global",
                        )
                    )
                    gateway.context_economy.build_trace = lambda **kwargs: (_ for _ in ()).throw(
                        RuntimeError("trace unavailable")
                    )
                else:
                    gateway._record_benchmark_sample = _raise_async

                result = asyncio.run(
                    gateway._elastic_call_result(
                        pool_name="task",
                        prompt="hello",
                        system_prompt="system",
                        models=["model-a"],
                        use_fallback=False,
                        workload_policy=workload_policy,
                    )
                )

                self.assertTrue(result.ok)
                self.assertEqual(result.text, "visible reply")
                stats = gateway.router.get_stats()["task"]["models"]["model-a"]
                self.assertEqual(stats["failures"], 0)
                self.assertEqual(stats["calls"], 1)

    def test_json_success_survives_usage_logging_failure(self):
        gateway = self._gateway()
        gateway.context.llm_generate = lambda **kwargs: asyncio.sleep(
            0,
            result=SimpleNamespace(
                completion_text='{"answer": "ok"}',
                usage=SimpleNamespace(input=3, input_cached=0, output=2),
            ),
        )
        gateway._log_usage = lambda *args, **kwargs: (_ for _ in ()).throw(
            RuntimeError("usage unavailable")
        )

        result = asyncio.run(
            gateway._elastic_call_result(
                pool_name="task",
                prompt="hello",
                system_prompt="system",
                models=["model-a"],
                is_json=True,
                use_fallback=False,
            )
        )

        self.assertTrue(result.ok)
        self.assertEqual(result.parsed_json, {"answer": "ok"})
        stats = gateway.router.get_stats()["task"]["models"]["model-a"]
        self.assertEqual(stats["failures"], 0)
        self.assertEqual(stats["calls"], 1)

    def test_reflector_weight_failure_does_not_consume_next_batch(self):
        sys.modules.pop("astrmai.learning.review.reflector", None)
        from astrmai.learning.review.reflector import ExpressionReflector

        class _Gateway:
            config = SimpleNamespace()

            async def call_data_process_task(self, *args, **kwargs):
                return [{"index": 1, "score": 1}]

        reflector = ExpressionReflector(SimpleNamespace(memory_engine=None), _Gateway())
        reflector._pending_reflections = [
            {
                "pattern_id": str(index),
                "chat_id": "chat-1",
                "situation": "situation",
                "expression": f"expression-{index}",
                "reply": "reply",
                "reaction": "",
                "time": float(index),
            }
            for index in range(10)
        ]

        async def _fail_adjustment(*args, **kwargs):
            raise RuntimeError("database unavailable")

        reflector._adjust_canonical_pattern_weight = _fail_adjustment

        asyncio.run(reflector.reflect_batch("chat-1"))

        self.assertEqual(
            [item["pattern_id"] for item in reflector._pending_reflections],
            ["8", "9"],
        )

    def test_bm25_orders_more_relevant_document_first_and_normalizes_high(self):
        from astrmai.memory.retrieval.bm25 import BM25Retriever

        db_path = Path(self.temp_dir.name) / "bm25.sqlite"
        with sqlite3.connect(db_path) as db:
            db.execute("CREATE TABLE documents (id INTEGER PRIMARY KEY, content TEXT, metadata TEXT)")
            db.executemany(
                "INSERT INTO documents(id, content, metadata) VALUES (?, ?, ?)",
                [
                    (1, "apple apple apple", json.dumps({"session_id": "chat-1"})),
                    (2, "apple banana", json.dumps({"session_id": "chat-1"})),
                ],
            )

        retriever = BM25Retriever(str(db_path))

        async def _run():
            await retriever.initialize()
            await retriever.add_document(1, "apple apple apple")
            await retriever.add_document(2, "apple banana")
            return await retriever.search("apple", k=2, session_id="chat-1")

        results = asyncio.run(_run())

        self.assertEqual([item.doc_id for item in results], [1, 2])
        self.assertEqual(results[0].score, 1.0)
        self.assertEqual(results[1].score, 0.0)


if __name__ == "__main__":
    unittest.main()
