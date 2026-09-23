from __future__ import annotations

from dataclasses import asdict, dataclass
import json
from pathlib import Path
from typing import Callable


@dataclass(frozen=True, slots=True)
class PointerState:
    canonical_pointer: str
    vector_pointer: str
    retrieval_pointer: str
    generation: int
    mapping_sha256: str
    index_sha256: str
    provenance_digest: str


@dataclass(frozen=True, slots=True)
class RollbackResult:
    succeeded: bool
    reason: str
    artifact_path: Path


class RollbackCoordinator:
    def __init__(
        self,
        *,
        inspect: Callable[[], PointerState],
        restore: Callable[[PointerState], None],
        verify: Callable[[PointerState], bool],
        artifact_path: Path,
    ):
        self.inspect = inspect
        self.restore = restore
        self.verify = verify
        self.artifact_path = Path(artifact_path)

    def rehearse(
        self,
        *,
        release_id: str,
        manifest_sha256: str,
        expected_current: PointerState,
        manifest_before: PointerState,
        requested_target: PointerState,
    ) -> RollbackResult:
        before = self.inspect()
        result = RollbackResult(False, "not_attempted", self.artifact_path)
        try:
            if before != expected_current:
                raise ValueError("current pointer does not match expected release state")
            if requested_target != manifest_before:
                raise ValueError("rollback target is not manifest-bound")
            self.restore(manifest_before)
            if not self.verify(manifest_before) or self.inspect() != manifest_before:
                raise ValueError("rollback pointer verification failed")
            result = RollbackResult(True, "restored", self.artifact_path)
        except Exception as exc:
            result = RollbackResult(False, str(exc), self.artifact_path)
        self.artifact_path.parent.mkdir(parents=True, exist_ok=True)
        self.artifact_path.write_text(
            json.dumps(
                {
                    "release_id": release_id,
                    "manifest_sha256": manifest_sha256,
                    "before": asdict(before),
                    "manifest_before": asdict(manifest_before),
                    "requested_target": asdict(requested_target),
                    "succeeded": result.succeeded,
                    "reason": result.reason,
                },
                sort_keys=True,
            ),
            encoding="utf-8",
        )
        return result
