from __future__ import annotations

import asyncio
import importlib
import time
import types
from dataclasses import replace

from astrmai.infrastructure.runtime.trace_runtime import (
    append_trace_stage,
    ensure_external_result_id,
)


def test_external_result_id_is_stable_for_object_events_and_trace_is_bounded():
    event = types.SimpleNamespace(_extra={})
    event.get_extra = lambda key, default=None: event._extra.get(key, default)
    event.set_extra = lambda key, value: event._extra.__setitem__(key, value)

    first = ensure_external_result_id(event)
    second = ensure_external_result_id(event)
    append_trace_stage(event, "external_result.hook_enter", external_result_id=first, preview="secret")

    assert first == second
    assert first.startswith("ext-")
    assert event._extra["astrmai_trace_log"][0]["external_result_id"] == first
    assert event._extra["astrmai_trace_log"][0]["preview"] == "secret"


def test_external_result_id_and_trace_work_for_injected_dict_events():
    event = {}

    first = ensure_external_result_id(event)
    second = ensure_external_result_id(event)
    append_trace_stage(event, "external_result.kernel_tick_enter", external_result_id=first)

    assert first == second
    assert event["astrmai_external_result_id"] == first
    assert event["astrmai_trace_log"] == [
        {"trace_id": event["astrmai_trace_id"], "stage": "external_result.kernel_tick_enter", "external_result_id": first}
    ]


def test_bridge_propagates_probe_id_and_records_injection_boundaries(monkeypatch):
    bridge = importlib.import_module("astrmai.conversation.ingress.external_result_bridge")

    class Plain:
        def __init__(self, text):
            self.text = text

    class Image:
        pass

    class AttentionGate:
        def __init__(self):
            self.calls = []

        async def inject_external_event(self, chat_id, event):
            self.calls.append((chat_id, event))

    class Event:
        def __init__(self):
            self.unified_msg_origin = "default:GroupMessage:group-1"
            self._extra = {}
            self.message_obj = types.SimpleNamespace(self_id="bot-1")
            self._result = types.SimpleNamespace(chain=[Plain("done")])

        def get_extra(self, key, default=None):
            return self._extra.get(key, default)

        def set_extra(self, key, value):
            self._extra[key] = value

        def get_result(self):
            return self._result

        def get_group_id(self):
            return "group-1"

        def get_sender_id(self):
            return "user-1"

        def get_self_id(self):
            return "bot-1"

    monkeypatch.setattr(bridge, "Comp", types.SimpleNamespace(Plain=Plain, Image=Image))
    attention_gate = AttentionGate()
    runtime = types.SimpleNamespace(attention_gate=attention_gate)
    event = Event()

    asyncio.run(bridge.bridge_external_plugin_result(runtime, event))

    external_result_id = event._extra["astrmai_external_result_id"]
    payload = attention_gate.calls[0][1]
    stages = [item["stage"] for item in event._extra["astrmai_trace_log"]]
    assert payload["extra"]["astrmai_external_result_id"] == external_result_id
    assert stages == [
        "external_result.bridge_enter",
        "external_result.result_snapshot_ready",
        "external_result.bridge_ready",
        "ingress.external_result",
        "external_result.inject_start",
        "external_result.inject_done",
        "external_result.bridge_end",
    ]


