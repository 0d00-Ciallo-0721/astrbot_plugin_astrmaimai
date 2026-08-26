"""Scheduled life scenarios adapted from astrbot_plugin_InitiativeDialogue.

The community plugin's schedule, greeting, festival, and weather concepts are
reworked here to use AstrMai's existing attention, dispatcher, persistence,
hot-reload, and reply lifecycle instead of adding an independent event loop.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import date, datetime
import inspect
import json
import time
import uuid
from typing import Any, Awaitable, Callable

import aiohttp
from astrbot.api import logger

try:
    from lunardate import LunarDate
except ImportError:  # Optional at import time so plugin startup can degrade cleanly.
    LunarDate = None

from ..infrastructure.persistence.sqlite_helpers import connect_aiosqlite
from ..infrastructure.runtime.background_task_budget import BackgroundTaskQueueFull
from .dispatcher import ProactiveMessageIntent


SCHEDULE_SLOTS = ("morning", "forenoon", "lunch", "afternoon", "dinner", "evening", "night")
DEFAULT_SCHEDULE = {
    "morning": "起床整理，准备开始新的一天",
    "forenoon": "处理今天的重要事务",
    "lunch": "吃午饭并稍作休息",
    "afternoon": "继续工作或学习，留意自己的状态",
    "dinner": "吃晚饭，放松一下",
    "evening": "做喜欢的事，也可以和熟悉的人聊聊天",
    "night": "整理今天的心情，然后准备休息",
}


@dataclass(slots=True)
class WeatherSnapshot:
    text: str
    temperature: str
    location: str
    fetched_at: float

    def render(self) -> str:
        return f"{self.location}当前天气{self.text}，{self.temperature}°C"


class DailyScheduleStore:
    def __init__(self, db_path: Any) -> None:
        self.db_path = db_path

    async def load(self, plan_date: str) -> tuple[dict[str, str], str] | None:
        if not self.db_path:
            return None
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                "SELECT plan_json, source FROM proactive_daily_plan WHERE plan_date=?",
                (plan_date,),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if not row:
            return None
        try:
            payload = json.loads(str(row[0] or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            return None
        return self._normalize(payload), str(row[1] or "fallback")

    async def save(self, plan_date: str, plan: dict[str, str], *, source: str) -> None:
        if not self.db_path:
            return
        now = time.time()
        payload = json.dumps(self._normalize(plan), ensure_ascii=False)
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute(
                """
                INSERT INTO proactive_daily_plan(plan_date, plan_json, source, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(plan_date) DO UPDATE SET
                    plan_json=excluded.plan_json,
                    source=excluded.source,
                    updated_at=excluded.updated_at
                """,
                (plan_date, payload, source, now, now),
            )
            await db.commit()

    @staticmethod
    def _normalize(payload: Any) -> dict[str, str]:
        source = payload if isinstance(payload, dict) else {}
        return {
            slot: str(source.get(slot) or DEFAULT_SCHEDULE[slot]).strip()[:240]
            for slot in SCHEDULE_SLOTS
        }


class ScenarioDeliveryStore:
    CLAIM_LEASE_SECONDS = 300.0
    MEMORY_CLAIM_LIMIT = 1024
    MEMORY_CLAIM_RETENTION_SECONDS = 3600.0

    def __init__(self, db_path: Any) -> None:
        self.db_path = db_path
        # Keep a process-local claim ledger when persistence is unavailable.
        # This preserves ownership checks without pretending that claims
        # survive a restart or are shared across processes.
        self._memory_claims: dict[str, dict[str, Any]] = {}
        self._memory_claim_capacity_rejected = 0

    def _prune_memory_claims(self, *, now: float | None = None) -> None:
        if self.db_path or not self._memory_claims:
            return
        current_time = float(now if now is not None else time.time())
        removable: list[str] = []
        for key, record in self._memory_claims.items():
            try:
                updated_at = float(record.get("updated_at", 0.0) or 0.0)
            except (TypeError, ValueError):
                updated_at = 0.0
            status = str(record.get("status", "") or "")
            age = current_time - updated_at
            if status == "claimed" and age < self.CLAIM_LEASE_SECONDS:
                continue
            if (status == "claimed" and age >= self.CLAIM_LEASE_SECONDS) or age >= self.MEMORY_CLAIM_RETENTION_SECONDS:
                removable.append(key)
        for key in removable:
            self._memory_claims.pop(key, None)

        if len(self._memory_claims) <= self.MEMORY_CLAIM_LIMIT:
            return
        candidates: list[tuple[str, float]] = []
        for key, record in self._memory_claims.items():
            try:
                updated_at = float(record.get("updated_at", 0.0) or 0.0)
            except (TypeError, ValueError):
                updated_at = 0.0
            if str(record.get("status", "") or "") != "claimed" or (
                current_time - updated_at >= self.CLAIM_LEASE_SECONDS
            ):
                candidates.append((key, updated_at))
        candidates.sort(key=lambda item: item[1])
        for key, _updated_at in candidates:
            if len(self._memory_claims) <= self.MEMORY_CLAIM_LIMIT:
                break
            self._memory_claims.pop(key, None)

    def clear_memory_claims(self) -> None:
        """Drop process-local claims during teardown; persistent claims remain in SQLite."""
        if not self.db_path:
            self._memory_claims.clear()
            self._memory_claim_capacity_rejected = 0

    async def claim(self, delivery_key: str, *, chat_id: str, scenario: str, local_date: str) -> str | None:
        if not self.db_path:
            now = time.time()
            self._prune_memory_claims(now=now)
            current = self._memory_claims.get(delivery_key)
            if current:
                status = str(current.get("status", "") or "")
                updated_at = float(current.get("updated_at", 0.0) or 0.0)
                next_retry_at = float(current.get("next_retry_at", 0.0) or 0.0)
                if status in {"queued", "sent"}:
                    return None
                if status == "claimed" and now - updated_at < self.CLAIM_LEASE_SECONDS:
                    return None
                if next_retry_at > now:
                    return None
            if len(self._memory_claims) >= self.MEMORY_CLAIM_LIMIT:
                self._memory_claim_capacity_rejected += 1
                return None
            claim_token = uuid.uuid4().hex
            self._memory_claims[delivery_key] = {
                "status": "claimed",
                "claim_token": claim_token,
                "updated_at": now,
                "next_retry_at": 0.0,
                "chat_id": chat_id,
                "scenario": scenario,
                "local_date": local_date,
            }
            return claim_token
        now = time.time()
        claim_token = uuid.uuid4().hex
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute("BEGIN IMMEDIATE")
            cursor = await db.execute(
                "SELECT status, updated_at, next_retry_at FROM proactive_scenario_delivery WHERE delivery_key=?",
                (delivery_key,),
            )
            row = await cursor.fetchone()
            await cursor.close()
            if row:
                status = str(row[0] or "")
                updated_at = float(row[1] or 0.0)
                next_retry_at = float(row[2] or 0.0)
                if status in {"queued", "sent"}:
                    await db.rollback()
                    return False
                if status == "claimed" and now - updated_at < self.CLAIM_LEASE_SECONDS:
                    await db.rollback()
                    return False
                if next_retry_at > now:
                    await db.rollback()
                    return False
            await db.execute(
                """
                INSERT INTO proactive_scenario_delivery(
                    delivery_key, chat_id, scenario, local_date, status,
                    claim_token, attempts, next_retry_at, last_error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, 'claimed', ?, 1, 0, '', ?, ?)
                ON CONFLICT(delivery_key) DO UPDATE SET
                    status='claimed',
                    claim_token=excluded.claim_token,
                    attempts=proactive_scenario_delivery.attempts + 1,
                    next_retry_at=0,
                    last_error='',
                    updated_at=excluded.updated_at
                """,
                (delivery_key, chat_id, scenario, local_date, claim_token, now, now),
            )
            await db.commit()
        return claim_token

    async def is_claim_current(self, delivery_key: str, *, claim_token: str) -> bool | None:
        """Check that a scheduled delivery lease is still owned and live."""
        if not self.db_path:
            self._prune_memory_claims()
            current = self._memory_claims.get(delivery_key)
            if not current:
                return None
            if str(current.get("claim_token", "") or "") != str(claim_token or ""):
                return False
            if str(current.get("status", "") or "") != "claimed":
                return False
            try:
                updated_at = float(current.get("updated_at", 0.0) or 0.0)
            except (TypeError, ValueError):
                return False
            return time.time() - updated_at < self.CLAIM_LEASE_SECONDS
        async with connect_aiosqlite(self.db_path) as db:
            cursor = await db.execute(
                """
                SELECT status, updated_at
                FROM proactive_scenario_delivery
                WHERE delivery_key=? AND claim_token=?
                """,
                (str(delivery_key or ""), str(claim_token or "")),
            )
            row = await cursor.fetchone()
            await cursor.close()
        if not row or str(row[0] or "") != "claimed":
            return False
        try:
            updated_at = float(row[1] or 0.0)
        except (TypeError, ValueError):
            return False
        return time.time() - updated_at < self.CLAIM_LEASE_SECONDS

    async def update(
        self,
        delivery_key: str,
        *,
        status: str,
        error: str = "",
        retry_after: float = 0.0,
        claim_token: str = "",
    ) -> None:
        if not self.db_path:
            self._prune_memory_claims()
            current = self._memory_claims.get(delivery_key)
            if not current or str(current.get("claim_token", "") or "") != str(claim_token or ""):
                return
            current["status"] = status
            current["next_retry_at"] = time.time() + max(0.0, retry_after)
            current["updated_at"] = time.time()
            return
        now = time.time()
        async with connect_aiosqlite(self.db_path) as db:
            await db.execute(
                """
                UPDATE proactive_scenario_delivery
                SET status=?, next_retry_at=?, last_error=?, updated_at=?
                WHERE delivery_key=? AND claim_token=?
                  AND NOT (status='sent' AND ?='queued')
                """,
                (
                    status,
                    now + max(0.0, retry_after),
                    str(error or "")[:500],
                    now,
                    delivery_key,
                    str(claim_token or ""),
                    status,
                ),
            )
            await db.commit()


class FestivalProvider:
    SOLAR = {
        (1, 1): "元旦",
        (2, 14): "情人节",
        (5, 1): "劳动节",
        (6, 1): "儿童节",
        (10, 1): "国庆节",
        (10, 31): "万圣节",
        (12, 25): "圣诞节",
    }
    LUNAR = {
        (1, 1): "春节",
        (1, 15): "元宵节",
        (5, 5): "端午节",
        (7, 7): "七夕节",
        (8, 15): "中秋节",
        (9, 9): "重阳节",
        (12, 8): "腊八节",
    }

    @classmethod
    def get_name(cls, current: date, *, lunar_converter: Any = None) -> str:
        fixed = cls.SOLAR.get((current.month, current.day), "")
        if fixed:
            return fixed
        converter = LunarDate if lunar_converter is None else lunar_converter
        if converter is not None:
            try:
                lunar = converter.fromSolarDate(current.year, current.month, current.day)
                lunar_name = cls.LUNAR.get((int(lunar.month), int(lunar.day)), "")
                if lunar_name:
                    return lunar_name
                tomorrow = date.fromordinal(current.toordinal() + 1)
                next_lunar = converter.fromSolarDate(tomorrow.year, tomorrow.month, tomorrow.day)
                if int(lunar.month) == 12 and int(next_lunar.month) == 1 and int(next_lunar.day) == 1:
                    return "除夕"
            except (AttributeError, TypeError, ValueError, OverflowError) as exc:
                logger.debug(f"[ScheduledScenario] lunar festival lookup degraded: {type(exc).__name__}: {exc}")
        if current.month == 5 and 8 <= current.day <= 14 and current.weekday() == 6:
            return "母亲节"
        if current.month == 6 and 15 <= current.day <= 21 and current.weekday() == 6:
            return "父亲节"
        if current.month == 11 and 22 <= current.day <= 28 and current.weekday() == 3:
            return "感恩节"
        return ""


class WeatherProvider:
    ENDPOINT = "https://api.seniverse.com/v3/weather/now.json"

    def __init__(self, config: Any) -> None:
        self.config = config
        self._cached: WeatherSnapshot | None = None

    def refresh_config(self, config: Any) -> None:
        self.config = config
        self._cached = None

    async def get(self) -> WeatherSnapshot | None:
        life = getattr(self.config, "life", None)
        if not bool(getattr(life, "weather_context_enabled", False)):
            return None
        api_key = str(getattr(life, "weather_api_key", "") or "").strip()
        if not api_key:
            return None
        now = time.time()
        ttl = int(getattr(life, "weather_cache_ttl_sec", 1800) or 1800)
        if self._cached and now - self._cached.fetched_at <= ttl:
            return self._cached
        timeout = float(getattr(life, "weather_timeout_sec", 5.0) or 5.0)
        location = str(getattr(life, "weather_location", "beijing") or "beijing").strip()
        try:
            client_timeout = aiohttp.ClientTimeout(total=timeout)
            async with aiohttp.ClientSession(timeout=client_timeout) as session:
                async with session.get(
                    self.ENDPOINT,
                    params={"key": api_key, "location": location, "language": "zh-Hans", "unit": "c"},
                ) as response:
                    response.raise_for_status()
                    payload = await response.json()
            result = payload["results"][0]
            snapshot = WeatherSnapshot(
                text=str(result["now"]["text"]),
                temperature=str(result["now"]["temperature"]),
                location=str(result["location"]["path"]),
                fetched_at=now,
            )
            self._cached = snapshot
            return snapshot
        except (aiohttp.ClientError, asyncio.TimeoutError, KeyError, IndexError, TypeError, ValueError) as exc:
            logger.warning(f"[ScheduledScenario] weather lookup degraded: {type(exc).__name__}: {exc}")
            return None


class ScheduledScenarioService:
    """Builds idempotent life-scene candidates and delegates all visible behavior."""

    def __init__(
        self,
        *,
        state_engine: Any,
        dispatcher: Any,
        config: Any,
        db_path: Any,
        call_background_lane: Callable[..., Awaitable[str]],
        task_launcher: Callable[[Callable[[], Awaitable[Any]]], Any],
    ) -> None:
        self.state_engine = state_engine
        self.dispatcher = dispatcher
        self.config = config
        self.schedule_store = DailyScheduleStore(db_path)
        self.delivery_store = ScenarioDeliveryStore(db_path)
        self.call_background_lane = call_background_lane
        self.task_launcher = task_launcher
        self.weather = WeatherProvider(config)
        self._schedule_cache: dict[str, tuple[dict[str, str], str]] = {}
        self._generation_started: set[str] = set()
        self._generation_attempts: dict[str, int] = {}
        self._generation_retry_at: dict[str, float] = {}
        self._generation_state: dict[str, str] = {}
        self._generation_last_error: dict[str, str] = {}
        self._shutdown_requested = False
        self._last_tick_at = 0.0
        self._last_report: dict[str, Any] = {}

    def refresh_config(self, config: Any) -> None:
        self.config = config
        self.weather.refresh_config(config)

    def request_shutdown(self) -> None:
        """Fence schedule generation retries before lifecycle drains the budget."""
        self._shutdown_requested = True
        self._generation_started.clear()

    def resume(self) -> None:
        self._shutdown_requested = False

    @staticmethod
    def _slot(now: datetime) -> str:
        hour = now.hour
        if 6 <= hour < 8:
            return "morning"
        if 8 <= hour < 11:
            return "forenoon"
        if 11 <= hour < 13:
            return "lunch"
        if 13 <= hour < 17:
            return "afternoon"
        if 17 <= hour < 19:
            return "dinner"
        if 19 <= hour < 23:
            return "evening"
        return "night"

    @staticmethod
    def _inside_window(now: datetime, value: str, window_minutes: int) -> bool:
        try:
            hour_text, minute_text = str(value or "").split(":", 1)
            start_minutes = int(hour_text) * 60 + int(minute_text)
        except (TypeError, ValueError):
            return False
        current_minutes = now.hour * 60 + now.minute
        delta = (current_minutes - start_minutes) % (24 * 60)
        return 0 <= delta < max(1, int(window_minutes or 1))

    async def _get_schedule(self, plan_date: str) -> tuple[dict[str, str], str]:
        cached = self._schedule_cache.get(plan_date)
        if cached:
            return cached
        loaded = await self.schedule_store.load(plan_date)
        if loaded:
            self._schedule_cache[plan_date] = loaded
            return loaded
        fallback = (dict(DEFAULT_SCHEDULE), "fallback")
        self._schedule_cache[plan_date] = fallback
        await self.schedule_store.save(plan_date, fallback[0], source=fallback[1])
        return fallback

    def _start_schedule_generation(self, plan_date: str) -> None:
        life = getattr(self.config, "life", None)
        if self._shutdown_requested:
            return
        if plan_date in self._generation_started:
            return
        if self._generation_state.get(plan_date) in {"succeeded", "exhausted"}:
            return
        if not bool(getattr(life, "daily_schedule_enabled", True)):
            return
        if not bool(getattr(life, "daily_schedule_ai_enabled", True)):
            return
        if float(self._generation_retry_at.get(plan_date, 0.0) or 0.0) > time.time():
            return
        self._generation_started.add(plan_date)
        self._generation_state[plan_date] = "running"
        try:
            task = self.task_launcher(lambda: self._generate_schedule(plan_date))
            if inspect.isawaitable(task):
                task = asyncio.ensure_future(task)
            if task is None or not hasattr(task, "add_done_callback"):
                raise RuntimeError("schedule task launcher did not return a task")
        except BaseException as exc:
            self._generation_state[plan_date] = "launch_rejected"
            self._schedule_generation_failed(plan_date, exc)
            if plan_date in self._generation_retry_at:
                self._generation_state[plan_date] = "launch_rejected"
            return

        def _on_task_done(completed: Any) -> None:
            try:
                if completed.cancelled():
                    self._schedule_generation_failed(
                        plan_date,
                        asyncio.CancelledError("schedule generation task cancelled before completion"),
                    )
                    return
                error = completed.exception()
            except BaseException as exc:
                self._schedule_generation_failed(plan_date, exc)
                return
            if error is not None:
                self._schedule_generation_failed(plan_date, error)

        task.add_done_callback(_on_task_done)

    async def _generate_schedule(self, plan_date: str) -> None:
        prompt = (
            "请为角色生成今天的生活日程。只返回一个 JSON 对象，必须恰好包含 morning、forenoon、lunch、"
            "afternoon、dinner、evening、night 七个键。每项用一句简短中文描述角色当时在做什么；"
            "日程只作为聊天背景，不要替任何用户安排事务，也不要包含发送消息的指令。"
        )
        try:
            async with asyncio.timeout(45.0):
                raw = await self.call_background_lane("daily_schedule", plan_date, prompt)
            start = str(raw or "").find("{")
            end = str(raw or "").rfind("}") + 1
            if start < 0 or end <= start:
                raise ValueError("missing JSON object")
            parsed = json.loads(str(raw)[start:end])
            self._validate_schedule_payload(parsed)
            normalized = DailyScheduleStore._normalize(parsed)
            await self.schedule_store.save(plan_date, normalized, source="model")
            self._schedule_cache[plan_date] = (normalized, "model")
            self._generation_attempts.pop(plan_date, None)
            self._generation_retry_at.pop(plan_date, None)
            self._generation_state[plan_date] = "succeeded"
            self._generation_last_error.pop(plan_date, None)
            self._generation_started.discard(plan_date)
            logger.info(f"[ScheduledScenario] daily schedule generated date={plan_date}")
        except (asyncio.TimeoutError, TypeError, ValueError, json.JSONDecodeError) as exc:
            logger.warning(f"[ScheduledScenario] daily schedule degraded to fallback: {type(exc).__name__}: {exc}")
            self._schedule_generation_failed(plan_date, exc)
        except Exception as exc:
            logger.warning(f"[ScheduledScenario] daily schedule model failure: {type(exc).__name__}: {exc}")
            self._schedule_generation_failed(plan_date, exc)

    def _schedule_generation_failed(self, plan_date: str, exc: Exception) -> None:
        if self._shutdown_requested or isinstance(exc, BackgroundTaskQueueFull):
            self._generation_started.discard(plan_date)
            self._generation_retry_at.pop(plan_date, None)
            self._generation_state[plan_date] = "shutdown_rejected"
            self._generation_last_error[plan_date] = f"{type(exc).__name__}: {exc}"[:500]
            logger.info(
                f"[ScheduledScenario] daily schedule retry rejected during shutdown "
                f"date={plan_date} reason={type(exc).__name__}"
            )
            return
        life = getattr(self.config, "life", None)
        attempts = int(self._generation_attempts.get(plan_date, 0) or 0) + 1
        self._generation_attempts[plan_date] = attempts
        self._generation_last_error[plan_date] = f"{type(exc).__name__}: {exc}"[:500]
        max_retries = max(0, int(getattr(life, "daily_schedule_max_retries", 2) or 0))
        if attempts <= max_retries:
            base = max(30, int(getattr(life, "daily_schedule_retry_base_sec", 300) or 300))
            retry_at = time.time() + base * (2 ** max(0, attempts - 1))
            self._generation_retry_at[plan_date] = retry_at
            self._generation_started.discard(plan_date)
            self._generation_state[plan_date] = "retry_scheduled"
            logger.info(
                f"[ScheduledScenario] daily schedule retry scheduled date={plan_date} "
                f"attempt={attempts} retry_at={retry_at:.0f} reason={type(exc).__name__}"
            )
        else:
            self._generation_started.discard(plan_date)
            self._generation_retry_at.pop(plan_date, None)
            self._generation_state[plan_date] = "exhausted"
            logger.warning(
                f"[ScheduledScenario] daily schedule retries exhausted date={plan_date} "
                f"attempt={attempts} reason={type(exc).__name__}"
            )

    @staticmethod
    def _validate_schedule_payload(payload: Any) -> None:
        if not isinstance(payload, dict) or set(payload) != set(SCHEDULE_SLOTS):
            raise ValueError("schedule JSON must contain exactly the seven schedule slots")
        if any(not isinstance(payload[slot], str) or not payload[slot].strip() for slot in SCHEDULE_SLOTS):
            raise ValueError("schedule JSON slot values must be non-empty strings")

    def _scenario_for(self, now: datetime) -> str:
        life = getattr(self.config, "life", None)
        if bool(getattr(life, "morning_greeting_enabled", True)) and self._inside_window(
            now,
            str(getattr(life, "morning_greeting_time", "08:00") or "08:00"),
            int(getattr(life, "morning_greeting_window_min", 90) or 90),
        ):
            return "morning_greeting"
        if bool(getattr(life, "night_greeting_enabled", True)) and self._inside_window(
            now,
            str(getattr(life, "night_greeting_time", "22:30") or "22:30"),
            int(getattr(life, "night_greeting_window_min", 90) or 90),
        ):
            return "night_greeting"
        return ""

    @staticmethod
    def _chat_kind(state: Any) -> str:
        explicit = str(getattr(state, "chat_kind", "") or "").lower()
        if explicit in {"group", "private"}:
            return explicit
        return "group" if "GroupMessage" in str(getattr(state, "chat_id", "") or "") else "private"

    async def tick(self, *, now: float | None = None) -> dict[str, Any]:
        timestamp = float(now or time.time())
        life = getattr(self.config, "life", None)
        if not bool(getattr(life, "enable_proactive", True)) or not bool(
            getattr(life, "scheduled_scenarios_enabled", False)
        ):
            self._last_report = {"enabled": False, "timestamp": timestamp}
            return self._last_report
        if timestamp - self._last_tick_at < 30.0:
            return dict(self._last_report)
        self._last_tick_at = timestamp
        local_now = datetime.fromtimestamp(timestamp)
        plan_date = local_now.date().isoformat()
        schedule_enabled = bool(getattr(life, "daily_schedule_enabled", True))
        if schedule_enabled:
            schedule, schedule_source = await self._get_schedule(plan_date)
            self._start_schedule_generation(plan_date)
        else:
            schedule, schedule_source = {}, "disabled"
        scenario = self._scenario_for(local_now)
        if not scenario:
            self._last_report = {
                "enabled": True,
                "timestamp": timestamp,
                "scenario": "",
                "schedule_source": schedule_source,
                "dispatched": 0,
            }
            return self._last_report
        festival = FestivalProvider.get_name(local_now.date()) if bool(
            getattr(life, "festival_greeting_enabled", True)
        ) else ""
        weather = await self.weather.get()
        states = list(self.state_engine.get_active_states() or []) if hasattr(self.state_engine, "get_active_states") else []
        attempted = 0
        queued = 0
        blocked: dict[str, int] = {}
        for state in states[:64]:
            chat_id = str(getattr(state, "chat_id", "") or "")
            chat_kind = self._chat_kind(state)
            if not chat_id:
                continue
            if chat_kind == "group" and not bool(getattr(life, "enable_group_proactive", True)):
                continue
            if chat_kind == "private" and not bool(getattr(life, "enable_private_proactive", True)):
                continue
            delivery_key = f"{plan_date}:{scenario}:{chat_id}"
            claim_token = await self.delivery_store.claim(
                delivery_key,
                chat_id=chat_id,
                scenario=scenario,
                local_date=plan_date,
            )
            if not claim_token:
                continue
            attempted += 1
            slot = self._slot(local_now)
            greeting = "早安" if scenario == "morning_greeting" else "晚安"
            context_parts = [
                f"现在处于{greeting}候选窗口。",
            ]
            if schedule_enabled:
                context_parts.append(f"角色当前日程背景：{schedule.get(slot, DEFAULT_SCHEDULE[slot])}。")
            if festival:
                context_parts.append(f"今天是{festival}，可在自然时顺带提及，但不要强行祝福。")
            if weather:
                context_parts.append(f"客观天气信息：{weather.render()}。")
            context_parts.append("请先结合当前聊天判断是否适合开口；不适合就等待或忽略，不要机械报时。")

            async def _complete(
                sent: bool,
                preview: str,
                *,
                key: str = delivery_key,
                token: str = claim_token,
            ) -> None:
                await self.delivery_store.update(
                    key,
                    status="sent" if sent else "skipped",
                    claim_token=token,
                    error="" if sent else "attention_or_reply_skipped",
                    retry_after=0.0 if sent else float(
                        getattr(life, "proactive_failure_retry_sec", 300) or 300
                    ),
                )

            decision = await self.dispatcher.dispatch(
                ProactiveMessageIntent(
                    chat_id=chat_id,
                    source="scheduled_scenario",
                    reason=scenario,
                    guidance="\n".join(context_parts),
                    urgency=0.2,
                    cost=0.05,
                    cooldown=float(getattr(life, "wakeup_cooldown", 28800) or 28800),
                    metadata={
                        "claim_status": "claimed",
                        "claim_id": delivery_key,
                        "chat_kind": chat_kind,
                        "group_id": chat_id.rsplit(":", 1)[-1] if chat_kind == "group" else "",
                        "scenario": scenario,
                        "delivery_key": delivery_key,
                        "schedule_slot": slot,
                        "schedule_source": schedule_source,
                        "festival": festival,
                        "weather_available": bool(weather),
                        "allow_inactive_chat": bool(
                            getattr(life, "scheduled_scenarios_allow_inactive_chat", False)
                        ),
                    },
                    claim_validator=lambda key, token=claim_token: self.delivery_store.is_claim_current(
                        key,
                        claim_token=token,
                    ),
                ),
                on_complete=_complete,
            )
            if decision.synthetic_event_queued:
                queued += 1
                if not decision.reply_sent:
                    await self.delivery_store.update(
                        delivery_key,
                        status="queued",
                        claim_token=claim_token,
                    )
            else:
                reason = str(decision.blocked_reason or decision.status or "blocked")
                blocked[reason] = blocked.get(reason, 0) + 1
                has_completion_terminal = (
                    str(getattr(decision, "status", "") or "") in {"sent", "skipped", "timeout"}
                    and any(
                        item.get("stage") == "proactive.reply_commit"
                        for item in (getattr(decision, "stage_ledger", None) or [])
                    )
                )
                if not has_completion_terminal:
                    await self.delivery_store.update(
                        delivery_key,
                        status="blocked",
                        claim_token=claim_token,
                        error=reason,
                        retry_after=float(
                            getattr(life, "proactive_failure_retry_sec", 300) or 300
                        ),
                    )
        self._last_report = {
            "enabled": True,
            "timestamp": timestamp,
            "scenario": scenario,
            "schedule_source": schedule_source,
            "festival": festival,
            "weather_available": bool(weather),
            "attempted": attempted,
            "dispatched": queued,
            "blocked": blocked,
        }
        return dict(self._last_report)

    def clear_memory_claims(self) -> None:
        self.delivery_store.clear_memory_claims()

    def describe_status(self) -> dict[str, Any]:
        return {
            "generation_dates": sorted(self._generation_started)[-7:],
            "generation_attempts": dict(self._generation_attempts),
            "generation_retry_at": dict(self._generation_retry_at),
            "generation_state": dict(self._generation_state),
            "generation_last_error": dict(self._generation_last_error),
            "cached_schedule_dates": sorted(self._schedule_cache)[-7:],
            "weather_cached": self.weather._cached is not None,
            "claim_persistence_mode": "sqlite" if self.delivery_store.db_path else "memory_process_local",
            "memory_claim_count": len(self.delivery_store._memory_claims),
            "memory_claim_limit": self.delivery_store.MEMORY_CLAIM_LIMIT,
            "memory_claim_capacity_rejected": self.delivery_store._memory_claim_capacity_rejected,
            "shutdown_requested": self._shutdown_requested,
            "last_tick": dict(self._last_report),
        }


__all__ = [
    "DEFAULT_SCHEDULE",
    "DailyScheduleStore",
    "FestivalProvider",
    "ScenarioDeliveryStore",
    "ScheduledScenarioService",
    "WeatherProvider",
    "WeatherSnapshot",
]
