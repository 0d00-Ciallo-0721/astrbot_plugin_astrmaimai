from __future__ import annotations

import time
from typing import Any


def apply_natural_decay(state: Any, config: Any) -> None:
    # ponytail: wall-clock, mixed with DB values — do NOT replace with monotonic
    now = time.time()
    is_dirty = False
    minutes_silent = 999.0
    if getattr(state, "last_reply_time", 0):
        minutes_silent = (now - state.last_reply_time) / 60.0

    raw_last_decay = float(getattr(state, "last_passive_decay_time", 0.0) or 0.0)
    mood_last_decay = raw_last_decay if raw_last_decay > 0.0 else now

    _energy_cfg = getattr(config, "energy", None)
    recovery_min = getattr(_energy_cfg, "recovery_silence_min", 60) if _energy_cfg else 60
    recovery_window = recovery_min * 60.0 if recovery_min > 0 else 0.0
    raw_last_recovery = float(getattr(state, "last_energy_recovery_time", 0.0) or 0.0)
    recovery_anchor = raw_last_recovery if raw_last_recovery > 0.0 else float(getattr(state, "last_reply_time", 0.0) or 0.0)
    if (
        minutes_silent > recovery_min
        and state.energy < 0.8
        and recovery_window > 0.0
        and (now - recovery_anchor) >= recovery_window
    ):
        state.energy = min(0.8, state.energy + 0.1)
        state.last_energy_recovery_time = now
        is_dirty = True

    _mood_cfg = getattr(config, "mood", None)
    decay_interval = getattr(_mood_cfg, "decay_interval", 3600) if _mood_cfg else 3600
    decay_rate = getattr(_mood_cfg, "decay_rate", 0.05) if _mood_cfg else 0.05
    elapsed = now - mood_last_decay
    if decay_interval > 0 and elapsed >= decay_interval:
        decay_steps = int(elapsed / decay_interval)
        total_decay = decay_steps * decay_rate
        old_mood = state.mood
        if state.mood > 0:
            state.mood = max(0.0, state.mood - total_decay)
        elif state.mood < 0:
            state.mood = min(0.0, state.mood + total_decay)
        if old_mood != state.mood:
            advanced_decay_time = mood_last_decay + decay_steps * decay_interval
            state.last_passive_decay_time = advanced_decay_time
            is_dirty = True
    elif raw_last_decay <= 0.0:
        state.last_passive_decay_time = now

    if is_dirty:
        state.is_dirty = True
