from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import aiohttp


ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


BASE_URL = "https://api.moonshot.cn/v1"
DEFAULT_MODEL = "kimi-k2.6"
DEFAULT_MAX_CALLS = 10
DEFAULT_RPM_DELAY_SECONDS = 0.0
DEFAULT_TIMEOUT_SECONDS = 25.0
DEFAULT_MAX_TOKENS = 160
DEFAULT_TIER0_CONCURRENCY_LIMIT = 3
DEFAULT_TIER0_RPM_LIMIT = 20
DEFAULT_RPM_SAFETY_MARGIN = 1
RATE_LIMIT_WINDOW_SECONDS = 60.0


SECRET_RE = re.compile(r"sk-[A-Za-z0-9_\-]{12,}")
RETRY_AFTER_RE = re.compile(r"after\s+([0-9]+(?:\.[0-9]+)?)\s+seconds?", re.IGNORECASE)


def redact(value: Any, api_key: str = "") -> Any:
    if isinstance(value, dict):
        return {key: redact(item, api_key) for key, item in value.items()}
    if isinstance(value, list):
        return [redact(item, api_key) for item in value]
    if not isinstance(value, str):
        return value
    text = value
    if api_key:
        text = text.replace(api_key, _redacted_key(api_key))
    return SECRET_RE.sub("sk-...REDACTED", text)


def _redacted_key(api_key: str) -> str:
    if not api_key:
        return ""
    if len(api_key) <= 12:
        return "sk-...REDACTED"
    return f"{api_key[:5]}...{api_key[-6:]}"


def json_dumps(data: Any) -> str:
    return json.dumps(data, ensure_ascii=False, sort_keys=True)


def preview(value: Any, limit: int = 220) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


class ReplayStop(RuntimeError):
    def __init__(self, reason: str, detail: dict[str, Any] | None = None):
        super().__init__(reason)
        self.reason = reason
        self.detail = detail or {}


class SlidingWindowRateLimiter:
    def __init__(
        self,
        *,
        rpm_limit: int = DEFAULT_TIER0_RPM_LIMIT,
        safety_margin: int = DEFAULT_RPM_SAFETY_MARGIN,
        window_seconds: float = RATE_LIMIT_WINDOW_SECONDS,
    ):
        self.rpm_limit = max(1, int(rpm_limit or 1))
        self.safety_margin = max(0, int(safety_margin or 0))
        self.window_seconds = max(1.0, float(window_seconds or RATE_LIMIT_WINDOW_SECONDS))
        self.effective_limit = max(1, self.rpm_limit - self.safety_margin)
        self._timestamps: list[float] = []

    def _prune(self, now: float) -> None:
        cutoff = now - self.window_seconds
        self._timestamps = [stamp for stamp in self._timestamps if stamp > cutoff]

    async def wait_for_slot(self) -> float:
        while True:
            now = time.monotonic()
            self._prune(now)
            if len(self._timestamps) < self.effective_limit:
                self._timestamps.append(now)
                return 0.0
            oldest = min(self._timestamps)
            wait_seconds = max(0.05, self.window_seconds - (now - oldest) + 0.05)
            await asyncio.sleep(wait_seconds)

    def snapshot(self) -> dict[str, Any]:
        now = time.monotonic()
        self._prune(now)
        return {
            "rpm_limit": self.rpm_limit,
            "rpm_safety_margin": self.safety_margin,
            "effective_rpm_limit": self.effective_limit,
            "window_seconds": self.window_seconds,
            "requests_in_window": len(self._timestamps),
        }


@dataclass(slots=True)
class KimiAPIError(Exception):
    status: int
    error_type: str
    message: str
    raw: Any = None
    retry_after_seconds: float = 0.0

    def safe_dict(self, api_key: str = "") -> dict[str, Any]:
        return redact(
            {
                "status": self.status,
                "error_type": self.error_type,
                "message": self.message,
                "retry_after_seconds": self.retry_after_seconds,
                "raw": self.raw,
            },
            api_key,
        )


@dataclass(slots=True)
class ReplayCase:
    case_id: str
    text: str
    expected: str
    sender_name: str = "Alice"
    sender_id: str = "user-1"
    chat_id: str = "default:GroupMessage:group-1"
    group_id: str = "group-1"
    focus_reason: str = "latest_user_message"
    is_private: bool = False
    extras: dict[str, Any] = field(default_factory=dict)
    event_messages: list["ReplayCase"] = field(default_factory=list)


