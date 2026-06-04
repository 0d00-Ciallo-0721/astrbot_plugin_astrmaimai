from __future__ import annotations

import argparse
import asyncio
import importlib
import json
import os
import contextlib
import sys
import tempfile
import types
from pathlib import Path
from types import SimpleNamespace

from tests.helpers.astrbot_stubs import install_astrbot_stubs


HOST_MOOD_SAMPLE_CASES = [
    {
        "case_id": "positive_short",
        "text": "谢谢你呀，我真的好开心，贴贴。",
        "sender_id": "user-positive",
        "sender_name": "Positive",
        "expected_tone": "positive",
        "expected_social_direction": "positive",
        "min_expected_social_score": 1.0,
    },
    {
        "case_id": "hostile_short",
        "text": "闭嘴，烦死了，你真讨厌。",
        "sender_id": "user-hostile",
        "sender_name": "Hostile",
        "expected_tone": "negative",
        "expected_social_direction": "negative",
        "max_expected_social_score": -1.0,
    },
    {
        "case_id": "mixed_short",
        "text": "谢谢你，但我还是有点难过。",
        "sender_id": "user-mixed",
        "sender_name": "Mixed",
        "expected_tone": "mixed",
        "expected_social_direction": "positive",
        "min_expected_social_score": 0.2,
        "max_expected_social_score": 0.4,
    },
    {
        "case_id": "comfort_complaint_short",
        "text": "抱抱，谢谢你愿意安慰我，但你刚才那句还是让我有点受伤。",
        "sender_id": "user-comfort-complaint",
        "sender_name": "ComfortComplaint",
        "expected_tone": "mixed",
        "expected_social_direction": "positive",
        "min_expected_social_score": 0.2,
        "max_expected_social_score": 0.4,
    },
    {
        "case_id": "ambiguous_soft_affection_short",
        "text": "晚安呀，早点休息，别太累了。",
        "sender_id": "user-ambiguous",
        "sender_name": "Ambiguous",
        "expected_tone": "neutral",
        "expected_social_direction": "positive",
        "min_expected_social_score": 0.12,
        "max_expected_social_score": 0.24,
    },
    {
        "case_id": "tool_intent_short",
        "text": "帮我查一下明天上海天气。",
        "sender_id": "user-tool",
        "sender_name": "Tool",
        "expected_tone": "neutral",
        "expected_social_direction": "positive",
        "min_expected_social_score": 0.3,
        "max_expected_social_score": 0.45,
    },
    {
        "case_id": "sarcasm_short",
        "text": "你可真行啊，又把事情搞砸了，真棒。",
        "sender_id": "user-sarcasm",
        "sender_name": "Sarcasm",
        "expected_tone": "negative",
        "expected_social_direction": "negative",
        "min_expected_social_score": -3.0,
        "max_expected_social_score": -1.0,
    },
    {
        "case_id": "cold_distance_short",
        "text": "哦，那你先忙吧，我就不打扰了。",
        "sender_id": "user-cold",
        "sender_name": "Cold",
        "expected_tone": "neutral",
        "expected_social_direction": "negative",
        "min_expected_social_score": -0.30,
        "max_expected_social_score": -0.20,
    },
    {
        "case_id": "perfunctory_brief_short",
        "text": "哦，行吧，就这样。",
        "sender_id": "user-perfunctory",
        "sender_name": "Perfunctory",
        "expected_tone": "neutral",
        "expected_social_direction": "negative",
        "min_expected_social_score": -0.40,
        "max_expected_social_score": -0.30,
    },
    {
        "case_id": "mild_irritation_short",
        "text": "行了，别说了，我知道了。",
        "sender_id": "user-irritation",
        "sender_name": "Irritation",
        "expected_tone": "neutral",
        "expected_social_direction": "negative",
        "min_expected_social_score": -0.55,
        "max_expected_social_score": -0.40,
    },
    {
        "case_id": "long_mixed_balance_short",
        "text": "谢谢你一直愿意听我说这些，我知道你是好意，但刚才那句还是让我有点失望和不舒服。",
        "sender_id": "user-long-mixed",
        "sender_name": "LongMixed",
        "expected_tone": "mixed",
        "expected_social_direction": "positive",
        "min_expected_social_score": 0.2,
        "max_expected_social_score": 0.4,
    },
]


