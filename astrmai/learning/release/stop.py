from __future__ import annotations

from dataclasses import dataclass
import threading
from typing import Callable

from .flags import PersistentKillSwitch


@dataclass(frozen=True, slots=True)
class StopResult:
    actions: tuple[str, ...]
    reason: str
    already_stopped: bool


class StopCoordinator:
    def __init__(
        self,
        *,
        kill_switch: PersistentKillSwitch,
        disable_flags: Callable[[], None],
        stop_claims: Callable[[], None],
        persist_alert_and_pointers: Callable[[], None],
        produce_rollback_artifact: Callable[[], None],
    ):
        self.kill_switch = kill_switch
        self.disable_flags = disable_flags
        self.stop_claims = stop_claims
        self.persist_alert_and_pointers = persist_alert_and_pointers
        self.produce_rollback_artifact = produce_rollback_artifact
        self._result: StopResult | None = None
        self._lock = threading.Lock()

    def stop(self, *, reason: str, operator_id: str) -> StopResult:
        with self._lock:
            if self._result is not None:
                return self._result
            self.kill_switch.trip(reason=reason, operator_id=operator_id)
            self.disable_flags()
            self.stop_claims()
            self.persist_alert_and_pointers()
            self.produce_rollback_artifact()
            self._result = StopResult(
                (
                    "kill_switch",
                    "disable_injection_enrichment",
                    "stop_new_claims",
                    "persist_alert_pointer_state",
                    "produce_rollback_artifact",
                ),
                reason,
                False,
            )
            return self._result
