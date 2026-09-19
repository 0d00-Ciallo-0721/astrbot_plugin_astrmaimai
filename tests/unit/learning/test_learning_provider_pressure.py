import asyncio
import time
from types import SimpleNamespace

import pytest

from astrmai.infrastructure.gateway.model_gateway import GlobalModelGateway
from astrmai.infrastructure.runtime.background_task_budget import BackgroundTaskBudget
from astrmai.infrastructure.runtime.lane_manager import LaneKey
from astrmai.learning.runtime.learning_lane import LearningLaneBudget, LearningLaneConfig
from astrmai.learning.runtime.provider_adapter import LearningProviderCallAdapter
from astrmai.shared.constants.defaults import GatewaySettings


class _Response:
    def __init__(self, text: str, request_id: str):
        self.completion_text = text
        self.id = request_id
        self.usage = SimpleNamespace(input=1, input_cached=0, output=1)


class _SlowLearningContext:
    def __init__(self):
        self.learning_started = asyncio.Event()
        self.release_learning = asyncio.Event()
        self.calls = 0

    async def llm_generate(self, **kwargs):
        self.calls += 1
        prompt = str(kwargs.get("prompt") or "")
        if prompt.startswith("learning-") and not self.release_learning.is_set():
            self.learning_started.set()
            await self.release_learning.wait()
        text = '{"items":[{"index":1}]}' if prompt.startswith("learning-") else "dialog-ok"
        return _Response(text, f"request-{self.calls}")


async def _wait_for_queue(lane: LearningLaneBudget, queued: int) -> None:
    for _ in range(200):
        if int(lane.status().get("queued", 0)) == queued:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"learning queue never reached {queued}: {lane.status()}")


@pytest.mark.asyncio
async def test_twenty_round_learning_pressure_preserves_dialog_reserved_slot():
    dialog_waits = []
    for round_id in range(20):
        context = _SlowLearningContext()
        settings = GatewaySettings(
            max_concurrent_llm_calls=2,
            critical_path_reserved_slots=1,
            llm_retries=0,
            api_timeout=2.0,
            semaphore_wait_timeout_sec=1.0,
            task_models=("openai/model-a",),
        )
        gateway = GlobalModelGateway(context, SimpleNamespace(), settings=settings)
        lane = LearningLaneBudget(
            LearningLaneConfig(
                limit=1,
                max_queue=8,
                admission_timeout_sec=1.0,
                execution_timeout_sec=2.0,
            )
        )
        runtime_budget = BackgroundTaskBudget(
            limit=2,
            max_queue=8,
            wait_timeout_sec=1.0,
            execution_timeout_sec=2.0,
        )
        adapter = LearningProviderCallAdapter(
            learning_lane=lane,
            runtime_budget=runtime_budget,
            gateway=gateway,
        )

        learning_tasks = [
            asyncio.create_task(
                adapter.call(
                    task_name="learning.expression_enrichment",
                    scope_id=f"chat-{round_id}-{index}",
                    prompt=f"learning-{index}",
                    hard_timeout_sec=2.0,
                )
            )
            for index in range(8)
        ]
        await asyncio.wait_for(context.learning_started.wait(), timeout=1.0)
        await _wait_for_queue(lane, 7)
        assert lane.status()["active"] == 1
        assert lane.status()["queued"] == 7

        started = time.monotonic()
        dialog = await asyncio.wait_for(
            gateway.chat_in_lane_result(
                lane_key=LaneKey(
                    subsystem="sys2",
                    task_family="dialog",
                    scope_id=f"dialog-{round_id}",
                    scope_kind="chat",
                ),
                base_origin=f"dialog-{round_id}",
                prompt="dialog",
                system_prompt="",
                models=["openai/model-a"],
                use_fallback=False,
                critical_path=True,
                reserve_for_reply=True,
            ),
            timeout=1.0,
        )
        dialog_waits.append(time.monotonic() - started)
        assert dialog.text == "dialog-ok"

        context.release_learning.set()
        results = await asyncio.gather(*learning_tasks)
        assert all(result.ok for result in results)
        assert lane.status()["active"] == 0
        assert lane.status()["queued"] == 0
        assert runtime_budget.status()["active"] == 0
        assert gateway._background_semaphore._value == 1
        assert gateway._global_semaphore._value == 2

    p95 = sorted(dialog_waits)[18]
    assert p95 < 1.0