REPLAY_CASES = [
    ReplayCase(
        case_id="poke_lightweight",
        text="戳了戳 AstrMai",
        expected="think_level=0; memory skipped; lightweight reply only",
        focus_reason="poke",
        extras={
            "is_virtual_poke": True,
            "astrmai_interaction_kind": "poke",
            "astrmai_rich_text": "戳了戳 AstrMai",
        },
    ),
    ReplayCase(
        case_id="normal_private",
        text="今天有点累，陪我聊两句就好。",
        expected="normal private/direct turn; think_level around 1 or 2; no full tool by default",
        chat_id="default:FriendMessage:user-1",
        group_id="",
        focus_reason="private",
        is_private=True,
    ),
    ReplayCase(
        case_id="deep_memory",
        text="Do you still remember what I said last time about exams?",
        expected="deep memory/tool budget; MemoryDecision visible; ReAct and fallback remain mutually exclusive",
        focus_reason="at_bot",
    ),
    ReplayCase(
        case_id="tool_intent",
        text="Please look up whether you should withdraw the last reply, then answer briefly.",
        expected="think_level=3; full/tool tier should be reachable; side-effect tools are not actually executed",
        focus_reason="at_bot",
    ),
    ReplayCase(
        case_id="group_non_direct",
        text="this game was fun today",
        expected="non-direct group short/ambient turn; should avoid forced insertion",
        focus_reason="latest_user_message",
    ),
    ReplayCase(
        case_id="pushback_strict",
        text="That reply crossed a line; don't talk to me like that.",
        expected="boundary is acceptable; pushback should remain high-threshold",
        focus_reason="at_bot",
    ),
    ReplayCase(
        case_id="zh_group_non_direct",
        text="这个游戏今天还挺好玩的",
        expected="中文群聊非直接短句；应 think_level=0，跳过 CognitiveLoop，不主动插话",
        focus_reason="latest_user_message",
    ),
    ReplayCase(
        case_id="zh_memory_intent",
        text="你还记得我上次说考试压力那件事吗？",
        expected="中文深记忆场景；应 think_level=3，记忆裁决可见，ReAct/fallback 互斥",
        focus_reason="at_bot",
    ),
    ReplayCase(
        case_id="zh_tool_intent",
        text="帮我查一下上一条回复需不需要撤回，简单回答。",
        expected="中文工具意图；应 think_level=3，explicit_tool_intent=true，full tier 可达",
        focus_reason="at_bot",
    ),
    ReplayCase(
        case_id="zh_boundary_mild",
        text="这句话有点越界了，别这样跟我说。",
        expected="中文轻度边界反馈；不应低门槛 pushback，attack_confidence 应低于 0.85",
        focus_reason="at_bot",
    ),
]


class KimiClient:
    def __init__(
        self,
        *,
        api_key: str,
        model: str,
        timeout: float,
        max_calls: int,
        max_tokens: int,
        temperature: float,
        rpm_limit: int,
        rpm_safety_margin: int,
        concurrency_limit: int,
    ):
        self.api_key = api_key
        self.model = model
        self.timeout = float(timeout)
        self.max_calls = int(max_calls)
        self.max_tokens = int(max_tokens)
        self.temperature = float(temperature)
        self.concurrency_limit = max(1, int(concurrency_limit or DEFAULT_TIER0_CONCURRENCY_LIMIT))
        self.rate_limiter = SlidingWindowRateLimiter(
            rpm_limit=rpm_limit,
            safety_margin=rpm_safety_margin,
        )
        self.chat_calls = 0
        self.total_calls = 0
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "KimiClient":
        timeout = aiohttp.ClientTimeout(total=self.timeout)
        self._session = aiohttp.ClientSession(timeout=timeout)
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        if self._session:
            await self._session.close()

    @property
    def safe_key(self) -> str:
        return _redacted_key(self.api_key)

    async def get_balance(self) -> dict[str, Any]:
        return await self._request("GET", "/users/me/balance")

    async def list_models(self) -> dict[str, Any]:
        return await self._request("GET", "/models")

    async def chat_completion(
        self,
        *,
        messages: list[dict[str, str]],
        request_label: str,
        allow_thinking_fallback: bool = True,
    ) -> dict[str, Any]:
        if self.chat_calls >= self.max_calls:
            raise ReplayStop(
                "max_calls_reached",
                {"max_calls": self.max_calls, "chat_calls": self.chat_calls},
            )
        self.chat_calls += 1
        body = {
            "model": self.model,
            "messages": messages,
            "max_tokens": self.max_tokens,
            "temperature": self.temperature,
            "thinking": {"type": "disabled"},
        }
        try:
            return await self._request("POST", "/chat/completions", json_body=body)
        except KimiAPIError as exc:
            message = f"{exc.error_type} {exc.message}".lower()
            if allow_thinking_fallback and exc.status == 400 and "thinking" in message:
                body.pop("thinking", None)
                retry = await self._request("POST", "/chat/completions", json_body=body)
                retry["_thinking_param_fallback"] = True
                return retry
            raise

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not self._session:
            raise RuntimeError("KimiClient session is not open")
        self.total_calls += 1
        rate_wait_seconds = await self.rate_limiter.wait_for_slot()
        url = f"{BASE_URL}{path}"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        started = time.perf_counter()
        try:
            async with self._session.request(method, url, headers=headers, json=json_body) as resp:
                text = await resp.text()
                latency_ms = int((time.perf_counter() - started) * 1000)
                try:
                    payload = json.loads(text) if text else {}
                except json.JSONDecodeError:
                    payload = {"raw_text": text}
                if resp.status >= 400:
                    error = payload.get("error") if isinstance(payload, dict) else None
                    error_type = str((error or {}).get("type") or f"http_{resp.status}")
                    message = str((error or {}).get("message") or text or f"HTTP {resp.status}")
                    retry_after = self._parse_retry_after(message, resp.headers.get("Retry-After"))
                    raise KimiAPIError(resp.status, error_type, message, payload, retry_after)
                if isinstance(payload, dict):
                    payload["_http_status"] = resp.status
                    payload["_latency_ms"] = latency_ms
                    payload["_rpm_wait_ms"] = int(rate_wait_seconds * 1000)
                    payload["_rate_limit"] = self.rate_limiter.snapshot()
                return payload
        except asyncio.TimeoutError as exc:
            raise KimiAPIError(0, "timeout", f"request timeout after {self.timeout}s") from exc
        except aiohttp.ClientError as exc:
            raise KimiAPIError(0, "client_error", str(exc)) from exc

    @staticmethod
    def _parse_retry_after(message: str, header: str | None) -> float:
        if header:
            try:
                return float(header)
            except ValueError:
                pass
        match = RETRY_AFTER_RE.search(message or "")
        if not match:
            return 0.0
        try:
            return float(match.group(1))
        except ValueError:
            return 0.0