def _purge_modules(prefixes: tuple[str, ...]) -> None:
    for name in list(sys.modules.keys()):
        if any(name == prefix or name.startswith(prefix + ".") for prefix in prefixes):
            sys.modules.pop(name, None)


def _install_extended_astrbot_stubs(data_dir: str) -> None:
    install_astrbot_stubs(data_dir)
    event_mod = sys.modules["astrbot.api.event"]
    comp_mod = sys.modules["astrbot.api.message_components"]
    command_mod = sys.modules["astrbot.core.star.command_management"]

    class _At:
        def __init__(self, qq=""):
            self.qq = qq

    class _Plain:
        def __init__(self, text=""):
            self.text = text

    class _MessageChain:
        def __init__(self, message=None):
            self.message = list(message or [])

    async def _list_commands():
        return []

    comp_mod.At = _At
    comp_mod.Plain = _Plain
    event_mod.MessageChain = _MessageChain
    command_mod.list_commands = _list_commands


class _FakePersistence:
    def __init__(self):
        self.saved_chat_states: list[tuple[str, float, float]] = []
        self.user_profiles: dict[str, object] = {}

    async def load_chat_state(self, chat_id):
        return None

    async def save_chat_state(self, chat_id, state):
        self.saved_chat_states.append((chat_id, float(state.energy), float(state.mood)))
        return None

    async def load_user_profile(self, user_id):
        return self.user_profiles.get(user_id)

    async def save_user_profile(self, user_id, profile):
        self.user_profiles[str(user_id)] = profile
        return None


class _HostAuditEvent:
    def __init__(
        self,
        *,
        chat_id: str,
        sender_id: str,
        sender_name: str,
        text: str,
        self_id: str = "bot-1",
        group_id: str = "group-1",
        message=None,
    ):
        self.unified_msg_origin = chat_id
        self.message_str = text
        self.timestamp = 0.0
        self._sender_id = sender_id
        self._sender_name = sender_name
        self._self_id = self_id
        self._group_id = group_id
        self._extra: dict[str, object] = {}
        self.message_obj = SimpleNamespace(self_id=self_id, message=list(message or []))

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_group_id(self):
        return self._group_id

    def get_self_id(self):
        return self._self_id

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def plain_result(self, text):
        return {"type": "plain", "text": text}


class _Facade:
    # NOTE: record_and_dispatch_attention 简化返回 "BUFFERED"——
    # mood chain audit 关注 mood extras 而非 ghost suppression，
    # 因此不需要按 @/Reply/私聊 分发状态。

    def __init__(self, runtime):
        self.runtime = runtime

    def is_framework_command(self, msg: str) -> bool:
        return False

    async def update_user_stats(self, user_id: str):
        return None

    async def handle_poke(self, event):
        from astrmai.presentation.dto.message_scope import IngressDecision
        return IngressDecision.allow()

    def check_message_scope_access(self, scope):
        from astrmai.presentation.dto.message_scope import IngressDecision
        return IngressDecision.allow()

    async def handle_group_reply_wait(self, event, scope):
        return "NONE"

    def is_debug_mode(self) -> bool:
        return False

    def track_incoming_user_activity(self, sender_id: str) -> None:
        pass

    async def try_consume_reflect_feedback(self, event):
        return None

    async def record_and_dispatch_attention(self, event, scope):
        await self.runtime.evolution.record_user_message(event)
        if getattr(self.runtime, "chat_loop_kernel", None) is not None:
            tick_result = await self.runtime.chat_loop_kernel.tick(
                chat_id=scope.chat_id,
                trigger="message",
                event=event,
            )
            return tick_result.dispatch_result
        return await self.runtime.attention_gate.process_event(event)

    def cancel_group_wait_if_interrupted(self, event, result, status) -> None:
        pass

    def suppress_default_llm_if_engaged(self, event, status, is_direct_call):
        if status == "ENGAGED" or is_direct_call:
            return self.runtime.host_bridge.suppress_default_llm(event)
        return None


class _LifecycleManager:
    def track_task(self, coro):
        return asyncio.create_task(coro)


def _reset_host_modules() -> None:
    _purge_modules(
        (
            "astrmai.presentation.events.message_entry",
            "astrmai.conversation.attention.gate",
            "astrmai.conversation.ingress.sensors",
            "astrmai.state.chat_state_service",
            "astrmai.state.mood.mood_manager",
        )
    )