def test_result_hook_returns_before_blocked_external_dispatch(monkeypatch):
    bridge = importlib.import_module("astrmai.conversation.ingress.external_result_bridge")
    sniffer = importlib.import_module("astrmai.presentation.events.result_sniffer")

    class Plain:
        def __init__(self, text):
            self.text = text

    class Image:
        pass

    class Event:
        def __init__(self):
            self.unified_msg_origin = "default:GroupMessage:group-1"
            self._extra = {}
            self.message_obj = types.SimpleNamespace(self_id="bot-1")
            self._result = types.SimpleNamespace(chain=[Plain("immutable")])

        def get_extra(self, key, default=None):
            return self._extra.get(key, default)

        def set_extra(self, key, value):
            self._extra[key] = value

        def get_result(self):
            return self._result

        def get_group_id(self):
            return "group-1"

        def get_sender_id(self):
            return "user-1"

        def get_self_id(self):
            return "bot-1"

    class Manager:
        def __init__(self):
            self.tasks = []

        def track_task(self, coro):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task

    class Gate:
        def __init__(self):
            self.started = asyncio.Event()
            self.release = asyncio.Event()
            self.calls = []

        async def inject_external_event(self, chat_id, payload):
            self.started.set()
            await self.release.wait()
            self.calls.append((chat_id, payload))

    monkeypatch.setattr(bridge, "Comp", types.SimpleNamespace(Plain=Plain, Image=Image))
    manager = Manager()
    gate = Gate()
    runtime = types.SimpleNamespace(
        attention_gate=gate,
        runtime_generation=1,
        lifecycle=types.SimpleNamespace(manager=manager, external_result_dispatcher=None),
    )
    event = Event()

    async def scenario():
        started = time.monotonic()
        await sniffer.sniff_external_plugin_results(runtime, event)
        hook_elapsed = time.monotonic() - started
        assert hook_elapsed < 0.05
        event._result.chain[0].text = "mutated-after-hook"
        await asyncio.wait_for(gate.started.wait(), timeout=0.2)
        gate.release.set()
        await asyncio.gather(*manager.tasks)

    asyncio.run(scenario())
    assert gate.calls[0][1]["content"] == "immutable"
    assert runtime.lifecycle.external_result_dispatcher.describe_status()["terminal_counts"] == {"injected": 1}


def test_dispatcher_duplicate_and_generation_mismatch_are_terminal(monkeypatch):
    dispatcher_mod = importlib.import_module("astrmai.conversation.ingress.external_result_dispatcher")
    bridge = importlib.import_module("astrmai.conversation.ingress.external_result_bridge")

    async def _injected(_runtime, _envelope):
        return "injected"

    monkeypatch.setattr(bridge, "bridge_external_plugin_result", _injected)

    class Manager:
        def __init__(self):
            self.tasks = []

        def track_task(self, coro):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task

    def make_envelope(result_id, generation):
        return dispatcher_mod.ExternalResultEnvelope(
            external_result_id=result_id,
            trace_id="trace",
            source="astrbot_builtin",
            chat_id="chat-1",
            group_id="group-1",
            sender_id="bot",
            self_id="bot",
            event_id="event",
            result_chain_hash="chain",
            text_preview_hash="text",
            text_preview="ok",
            has_image=False,
            created_at=time.time(),
            runtime_generation=generation,
            event_data={"extra": {"astrmai_external_result_id": result_id}},
        )

    async def scenario():
        manager = Manager()
        runtime = types.SimpleNamespace(
            runtime_generation=2,
            lifecycle=types.SimpleNamespace(manager=manager),
        )
        dispatcher = dispatcher_mod.ExternalResultDispatcher(runtime)
        assert dispatcher.enqueue(make_envelope("one", 1)) == "queued"
        assert dispatcher.enqueue(make_envelope("two", 2)) == "queued"
        assert dispatcher.enqueue(make_envelope("two", 2)) == "duplicate"
        await asyncio.gather(*manager.tasks)
        return dispatcher.describe_status()

    status = asyncio.run(scenario())
    assert status["terminal_counts"] == {"stale": 1, "duplicate": 1, "injected": 1}