class KimiGateway:
    def __init__(self, client: KimiClient):
        self.client = client
        self.config = self._build_config(client.timeout)
        self.lane_manager = _FakeLaneManager()

    @staticmethod
    def _build_config(timeout: float) -> SimpleNamespace:
        return SimpleNamespace(
            system1=SimpleNamespace(nicknames=["AstrMai", "ATRI", "亚托莉"]),
            global_settings=SimpleNamespace(debug_mode=False, enable_error_interception=False, admin_ids=[]),
            provider=SimpleNamespace(),
            reply=SimpleNamespace(follow_up_probability=0.0, emotion_mapping={}, fallback_text="(silent)"),
            memory=SimpleNamespace(enable_react_agent=True, auto_recall_probability=0.0),
            agent=SimpleNamespace(max_steps=5, timeout=max(10, int(timeout))),
            persona=SimpleNamespace(persona_id="kimi-replay"),
            infra=SimpleNamespace(api_timeout=float(timeout)),
        )

    def get_agent_models(self) -> list[str]:
        return [self.client.model]

    def get_models_for_task(self, pool_name: str, models: list[str]) -> list[str]:
        return list(models or [self.client.model])

    async def call_data_process_task(
        self,
        prompt: str,
        *,
        system_prompt: str = "",
        is_json: bool = False,
        **kwargs,
    ) -> Any:
        user_prompt = str(prompt or "")
        if is_json:
            user_prompt += "\n\nReturn only one valid JSON object. No markdown."
        result = await self._completion(
            system_prompt=system_prompt or "You are a concise hidden planning assistant.",
            prompt=user_prompt,
            request_label="data_process",
        )
        return result

    async def chat_in_lane_result(self, **kwargs) -> SimpleNamespace:
        text = await self._completion(
            system_prompt=str(kwargs.get("system_prompt", "") or ""),
            prompt=str(kwargs.get("prompt", "") or ""),
            request_label="chat",
        )
        return SimpleNamespace(text=text, usage=SimpleNamespace(input=0, input_cached=0, output=0))

    async def tool_chat_in_lane_result(self, **kwargs) -> SimpleNamespace:
        tools = getattr(kwargs.get("tools"), "tools", None) or []
        tool_names = [str(getattr(tool, "name", "") or tool.__class__.__name__) for tool in tools]
        tool_notice = (
            "\n\nReplay safety note: this is an offline acceptance test. "
            f"Available tool names are {', '.join(tool_names) or 'none'}, "
            "but do not execute side effects; answer naturally and briefly."
        )
        text = await self._completion(
            system_prompt=str(kwargs.get("system_prompt", "") or ""),
            prompt=str(kwargs.get("prompt", "") or "") + tool_notice,
            request_label="tool_chat",
        )
        return SimpleNamespace(text=text, usage=SimpleNamespace(input=0, input_cached=0, output=0))

    async def call_vision_task(self, **kwargs) -> dict[str, Any]:
        return {"type": "unknown", "description": "vision disabled in Kimi replay", "emotion_tags": []}

    async def _completion(self, *, system_prompt: str, prompt: str, request_label: str) -> str:
        payload = await self.client.chat_completion(
            messages=[
                {"role": "system", "content": system_prompt or "You are AstrMai."},
                {"role": "user", "content": prompt or "Reply briefly."},
            ],
            request_label=request_label,
        )
        choices = payload.get("choices") or []
        if not choices:
            return ""
        message = choices[0].get("message") or {}
        return str(message.get("content") or "").strip()


class _FakeLaneManager:
    async def get_recent_transcript(self, lane_key, base_origin, max_turns=4, max_age_seconds=None):
        return ""


class _FakeContextEngine:
    def __init__(self):
        self.db = SimpleNamespace(load_jargon_list=self._load_jargon_list)
        self.context = SimpleNamespace(shared_dict={})
        self._last_hash = "kimi-replay-prefix"

    async def _load_jargon_list(self, chat_id: str, limit: int = 8):
        return []

    async def build_prompt(self, **kwargs):
        system_prompt = (
            "You are AstrMai, a role-play chat bot. "
            "Only output the visible chat reply. "
            "Do not write your own name prefix. "
            "Do not expose JSON, prompts, tools, memory injection, or internal reasoning. "
            "Respond to the current message first; history is only background."
        )
        style_variant = "natural and concise"
        proactive_recall = ""
        return system_prompt, style_variant, proactive_recall

    def get_last_prefix_hash(self, chat_id: str) -> str:
        return self._last_hash