def _load_runtime_modules():
    _reset_host_modules()
    state_mod = importlib.import_module("astrmai.state.chat_state_service")
    state_mod = importlib.reload(state_mod)
    gate_mod = importlib.import_module("astrmai.conversation.attention.gate")
    gate_mod = importlib.reload(gate_mod)
    sensors_mod = importlib.import_module("astrmai.conversation.ingress.sensors")
    sensors_mod = importlib.reload(sensors_mod)
    entry_mod = importlib.import_module("astrmai.presentation.events.message_entry")
    entry_mod = importlib.reload(entry_mod)
    mood_mod = importlib.import_module("astrmai.state.mood.mood_manager")
    mood_mod = importlib.reload(mood_mod)
    return state_mod, gate_mod, sensors_mod, entry_mod, mood_mod


def _resolve_gateway_factory(factory=None):
    if factory is not None:
        return factory
    factory_spec = os.getenv("ASTRMAI_HOST_MOOD_GATEWAY_FACTORY", "").strip() or os.getenv(
        "ASTRMAI_LIVE_MOOD_GATEWAY_FACTORY", ""
    ).strip()
    if not factory_spec:
        return None
    module_name, attr_name = factory_spec.rsplit(".", 1)
    module = importlib.import_module(module_name)
    return getattr(module, attr_name)


def _build_runtime_config(gateway):
    gateway_config = getattr(gateway, "config", SimpleNamespace())
    reply_cfg = getattr(gateway_config, "reply", SimpleNamespace(emotion_mapping=[]))
    provider_cfg = getattr(gateway_config, "provider", SimpleNamespace(task_models=[]))
    return SimpleNamespace(
        energy=SimpleNamespace(
            cost_per_reply=0.1,
            min_reply_threshold=0.1,
            daily_recovery=0.1,
            recovery_silence_min=60,
        ),
        mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
        reply=SimpleNamespace(
            emotion_mapping=list(getattr(reply_cfg, "emotion_mapping", []) or []),
            segment_min_len=4,
            no_segment_max_len=200,
            meme_probability=0,
            fallback_text="...",
            typing_speed_factor=0.0,
        ),
        provider=SimpleNamespace(task_models=list(getattr(provider_cfg, "task_models", []) or [])),
        system1=SimpleNamespace(extra_command_list=[], nicknames=["Mai"]),
        global_settings=SimpleNamespace(
            debug_mode=False,
            whitelist_ids=[],
            admin_ids=[],
            enable_private_chat=True,
            command_prefixes=["/"],
        ),
    )


async def _collect_asyncgen(gen) -> list:
    return [item async for item in gen]


async def _run_host_case(
    *,
    gateway,
    case: dict,
    state_mod,
    gate_mod,
    sensors_mod,
    entry_mod,
):
    config = _build_runtime_config(gateway)
    persistence = _FakePersistence()
    state_engine = state_mod.StateEngine(persistence, gateway, config=config)
    sensors = sensors_mod.PreFilters(config)
    published_change: dict[str, object] = {}

    async def _capture_publish(user_id, old_score, new_score, mood_tag, event_type):
        published_change.update(
            {
                "user_id": str(user_id or ""),
                "old_score": float(old_score),
                "new_score": float(new_score),
                "mood_tag": str(mood_tag or ""),
                "event_type": str(event_type or ""),
            }
        )

    state_engine.affection_router.publish_change = _capture_publish

    async def _record_user_message(event):
        return None

    async def _sys2_noop(event, queue_events):
        return None

    attention_gate = gate_mod.AttentionGate(
        state_engine=state_engine,
        judge=None,
        sensors=sensors,
        system2_callback=_sys2_noop,
        config=config,
        private_chat_manager=None,
    )
    runtime = SimpleNamespace(
        config=config,
        group_reply_wait_manager=None,
        lifecycle=SimpleNamespace(manager=_LifecycleManager()),
        reflect_tracker=None,
        evolution=SimpleNamespace(record_user_message=_record_user_message),
        attention_gate=attention_gate,
        host_bridge=SimpleNamespace(suppress_default_llm=lambda event: "(ghost)"),
        sensors=sensors,
        context=SimpleNamespace(),
        chat_loop_kernel=None,
    )
    facade = _Facade(runtime)

    mood_manager = importlib.import_module("astrmai.state.mood.mood_manager").MoodManager(gateway, config)
    direct_tag, direct_value = await mood_manager.analyze_mood(case["text"], 0.0, chat_id=f"direct:{case['case_id']}")

    comp_mod = importlib.import_module("astrbot.api.message_components")
    event = _HostAuditEvent(
        chat_id=f"default:GroupMessage:{case['case_id']}",
        sender_id=case["sender_id"],
        sender_name=case["sender_name"],
        text=case["text"],
        group_id=case["case_id"],
        message=[comp_mod.At("bot-1"), comp_mod.Plain(case["text"])],
    )
    results = await _collect_asyncgen(entry_mod.handle_global_message(facade, event))
    host_chat_id = str(event.get_group_id() or event.unified_msg_origin)
    state = await state_engine.get_state(host_chat_id)
    pending = list(getattr(attention_gate, "_background_tasks", set()) or [])
    for task in pending:
        task.cancel()
    if pending:
        await asyncio.gather(*pending, return_exceptions=True)
    host_tag = str(event.get_extra("astrmai_primary_mood_tag", "") or "")
    host_value = float(event.get_extra("astrmai_primary_mood_value", 0.0) or 0.0)
    return {
        "case_id": case["case_id"],
        "text": case["text"],
        "expected_tone": case.get("expected_tone", ""),
        "direct_tag": str(direct_tag or ""),
        "direct_value": float(direct_value),
        "host_tag": host_tag,
        "host_value": host_value,
        "host_source": str(event.get_extra("astrmai_primary_mood_source", "") or ""),
        "host_status": "ENGAGED" if results else "NO_REPLY",
        "host_results": results,
        "saved_state_mood": float(getattr(state, "mood", 0.0) or 0.0),
        "matched": host_tag == str(direct_tag or "") and abs(host_value - float(direct_value)) < 1e-6,
    }


