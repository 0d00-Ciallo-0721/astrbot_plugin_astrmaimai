from __future__ import annotations

import random
from typing import Any


class EnergyManager:
    def __init__(self, config: Any):
        self.config = config

    def _energy_config(self) -> Any:
        return getattr(self.config, "energy", None)

    def get_reply_cost(self, explicit_amount: float | None = None) -> float:
        if explicit_amount is not None:
            return float(explicit_amount)
        energy_cfg = self._energy_config()
        return float(getattr(energy_cfg, "cost_per_reply", 0.1) if energy_cfg else 0.1)

    def should_drop_by_energy(self, state: Any, msg_count: int) -> bool:
        """Return True when the message should be dropped due to low energy.

        Side Effect (documented):
            When this method returns ``True``, it implements a "skip-to-recharge"
            design: the bot skips a reply to preserve energy for more important
            messages. Energy is recovered as::

                recover = float(msg_count) * cost_per_reply
                state.energy = min(1.0, current_energy + recover)

            ``state.is_dirty`` is set to ``True`` so the caller can persist.
            Callers **MUST** persist ``state.energy`` after this call regardless
            of the return value, as ``is_dirty`` may also be set by daily reset
            or natural decay executed prior to this call.
        """
        current_energy = float(getattr(state, "energy", 1.0))
        energy_cfg = self._energy_config()
        min_threshold = float(getattr(energy_cfg, "min_reply_threshold", 0.1) if energy_cfg else 0.1)
        if current_energy >= 0.5:
            return False
        if current_energy <= min_threshold:
            drop_prob = 1.0
        else:
            drop_prob = min(1.0, max(0.0, (0.5 - current_energy) / max(0.001, (0.5 - min_threshold))))
        if random.random() < drop_prob:
            recover_amount = max(self.get_reply_cost(), float(msg_count) * self.get_reply_cost())
            state.energy = min(1.0, current_energy + recover_amount)
            state.is_dirty = True
            return True
        return False