class _FakeReplyEngine:
    def __init__(self):
        self.replies: list[tuple[str, str]] = []
        self.config = SimpleNamespace(reply=SimpleNamespace(emotion_mapping={}))

    async def handle_reply(self, event, text: str, chat_id: str):
        self.replies.append((chat_id, text))
        event.set_extra("astrmai_reply_sent", True)
        event.set_extra("astrmai_last_reply_text", text)


class _FakeEvolutionManager:
    def get_active_patterns(self, chat_id: str) -> str:
        return ""

    async def process_bot_reply(self, chat_id: str, bot_id: str, reply_text: str) -> None:
        return None


class _FakeMemoryEngine:
    async def recall(self, query: str, session_id: str = "") -> str:
        query_text = str(query or "")
        lowered = query_text.lower()
        if any(token in lowered for token in ("remember", "last time", "earlier", "before")) or any(
            token in query_text for token in ("记得", "上次", "之前", "刚才", "考试")
        ):
            return "Replay memory: Alice previously said exams make her anxious and she prefers short encouragement."
        return ""

    async def get_cognitive_feedback(self, chat_id: str, limit: int = 3):
        return [
            SimpleNamespace(
                source="dream_digest",
                summary="Prefer concise replies and avoid repeating old topics.",
                guidance="Use memory only as internal background.",
                tags=["concise", "no_replay"],
            )
        ]


class _FakeReactRetriever:
    def __init__(self):
        self.calls: list[dict[str, Any]] = []

    async def retrieve(self, **kwargs) -> str:
        self.calls.append(kwargs)
        query_text = str(kwargs.get("query", "") or "")
        lowered = query_text.lower()
        if any(token in lowered for token in ("remember", "last time", "earlier", "before")) or any(
            token in query_text for token in ("记得", "上次", "之前", "刚才", "考试")
        ):
            return "ReAct memory: Alice mentioned exam stress; respond with gentle continuity, not a verbatim quote."
        return ""


class _FakeStateEngine:
    async def get_state(self, chat_id: str = ""):
        return SimpleNamespace(energy=0.65, mood=0.1, patience=0.7, curiosity=0.5, caution=0.4)


class _FakeEvent:
    def __init__(self, case: ReplayCase):
        self.message_str = case.text
        self.unified_msg_origin = case.chat_id
        self.message_obj = SimpleNamespace(message=[])
        self.timestamp = time.time()
        self.message_id = f"replay-{case.case_id}-{int(self.timestamp)}"
        self._sender_id = case.sender_id
        self._sender_name = case.sender_name
        self._group_id = case.group_id
        self._self_id = "astrmai-bot"
        self._extra: dict[str, Any] = {
            "retrieve_keys": [],
            "judge_action": "REPLY",
            "astrmai_trace_id": f"kimi-replay-{case.case_id}",
        }
        self._extra.update(case.extras)

    def get_extra(self, key, default=None):
        return self._extra.get(key, default)

    def set_extra(self, key, value):
        self._extra[key] = value

    def get_sender_id(self):
        return self._sender_id

    def get_sender_name(self):
        return self._sender_name

    def get_group_id(self):
        return self._group_id

    def get_self_id(self):
        return self._self_id


def install_runtime_stubs() -> None:
    from tests.helpers.astrbot_stubs import install_astrbot_stubs
    from tests.helpers.executor_stubs import install_executor_stubs
    from tests.helpers.planner_stubs import install_planner_stubs

    temp_root = tempfile.mkdtemp(prefix="astrmai-kimi-replay-")
    install_astrbot_stubs(temp_root)
    install_executor_stubs()
    install_planner_stubs()


def build_event(case: ReplayCase):
    from astrmai.conversation.contracts.focus_context import ReplyMode
    from astrmai.conversation.contracts.turn_context import ensure_turn_context

    event = _FakeEvent(case)
    focus_line = f"{case.sender_name}: {case.text}"
    event.set_extra("astrmai_focus_event", event)
    event.set_extra("astrmai_focus_reason", case.focus_reason)
    event.set_extra("astrmai_focus_message_text", focus_line)
    event.set_extra("astrmai_focus_sender_id", case.sender_id)
    event.set_extra("astrmai_focus_sender_name", case.sender_name)
    event.set_extra("astrmai_focus_thread_root_event", event)
    event.set_extra("astrmai_focus_thread_root_reason", case.focus_reason)
    event.set_extra("astrmai_focus_thread_core_events", [event])
    event.set_extra("astrmai_focus_thread_related_events", [])
    event.set_extra("astrmai_focus_thread_ambient_events", [])
    event.set_extra("astrmai_focus_thread_reason", case.focus_reason)
    event.set_extra("astrmai_reply_mode", ReplyMode.PLAYFUL_INTERACTION.value if case.case_id == "poke_lightweight" else ReplyMode.CASUAL_FOLLOWUP.value)
    turn_context = ensure_turn_context(event)
    turn_context.perception.chat_id = case.chat_id
    turn_context.perception.self_id = "astrmai-bot"
    turn_context.perception.sender_id = case.sender_id
    turn_context.perception.sender_name = case.sender_name
    turn_context.perception.text = case.text
    turn_context.perception.timestamp = event.timestamp
    turn_context.perception.is_private = bool(case.is_private)
    turn_context.perception.is_at_bot = case.focus_reason in {"at_bot", "mention"}
    turn_context.perception.is_reply_to_bot = "reply" in case.focus_reason
    turn_context.perception.is_direct_wakeup = case.is_private or case.focus_reason in {"at_bot", "private"}
    turn_context.perception.is_strong_wakeup = case.focus_reason in {"at_bot", "private"}
    return event