async def _build_host_mood_chain_baseline_async(gateway_factory=None) -> dict:
    resolved_factory = _resolve_gateway_factory(gateway_factory)
    if resolved_factory is None:
        return {
            "status": "not_run",
            "reason": "missing host mood gateway factory",
            "cases": [],
            "all_matched": False,
        }

    with tempfile.TemporaryDirectory(prefix="astrmai-host-mood-") as temp_dir:
        _install_extended_astrbot_stubs(temp_dir)
        state_mod, gate_mod, sensors_mod, entry_mod, _ = _load_runtime_modules()
        gateway = resolved_factory()
        cases = []
        for sample in HOST_MOOD_SAMPLE_CASES:
            cases.append(
                await _run_host_case(
                    gateway=gateway,
                    case=sample,
                    state_mod=state_mod,
                    gate_mod=gate_mod,
                    sensors_mod=sensors_mod,
                    entry_mod=entry_mod,
                )
            )

    return {
        "status": "passed" if all(case["matched"] for case in cases) else "drift_detected",
        "reason": "host message entry mood audit executed",
        "model": str((getattr(gateway, "task_models", []) or [""])[0] or ""),
        "cases": cases,
        "all_matched": all(case["matched"] for case in cases),
    }


def build_host_mood_chain_baseline(gateway_factory=None) -> dict:
    return asyncio.run(_build_host_mood_chain_baseline_async(gateway_factory=gateway_factory))


def _render_markdown(payload: dict) -> str:
    lines = [
        "# Host Mood Chain Audit",
        "",
        f"- status: `{payload.get('status', 'unknown')}`",
        f"- model: `{payload.get('model', '')}`",
        f"- all matched: `{payload.get('all_matched', False)}`",
        "",
        "## Cases",
    ]
    for case in payload.get("cases", []) or []:
        lines.extend(
            [
                f"- `{case['case_id']}`",
                f"  - text: `{case['text']}`",
                f"  - direct: `{case['direct_tag']}` / `{case['direct_value']:.4f}`",
                f"  - host: `{case['host_tag']}` / `{case['host_value']:.4f}`",
                f"  - source: `{case['host_source']}`",
                f"  - matched: `{case['matched']}`",
            ]
        )
    return "\n".join(lines) + "\n"


def write_host_mood_chain_artifacts(base_dir: str | Path, gateway_factory=None) -> dict:
    payload = build_host_mood_chain_baseline(gateway_factory=gateway_factory)
    base_path = Path(base_dir)
    base_path.mkdir(parents=True, exist_ok=True)
    json_path = base_path / "host_mood_chain_audit.json"
    md_path = base_path / "host_mood_chain_audit.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_markdown(payload), encoding="utf-8")
    return {"json_path": str(json_path), "markdown_path": str(md_path), "payload": payload}


