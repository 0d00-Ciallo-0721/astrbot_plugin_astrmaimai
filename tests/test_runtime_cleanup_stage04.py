from __future__ import annotations

import asyncio
from types import SimpleNamespace

from astrmai.infrastructure.runtime.background_task_owner_registry import (
    BackgroundTaskOwnerRegistry,
)
from main import _llm_response_diagnostics


def test_owner_registry_bounds_terminal_history_and_keeps_active_records():
    async def run():
        now = [100.0]
        registry = BackgroundTaskOwnerRegistry(
            max_terminal_records=2,
            terminal_ttl_sec=10.0,
            clock=lambda: now[0],
        )

        first = registry.register_rejected(task_family="demo", scope_id="scope", run_id="one")
        now[0] = 105.0
        second = registry.register_rejected(task_family="demo", scope_id="scope", run_id="two")
        now[0] = 106.0
        third = registry.register_rejected(task_family="demo", scope_id="scope", run_id="three")
        assert {item["task_id"] for item in registry.describe()["tasks"]} == {second, third}

        async def pending():
            await asyncio.Event().wait()

        active_task = asyncio.create_task(pending())
        active = registry.register(
            active_task,
            task_family="demo",
            scope_id="scope",
            run_id="active",
        )

        assert {item["task_id"] for item in registry.describe()["tasks"]} == {second, third, active}

        now[0] = 117.0
        described = registry.describe()
        assert described["active"] == 1
        assert described["terminal_count"] == 0
        assert [item["task_id"] for item in described["tasks"]] == [active]

        active_task.cancel()
        await asyncio.gather(active_task, return_exceptions=True)

    asyncio.run(run())


def test_llm_response_diagnostics_excludes_completion_text():
    event = SimpleNamespace(
        unified_msg_origin="chat-1",
        get_extra=lambda name: {
            "astrmai_request_trace": {
                "provider_id": "provider-a",
                "turn_id": "turn-9",
                "generation": 4,
            }
        }.get(name),
    )
    response = SimpleNamespace(completion_text="SECRET USER CONTENT")

    diagnostics = _llm_response_diagnostics(event, response)

    assert diagnostics["chat_id"] == "chat-1"
    assert diagnostics["provider_id"] == "provider-a"
    assert diagnostics["turn_id"] == "turn-9"
    assert diagnostics["generation"] == 4
    assert diagnostics["completion_length"] == len("SECRET USER CONTENT")
    assert diagnostics["completion_sha256"]
    assert "SECRET USER CONTENT" not in repr(diagnostics)