def build_planner(client: KimiClient):
    install_runtime_stubs()
    from astrmai.conversation.planning.planner import Planner
    from astrmai.conversation.planning.prompt_refiner import PromptRefiner

    gateway = KimiGateway(client)
    memory_engine = _FakeMemoryEngine()
    planner = Planner(
        context=SimpleNamespace(),
        gateway=gateway,
        context_engine=_FakeContextEngine(),
        reply_engine=_FakeReplyEngine(),
        memory_engine=memory_engine,
        evolution_manager=_FakeEvolutionManager(),
        state_engine=_FakeStateEngine(),
        prompt_refiner=PromptRefiner(
            memory_engine=memory_engine,
            db_service=None,
            config=gateway.config,
            react_retriever=_FakeReactRetriever(),
        ),
        sys3_router=None,
        runtime_coordinator=None,
    )
    planner.cognitive_loop.SOFT_TIMEOUT_SECONDS = max(2.5, min(float(client.timeout), 12.0))
    return planner


class ReportWriter:
    def __init__(self, run_dir: Path, api_key: str):
        self.run_dir = run_dir
        self.api_key = api_key
        self.report_path = run_dir / "report.jsonl"
        self.state_path = run_dir / "state.json"
        self.summary_path = run_dir / "summary.md"
        self.acceptance_report_path = run_dir / "ACCEPTANCE_REPORT.md"
        run_dir.mkdir(parents=True, exist_ok=True)

    def append(self, item: dict[str, Any]) -> None:
        safe_item = redact(item, self.api_key)
        with self.report_path.open("a", encoding="utf-8") as handle:
            handle.write(json_dumps(safe_item) + "\n")

    def save_state(self, state: dict[str, Any]) -> None:
        safe_state = redact(state, self.api_key)
        self.state_path.write_text(json.dumps(safe_state, ensure_ascii=False, indent=2), encoding="utf-8")

    def write_summary(self, state: dict[str, Any], items: list[dict[str, Any]]) -> None:
        completed = sorted(state.get("completed", []))
        failed = state.get("failed", {})
        lines = [
            "# Kimi Replay Acceptance Summary",
            "",
            f"- run_id: `{state.get('run_id', '')}`",
            f"- model: `{state.get('model', '')}`",
            f"- safe_key: `{state.get('safe_key', '')}`",
            f"- tier0_limits: concurrency=`{state.get('concurrency_limit', '')}`, rpm=`{state.get('rpm_limit', '')}`, effective_rpm=`{state.get('effective_rpm_limit', '')}`",
            f"- chat_calls: `{state.get('chat_calls', 0)}`",
            f"- stopped_reason: `{state.get('stopped_reason', '')}`",
            f"- completed: `{', '.join(completed) if completed else 'none'}`",
            f"- failed: `{', '.join(failed.keys()) if failed else 'none'}`",
            "",
            "## Case Results",
            "",
        ]
        for item in items:
            trace = item.get("turn_trace") or {}
            cognitive = trace.get("cognitive") or {}
            memory = trace.get("memory") or {}
            tools = trace.get("tools") or {}
            lines.extend(
                [
                    f"### {item.get('case_id')}",
                    "",
                    f"- status: `{item.get('status')}`",
                    f"- expected: {item.get('expected')}",
                    f"- validation_errors: {item.get('validation_errors', [])}",
                    f"- think_level: `{cognitive.get('think_level')}` ({cognitive.get('think_reason', '')})",
                    f"- cognitive_loop: ran=`{cognitive.get('cognitive_loop_ran')}`, skipped=`{cognitive.get('cognitive_loop_skipped_reason', '')}`",
                    f"- memory: injected=`{memory.get('injected')}`, source=`{memory.get('source')}`, skip=`{memory.get('skip_reason')}`",
                    f"- tools: requested=`{tools.get('requested_tier')}`, final=`{tools.get('final_tier')}`, filtered=`{len(tools.get('filtered_tools') or [])}`",
                    f"- reply_preview: {item.get('reply_preview', '')}",
                    "",
                ]
            )
        self.summary_path.write_text("\n".join(lines), encoding="utf-8-sig")
        self.write_acceptance_report(state, items)

    def write_acceptance_report(self, state: dict[str, Any], items: list[dict[str, Any]]) -> None:
        failed = state.get("failed", {}) or {}
        balance = state.get("balance", {}) or {}
        side_input_failures: dict[str, int] = {}
        side_input_skips: dict[str, int] = {}
        rows: list[str] = []
        for item in items:
            trace = item.get("turn_trace") or {}
            cognitive = trace.get("cognitive") or {}
            memory = trace.get("memory") or {}
            tools = trace.get("tools") or {}
            side_inputs = ((trace.get("side_inputs") or {}).get("timings") or [])
            for timing in side_inputs:
                name = str(timing.get("name", "") or "unknown")
                if timing.get("ok") is False:
                    side_input_failures[name] = side_input_failures.get(name, 0) + 1
                if timing.get("skipped_reason"):
                    side_input_skips[name] = side_input_skips.get(name, 0) + 1
            rows.append(
                "| {case} | {status} | {level} | {ran} | {memory} | {tier} | {errors} |".format(
                    case=item.get("case_id", ""),
                    status=item.get("status", ""),
                    level=cognitive.get("think_level", ""),
                    ran=cognitive.get("cognitive_loop_ran", ""),
                    memory=(memory.get("source") or memory.get("skip_reason") or ""),
                    tier=tools.get("final_tier", ""),
                    errors="; ".join(str(err) for err in (item.get("validation_errors") or [])),
                )
            )

        lines = [
            "# Kimi Replay Acceptance Report",
            "",
            "## Run",
            "",
            f"- run_id: `{state.get('run_id', '')}`",
            f"- model: `{state.get('model', '')}`",
            f"- safe_key: `{state.get('safe_key', '')}`",
            f"- stopped_reason: `{state.get('stopped_reason', '')}`",
            f"- completed: `{', '.join(state.get('completed', []) or []) or 'none'}`",
            f"- failed: `{', '.join(failed.keys()) if failed else 'none'}`",
            "",
            "## API Usage",
            "",
            f"- chat_calls: `{state.get('chat_calls', 0)}`",
            f"- total_http_calls: `{state.get('total_http_calls', 0)}`",
            f"- tier0_limits: concurrency=`{state.get('concurrency_limit', '')}`, rpm=`{state.get('rpm_limit', '')}`, effective_rpm=`{state.get('effective_rpm_limit', '')}`",
        ]
        if balance:
            lines.append(f"- available_balance: `{balance.get('available_balance', '')}`")
        lines.extend(
            [
                "",
                "## Scenario Matrix",
                "",
                "| Case | Status | Think | CognitiveLoop | Memory | Tools | Validation Errors |",
                "| --- | --- | --- | --- | --- | --- | --- |",
                *(rows or ["| none | - | - | - | - | - | - |"]),
                "",
                "## Side Input Diagnostics",
                "",
                f"- failed_inputs: `{side_input_failures or 'none'}`",
                f"- skipped_inputs: `{side_input_skips or 'none'}`",
                "",
                "## Conclusions",
                "",
                "- All listed cases are safe to interpret only if `failed=none` and every row has `status=ok`.",
                "- Reports include TurnTrace summaries only; prompt text, inner monologue and raw API key are intentionally excluded.",
                "- If a case failed due to `max_calls_reached` or rate limit, rerun with `--resume` after the indicated wait.",
            ]
        )
        self.acceptance_report_path.write_text("\n".join(lines), encoding="utf-8-sig")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Kimi real API replay acceptance for AstrMai.")
    parser.add_argument("--api-key", default="", help="Moonshot API key. Prefer MOONSHOT_API_KEY env var.")
    parser.add_argument("--model", default=os.getenv("KIMI_MODEL", DEFAULT_MODEL))
    parser.add_argument("--max-calls", type=int, default=DEFAULT_MAX_CALLS)
    parser.add_argument("--rpm-delay", type=float, default=DEFAULT_RPM_DELAY_SECONDS, help="Extra delay between replay cases; RPM is already guarded by --rpm-limit.")
    parser.add_argument("--rpm-limit", type=int, default=DEFAULT_TIER0_RPM_LIMIT, help="Account RPM limit. Tier0 is currently 20.")
    parser.add_argument("--rpm-safety-margin", type=int, default=DEFAULT_RPM_SAFETY_MARGIN, help="Keep this many RPM slots unused as safety margin.")
    parser.add_argument("--concurrency-limit", type=int, default=DEFAULT_TIER0_CONCURRENCY_LIMIT, help="Recorded account concurrency limit. The replay runner stays serial.")
    parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    parser.add_argument("--max-tokens", type=int, default=DEFAULT_MAX_TOKENS)
    parser.add_argument("--temperature", type=float, default=0.6)
    parser.add_argument("--only", default="", help="Comma-separated probes/cases: balance,models,poke_lightweight,...")
    parser.add_argument("--resume", action="store_true", help="Resume from the latest or --run-dir state.")
    parser.add_argument("--run-dir", default="", help="Existing run dir for --resume or target dir for new run.")
    return parser.parse_args()


