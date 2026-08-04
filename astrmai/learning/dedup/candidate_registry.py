from __future__ import annotations

import threading
from collections.abc import Iterable


class CandidateRegistry:
    """Process-local guard against concurrent enrichment of the same candidate."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._claimed: set[str] = set()

    def claim(self, fingerprints: Iterable[str]) -> tuple[set[str], set[str]]:
        accepted: set[str] = set()
        rejected: set[str] = set()
        with self._lock:
            for raw in fingerprints:
                fingerprint = str(raw or "").strip()
                if not fingerprint:
                    continue
                if fingerprint in self._claimed:
                    rejected.add(fingerprint)
                    continue
                self._claimed.add(fingerprint)
                accepted.add(fingerprint)
        return accepted, rejected

    def release(self, fingerprints: Iterable[str]) -> None:
        with self._lock:
            self._claimed.difference_update(
                str(item or "").strip() for item in fingerprints if str(item or "").strip()
            )


GLOBAL_CANDIDATE_REGISTRY = CandidateRegistry()


__all__ = ["CandidateRegistry", "GLOBAL_CANDIDATE_REGISTRY"]
