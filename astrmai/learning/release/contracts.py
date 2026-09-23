from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping


_SENSITIVE_KEYS = {
    "api_key", "apikey", "access_token", "authorization", "cookie",
    "password", "secret", "token", "private_key", "full_message",
    "message_body", "raw_message", "prompt_text", "completion_text",
}


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, sort_keys=True, separators=(",", ":"))


def _jsonable(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if hasattr(value, "as_dict"):
        return value.as_dict()
    if hasattr(value, "to_dict"):
        return value.to_dict()
    if hasattr(value, "__dataclass_fields__"):
        return {name: _jsonable(getattr(value, name)) for name in value.__dataclass_fields__}
    if isinstance(value, Mapping):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, (tuple, list)):
        return [_jsonable(item) for item in value]
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, (tuple, list)):
        return tuple(_freeze(item) for item in value)
    return value


def _contains_sensitive(value: Any, path: str = "") -> bool:
    if isinstance(value, Mapping):
        for key, item in value.items():
            key_name = str(key).lower()
            if key_name in _SENSITIVE_KEYS or any(token in key_name for token in ("api_key", "cookie", "password", "secret")):
                return True
            if _contains_sensitive(item, f"{path}.{key}"):
                return True
        return False
    if isinstance(value, (tuple, list)):
        return any(_contains_sensitive(item, path) for item in value)
    return False


def _path_overlap(left: Path, right: Path) -> bool:
    try:
        if left.exists() and right.exists() and os.path.samefile(left, right):
            return True
    except OSError:
        pass
    try:
        left_resolved = left.resolve(strict=False)
        right_resolved = right.resolve(strict=False)
        return left_resolved == right_resolved or left_resolved in right_resolved.parents or right_resolved in left_resolved.parents
    except OSError:
        return False


@dataclass(frozen=True, slots=True)
class ProviderIdentity:
    provider_id: str
    provider_family: str
    model_id: str
    identity_source: str

    def __post_init__(self) -> None:
        if not all(
            isinstance(value, str) and value.strip()
            for value in (
                self.provider_id,
                self.provider_family,
                self.model_id,
                self.identity_source,
            )
        ):
            raise ValueError("provider identity fields must be non-empty")