def resolve_api_key(args: argparse.Namespace) -> str:
    return os.getenv("MOONSHOT_API_KEY", "").strip() or str(args.api_key or "").strip()


def find_latest_run_dir() -> Path | None:
    root = ROOT / "artifacts" / "kimi_replay"
    if not root.exists():
        return None
    candidates = [path for path in root.iterdir() if path.is_dir() and (path / "state.json").exists()]
    if not candidates:
        return None
    return max(candidates, key=lambda path: path.stat().st_mtime)


def prepare_run_dir(args: argparse.Namespace) -> Path:
    if args.run_dir:
        return Path(args.run_dir).resolve()
    if args.resume:
        latest = find_latest_run_dir()
        if latest:
            return latest
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return ROOT / "artifacts" / "kimi_replay" / stamp


def load_state(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def only_tokens(value: str) -> set[str]:
    return {token.strip() for token in str(value or "").split(",") if token.strip()}


def selected_cases(tokens: set[str]) -> list[ReplayCase]:
    if not tokens:
        return REPLAY_CASES
    probe_names = {"balance", "models"}
    requested = tokens - probe_names
    if not requested:
        return []
    return [case for case in REPLAY_CASES if case.case_id in requested]


def classify_stop(exc: KimiAPIError) -> tuple[str, bool]:
    error_type = exc.error_type.lower()
    message = exc.message.lower()
    if exc.status == 401:
        return "authentication_failed", True
    if exc.status == 429:
        if "quota" in error_type or "quota" in message or "balance" in message:
            return "quota_exhausted", True
        return "rate_limited", True
    if exc.status in {500, 502, 503, 504} or error_type in {"timeout", "client_error"}:
        return "transient_error", False
    return "api_error", True


async def retry_api_call(label: str, func, *, api_key: str, max_retries: int = 2) -> Any:
    delays = [8.0, 20.0]
    attempt = 0
    while True:
        try:
            return await func()
        except KimiAPIError as exc:
            stop_reason, fatal = classify_stop(exc)
            if fatal or attempt >= max_retries:
                raise ReplayStop(stop_reason, exc.safe_dict(api_key)) from exc
            await asyncio.sleep(delays[min(attempt, len(delays) - 1)])
            attempt += 1


async def run_probe_balance(client: KimiClient, writer: ReportWriter, state: dict[str, Any]) -> None:
    payload = await retry_api_call("balance", client.get_balance, api_key=client.api_key, max_retries=1)
    data = payload.get("data") if isinstance(payload, dict) else {}
    item = {
        "kind": "probe",
        "probe": "balance",
        "status": "ok",
        "balance": {
            "available_balance": (data or {}).get("available_balance"),
            "voucher_balance": (data or {}).get("voucher_balance"),
            "cash_balance": (data or {}).get("cash_balance"),
        },
        "latency_ms": payload.get("_latency_ms"),
        "rpm_wait_ms": payload.get("_rpm_wait_ms"),
        "rate_limit": payload.get("_rate_limit"),
    }
    writer.append(item)
    state["balance"] = item["balance"]


async def run_probe_models(client: KimiClient, writer: ReportWriter, state: dict[str, Any]) -> None:
    payload = await retry_api_call("models", client.list_models, api_key=client.api_key, max_retries=1)
    models = []
    for item in payload.get("data", []) if isinstance(payload, dict) else []:
        model_id = item.get("id") if isinstance(item, dict) else None
        if model_id:
            models.append(model_id)
    result = {
        "kind": "probe",
        "probe": "models",
        "status": "ok",
        "model": client.model,
        "model_available": client.model in models if models else None,
        "models_preview": models[:20],
        "latency_ms": payload.get("_latency_ms"),
        "rpm_wait_ms": payload.get("_rpm_wait_ms"),
        "rate_limit": payload.get("_rate_limit"),
    }
    writer.append(result)
    state["models_preview"] = models[:20]
    if models and client.model not in models:
        raise ReplayStop("model_unavailable", result)


async def run_case(planner, case: ReplayCase, writer: ReportWriter, client: KimiClient) -> dict[str, Any]:
    event = build_event(case)
    started = time.perf_counter()
    reply_text = await planner.plan_and_execute(event, [event])
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    trace = planner.turn_trace_history[-1] if planner.turn_trace_history else {}
    validation_errors = validate_case_trace(case, trace)
    item = {
        "kind": "case",
        "case_id": case.case_id,
        "status": "failed" if validation_errors else "ok",
        "expected": case.expected,
        "validation_errors": validation_errors,
        "elapsed_ms": elapsed_ms,
        "chat_calls_used_total": client.chat_calls,
        "reply_preview": preview(reply_text, 180),
        "turn_trace": trace,
    }
    writer.append(item)
    return item


def validate_case_trace(case: ReplayCase, trace: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    cognitive = trace.get("cognitive") or {}
    tools = trace.get("tools") or {}
    case_id = str(case.case_id or "")

    if case_id in {"group_non_direct", "zh_group_non_direct"}:
        if cognitive.get("think_level") != 0:
            errors.append(f"expected think_level=0, got {cognitive.get('think_level')!r}")
        if cognitive.get("cognitive_loop_ran") is not False:
            errors.append("expected CognitiveLoop to be skipped for non-direct group turn")

    if case_id in {"tool_intent", "zh_tool_intent"}:
        if cognitive.get("think_level") != 3:
            errors.append(f"expected think_level=3, got {cognitive.get('think_level')!r}")
        if tools.get("final_tier") != "full":
            errors.append(f"expected tools.final_tier='full', got {tools.get('final_tier')!r}")
        if tools.get("explicit_tool_intent") is not True:
            errors.append("expected explicit_tool_intent=true")

    if case_id in {"deep_memory", "zh_memory_intent"}:
        memory = trace.get("memory") or {}
        if cognitive.get("think_level") != 3:
            errors.append(f"expected think_level=3 for memory case, got {cognitive.get('think_level')!r}")
        if memory.get("injected") is not True:
            errors.append("expected memory.injected=true")
        if memory.get("source") not in {"memory_v2", "proactive_recall+memory_v2"}:
            errors.append(f"expected memory source memory_v2/proactive_recall+memory_v2, got {memory.get('source')!r}")

    if case_id in {"pushback_strict", "zh_boundary_mild"}:
        social_intent = str(cognitive.get("social_intent") or cognitive.get("intent") or "").strip().lower()
        try:
            attack_confidence = float(cognitive.get("attack_confidence") or 0.0)
        except (TypeError, ValueError):
            attack_confidence = 0.0
        if social_intent == "pushback":
            errors.append("pushback should not trigger for this mild boundary case")
        if attack_confidence >= 0.85:
            errors.append(f"attack_confidence should stay below 0.85, got {attack_confidence:.2f}")

    return errors


async def main_async() -> int:
    args = parse_args()
    api_key = resolve_api_key(args)
    if not api_key:
        print("MOONSHOT_API_KEY is required. Example: $env:MOONSHOT_API_KEY=\"sk-...\"")
        return 2

    tokens = only_tokens(args.only)
    run_dir = prepare_run_dir(args)
    writer = ReportWriter(run_dir, api_key)
    state = load_state(writer.state_path) if args.resume else {}
    state.setdefault("run_id", run_dir.name)
    state.setdefault("created_at", time.time())
    state["model"] = args.model
    state["safe_key"] = _redacted_key(api_key)
    state["concurrency_limit"] = int(args.concurrency_limit)
    state["rpm_limit"] = int(args.rpm_limit)
    state["rpm_safety_margin"] = int(args.rpm_safety_margin)
    state["effective_rpm_limit"] = max(1, int(args.rpm_limit) - max(0, int(args.rpm_safety_margin)))
    state.setdefault("completed", [])
    state.setdefault("failed", {})
    state.setdefault("stopped_reason", "")
    completed = set(state.get("completed", []))
    case_results: list[dict[str, Any]] = []

    async with KimiClient(
        api_key=api_key,
        model=args.model,
        timeout=args.timeout,
        max_calls=args.max_calls,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        rpm_limit=args.rpm_limit,
        rpm_safety_margin=args.rpm_safety_margin,
        concurrency_limit=args.concurrency_limit,
    ) as client:
        try:
            if not tokens or "balance" in tokens:
                await run_probe_balance(client, writer, state)
                writer.save_state(state)
            if not tokens or "models" in tokens:
                await run_probe_models(client, writer, state)
                writer.save_state(state)

            cases = selected_cases(tokens)
            if cases:
                planner = build_planner(client)
                for index, case in enumerate(cases):
                    if case.case_id in completed:
                        continue
                    if client.chat_calls >= args.max_calls:
                        raise ReplayStop(
                            "max_calls_reached",
                            {"max_calls": args.max_calls, "chat_calls": client.chat_calls},
                        )
                    result = await run_case(planner, case, writer, client)
                    case_results.append(result)
                    if result.get("status") == "failed":
                        state["failed"][case.case_id] = result.get("validation_errors", [])
                    else:
                        completed.add(case.case_id)
                        state["failed"].pop(case.case_id, None)
                    state["completed"] = sorted(completed)
                    state["chat_calls"] = client.chat_calls
                    writer.save_state(state)
                    if index < len(cases) - 1 and args.rpm_delay > 0:
                        await asyncio.sleep(args.rpm_delay)
        except ReplayStop as exc:
            state["stopped_reason"] = exc.reason
            state["last_error"] = redact(exc.detail, api_key)
            if exc.reason == "rate_limited":
                retry_after = float((exc.detail or {}).get("retry_after_seconds") or 0.0)
                state["next_retry_after_seconds"] = retry_after
                state["next_retry_not_before"] = time.time() + retry_after if retry_after > 0 else 0
            writer.save_state(state)
            writer.append({"kind": "stop", "status": "stopped", "reason": exc.reason, "detail": exc.detail})
            print(f"Stopped: {exc.reason}. State saved to {writer.state_path}")
        finally:
            state["chat_calls"] = client.chat_calls
            state["total_http_calls"] = client.total_calls
            state["rate_limit"] = client.rate_limiter.snapshot()
            writer.save_state(state)
            writer.write_summary(state, case_results)

    print(f"Replay artifacts: {run_dir}")
    print(f"Summary: {writer.summary_path}")
    return 0


def main() -> int:
    try:
        return asyncio.run(main_async())
    except KeyboardInterrupt:
        print("Interrupted by user.")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
