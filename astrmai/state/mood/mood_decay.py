from __future__ import annotations

import time
from typing import Any


def apply_natural_decay(state: Any, config: Any) -> None:
    now = time.time()
    is_dirty = False
    minutes_silent = 999.0
    if getattr(state, "last_reply_time", 0):
        minutes_silent = (now - state.last_reply_time) / 60.0

    recovery_min = getattr(config.energy, "recovery_silence_min", 60)
    if minutes_silent > recovery_min and state.energy < 0.8:
        state.energy = min(0.8, state.energy + 0.1)
        is_dirty = True

    last_decay = getattr(state, "last_passive_decay_time", 0) or now
    if getattr(state, "last_passive_decay_time", 0) == 0:
        state.last_passive_decay_time = now

    decay_interval = getattr(config.mood, "decay_interval", 3600)
    decay_rate = getattr(config.mood, "decay_rate", 0.05)
    elapsed = now - last_decay
    if decay_interval > 0 and elapsed >= decay_interval:
        decay_steps = int(elapsed / decay_interval)
        total_decay = decay_steps * decay_rate
        old_mood = state.mood
        if state.mood > 0:
            state.mood = max(0.0, state.mood - total_decay)
        elif state.mood < 0:
            state.mood = min(0.0, state.mood + total_decay)
        if old_mood != state.mood:
            state.last_passive_decay_time += decay_steps * decay_interval
            is_dirty = True

    if is_dirty:
        state.is_dirty = True
