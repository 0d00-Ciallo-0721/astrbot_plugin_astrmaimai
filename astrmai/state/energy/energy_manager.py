from __future__ import annotations

import random
from typing import Any


class EnergyManager:
    def __init__(self, config: Any):
        self.config = config

    def get_reply_cost(self, explicit_amount: float | None = None) -> float:
        if explicit_amount is not None:
            return float(explicit_amount)
        return float(self.config.energy.cost_per_reply)

    def should_drop_by_energy(self, state: Any, msg_count: int) -> bool:
        current_energy = float(getattr(state, "energy", 1.0))
        min_threshold = float(self.config.energy.min_reply_threshold)
        if current_energy >= 0.5:
            return False
        if current_energy <= min_threshold:
            drop_prob = 1.0
        else:
            drop_prob = max(0.0, (0.5 - current_energy) / max(0.001, (0.5 - min_threshold)))
        if random.random() < drop_prob:
            recover_amount = float(msg_count) * float(self.config.energy.cost_per_reply)
            state.energy = min(1.0, current_energy + recover_amount)
            state.is_dirty = True
            return True
        return False