@dataclass(frozen=True, slots=True)
class ReleaseManifest:
    release_id: str
    code_revision: str
    schema_version: int
    configuration: Mapping[str, Any]
    prompt_fingerprint_version: str
    provider: ProviderIdentity
    cohort_id: str
    cohort_candidates: tuple[str, ...]
    cohort_exclusions: tuple[tuple[str, tuple[str, ...]], ...]
    flags: Mapping[str, bool]
    kill_switch_state: bool
    baseline_window: str
    artifact_root: Path
    created_at: str
    manifest_sha256: str
    source_snapshot_hash: str = ""
    lane_parameters: Mapping[str, Any] = field(default_factory=dict)
    rollback_pointers: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def create(
        cls,
        *,
        code_revision: str,
        schema_version: int,
        configuration: Mapping[str, Any],
        prompt_fingerprint_version: str,
        provider: ProviderIdentity,
        cohort_id: str,
        cohort_candidates: tuple[str, ...] | list[str],
        cohort_exclusions: tuple[tuple[str, tuple[str, ...]], ...] | list[tuple[str, tuple[str, ...]]],
        flags: Mapping[str, bool],
        baseline_window: str,
        artifact_root: Path,
        created_at: str,
        repository_root: Path | None = None,
        kill_switch_state: bool | None = None,
        protected_paths: tuple[Path, ...] = (),
        source_snapshot_hash: str = "",
        lane_parameters: Mapping[str, Any] | None = None,
        rollback_pointers: Mapping[str, Any] | None = None,
    ) -> "ReleaseManifest":
        repository_root = (repository_root or Path.cwd()).resolve()
        artifact_root = Path(artifact_root)
        if artifact_root.is_symlink():
            raise ValueError("artifact root must not be a symlink")
        if not artifact_root.is_absolute():
            artifact_root = (Path.cwd() / artifact_root).resolve()
        else:
            artifact_root = artifact_root.resolve(strict=False)
        if artifact_root == repository_root or repository_root in artifact_root.parents:
            raise ValueError("artifact root must be outside repository")
        for protected in protected_paths:
            protected = Path(protected)
            if _path_overlap(artifact_root, protected) or _path_overlap(artifact_root, protected.with_name(protected.name + "-wal")) or _path_overlap(artifact_root, protected.with_name(protected.name + "-shm")):
                raise ValueError("artifact root overlaps protected source or sidecar")
        if _contains_sensitive(configuration) or _contains_sensitive(flags) or _contains_sensitive(cohort_exclusions):
            raise ValueError("manifest contains sensitive material")
        if not isinstance(code_revision, str) or not code_revision.strip() or not isinstance(prompt_fingerprint_version, str) or not prompt_fingerprint_version.strip() or not isinstance(cohort_id, str) or not cohort_id.strip() or not isinstance(created_at, str) or not created_at.strip():
            raise ValueError("manifest identity fields must be non-empty")
        if not isinstance(schema_version, int) or schema_version < 0:
            raise ValueError("schema version must be a non-negative integer")
        if not isinstance(flags, Mapping) or not all(isinstance(value, bool) for value in flags.values()):
            raise ValueError("manifest flags must be booleans")
        candidates = tuple(sorted(str(item) for item in cohort_candidates))
        exclusions = tuple(sorted((str(scope), tuple(sorted(str(reason) for reason in reasons))) for scope, reasons in cohort_exclusions))
        payload = {
            "code_revision": str(code_revision),
            "schema_version": schema_version,
            "configuration": _jsonable(configuration),
            "prompt_fingerprint_version": str(prompt_fingerprint_version),
            "provider": _jsonable(provider),
            "cohort_id": str(cohort_id),
            "cohort_candidates": candidates,
            "cohort_exclusions": exclusions,
            "flags": dict(sorted(flags.items())),
            "kill_switch_state": bool(flags.get("learning_release_kill_switch", False) if kill_switch_state is None else kill_switch_state),
            "baseline_window": str(baseline_window),
            "artifact_root": str(artifact_root),
            "created_at": str(created_at),
            "source_snapshot_hash": str(source_snapshot_hash),
            "lane_parameters": _jsonable(lane_parameters or {}),
            "rollback_pointers": _jsonable(rollback_pointers or {}),
        }
        digest = hashlib.sha256(_canonical(payload).encode("utf-8")).hexdigest()
        return cls(
            release_id=f"release-{digest[:16]}",
            code_revision=str(code_revision),
            schema_version=schema_version,
            configuration=_freeze(_jsonable(configuration)),
            prompt_fingerprint_version=str(prompt_fingerprint_version),
            provider=provider,
            cohort_id=str(cohort_id),
            cohort_candidates=candidates,
            cohort_exclusions=exclusions,
            flags=_freeze(dict(sorted(flags.items()))),
            kill_switch_state=payload["kill_switch_state"],
            baseline_window=str(baseline_window),
            artifact_root=artifact_root,
            created_at=str(created_at),
            manifest_sha256=digest,
            source_snapshot_hash=str(source_snapshot_hash),
            lane_parameters=_freeze(_jsonable(lane_parameters or {})),
            rollback_pointers=_freeze(_jsonable(rollback_pointers or {})),
        )

    @classmethod
    def from_config(cls, config: object, **kwargs: Any) -> "ReleaseManifest":
        """Build release-facing flags from an existing config object."""
        evolution = getattr(config, "evolution", config)
        names = (
            "learning_discovery_enabled",
            "learning_enrichment_enabled",
            "learning_quality_shadow_enabled",
            "learning_retrieval_shadow_enabled",
            "learning_injection_enabled",
            "learning_release_kill_switch",
        )
        missing = [name for name in names if not hasattr(evolution, name)]
        if missing:
            raise ValueError(f"missing release configuration: {', '.join(missing)}")
        from .flags import runtime_release_flags
        flags = runtime_release_flags(evolution).as_dict()
        kwargs.setdefault("flags", flags)
        kwargs.setdefault("kill_switch_state", flags["learning_release_kill_switch"])
        return cls.create(**kwargs)

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "release_id": self.release_id,
            "code_revision": self.code_revision,
            "schema_version": self.schema_version,
            "configuration": _jsonable(self.configuration),
            "prompt_fingerprint_version": self.prompt_fingerprint_version,
            "provider": _jsonable(self.provider),
            "cohort_id": self.cohort_id,
            "cohort_candidates": self.cohort_candidates,
            "cohort_exclusions": self.cohort_exclusions,
            "flags": dict(self.flags),
            "kill_switch_state": self.kill_switch_state,
            "baseline_window": self.baseline_window,
            "artifact_root": str(self.artifact_root),
            "created_at": self.created_at,
            "source_snapshot_hash": self.source_snapshot_hash,
            "lane_parameters": _jsonable(self.lane_parameters or {}),
            "rollback_pointers": _jsonable(self.rollback_pointers or {}),
            "manifest_sha256": self.manifest_sha256,
        }

    def to_json(self) -> str:
        return _canonical(self.canonical_payload())