def _settlement_delta(tag: str) -> float:
    normalized = str(tag or "").strip().lower()
    if normalized == "happy":
        return 0.1
    if normalized in {"sad", "angry"}:
        return -0.1
    return 0.0


def _social_score_direction(score: float) -> str:
    if score >= 0.2:
        return "positive"
    if score <= -0.2:
        return "negative"
    return "neutral"


def _social_score_amplitude_issue(case: dict, actual_score: float) -> str:
    expected_direction = str(case.get("expected_social_direction", "") or "")
    actual_direction = _social_score_direction(actual_score)
    if expected_direction and actual_direction != expected_direction:
        return f"direction_mismatch:{expected_direction}->{actual_direction}"

    min_expected = case.get("min_expected_social_score")
    if min_expected is not None and actual_score < float(min_expected):
        return f"below_range:{actual_score:.2f}<{float(min_expected):.2f}"

    max_expected = case.get("max_expected_social_score")
    if max_expected is not None and actual_score > float(max_expected):
        return f"above_range:{actual_score:.2f}>{float(max_expected):.2f}"
    return ""


async def _expected_social_score(gateway, *, user_id: str, chat_id: str, mood_tag: str, message_text: str) -> float:
    state_mod = importlib.import_module("astrmai.state.chat_state_service")
    config = _build_runtime_config(gateway)
    engine = state_mod.StateEngine(_FakePersistence(), gateway, config=config)
    await engine.calculate_and_update_affection(
        user_id=user_id,
        group_id=chat_id,
        mood_tag=mood_tag,
        intensity=1.0,
        message_text=message_text,
    )
    profile = await engine.get_user_profile(user_id)
    return float(getattr(profile, "social_score", 0.0) or 0.0)


@contextlib.asynccontextmanager
async def _patched_post_send_send_meme():
    post_send_mod = importlib.import_module("astrmai.conversation.execution.reply_post_send")
    original = post_send_mod.send_meme

    async def _noop_send_meme(**kwargs):
        return None

    post_send_mod.send_meme = _noop_send_meme
    try:
        yield
    finally:
        post_send_mod.send_meme = original


