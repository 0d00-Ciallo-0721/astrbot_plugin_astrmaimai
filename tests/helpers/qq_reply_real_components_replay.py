"""Offline replay with installed AstrBot components; never installs stubs."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from astrbot.api import message_components as Comp
from PIL import Image

from astrmai.conversation.attention.gate import AttentionGate
from astrmai.conversation.attention.group_dialogue_store import GroupDialogueStore
from astrmai.conversation.attention.perception import PerceptionBuilder
from astrmai.conversation.contracts.turn_identity import TurnIdentity
from astrmai.conversation.execution.executor import ConcurrentExecutor
from astrmai.conversation.execution.reply_service import ReplyService
from astrmai.conversation.ingress.sensors import PreFilters
from astrmai.infrastructure.runtime.chat_runtime_coordinator import ChatRuntimeCoordinator
from astrmai.multimodal.napcat_image_resolver import NapCatImageResolver
from tests.helpers.reply_engine_stubs import FakeEvent, FakeStateEngine


async def replay():
    assert Comp.Reply.__module__.startswith("astrbot.")
    image_path = Path.cwd() / "real-component-image.png"
    Image.new("RGB", (8, 8), "blue").save(image_path)
    evidence = []
    for case in ("loaded_text", "fetched_text", "unknown", "api_failure", "embedded_image", "fetched_image", "inline_image", "quote", "quote_only", "quote_false", "image_timeout_text", "image_timeout_dependent"):
        state = FakeStateEngine()
        config = state.config
        config.system1 = SimpleNamespace(nicknames=[], extra_command_list=[])
        config.global_settings.command_prefixes = ["/"]
        config.global_settings.enable_error_interception = False
        config.global_settings.admin_ids = []
        config.agent = SimpleNamespace(max_steps=5, timeout=10)
        config.infra = SimpleNamespace(api_timeout=15)
        config.vision = SimpleNamespace(enable_vision=True, image_recognition_probability=1.0,
                                       vision_reply_policy="timeout_fallback", max_images_per_turn=1,
                                       ignore_placeholder_without_question=True)
        coordinator = ChatRuntimeCoordinator()
        service = ReplyService(state_engine=state, mood_manager=SimpleNamespace(), runtime_coordinator=coordinator)
        service.dialogue_store = GroupDialogueStore()
        service._settle_post_send = AsyncMock()
        host_send = AsyncMock(return_value=case != "quote_false")
        state.gateway.context.send_message = host_send
        vision = AsyncMock(return_value={"type": "image", "description": "一张蓝色图片。", "emotion_tags": []})
        state.gateway.get_agent_models = lambda: ["offline-model"]
        state.gateway.chat_in_lane_result = AsyncMock(return_value=SimpleNamespace(text="文本回答"))
        text = "看图顺便告诉我天气" if case == "image_timeout_text" else "这张图里是什么" if case == "image_timeout_dependent" else "解释这句话"
        event = FakeEvent("123", "Alice", text)
        event.get_platform_name = lambda: "aiocqhttp"
        image = Comp.Image.fromFileSystem(str(image_path))
        reply = Comp.Reply(id=88)
        if case in {"loaded_text", "quote", "quote_only", "quote_false"}:
            reply = Comp.Reply(id=88, chain=[Comp.Plain(text="引用的纯文本")], message_str="引用的纯文本")
        elif case == "embedded_image":
            reply = Comp.Reply(id=88, chain=[image])
        components = [reply, Comp.At(qq="bot-1"), Comp.Plain(text=text)]
        if case in {"inline_image", "image_timeout_text", "image_timeout_dependent"}:
            components = [Comp.At(qq="bot-1"), Comp.Plain(text=text), image]
        raw = {"message": [{"type": "reply", "data": {"id": "88"}},
                           {"type": "at", "data": {"qq": "bot-1"}},
                           {"type": "text", "data": {"text": text}}],
               "replyElement": {"replyAbsElemType": 1, "picElem": None}}
        event.message_obj = SimpleNamespace(message=components, message_id="99", self_id="bot-1", raw_message=raw)
        payload = {"message": [{"type": "text", "data": {"text": "quoted text"}}], "picElem": None}
        if case == "fetched_image":
            payload = {"message": [{"type": "image", "data": {"file": str(image_path)}}]}
        elif case == "unknown":
            payload = {"message": [{"type": "unknown", "data": {}}]}
        api = AsyncMock(return_value=payload)
        if case == "api_failure":
            api.side_effect = RuntimeError("offline get_msg failure")
        event.bot = SimpleNamespace(api=SimpleNamespace(call_action=api))
        event.set_extra("astrmai_trace_id", case)
        event.set_extra("astrmai_attempt_id", case + "-attempt")
        generation = await coordinator.advance_generation(event.unified_msg_origin, "offline-thread")
        event.set_extra("astrmai_turn_identity", TurnIdentity(mode="group", chat_id=event.unified_msg_origin,
                                                              thread_id="offline-thread", generation=generation))
        filters = PreFilters(config)
        filters._commands_loaded = True
        assert await filters.should_process_message(event)
        gate = AttentionGate.__new__(AttentionGate)
        gate.config = config
        gate.sensors = filters
        perception = PerceptionBuilder(gate).build(event)
        assert perception.is_at_bot and perception.text == text
        assert event.message_obj.message == components
        candidates = event.get_extra("astrmai_vision_candidates", [])
        has_image = case in {"embedded_image", "fetched_image", "inline_image", "image_timeout_text", "image_timeout_dependent"}
        assert len(candidates) == int(has_image), (case, candidates)
        assert len(perception.image_urls) == int(has_image), (case, perception.image_urls)
        assert event.get_extra("astrmai_image_raw_component_count") == int(has_image)
        if candidates:
            event.set_extra("astrmai_final_vision_target", candidates[0])
        if case.startswith("quote"):
            event.set_extra("astrmai_pending_actions", [{"action": "quote_reply", "action_instance_id": "offline-quote",
                "message_id": "88", "group_id": "group-1", "payload": {"text": "引用第一段\n\n引用第二段"}}])
        resolver = NapCatImageResolver(Path.cwd() / case / "image-cache", config)
        if case.startswith("image_timeout"):
            resolver.resolve_candidate = AsyncMock(return_value=SimpleNamespace(
                had_images=True, images=[], failures=["timeout"], failure_details=[]))
        executor = ConcurrentExecutor(context=state.gateway.context, gateway=state.gateway, reply_engine=service,
            evolution_manager=SimpleNamespace(process_bot_reply=AsyncMock()), config=config,
            runtime_coordinator=coordinator, image_resolver=resolver,
            visual_cortex=SimpleNamespace(analyze_image_path=vision))
        if case in {"quote_only", "quote_false"}:
            artifact = await service.handle_reply(event, "" if case == "quote_only" else "普通正文", event.unified_msg_origin)
            result = artifact.persistable_text if artifact.sent else None
        else:
            result = await executor.execute(event, prompt=text, system_prompt="stable-system", direct_vision_urls=perception.image_urls)
        assert host_send.await_count == 1, (case, host_send.await_count)
        chain = host_send.await_args.args[1].chain
        body = "".join(c.text for c in chain if isinstance(c, Comp.Plain))
        turns = await service.dialogue_store.get_recent_bot_turns(event.unified_msg_origin, target_sender_id="123")
        key = event.get_extra("astrmai_reply_send_key")
        claim = await coordinator.get_send_claim(event.unified_msg_origin, key)
        if case == "quote_false":
            assert result is None and not turns
            assert not event.get_extra("astrmai_reply_sent", False)
            assert claim["status"] == "failed"
        else:
            assert event.get_extra("astrmai_reply_sent") and len(turns) == 1
            assert turns[0].reply_text == result == body
            assert claim["status"] == "committed"
        assert claim["outbound_message_ids"] == []  # True is an acknowledgement, not a message ID.
        if case.startswith("quote"):
            assert isinstance(chain[0], Comp.Reply) and str(chain[0].id) == "88"
            assert body == "引用第一段\n引用第二段"
            api.assert_not_awaited()
        elif case == "image_timeout_text":
            assert event.get_extra("astrmai_vision_failure_disposition") == "continue_text_only"
            assert result == "文本回答" and vision.await_count == 0
        elif case == "image_timeout_dependent":
            assert event.get_extra("astrmai_vision_failure_disposition") == "notify_failure"
            assert "无法确认图片内容" in body and vision.await_count == 0
        else:
            assert vision.await_count == int(has_image), (case, vision.await_count)
            assert "无法确认图片内容" not in body and "图片识别失败" not in body
        if service._post_send_tasks:
            await asyncio.gather(*service._post_send_tasks)
        evidence.append({"case": case, "image_count": len(perception.image_urls), "vision_calls": vision.await_count,
                         "host_send_calls": host_send.await_count, "claim": claim["status"],
                         "history_turns": len(turns), "body": body})
    print("REAL_COMPONENT_REPLAY=" + json.dumps(evidence, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(replay())
