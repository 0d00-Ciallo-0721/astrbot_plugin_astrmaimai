from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any


DEFAULT_QUIET_HOURS = ("23:30-07:30",)
_MISSING = object()


@dataclass(frozen=True, slots=True)
class ProactiveRhythm:
    quiet_hours: bool
    time_bucket: str
    quiet_ranges: tuple[str, ...]
    base_frequency: float
    base_frequency_factor: float

    def threshold(self, base: float) -> float:
        return max(0.0, min(1.0, float(base or 0.0) * self.base_frequency_factor))


def _clamp(value: float, low: float = 0.0, high: float = 1.0) -> float:
    return max(low, min(high, value))


def _minutes_from_hhmm(value: str) -> int | None:
    parts = str(value or "").strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hour = int(parts[0])
        minute = int(parts[1])
    except ValueError:
        return None
    if hour < 0 or hour > 23 or minute < 0 or minute > 59:
        return None
    return hour * 60 + minute


def _normalize_ranges(raw_ranges: Any) -> tuple[str, ...]:
    if raw_ranges is _MISSING:
        return DEFAULT_QUIET_HOURS
    if raw_ranges is None:
        return ()
    if isinstance(raw_ranges, str):
        items = [raw_ranges]
    else:
        try:
            items = list(raw_ranges)
        except TypeError:
            items = []
    normalized = tuple(str(item).strip() for item in items if str(item or "").strip())
    return normalized


def _in_range(now_minutes: int, item: str) -> bool:
    if "-" not in item:
        return False
    start_raw, end_raw = item.split("-", 1)
    start = _minutes_from_hhmm(start_raw)
    end = _minutes_from_hhmm(end_raw)
    if start is None or end is None or start == end:
        return False
    if start < end:
        return start <= now_minutes < end
    return now_minutes >= start or now_minutes < end


def _time_bucket(now_minutes: int, quiet: bool) -> str:
    if quiet:
        return "quiet"
    if 450 <= now_minutes < 720:
        return "morning"
    if 720 <= now_minutes < 1080:
        return "day"
    return "evening"


def evaluate_proactive_rhythm(config: Any = None, *, now: float | None = None) -> ProactiveRhythm:
    current = time.time() if now is None else float(now)
    # NOTE: uses local time; containerized deployments should configure host TZ
    local = time.localtime(current)
    now_minutes = int(local.tm_hour) * 60 + int(local.tm_min)
    life = getattr(config, "life", None) if config is not None else None
    reply = getattr(config, "reply", None)
    if config is None:
        ranges = ()
    else:
        ranges = _normalize_ranges(getattr(life, "proactive_quiet_hours", _MISSING))
    quiet = any(_in_range(now_minutes, item) for item in ranges)
    raw_frequency = getattr(reply, "base_frequency", 0.7)
    base_frequency = _clamp(float(0.7 if raw_frequency is None else raw_frequency))
    factor = _clamp(1.0 + (0.7 - base_frequency) * 0.25, 0.88, 1.12)
    return ProactiveRhythm(
        quiet_hours=quiet,
        time_bucket=_time_bucket(now_minutes, quiet),
        quiet_ranges=ranges,
        base_frequency=base_frequency,
        base_frequency_factor=factor,
    )


__all__ = ["DEFAULT_QUIET_HOURS", "ProactiveRhythm", "evaluate_proactive_rhythm"]