async def _run_host_post_send_case(
    *,
    gateway,
    case: dict,
    state_mod,
    gate_mod,
    sensors_mod,
    entry_mod,
    reply_mod,
):
    config = _build_runtime_config(gateway)
    persistence = _FakePersistence()
    state_engine = state_mod.StateEngine(persistence, gateway, config=config)
    sensors = sensors_mod.PreFilters(config)
    published_change: dict[str, object] = {}

    async def _capture_publish(user_id, old_score, new_score, mood_tag, event_type):
        published_change.update(
            {
                "user_id": str(user_id or ""),
                "old_score": float(old_score),
                "new_score": float(new_score),
                "mood_tag": str(mood_tag or ""),
                "event_type": str(event_type or ""),
            }
        )

    state_engine.affection_router.publish_change = _capture_publish

    async def _record_user_message(event):
        return None

    async def _signal_new_message(sender_id, msg_str, chat_id=None):
        return None

    attention_gate = gate_mod.AttentionGate(
        state_engine=state_engine,
        judge=None,
        sensors=sensors,
        system2_callback=None,
        config=config,
        private_chat_manager=SimpleNamespace(signal_new_message=_signal_new_message),
    )
    runtime = SimpleNamespace(
        config=config,
        group_reply_wait_manager=None,
        lifecycle=SimpleNamespace(manager=_LifecycleManager()),
        reflect_tracker=None,
        evolution=SimpleNamespace(record_user_message=_record_user_message),
        attention_gate=attention_gate,
        host_bridge=SimpleNamespace(suppress_default_llm=lambda event: "(ghost)"),
        sensors=sensors,
        context=SimpleNamespace(),
        chat_loop_kernel=None,
    )
    facade = _Facade(runtime)
    mood_manager = importlib.import_module("astrmai.state.mood.mood_manager").MoodManager(gateway, config)
    chat_id = f"default:FriendMessage:{case['sender_id']}"
    event = _HostAuditEvent(
        chat_id=chat_id,
        sender_id=case["sender_id"],
        sender_name=case["sender_name"],
        text=case["text"],
        group_id="",
        message=[],
    )

    direct_tag, direct_value = await mood_manager.analyze_mood(case["text"], 0.0, chat_id=f"direct_post:{case['case_id']}")
    host_results = await _collect_asyncgen(entry_mod.handle_global_message(facade, event))
    host_tag = str(event.get_extra("astrmai_primary_mood_tag", "") or "")
    host_value = float(event.get_extra("astrmai_primary_mood_value", 0.0) or 0.0)
    event.set_extra("astrmai_bypass_mood_analysis", host_tag)

    service = reply_mod.ReplyService(
        state_engine=state_engine,
        mood_manager=mood_manager,
        config=config,
        runtime_coordinator=None,
        memory_engine=None,
    )

    async def _send_ok(*args, **kwargs):
        return True

    service._send_segments = _send_ok
    async with _patched_post_send_send_meme():
        await service.handle_reply(
            event,
            "好的，我收到你的消息了。",
            chat_id,
            bypassed_tag=host_tag,
            window_events=[event],
            anchor_event=event,
        )

    final_state = await state_engine.get_state(chat_id)
    profile = await state_engine.get_user_profile(case["sender_id"])
    expected_final_mood = max(-1.0, min(1.0, host_value + _settlement_delta(host_tag)))
    expected_social_score = await _expected_social_score(
        gateway,
        user_id=case["sender_id"],
        chat_id=chat_id,
        mood_tag=host_tag,
        message_text=case["text"],
    )
    actual_social_score = float(getattr(profile, "social_score", 0.0) or 0.0)
    resolved_text_event_type = state_engine._resolve_affection_event_type(case["text"])
    softened_support_event = state_engine.relationship_engine.should_soften_support_event_for_message(
        case["text"],
        resolved_text_event_type,
    )
    effective_base_event = (
        state_mod.RelationshipEvent.NORMAL_CHAT if softened_support_event else resolved_text_event_type
    )
    mood_tag_remap_suppressed = (
        effective_base_event == state_mod.RelationshipEvent.NORMAL_CHAT
        and state_engine.relationship_engine.should_preserve_normal_chat_for_message(case["text"], host_tag)
    )
    effective_event_type = (
        effective_base_event
        if mood_tag_remap_suppressed or not host_tag or effective_base_event != state_mod.RelationshipEvent.NORMAL_CHAT
        else state_engine.relationship_engine.MOOD_TO_EVENT.get(host_tag, effective_base_event)
    )
    amplitude_issue = _social_score_amplitude_issue(case, actual_social_score)
    return {
        "case_id": case["case_id"],
        "text": case["text"],
        "direct_tag": str(direct_tag or ""),
        "direct_value": float(direct_value),
        "host_tag": host_tag,
        "host_value": host_value,
        "post_send_tag": host_tag,
        "resolved_text_event_type": resolved_text_event_type,
        "effective_event_type": effective_event_type,
        "mood_tag_remap_suppressed": bool(mood_tag_remap_suppressed),
        "published_mood_tag": str(published_change.get("mood_tag", "")),
        "published_event_type": str(published_change.get("event_type", "")),
        "expected_final_mood": float(expected_final_mood),
        "actual_final_mood": float(getattr(final_state, "mood", 0.0) or 0.0),
        "expected_social_score": float(expected_social_score),
        "actual_social_score": actual_social_score,
        "expected_social_direction": case.get("expected_social_direction", ""),
        "actual_social_direction": _social_score_direction(actual_social_score),
        "social_score_amplitude_issue": amplitude_issue,
        "host_results": host_results,
        "mood_matched": abs(float(getattr(final_state, "mood", 0.0) or 0.0) - float(expected_final_mood)) < 1e-6,
        "social_score_matched": abs(actual_social_score - float(expected_social_score)) < 1e-6,
    }