def test_dispatcher_queue_full_does_not_wait_for_worker():
    dispatcher_mod = importlib.import_module("astrmai.conversation.ingress.external_result_dispatcher")

    class Runtime:
        runtime_generation = 1
        lifecycle = types.SimpleNamespace(manager=None)

    dispatcher = dispatcher_mod.ExternalResultDispatcher(Runtime(), queue_max=1, per_chat_max=1)
    first = dispatcher_mod.ExternalResultEnvelope(
        external_result_id="first",
        trace_id="trace",
        source="source",
        chat_id="chat",
        group_id="group",
        sender_id="bot",
        self_id="bot",
        event_id="event",
        result_chain_hash="chain",
        text_preview_hash="text",
        text_preview="ok",
        has_image=False,
        created_at=time.time(),
        runtime_generation=1,
        event_data={"extra": {}},
    )
    second = replace(first, external_result_id="second")

    async def scenario():
        # A running loop is required for worker creation; the worker cannot
        # run until this coroutine yields, so the second enqueue is immediate.
        assert dispatcher.enqueue(first) == "queued"
        assert dispatcher.enqueue(second) == "queue_full"
        dispatcher.request_shutdown()
        await dispatcher.shutdown()

    asyncio.run(scenario())
    status = dispatcher.describe_status()
    assert status["terminal_counts"] == {"queue_full": 1, "shutdown": 1}


def test_dispatcher_shutdown_marks_queued_item_and_prevents_late_injection(monkeypatch):
    dispatcher_mod = importlib.import_module("astrmai.conversation.ingress.external_result_dispatcher")
    bridge = importlib.import_module("astrmai.conversation.ingress.external_result_bridge")
    injected = []

    async def _unexpected(_runtime, envelope):
        injected.append(envelope.external_result_id)
        return "injected"

    monkeypatch.setattr(bridge, "bridge_external_plugin_result", _unexpected)

    class Manager:
        def __init__(self):
            self.tasks = []

        def track_task(self, coro):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task

    async def scenario():
        manager = Manager()
        runtime = types.SimpleNamespace(
            runtime_generation=1,
            lifecycle=types.SimpleNamespace(manager=manager),
        )
        dispatcher = dispatcher_mod.ExternalResultDispatcher(runtime)
        envelope = dispatcher_mod.ExternalResultEnvelope(
            external_result_id="late",
            trace_id="trace",
            source="source",
            chat_id="chat",
            group_id="group",
            sender_id="bot",
            self_id="bot",
            event_id="event",
            result_chain_hash="chain",
            text_preview_hash="text",
            text_preview="ok",
            has_image=False,
            created_at=time.time(),
            runtime_generation=1,
            event_data={"extra": {}},
        )
        assert dispatcher.enqueue(envelope) == "queued"
        dispatcher.request_shutdown()
        await dispatcher.shutdown()
        return dispatcher.describe_status()

    status = asyncio.run(scenario())
    assert injected == []
    assert status["terminal_counts"] == {"shutdown": 1}


def test_dispatcher_timeout_is_terminal_and_does_not_raise_to_hook(monkeypatch):
    dispatcher_mod = importlib.import_module("astrmai.conversation.ingress.external_result_dispatcher")
    bridge = importlib.import_module("astrmai.conversation.ingress.external_result_bridge")

    async def _blocked(_runtime, _envelope):
        await asyncio.sleep(0.2)

    monkeypatch.setattr(bridge, "bridge_external_plugin_result", _blocked)

    class Manager:
        def __init__(self):
            self.tasks = []

        def track_task(self, coro):
            task = asyncio.create_task(coro)
            self.tasks.append(task)
            return task

    async def scenario():
        manager = Manager()
        runtime = types.SimpleNamespace(
            runtime_generation=1,
            lifecycle=types.SimpleNamespace(manager=manager),
        )
        dispatcher = dispatcher_mod.ExternalResultDispatcher(
            runtime,
            process_timeout_seconds=0.1,
        )
        envelope = dispatcher_mod.ExternalResultEnvelope(
            external_result_id="timeout",
            trace_id="trace",
            source="source",
            chat_id="chat",
            group_id="group",
            sender_id="bot",
            self_id="bot",
            event_id="event",
            result_chain_hash="chain",
            text_preview_hash="text",
            text_preview="ok",
            has_image=False,
            created_at=time.time(),
            runtime_generation=1,
            event_data={"extra": {}},
        )
        assert dispatcher.enqueue(envelope) == "queued"
        await asyncio.gather(*manager.tasks)
        return dispatcher.describe_status()

    status = asyncio.run(scenario())
    assert status["terminal_counts"] == {"timeout": 1}