async def _build_host_reply_post_send_baseline_async(gateway_factory=None) -> dict:
    resolved_factory = _resolve_gateway_factory(gateway_factory)
    if resolved_factory is None:
        return {
            "status": "not_run",
            "reason": "missing host mood gateway factory",
            "cases": [],
            "all_matched": False,
        }

    with tempfile.TemporaryDirectory(prefix="astrmai-host-post-send-") as temp_dir:
        _install_extended_astrbot_stubs(temp_dir)
        state_mod, gate_mod, sensors_mod, entry_mod, _ = _load_runtime_modules()
        _purge_modules(("astrmai.conversation.execution.reply_service", "astrmai.conversation.execution.reply_post_send"))
        reply_mod = importlib.import_module("astrmai.conversation.execution.reply_service")
        reply_mod = importlib.reload(reply_mod)
        gateway = resolved_factory()
        cases = []
        for sample in HOST_MOOD_SAMPLE_CASES:
            cases.append(
                await _run_host_post_send_case(
                    gateway=gateway,
                    case=sample,
                    state_mod=state_mod,
                    gate_mod=gate_mod,
                    sensors_mod=sensors_mod,
                    entry_mod=entry_mod,
                    reply_mod=reply_mod,
                )
            )

    amplitude_issues = [case["case_id"] for case in cases if case.get("social_score_amplitude_issue")]
    publish_change_semantics_aligned = all(
        case["published_mood_tag"] == ("" if case["mood_tag_remap_suppressed"] else case["host_tag"])
        and case["published_event_type"] == case["effective_event_type"]
        for case in cases
    )
    return {
        "status": "passed"
        if all(case["mood_matched"] and case["social_score_matched"] for case in cases) and not amplitude_issues
        else "drift_detected",
        "reason": "host reply_post_send audit executed",
        "model": str((getattr(gateway, "task_models", []) or [""])[0] or ""),
        "cases": cases,
        "all_matched": all(case["mood_matched"] and case["social_score_matched"] for case in cases) and not amplitude_issues,
        "amplitude_issue_case_ids": amplitude_issues,
        "publish_change_semantics_aligned": publish_change_semantics_aligned,
    }


def build_host_reply_post_send_baseline(gateway_factory=None) -> dict:
    return asyncio.run(_build_host_reply_post_send_baseline_async(gateway_factory=gateway_factory))


def _render_post_send_markdown(payload: dict) -> str:
    lines = [
        "# Host Reply Post Send Audit",
        "",
        f"- status: `{payload.get('status', 'unknown')}`",
        f"- model: `{payload.get('model', '')}`",
        f"- all matched: `{payload.get('all_matched', False)}`",
        "",
        "## Cases",
    ]
    for case in payload.get("cases", []) or []:
        lines.extend(
            [
                f"- `{case['case_id']}`",
                f"  - host tag: `{case['host_tag']}`",
                f"  - text event: `{case['resolved_text_event_type']}`",
                f"  - effective event: `{case['effective_event_type']}`",
                f"  - mood remap suppressed: `{case['mood_tag_remap_suppressed']}`",
                f"  - expected final mood: `{case['expected_final_mood']:.4f}`",
                f"  - actual final mood: `{case['actual_final_mood']:.4f}`",
                f"  - expected social_score: `{case['expected_social_score']:.4f}`",
                f"  - actual social_score: `{case['actual_social_score']:.4f}`",
                f"  - expected social direction: `{case['expected_social_direction']}`",
                f"  - actual social direction: `{case['actual_social_direction']}`",
                f"  - amplitude issue: `{case['social_score_amplitude_issue'] or 'none'}`",
                f"  - mood matched: `{case['mood_matched']}`",
                f"  - social matched: `{case['social_score_matched']}`",
            ]
        )
    return "\n".join(lines) + "\n"


def write_host_reply_post_send_artifacts(base_dir: str | Path, gateway_factory=None) -> dict:
    payload = build_host_reply_post_send_baseline(gateway_factory=gateway_factory)
    base_path = Path(base_dir)
    base_path.mkdir(parents=True, exist_ok=True)
    json_path = base_path / "host_reply_post_send_audit.json"
    md_path = base_path / "host_reply_post_send_audit.md"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(_render_post_send_markdown(payload), encoding="utf-8")
    return {"json_path": str(json_path), "markdown_path": str(md_path), "payload": payload}


def _default_output_dir() -> Path:
    return Path(__file__).resolve().parents[2] / "artifacts" / "state_bar_audit"


def _main() -> int:
    parser = argparse.ArgumentParser(description="Run the dev-only host message-entry mood audit.")
    parser.add_argument("--base-dir", default=str(_default_output_dir()))
    parser.add_argument("--post-send", action="store_true", help="Run the host reply_post_send audit instead of ingress-only audit.")
    args = parser.parse_args()
    if args.post_send:
        result = write_host_reply_post_send_artifacts(args.base_dir)
    else:
        result = write_host_mood_chain_artifacts(args.base_dir)
    print(json.dumps({"json_path": result["json_path"], "markdown_path": result["markdown_path"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
