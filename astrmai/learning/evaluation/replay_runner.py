"""Safe, offline evaluation replay runner.

The runner deliberately treats the existing learning pipeline as unavailable
unless a caller supplies an explicit recorded fixture.  It never opens the
source database for writing and never creates a provider client.
"""

from __future__ import annotations

import hashlib
import asyncio
import json
import os
import re
import shutil
import sqlite3
import tempfile
import time
from dataclasses import replace
from datetime import datetime, timezone
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterable, Mapping
from urllib.parse import quote

from .contracts import (
    MetricResult,
    ReplayManifest,
    ReplayResult,
    canonical_hash,
    canonical_json,
    evaluate_workflow_evidence,
    evaluate_stage10_readiness,
    metric_from_counts,
)


class ReplayInputError(ValueError):
    """Input failed the replay contract (CLI exit code 3)."""


class ReplaySecurityError(ReplayInputError):
    """A source/output integrity or path boundary was violated (exit code 4)."""


_REQUIRED_ARTIFACTS = (
    "gold_set_manifest.json",
    "gold_labels.jsonl",
    "adjudication_log.jsonl",
    "replay_manifest.json",
    "replay_result.json",
    "funnel.json",
    "metrics.json",
    "unknowns.jsonl",
    "blocked.jsonl",
    "query_catalog.json",
    "source_integrity_before.json",
    "source_integrity_after.json",
    "memory_v2_integrity_before.json",
    "memory_v2_integrity_after.json",
)
_METRIC_MINIMUMS = {
    "candidate_precision": 100,
    "candidate_recall": 50,
    "speaker_attribution_accuracy": 50,
    "scope_accuracy": 50,
    "duplicate_rate": 100,
    "enrichment_complete_rate": 30,
    "cursor_monotonicity_rate": 100,
    "trace_join_rate": 50,
    "prompt_visible_rate": 50,
    "reply_outcome_known_rate": 50,
    "dialog_reply_loss_rate": 100,
}
_MEMORY_V2_SCHEMA_VERSION = 4


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sidecars(path: Path) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for suffix in ("-wal", "-shm", "-journal"):
        item = Path(f"{path}{suffix}")
        if item.exists():
            result.append({
                "name": item.name,
                "size_bytes": item.stat().st_size,
                "sha256": sha256_file(item) if item.is_file() else None,
            })
    return result


@contextmanager
def _readonly_sqlite(path: Path):
    # `immutable=1` makes SQLite ignore a companion WAL. Replay inputs are
    # already isolated read-only snapshots, so normal read-only mode preserves
    # the committed state of db+wal+shm inputs without allowing writes.
    uri = f"file:{quote(path.resolve().as_posix(), safe='/:%')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True, timeout=5.0)
    try:
        connection.execute("PRAGMA query_only=ON")
        yield connection
    finally:
        connection.close()


def snapshot_integrity(path: str | Path) -> dict[str, Any]:
    """Return stable, non-sensitive file and SQLite integrity facts."""
    source = Path(path).expanduser().resolve()
    if not source.is_file():
        raise ReplaySecurityError("source snapshot must be a regular SQLite file")
    stat = source.stat()
    result: dict[str, Any] = {
        "basename": source.name,
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
        "sha256": sha256_file(source),
        "sidecars": _sidecars(source),
    }
    try:
        with _readonly_sqlite(source) as db:
            result["user_version"] = int(db.execute("PRAGMA user_version").fetchone()[0])
            schema_row = None
            if db.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'memory_v2_meta'"
            ).fetchone():
                schema_row = db.execute(
                    "SELECT value FROM memory_v2_meta WHERE key = 'schema_version'"
                ).fetchone()
            result["memory_v2_schema_version"] = (
                int(schema_row[0]) if schema_row and str(schema_row[0]).strip().isdigit() else None
            )
            result["page_count"] = int(db.execute("PRAGMA page_count").fetchone()[0])
            result["page_size"] = int(db.execute("PRAGMA page_size").fetchone()[0])
            result["integrity_check"] = str(db.execute("PRAGMA integrity_check").fetchone()[0])
            result["foreign_key_violations"] = [list(row) for row in db.execute("PRAGMA foreign_key_check").fetchall()]
            result["journal_mode"] = str(db.execute("PRAGMA journal_mode").fetchone()[0])
    except (sqlite3.Error, OSError) as exc:
        result.update({"integrity_check": "error", "foreign_key_violations": [], "error_kind": type(exc).__name__})
    result["valid"] = result.get("integrity_check") == "ok" and not result.get("foreign_key_violations")
    return result


def _copy_snapshot_bundle(
    source: Path,
    target: Path,
    integrity: Mapping[str, Any],
    *,
    label: str,
) -> dict[str, Any]:
    """Copy a SQLite snapshot and every declared sidecar with hash checks."""
    shutil.copy2(source, target)
    expected_sidecars = list(integrity.get("sidecars") or ())
    for sidecar in expected_sidecars:
        name = str(sidecar.get("name") or "")
        if not name:
            raise ReplaySecurityError(f"{label} sidecar identity missing")
        source_sidecar = source.parent / name
        target_sidecar = target.parent / name
        if source_sidecar.is_symlink() or not source_sidecar.is_file():
            raise ReplaySecurityError(f"{label} sidecar missing")
        shutil.copy2(source_sidecar, target_sidecar)
        expected_hash = str(sidecar.get("sha256") or "")
        if expected_hash and sha256_file(target_sidecar) != expected_hash:
            raise ReplaySecurityError(f"{label} sidecar hash mismatch")
    copied = snapshot_integrity(target)
    if copied.get("sha256") != integrity.get("sha256"):
        raise ReplaySecurityError(f"{label} working copy hash mismatch")
    copied_sidecars = {str(item.get("name")): str(item.get("sha256") or "") for item in copied.get("sidecars", ())}
    expected = {str(item.get("name")): str(item.get("sha256") or "") for item in expected_sidecars}
    if copied_sidecars != expected:
        raise ReplaySecurityError(f"{label} sidecar set mismatch")
    if not copied.get("valid"):
        raise ReplaySecurityError(f"{label} working copy integrity check failed")
    return copied


def _is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
        return True
    except ValueError:
        return False


def _contains_symlink(path: Path) -> bool:
    current = path
    while current != current.parent:
        if current.is_symlink():
            return True
        current = current.parent
    return current.is_symlink()


def _same_inode(a: Path, b: Path) -> bool:
    try:
        return a.exists() and b.exists() and os.stat(a).st_ino == os.stat(b).st_ino and os.stat(a).st_dev == os.stat(b).st_dev
    except OSError:
        return False


def _validate_output_root(source: Path, output_root: Path) -> None:
    repo = Path(__file__).resolve().parents[3]
    source_dir = source.parent.resolve()
    output = output_root.expanduser().resolve(strict=False)
    if _is_within(output, repo) or _is_within(repo, output):
        raise ReplaySecurityError("output root must be outside repository")
    if _is_within(output, source_dir) or _is_within(source_dir, output):
        raise ReplaySecurityError("output root overlaps source directory")
    if _contains_symlink(output_root.expanduser()):
        raise ReplaySecurityError("output root may not contain a symlink")
    if output.exists() and output.is_file():
        raise ReplaySecurityError("output root must be a directory")
    if output.exists() and _same_inode(output, source):
        raise ReplaySecurityError("output root overlaps source inode")
    if output.exists():
        for item in output.rglob("*"):
            if item.is_symlink() or _same_inode(item, source):
                raise ReplaySecurityError("output root has symlink or hardlink overlap")


def validate_output_file(source: str | Path, output_file: str | Path) -> Path:
    """Validate a single CLI artifact path before any write occurs."""
    source_input = Path(source).expanduser()
    if source_input.is_symlink():
        raise ReplaySecurityError("source snapshot may not be a symlink")
    source_resolved = source_input.resolve()
    output_input = Path(output_file).expanduser()
    if _contains_symlink(output_input):
        raise ReplaySecurityError("output file path may not contain a symlink")
    output = output_input.resolve(strict=False)
    if _is_within(output, source_resolved.parent) or _is_within(source_resolved.parent, output):
        raise ReplaySecurityError("output file overlaps source directory")
    if output.exists() and (_same_inode(output, source_resolved) or output.is_symlink()):
        raise ReplaySecurityError("output file overlaps source inode")
    return output


def _utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _write_json(path: Path, value: Any) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")


def _write_jsonl(path: Path, rows: Iterable[Mapping[str, Any]]) -> None:
    with path.open("w", encoding="utf-8", newline="\n") as handle:
        for row in rows:
            handle.write(canonical_json(dict(row)) + "\n")


def _load_json(path: Path) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ReplayInputError(f"invalid JSON fixture: {path.name}") from exc


def sample_manifest_hash(sample_path: Path) -> str:
    payload = _load_json(sample_path)
    if not isinstance(payload, Mapping):
        raise ReplayInputError("sample manifest must be a JSON object")
    declared = payload.get("manifest_hash")
    if not isinstance(declared, str) or len(declared) != 64:
        raise ReplayInputError("sample manifest_hash is required")
    content = dict(payload)
    content.pop("manifest_hash", None)
    computed = canonical_hash(content)
    if declared.lower() != computed:
        raise ReplayInputError("sample manifest hash does not match content")
    return declared.lower()


def _recording_key(row: Mapping[str, Any]) -> tuple[str, str, str, str, str]:
    return tuple(str(row.get(name) or "") for name in (
        "request_fingerprint", "provider_id", "model_id", "prompt_version", "schema_version",
    ))


def recorded_request_fingerprint(
    provider_id: str,
    model_id: str,
    normalized_request: str,
    prompt_version: str,
) -> str:
    """Return the stage-09 stable Provider recording identity."""
    return hashlib.sha256(
        f"{str(provider_id)}{str(model_id)}{str(normalized_request)}{str(prompt_version)}".encode("utf-8")
    ).hexdigest()


def _validate_recordings(path: Path, manifest: ReplayManifest) -> tuple[list[dict[str, Any]], list[str]]:
    if not path.is_file():
        raise ReplayInputError("recordings file is required for recorded replay")
    rows: list[dict[str, Any]] = []
    by_key: dict[tuple[str, str, str, str, str], str] = {}
    conflicts: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            if not line.strip():
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ReplayInputError(f"invalid recording JSON at line {line_number}") from exc
            if not isinstance(row, dict):
                raise ReplayInputError(f"recording line {line_number} is not an object")
            key = _recording_key(row)
            if len(key[0]) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in key[0]):
                raise ReplayInputError("recording request_fingerprint must be SHA-256")
            if not key[1] or not key[2] or not key[3] or not key[4]:
                raise ReplayInputError("recording provider/model/prompt/schema identity is required")
            response_hash = str(row.get("response_hash") or "")
            if len(response_hash) != 64 or any(ch not in "0123456789abcdefABCDEF" for ch in response_hash):
                raise ReplayInputError("recording response_hash must be SHA-256")
            previous = by_key.get(key)
            if previous is not None and previous != response_hash:
                conflicts.append(f"recording_conflict:{key[0]}")
            if manifest.provider_id != "none" and key[1] != manifest.provider_id:
                conflicts.append(f"recording_provider_mismatch:{key[0]}")
            if manifest.model_id != "none" and key[2] != manifest.model_id:
                conflicts.append(f"recording_model_mismatch:{key[0]}")
            if key[3] != manifest.prompt_version:
                conflicts.append(f"recording_prompt_mismatch:{key[0]}")
            if key[4] != str(manifest.execution_schema_version):
                conflicts.append(f"recording_schema_mismatch:{key[0]}")
            outcome = str(row.get("outcome") or "success").lower()
            if outcome != "success":
                conflicts.append(f"recording_outcome_{outcome}:{key[0]}")
            if outcome == "success" and not row.get("response_payload"):
                conflicts.append(f"recording_empty_response:{key[0]}")
            if outcome == "success" and row.get("response_payload") is not None:
                payload = row.get("response_payload")
                encoded = canonical_json(payload).encode("utf-8") if isinstance(payload, (dict, list)) else str(payload).encode("utf-8")
                if hashlib.sha256(encoded).hexdigest() != response_hash:
                    conflicts.append(f"recording_response_hash_mismatch:{key[0]}")
            by_key[key] = response_hash
            rows.append(row)
    return rows, conflicts


def _query_catalog(db: sqlite3.Connection) -> list[dict[str, Any]]:
    tables = db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name").fetchall()
    result = []
    for (name,) in tables:
        try:
            count = int(db.execute(f'SELECT COUNT(*) FROM "{str(name).replace(chr(34), chr(34) * 2)}"').fetchone()[0])
        except sqlite3.Error:
            count = None
        result.append({"table": str(name), "row_count": count})
    return result


def _table_columns(db: sqlite3.Connection, table: str) -> set[str]:
    """Return columns for an optional durable table without trusting input SQL."""
    try:
        return {str(row[1]) for row in db.execute(
            f'PRAGMA table_info("{table.replace(chr(34), chr(34) * 2)}")'
        ).fetchall()}
    except sqlite3.Error:
        return set()


def _row_dicts(db: sqlite3.Connection, table: str, where: str = "", params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    columns = _table_columns(db, table)
    if not columns:
        return []
    # Every where clause below is a constant selected by this module; values remain bound.
    quoted = f'"{table.replace(chr(34), chr(34) * 2)}"'
    names = sorted(columns)
    select = ",".join(f'"{name.replace(chr(34), chr(34) * 2)}"' for name in names)
    try:
        rows = db.execute(f"SELECT {select} FROM {quoted}{where}", params).fetchall()
    except sqlite3.Error:
        return []
    return [dict(zip(names, row)) for row in rows]


def _json_value(value: Any, default: Any) -> Any:
    if value in (None, ""):
        return default
    try:
        return json.loads(str(value))
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _int_or_none(value: Any) -> int | None:
    if type(value) is int:
        return value
    if isinstance(value, str) and value.strip().lstrip("-").isdigit():
        try:
            return int(value)
        except ValueError:
            return None
    return None


def _verify_enrichment_attempt(
    db: sqlite3.Connection,
    *,
    candidate: Mapping[str, Any],
    candidate_revision: int,
    source_evidence_ids: tuple[str, ...],
) -> tuple[str | None, tuple[str, ...]]:
    """Verify the durable enrichment settlement for the current candidate revision."""
    candidate_id = str(candidate.get("candidate_id") or "").strip()
    attempts = _row_dicts(
        db, "learning_candidate_attempt", " WHERE candidate_id = ? AND stage = 'enrichment'",
        (candidate_id,),
    )
    if not attempts:
        return "enrichment_attempt_missing", ()
    current = [
        row for row in attempts
        if (_int_or_none(row.get("revision")) or 0) + 1 == candidate_revision
    ]
    if not current:
        return "enrichment_attempt_revision_mismatch", ()
    completed = [
        row for row in current
        if str(row.get("status") or "").lower() == "completed"
    ]
    if not completed:
        return "enrichment_attempt_not_completed", ()
    if len(completed) != 1:
        return "enrichment_attempt_identity_ambiguous", ()
    attempt = completed[0]
    attempt_id = str(attempt.get("attempt_id") or "").strip()
    work_attempt = _int_or_none(attempt.get("candidate_work_attempt"))
    stage_attempt = _int_or_none(attempt.get("attempt"))
    if not attempt_id or work_attempt is None or work_attempt <= 0:
        return "enrichment_attempt_identity_invalid", ()
    if stage_attempt != work_attempt:
        return "enrichment_attempt_identity_mismatch", ()
    if str(attempt.get("scope_id") or "") != str(candidate.get("scope_id") or ""):
        return "enrichment_attempt_scope_mismatch", ()
    owner = str(attempt.get("owner") or "").strip()
    lease_token = str(attempt.get("lease_token") or "").strip()
    started_at = attempt.get("started_at")
    finished_at = attempt.get("finished_at")
    lease_until = attempt.get("lease_until")
    try:
        started_at = float(started_at)
        finished_at = float(finished_at)
        lease_until = float(lease_until)
    except (TypeError, ValueError):
        return "enrichment_attempt_lease_identity_missing", ()
    if (
        not owner or not lease_token or started_at <= 0
        or finished_at < started_at or lease_until < finished_at
    ):
        return "enrichment_attempt_lease_identity_invalid", ()
    provider_count = _int_or_none(attempt.get("provider_attempt"))
    if (
        _int_or_none(attempt.get("provider_request_started")) != 1
        or provider_count is None
        or provider_count <= 0
        or not str(attempt.get("provider_id") or "").strip()
        or not str(attempt.get("provider_family") or "").strip()
        or not str(attempt.get("model_id") or "").strip()
        or not str(attempt.get("identity_source") or "").strip()
        or not str(attempt.get("gateway_call_id") or "").strip()
        or not str(attempt.get("provider_request_id") or "").strip()
    ):
        return "enrichment_provider_start_fence_missing", ()

    payload = _json_value(attempt.get("settlement_payload_json"), None)
    if not isinstance(payload, Mapping) or str(payload.get("status") or "").lower() != "enriched":
        return "enrichment_attempt_payload_invalid", ()
    enrichment_payload = payload.get("enrichment_payload")
    if not isinstance(enrichment_payload, Mapping) or not enrichment_payload:
        return "enrichment_attempt_payload_incomplete", ()
    canonical_ids = payload.get("canonical_ids")
    stored_canonical_ids = _json_value(attempt.get("canonical_ids_json"), None)
    if (
        not isinstance(canonical_ids, (list, tuple))
        or not canonical_ids
        or any(not str(value).strip() for value in canonical_ids)
        or not isinstance(stored_canonical_ids, (list, tuple))
        or tuple(str(value) for value in canonical_ids) != tuple(str(value) for value in stored_canonical_ids)
    ):
        return "enrichment_attempt_canonical_ids_mismatch", ()
    if (
        str(payload.get("provider_id") or "") != str(attempt.get("provider_id") or "")
        or str(payload.get("model_id") or "") != str(attempt.get("model_id") or "")
    ):
        return "enrichment_provider_identity_mismatch", ()
    payload_started_at = payload.get("settlement_started_at")
    payload_finished_at = payload.get("finished_at")
    try:
        payload_started_at = float(payload_started_at)
        payload_finished_at = float(payload_finished_at)
    except (TypeError, ValueError):
        return "enrichment_attempt_time_identity_missing", ()
    if (
        payload_finished_at != finished_at
        or payload_started_at < started_at
        or payload_started_at > payload_finished_at
    ):
        return "enrichment_attempt_time_identity_mismatch", ()
    generated_rows = _row_dicts(
        db, "learning_candidate_evidence", " WHERE candidate_id = ?",
        (candidate_id,),
    )
    actual_generated_ids = tuple(sorted(
        str(row.get("evidence_id") or "").strip()
        for row in generated_rows
        if row.get("evidence_id") and bool(row.get("is_generated"))
    ))
    generated_ids = payload.get("generated_evidence_ids")
    if not isinstance(generated_ids, (list, tuple)) or tuple(sorted(str(value).strip() for value in generated_ids)) != actual_generated_ids:
        return "enrichment_attempt_evidence_mismatch", ()
    result_digest = str(attempt.get("result_digest") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", result_digest) or canonical_hash(payload) != result_digest:
        return "enrichment_attempt_result_digest_invalid", ()
    diagnostics = _json_value(attempt.get("diagnostics_json"), {})
    if not isinstance(diagnostics, Mapping):
        return "enrichment_attempt_diagnostics_invalid", ()
    expected_persistence_id = "candidate-enrichment:" + canonical_hash({
        "candidate_id": candidate_id,
        "source_evidence_ids": list(source_evidence_ids),
        "version": 1,
    })
    if diagnostics.get("canonical_persistence_id") != expected_persistence_id:
        return "enrichment_attempt_source_evidence_mismatch", ()
    provider_attempt = diagnostics.get("provider_attempt")
    if not isinstance(provider_attempt, Mapping):
        return "enrichment_provider_diagnostics_missing", ()
    provider_diagnostics = provider_attempt.get("diagnostics")
    provider_diagnostics = provider_diagnostics if isinstance(provider_diagnostics, Mapping) else {}
    report_provider_family = str(provider_attempt.get("provider_family") or "").strip()
    report_identity_source = str(provider_attempt.get("identity_source") or "").strip()
    report_request_id = str(provider_attempt.get("request_id") or "").strip()
    report_gateway_call_id = str(provider_diagnostics.get("gateway_call_id") or "").strip()
    report_provider_request_id = str(provider_diagnostics.get("provider_request_id") or "").strip()
    report_diagnostics_identity_source = str(provider_diagnostics.get("identity_source") or "").strip()
    if (
        str(provider_attempt.get("work_attempt_id") or "") != attempt_id
        or _int_or_none(provider_attempt.get("candidate_work_attempt")) != work_attempt
        or _int_or_none(provider_attempt.get("provider_attempt")) != provider_count
        or provider_attempt.get("provider_request_started") is not True
        or str(provider_attempt.get("provider_id") or "") != str(attempt.get("provider_id") or "")
        or str(provider_attempt.get("model_id") or "") != str(attempt.get("model_id") or "")
        or report_provider_family != str(attempt.get("provider_family") or "").strip()
        or report_identity_source != str(attempt.get("identity_source") or "").strip()
        or report_diagnostics_identity_source != str(attempt.get("identity_source") or "").strip()
        or report_request_id != str(attempt.get("provider_request_id") or "").strip()
        or report_provider_request_id != str(attempt.get("provider_request_id") or "").strip()
        or report_gateway_call_id != str(attempt.get("gateway_call_id") or "").strip()
    ):
        return "enrichment_attempt_work_identity_mismatch", ()
    return None, tuple(str(value) for value in canonical_ids)


def _rebuild_workflow_evidence(
    db: sqlite3.Connection,
    memory_db: sqlite3.Connection | None,
    supplied: Any,
) -> tuple[dict[str, Any] | None, tuple[str, ...]]:
    """Rebuild one workflow proof from durable rows in the replay copy.

    The sample manifest is an expectation/selector only. No supplied digest,
    identity, or completion flag is trusted as a fact.
    """
    if not isinstance(supplied, Mapping):
        return None, ("evidence_not_mapping",)
    candidate_id = str(supplied.get("candidate_id") or "").strip()
    candidate_revision = supplied.get("candidate_revision")
    if not candidate_id or type(candidate_revision) is not int or candidate_revision <= 0:
        return None, ("candidate_identity_unavailable",)
    candidates = _row_dicts(db, "learning_candidate", " WHERE candidate_id = ?", (candidate_id,))
    if len(candidates) != 1 or candidates[0].get("revision") != candidate_revision:
        return None, ("candidate_durable_row_missing_or_revision_mismatch",)
    candidate = candidates[0]

    evidence_rows = _row_dicts(db, "learning_candidate_evidence", " WHERE candidate_id = ?", (candidate_id,))
    actual_source_ids = tuple(sorted(
        str(row.get("evidence_id") or "").strip()
        for row in evidence_rows
        if row.get("evidence_id") and not bool(row.get("is_generated")) and bool(row.get("eligible", 1))
    ))
    expected_source_ids = supplied.get("source_evidence_ids")
    if not isinstance(expected_source_ids, (list, tuple)) or not expected_source_ids:
        return None, ("source_evidence_ids_unavailable",)
    expected_source_ids = tuple(sorted(str(value).strip() for value in expected_source_ids if str(value).strip()))
    if not expected_source_ids or expected_source_ids != actual_source_ids:
        return None, ("source_evidence_durable_mismatch",)
    if any("fallback" in value.lower() or "synthetic" in value.lower() for value in actual_source_ids):
        return None, ("source_evidence_identity_unavailable",)
    enrichment_failure, enrichment_canonical_ids = _verify_enrichment_attempt(
        db,
        candidate=candidate,
        candidate_revision=candidate_revision,
        source_evidence_ids=actual_source_ids,
    )
    if enrichment_failure:
        return None, (enrichment_failure,)

    decisions = _row_dicts(db, "learning_review_decision", " WHERE candidate_id = ? AND candidate_revision = ?", (candidate_id, candidate_revision))
    requested_review_id = str(supplied.get("review_id") or "").strip()
    matching = [row for row in decisions if not requested_review_id or str(row.get("decision_id") or "") == requested_review_id]
    matching = [row for row in matching if str(row.get("decision") or "").lower() == "approved"
                and str(row.get("reviewer_kind") or "").lower() == "rule"
                and str(row.get("pair_order") or "") == "quorum"
                and bool(row.get("order_invariant"))
                and str(row.get("candidate_id") or "") == candidate_id
                and _int_or_none(row.get("candidate_revision")) == candidate_revision]
    if len(matching) != 1:
        return None, ("review_durable_quorum_missing",)
    review = matching[0]
    review_sources = tuple(sorted(str(value) for value in (_json_value(review.get("source_evidence_ids_json"), []) or [])))
    if review_sources != actual_source_ids:
        return None, ("review_source_evidence_mismatch",)
    source_decision_ids = tuple(sorted(str(value) for value in (_json_value(review.get("source_decision_ids_json"), []) or [])))
    if len(source_decision_ids) < 2 or len(set(source_decision_ids)) != len(source_decision_ids):
        return None, ("review_quorum_final_row_invalid",)
    if str(review.get("decision_id") or "") in source_decision_ids:
        return None, ("review_quorum_source_decision_invalid",)
    source_rows = [row for row in decisions if str(row.get("decision_id") or "") in source_decision_ids]
    if len(source_rows) != len(source_decision_ids):
        return None, ("review_quorum_source_decisions_missing",)
    by_reviewer: dict[str, list[dict[str, Any]]] = {}
    for row in source_rows:
        if (
            str(row.get("candidate_id") or "") != candidate_id
            or _int_or_none(row.get("candidate_revision")) != candidate_revision
            or str(row.get("reviewer_kind") or "").lower() not in {"model", "human"}
            or str(row.get("pair_order") or "") not in {"ab", "ba"}
            or str(row.get("decision") or "").lower() not in {"approved", "rejected"}
        ):
            return None, ("review_quorum_source_decision_invalid",)
        evidence_ids = tuple(sorted(str(value) for value in (_json_value(row.get("source_evidence_ids_json"), []) or [])))
        if evidence_ids != actual_source_ids:
            return None, ("review_quorum_source_evidence_mismatch",)
        by_reviewer.setdefault(str(row.get("reviewer_id") or "").strip(), []).append(row)
    if "" in by_reviewer or len(by_reviewer) < 2:
        return None, ("review_quorum_reviewer_identity_missing",)
    diagnostics = _json_value(review.get("diagnostics_json"), {})
    configured_reviewers = diagnostics.get("reviewer_ids") if isinstance(diagnostics, Mapping) else None
    if configured_reviewers is not None:
        configured = {str(value).strip() for value in configured_reviewers if str(value).strip()}
        if configured != set(by_reviewer):
            return None, ("review_quorum_reviewer_set_mismatch",)
    if any(len(rows) != 2 or {str(row.get("pair_order") or "") for row in rows} != {"ab", "ba"} for rows in by_reviewer.values()):
        return None, ("review_quorum_pair_order_incomplete",)
    reviewer_kinds = {str(row.get("reviewer_kind") or "").lower() for row in source_rows}
    if len(reviewer_kinds) != 1:
        return None, ("review_quorum_reviewer_kind_conflict",)
    kind = next(iter(reviewer_kinds))
    model_identities: dict[str, str] = {}
    for reviewer_id, rows in by_reviewer.items():
        identities = {str(row.get("model_identity") or "") for row in rows}
        if kind == "model":
            if len(identities) != 1 or not next(iter(identities), ""):
                return None, ("review_quorum_model_identity_conflict",)
            model_identities[reviewer_id] = next(iter(identities))
        elif any(identities):
            return None, ("review_quorum_reviewer_identity_conflict",)
    if kind == "model" and len(set(model_identities.values())) != len(model_identities):
        return None, ("review_quorum_model_identity_conflict",)
    signatures = set()
    for row in source_rows:
        prompt = str(row.get("prompt_version") or "")
        prompt = prompt.rsplit(":", 1)[0] if prompt.endswith(":ab") or prompt.endswith(":ba") else prompt
        signatures.add((
            _int_or_none(row.get("candidate_revision")),
            str(row.get("reviewer_kind") or "").lower(),
            str(row.get("rubric_version") or ""), prompt,
            tuple(sorted(str(value) for value in (_json_value(row.get("source_evidence_ids_json"), []) or []))),
        ))
    if len(signatures) != 1:
        return None, ("review_quorum_input_mismatch",)
    reviewer_votes = {reviewer_id: {str(row.get("decision") or "").lower() for row in rows} for reviewer_id, rows in by_reviewer.items()}
    if any(len(votes) != 1 for votes in reviewer_votes.values()) or len({next(iter(votes)) for votes in reviewer_votes.values()}) != 1:
        return None, ("review_quorum_vote_disagreement",)
    if next(iter(next(iter(reviewer_votes.values())))) != str(review.get("decision") or "").lower():
        return None, ("review_quorum_vote_mismatch",)
    review_identity = f"rule:{review.get('reviewer_id') or 'review-quorum'}"
    review_revision = supplied.get("review_revision")
    if type(review_revision) is not int or review_revision <= 0:
        review_revision = int(review.get("expected_revision") or review.get("candidate_revision") or 0)
    if review_revision != candidate_revision:
        return None, ("review_revision_mismatch",)

    admissions = _row_dicts(db, "learning_admission", " WHERE candidate_id = ? AND candidate_revision = ?", (candidate_id, candidate_revision))
    if len(admissions) != 1:
        return None, ("admission_durable_row_missing",)
    admission = admissions[0]
    admission_revision = _int_or_none(admission.get("admission_revision")) or 0
    decision_ids = tuple(sorted(str(value) for value in (_json_value(admission.get("review_decision_ids_json"), []) or [])))
    if str(review.get("decision_id")) not in decision_ids or not bool(admission.get("pre_index_eligible")) or not bool(admission.get("post_publish_eligible")) or bool(admission.get("index_blocked")):
        return None, ("admission_not_durablely_eligible",)
    publish_digest = str(admission.get("publish_proof_digest") or "").lower()
    if not re.fullmatch(r"[0-9a-f]{64}", publish_digest):
        return None, ("publish_proof_durable_digest_missing",)
    if str(supplied.get("publish_proof_digest") or "").lower() != publish_digest:
        return None, ("publish_proof_digest_mismatch",)

    human_admissions = _row_dicts(
        db, "learning_human_admission",
        " WHERE candidate_id = ? AND candidate_revision = ? AND admission_revision = ?",
        (candidate_id, candidate_revision, admission_revision),
    )
    human_admissions = [
        row for row in human_admissions
        if str(row.get("decision") or "").lower() == "approved"
        and str(row.get("reviewer_identity") or "").lower().startswith("human:")
        and str(row.get("provenance_digest") or "") == str(admission.get("provenance_digest") or "")
        and str(row.get("publish_proof_digest") or "").lower() == publish_digest
    ]
    if len(human_admissions) != 1:
        return None, ("admission_reviewer_identity_unavailable",)
    human_admission = human_admissions[0]
    admission_identity = str(human_admission.get("reviewer_identity") or "").strip()

    if memory_db is None:
        return None, ("memory_v2_snapshot_required",)
    assets = _row_dicts(memory_db, "learning_asset_version", " WHERE asset_id = ?", (str(supplied.get("asset_id") or ""),))
    assets = [row for row in assets if str(row.get("candidate_id") or "") == candidate_id and _int_or_none(row.get("candidate_revision")) == candidate_revision]
    if len(assets) != 1:
        return None, ("asset_durable_row_missing",)
    asset = assets[0]
    asset_id = str(asset.get("asset_id") or "")
    asset_revision = _int_or_none(asset.get("asset_revision")) or 0
    canonical_id = str(asset.get("canonical_memory_id") or "")
    if canonical_id not in enrichment_canonical_ids:
        return None, ("enrichment_canonical_identity_mismatch",)
    generation = _int_or_none(admission.get("index_generation")) or _int_or_none(supplied.get("generation")) or 0
    if (
        str(admission.get("mapping_digest") or "") == ""
        or str(admission.get("index_hash") or "") == ""
        or str(admission.get("provenance_digest") or "") != str(asset.get("provenance_hash") or "")
    ):
        return None, ("publish_provenance_identity_mismatch",)
    if generation <= 0 or asset.get("lifecycle_status") != "active":
        return None, ("asset_generation_not_current",)
    meta_rows = _row_dicts(memory_db, "memory_v2_meta", " WHERE key IN ('learning_current_generation', 'learning_pending_generation')")
    meta = {str(row.get("key")): str(row.get("value") or "") for row in meta_rows}
    if _int_or_none(meta.get("learning_current_generation")) != generation:
        return None, ("current_generation_mismatch",)
    if meta.get("learning_pending_generation", "") not in {"", "0"}:
        return None, ("pending_generation_unsettled",)
    memberships = _row_dicts(memory_db, "learning_index_membership", " WHERE asset_id = ? AND asset_revision = ? AND generation = ?", (asset_id, asset_revision, generation))
    memberships = [row for row in memberships if row.get("membership_status") == "current" and str(supplied.get("index_membership_id") or "") in {str(row.get("vector_id") or ""), str(row.get("resource_id") or "")}]
    if len(memberships) != 1:
        return None, ("index_membership_durable_row_missing",)
    membership = memberships[0]
    if (
        str(admission.get("mapping_digest") or "") != str(membership.get("mapping_hash") or "")
        or str(admission.get("index_hash") or "") != str(membership.get("index_hash") or "")
        or str(admission.get("index_generation") or "") != str(generation)
    ):
        return None, ("publish_index_identity_mismatch",)
    retrieval_id = str(supplied.get("retrieval_event_id") or "").strip()
    events = _row_dicts(db, "learning_retrieval_event", " WHERE event_id = ?", (retrieval_id,))
    if len(events) != 1:
        return None, ("retrieval_event_durable_row_missing",)
    event = events[0]
    if (
        _int_or_none(event.get("generation")) != generation
        or _int_or_none(event.get("candidate_revision")) != candidate_revision
        or _int_or_none(event.get("review_revision")) != review_revision
        or _int_or_none(event.get("admission_revision")) != admission_revision
        or str(event.get("event_status") or "") != "observed"
        or str(event.get("stage") or "") not in {"selected", "accepted_for_prompt", "prompt_visible", "reply_outcome"}
    ):
        return None, ("retrieval_revision_identity_mismatch",)
    provenance = _json_value(event.get("asset_provenance_json"), None)
    if not isinstance(provenance, list) or not provenance:
        return None, ("retrieval_provenance_durable_missing",)
    matching_provenance = [item for item in provenance if isinstance(item, Mapping) and str(item.get("asset_id") or "") == asset_id]
    if len(matching_provenance) != 1:
        return None, ("retrieval_asset_provenance_mismatch",)
    asset_provenance = matching_provenance[0]
    if (
        _int_or_none(asset_provenance.get("asset_revision")) != asset_revision
        or _int_or_none(asset_provenance.get("generation")) != generation
        or _int_or_none(asset_provenance.get("candidate_revision")) != candidate_revision
        or _int_or_none(asset_provenance.get("admission_revision")) != admission_revision
        or str(asset_provenance.get("canonical_memory_id") or "") != canonical_id
        or str(asset_provenance.get("provenance_hash") or "") != str(asset.get("provenance_hash") or "")
        or tuple(sorted(str(value) for value in (asset_provenance.get("source_evidence_ids") or ()))) != actual_source_ids
    ):
        return None, ("retrieval_asset_provenance_identity_mismatch",)
    event_asset_revisions = _json_value(event.get("asset_revision_ids_json"), [])
    if f"{asset_id}:{asset_revision}" not in {str(value) for value in (event_asset_revisions or ())}:
        return None, ("retrieval_asset_revision_missing",)
    event_stage = str(event.get("stage") or "")
    required_id_fields = ["selected_ids_json"]
    if event_stage in {"accepted_for_prompt", "prompt_visible", "reply_outcome"}:
        required_id_fields.append("accepted_ids_json")
    if event_stage in {"prompt_visible", "reply_outcome"}:
        required_id_fields.append("visible_ids_json")
    for field_name in required_id_fields:
        ids = {str(value) for value in (_json_value(event.get(field_name), []) or ())}
        if not ids or str(asset_id) not in ids:
            return None, ("retrieval_visibility_incomplete" if field_name != "selected_ids_json" else "retrieval_selected_asset_missing")
    provenance_digest = canonical_hash(provenance)
    if str(supplied.get("retrieval_provenance_digest") or "").lower() != provenance_digest:
        return None, ("retrieval_provenance_digest_mismatch",)
    actual = dict(supplied)
    actual.update({
        "candidate_id": candidate_id, "candidate_revision": candidate_revision,
        "source_evidence_ids": list(actual_source_ids), "review_id": str(review.get("decision_id")),
        "review_revision": review_revision, "review_candidate_revision": _int_or_none(review.get("candidate_revision")) or 0,
        "review_status": "completed", "reviewer_identity": review_identity,
        "human_admission_id": f"{candidate_id}:{candidate_revision}",
        "admission_revision": admission_revision, "admission_candidate_revision": candidate_revision,
        "human_admission_decision": "approved", "admission_reviewer_identity": admission_identity,
        "publish_proof_digest": publish_digest, "asset_id": asset_id, "asset_revision": asset_revision,
        "canonical_memory_id": canonical_id, "generation": generation,
        "retrieval_event_id": retrieval_id, "retrieval_generation": generation,
        "retrieval_provenance_digest": provenance_digest, "retrieval_provenance": provenance,
    })
    return actual, ()


def _metrics(created_at: str) -> tuple[MetricResult, ...]:
    semantic_metrics = {
        "candidate_precision",
        "candidate_recall",
        "speaker_attribution_accuracy",
        "scope_accuracy",
        "trace_join_rate",
        "prompt_visible_rate",
        "reply_outcome_known_rate",
        "dialog_reply_loss_rate",
    }
    return tuple(
        metric_from_counts(
            name, 0, 0, minimum_denominator=minimum,
            source=("ai_exploratory_or_no_human_gold" if name in semantic_metrics else "durable_persistence_facts"),
            window_start=created_at, window_end=created_at,
            warnings=("learning_kpi_aggregation_unavailable",),
            unavailable_reason=(
                "independent_human_gold_required"
                if name in semantic_metrics else "no_admissible_denominator"
            ),
        )
        for name, minimum in _METRIC_MINIMUMS.items()
    )


def _run_learning_pipeline_on_copy(
    working: Path,
    *,
    recordings: Iterable[Mapping[str, Any]] = (),
    recorded: bool = False,
    provider_id: str = "none",
    model_id: str = "none",
    prompt_version: str = "none",
    schema_version: int = 0,
) -> tuple[bool, int, list[dict[str, Any]]]:
    """Execute the approved EvolutionManager entry points on the writable copy.

    Deterministic replay uses a gateway that raises on provider access. Recorded
    replay uses the same adapter boundary but resolves responses from a validated
    fixture, so no network-capable gateway is ever constructed.
    """
    provider_calls = 0
    try:
        from types import SimpleNamespace
        from sqlmodel import SQLModel, Session, create_engine
        from astrmai.infrastructure.persistence.database_service import DatabaseService
        from astrmai.infrastructure.runtime.runtime_contracts import LLMCallDiagnostics, LLMCallResult, LLMProviderSelection
        from astrmai.learning.evolution_manager import EvolutionManager
        from config import load_astrmai_config

        db = sqlite3.connect(working)
        try:
            tables = {str(row[0]) for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            db.close()
        if "messagelog" not in tables:
            return False, 0, [{"reason": "pipeline_input_schema_unavailable"}]
        engine = create_engine(f"sqlite:///{working}")
        SQLModel.metadata.create_all(engine)
        persistence = SimpleNamespace(
            db_path=working,
            engine=engine,
            orm_models=None,
            get_session=lambda: Session(engine),
            bind_database_service=lambda _service: None,
        )
        service = DatabaseService(persistence)
        config = load_astrmai_config({})
        evolution = config.evolution.model_copy(update={
            "learning_candidate_ledger_enabled": True,
            "learning_discovery_cursor_v2_enabled": True,
            "learning_enrichment_enabled": bool(recorded),
            "learning_enrichment_worker_enabled": bool(recorded),
        })
        config = config.model_copy(update={"evolution": evolution})
        recording_map = {
            _recording_key(row): row
            for row in recordings
            if isinstance(row, Mapping)
        }

        class _ReplayBudget:
            async def run(self, awaitable_factory, **kwargs):
                from types import SimpleNamespace
                started = time.monotonic()
                try:
                    value = await awaitable_factory()
                except Exception as exc:
                    return SimpleNamespace(
                        status="failed",
                        value=None,
                        queue_wait_ms=0.0,
                        execution_ms=(time.monotonic() - started) * 1000.0,
                        failure_kind=type(exc).__name__,
                        error=exc,
                    )
                return SimpleNamespace(
                    status="completed",
                    value=value,
                    queue_wait_ms=0.0,
                    execution_ms=(time.monotonic() - started) * 1000.0,
                    failure_kind="",
                    error=None,
                )

        class _OfflineGateway:
            def __init__(self):
                self.config = config
                self._learning_legacy_provider_test_double = False
                first = next(iter(recording_map.values()), {})
                self.provider_id = str(provider_id or first.get("provider_id") or ("recorded-provider" if recorded else "deterministic-provider"))
                self.model_id = str(model_id or first.get("model_id") or ("recorded-model" if recorded else "deterministic-model"))
                self.prompt_version = str(prompt_version or first.get("prompt_version") or "none")
                self.schema_version = str(schema_version or first.get("schema_version") or "158")

            def select_data_process_provider(self, **kwargs):
                return LLMProviderSelection(
                    provider_id=self.provider_id,
                    provider_family="recorded" if recorded else "deterministic",
                    model_id=self.model_id,
                    identity_source="evaluation-fixture",
                    pool_name="learning",
                )

            def _request_key(self, prompt: str, system_prompt: str, is_json: bool, model_id: str) -> tuple[str, str, str, str, str]:
                normalized_request = canonical_json({
                    "prompt": str(prompt or ""),
                    "system_prompt": str(system_prompt or ""),
                    "is_json": bool(is_json),
                })
                fingerprint = recorded_request_fingerprint(
                    self.provider_id, model_id, normalized_request, self.prompt_version
                )
                return (fingerprint, self.provider_id, model_id, self.prompt_version, self.schema_version)

            async def call_data_process_task(self, *args, **kwargs):
                nonlocal provider_calls
                provider_calls += 1
                raise RuntimeError("deterministic_provider_call_blocked")

            async def call_data_process_task_result(self, *args, **kwargs):
                nonlocal provider_calls
                provider_calls += 1
                if not recorded:
                    raise RuntimeError("deterministic_provider_call_blocked")
                prompt = str(kwargs.get("prompt") or "")
                system_prompt = str(kwargs.get("system_prompt") or "")
                is_json = bool(kwargs.get("is_json", False))
                model_id = str(kwargs.get("selected_model_id") or self.model_id)
                row = recording_map.get(self._request_key(prompt, system_prompt, is_json, model_id))
                if row is None:
                    raise RuntimeError("recording_request_fingerprint_unmatched")
                if str(row.get("outcome") or "success").lower() != "success" or not row.get("response_payload"):
                    raise RuntimeError("recorded_provider_outcome_not_success")
                payload = row.get("response_payload")
                diagnostics = LLMCallDiagnostics(
                    provider_id=self.provider_id,
                    provider_family="recorded",
                    model_id=model_id,
                    identity_source="recorded_fixture",
                    provider_request_started=True,
                )
                return LLMCallResult(
                    ok=True,
                    text=payload if isinstance(payload, str) else canonical_json(payload),
                    parsed_json=payload if is_json else None,
                    model_id=model_id,
                    provider_family="recorded",
                    raw_completion=payload if isinstance(payload, str) else canonical_json(payload),
                    call_diagnostics=diagnostics,
                )

        manager = EvolutionManager(
            service,
            _OfflineGateway(),
            config=config,
            background_task_budget=_ReplayBudget(),
        )
        db = sqlite3.connect(working)
        try:
            groups = [str(row[0]) for row in db.execute(
                "SELECT DISTINCT group_id FROM messagelog WHERE group_id IS NOT NULL ORDER BY group_id"
            ).fetchall()]
        finally:
            db.close()
        if not groups:
            return False, provider_calls, [{"reason": "no_learning_groups"}]

        pipeline_invoked = False

        async def _run() -> list[dict[str, Any]]:
            nonlocal pipeline_invoked
            reports: list[dict[str, Any]] = []
            for group_id in groups:
                for pipeline in ("expression", "jargon"):
                    logs = list(service.get_learning_logs(pipeline, group_id, limit=300) or [])
                    if not logs:
                        continue
                    pipeline_invoked = True
                    report = await manager._run_learning_pipeline(
                        pipeline, group_id, logs, run_id=f"evaluation:{pipeline}:{group_id}"
                    )
                    reports.append({"pipeline": pipeline, "group_id": group_id, "status": str(report.get("status", ""))})
            if recorded and pipeline_invoked and manager.enrichment_worker is not None:
                worker_report = await manager.enrichment_worker.run_due_once(limit=300)
                reports.append({
                    "stage": "enrichment_worker",
                    **{
                        name: int(getattr(worker_report, name))
                        for name in (
                            "scanned", "claimed", "enriched", "rejected", "retry_wait",
                            "quarantined", "blocked", "conflicts",
                        )
                    },
                })
            return reports

        reports = asyncio.run(_run())
        if not pipeline_invoked:
            return False, provider_calls, [{"reason": "no_work_available"}]
        return True, provider_calls, reports
    except Exception as exc:
        return False, provider_calls, [{"reason": "pipeline_invocation_unavailable", "error_kind": type(exc).__name__}]
    finally:
        try:
            engine.dispose()
        except (UnboundLocalError, AttributeError):
            pass


def _request_fingerprints(value: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(value, Mapping):
        fingerprint = value.get("request_fingerprint")
        if isinstance(fingerprint, str) and len(fingerprint) == 64:
            found.add(fingerprint)
        for child in value.values():
            found.update(_request_fingerprints(child))
    elif isinstance(value, list):
        for child in value:
            found.update(_request_fingerprints(child))
    return found


def _hashable_integrity(value: Mapping[str, Any]) -> dict[str, Any]:
    """Exclude filesystem clock noise while retaining integrity evidence."""
    return {key: item for key, item in value.items() if key not in {"mtime_ns"}}


def _blocked_result(
    manifest: ReplayManifest,
    reason: str,
    *,
    before: Mapping[str, Any] | None = None,
    memory_before: Mapping[str, Any] | None = None,
) -> ReplayResult:
    return ReplayResult(
        run_id=manifest.run_id,
        status="blocked",
        provider_calls=0,
        source_integrity_before=dict(before or {}),
        source_integrity_after=dict(before or {}),
        funnel={
            "replay_status": "blocked",
            "full_learning_pipeline_completed": False,
            "enrichment_complete": False,
            "review_complete": False,
            "admission_complete": False,
            "publish_complete": False,
            "retrieval_complete": False,
        },
        memory_v2_integrity_before=dict(memory_before or {}),
        memory_v2_integrity_after=dict(memory_before or {}),
        blocked_reasons=(reason,),
    )


def run_replay(manifest: ReplayManifest) -> ReplayResult:
    """Run a non-networked replay and persist auditable artifacts."""
    if manifest.mode == "staging":
        return _blocked_result(manifest, "staging_disabled_in_stage_09")
    if manifest.mode == "recorded" and (
        manifest.provider_id == "none" or manifest.model_id == "none"
    ):
        raise ReplayInputError("recorded replay requires provider and model identity")
    if manifest.mode == "recorded" and not manifest.request_fixture_hash:
        raise ReplayInputError("recorded replay requires request fixture hash")
    source_text = manifest.source_snapshot_path
    if not source_text:
        return _blocked_result(manifest, "source_snapshot_path_required")
    source_input = Path(source_text).expanduser()
    if source_input.is_symlink():
        raise ReplaySecurityError("source snapshot may not be a symlink")
    source = source_input.resolve()
    memory_source: Path | None = None
    memory_before: dict[str, Any] | None = None
    if manifest.memory_v2_snapshot_path:
        memory_input = Path(manifest.memory_v2_snapshot_path).expanduser()
        if memory_input.is_symlink():
            raise ReplaySecurityError("memory v2 snapshot may not be a symlink")
        memory_source = memory_input.resolve()
        if memory_source == source:
            raise ReplaySecurityError("memory v2 snapshot must be independent from source snapshot")
        if _same_inode(memory_source, source):
            raise ReplaySecurityError("memory v2 snapshot must not be a hardlink to source snapshot")
        if _is_within(memory_source.parent, source.parent) or _is_within(source.parent, memory_source.parent):
            raise ReplaySecurityError("source and memory v2 snapshot directories must be independent")
    output = Path(manifest.output_root).expanduser()
    before: dict[str, Any] | None = None
    try:
        if manifest.network_policy != "loopback-only":
            raise ReplaySecurityError("non-loopback network policy is disabled")
        _validate_output_root(source, output)
        if memory_source is not None:
            _validate_output_root(memory_source, output)
        before = snapshot_integrity(source)
        if not before.get("valid"):
            raise ReplaySecurityError("source integrity check failed")
        if before.get("sha256") != manifest.source_snapshot_hash:
            raise ReplaySecurityError("source snapshot hash mismatch")
        if manifest.source_schema_version != int(before.get("user_version", -1)):
            raise ReplayInputError("source schema version mismatch")
        if memory_source is not None:
            memory_before = snapshot_integrity(memory_source)
            if not memory_before.get("valid"):
                return _blocked_result(
                    manifest, "memory_v2_snapshot_integrity_failure",
                    before=before, memory_before=memory_before,
                )
            if not manifest.memory_v2_snapshot_hash or memory_before.get("sha256") != manifest.memory_v2_snapshot_hash:
                raise ReplayInputError("memory v2 snapshot hash mismatch")
            expected_memory_schema = manifest.memory_v2_schema_version
            if expected_memory_schema is None:
                expected_memory_schema = _MEMORY_V2_SCHEMA_VERSION
            if memory_before.get("memory_v2_schema_version") != expected_memory_schema:
                return _blocked_result(
                    manifest, "memory_v2_schema_mismatch",
                    before=before, memory_before=memory_before,
                )
        sample_payload: Any = None
        if manifest.sample_manifest_path:
            sample_path = Path(manifest.sample_manifest_path).expanduser().resolve()
            if not sample_path.is_file() or sample_manifest_hash(sample_path) != manifest.sample_manifest_hash:
                raise ReplayInputError("sample manifest hash mismatch")
            sample_payload = _load_json(sample_path)
        recordings: list[dict[str, Any]] = []
        recording_conflicts: list[str] = []
        if manifest.mode == "recorded":
            if not manifest.recordings_path:
                raise ReplayInputError("recordings_path is required for recorded mode")
            recording_path = Path(manifest.recordings_path).expanduser().resolve()
            if manifest.request_fixture_hash and sha256_file(recording_path) != manifest.request_fixture_hash:
                raise ReplayInputError("recording fixture hash mismatch")
            recordings, recording_conflicts = _validate_recordings(recording_path, manifest)
            recorded_fingerprints = {_recording_key(row)[0] for row in recordings}
            requested_fingerprints = _request_fingerprints(sample_payload)
            recording_conflicts.extend(
                f"recording_unmatched:{fingerprint}"
                for fingerprint in sorted(requested_fingerprints - recorded_fingerprints)
            )
        output.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="astrmai-eval-", dir=str(output.parent), ignore_cleanup_errors=True
        ) as temp_dir:
            working = Path(temp_dir) / source.name
            _copy_snapshot_bundle(source, working, before, label="source")
            memory_working: Path | None = None
            if memory_source is not None:
                memory_working = Path(temp_dir) / memory_source.name
                try:
                    _copy_snapshot_bundle(memory_source, memory_working, memory_before or {}, label="memory v2")
                except ReplaySecurityError:
                    return _blocked_result(
                        manifest, "memory_v2_copy_failed",
                        before=before, memory_before=memory_before,
                    )
            memory_copy_integrity: dict[str, Any] | None = None
            if memory_working is not None:
                memory_copy_integrity = snapshot_integrity(memory_working)
                if not memory_copy_integrity.get("valid") or memory_copy_integrity.get("foreign_key_violations"):
                    return _blocked_result(
                        manifest, "memory_v2_copy_integrity_failure",
                        before=before, memory_before=memory_before,
                    )
            migration_failure = False
            if manifest.migration_path:
                from astrmai.infrastructure.persistence.persistence_schema import _run_migrations
                try:
                    with sqlite3.connect(working) as writable:
                        _run_migrations(writable)
                        writable.commit()
                except (OSError, sqlite3.Error):
                    migration_failure = True
            with _readonly_sqlite(working) as migrated:
                if int(migrated.execute("PRAGMA user_version").fetchone()[0]) != manifest.execution_schema_version:
                    raise ReplayInputError("execution schema version mismatch")
            with _readonly_sqlite(working) as db:
                catalog = _query_catalog(db)
            created_at = datetime.fromtimestamp(
                int(before["mtime_ns"]) / 1_000_000_000,
                tz=timezone.utc,
            ).replace(microsecond=0).isoformat().replace("+00:00", "Z")
            pipeline_executed, provider_calls, pipeline_reports = _run_learning_pipeline_on_copy(
                working,
                recordings=recordings,
                recorded=manifest.mode == "recorded",
                provider_id=manifest.provider_id,
                model_id=manifest.model_id,
                prompt_version=manifest.prompt_version,
                schema_version=manifest.execution_schema_version,
            )
            with _readonly_sqlite(working) as db:
                durable_catalog = _query_catalog(db)
            unknowns: list[dict[str, Any]] = [] if pipeline_executed else list(pipeline_reports)
            blocked: list[dict[str, Any]] = [{"reason": item} for item in recording_conflicts]
            if manifest.mode == "recorded" and not recordings:
                blocked.append({"reason": "recordings_unavailable"})
            if provider_calls and manifest.mode != "recorded":
                blocked.append({"reason": "provider_call_out_of_scope"})
            pipeline_failure = any(
                str(item.get("reason") or "").startswith("pipeline_invocation_")
                or str(item.get("status") or "").lower() in {"failed", "blocked"}
                for item in pipeline_reports if isinstance(item, Mapping)
            )
            if pipeline_failure:
                blocked.append({"reason": "pipeline_invocation_failed"})
            if migration_failure:
                blocked.append({"reason": "migration_failed"})
            status = "blocked" if blocked else ("partial" if pipeline_executed else "unavailable")
            reasons = tuple(item["reason"] for item in blocked)
            metrics = _metrics(created_at)
            no_work_available = any(
                str(item.get("reason") or "") == "no_work_available"
                for item in pipeline_reports
            )
            pipeline_invoked = any(
                isinstance(item, Mapping) and "pipeline" in item
                for item in pipeline_reports
            )
            pipeline_completed = pipeline_invoked and all(
                str(item.get("status") or "") in {"completed", "waiting", "partial"}
                for item in pipeline_reports
                if isinstance(item, Mapping) and "pipeline" in item
            )
            pipeline_runtime_blocked = pipeline_failure
            supplied_workflow = sample_payload.get("workflow_evidence", ()) if isinstance(sample_payload, Mapping) else ()
            if isinstance(supplied_workflow, Mapping):
                supplied_workflow = (supplied_workflow,)
            supplied_workflow = tuple(supplied_workflow) if isinstance(supplied_workflow, (list, tuple)) else (supplied_workflow,)
            if not blocked and supplied_workflow and not pipeline_executed:
                status = "partial"
            with _readonly_sqlite(working) as db:
                rebuilt_workflow: list[dict[str, Any]] = []
                rebuild_failures: list[str] = []
                if supplied_workflow and memory_working is None:
                    rebuild_failures.append("memory_v2_snapshot_required")
                elif supplied_workflow:
                    with _readonly_sqlite(memory_working) as memory_db:
                        for item in supplied_workflow:
                            rebuilt, failures = _rebuild_workflow_evidence(db, memory_db, item)
                            if rebuilt is not None:
                                rebuilt_workflow.append(rebuilt)
                            rebuild_failures.extend(failures)
            workflow_results = tuple(
                evaluate_workflow_evidence(item) for item in rebuilt_workflow
            )
            if rebuild_failures:
                blocked.extend({"reason": reason} for reason in sorted(set(rebuild_failures)))
            after = snapshot_integrity(source)
            memory_after = snapshot_integrity(memory_source) if memory_source is not None else None
            integrity_failure = not after.get("valid") or (
                memory_after is not None and not memory_after.get("valid")
            )
            if after != before:
                blocked.append({"reason": "source_changed_during_replay"})
            if memory_before is not None and memory_after != memory_before:
                blocked.append({"reason": "memory_v2_changed_during_replay"})
            if integrity_failure:
                blocked.append({"reason": "snapshot_integrity_failure"})
            reasons = tuple(dict.fromkeys(str(item["reason"]) for item in blocked))
            status = "blocked" if blocked else (
                "partial" if pipeline_executed or supplied_workflow else "unavailable"
            )
            workflow_rebuild_failure = bool(rebuild_failures)
            enrichment_worker_completed = bool(supplied_workflow) and not any(
                reason.startswith("enrichment_attempt_")
                for reason in rebuild_failures
            )
            full_learning_pipeline_completed = bool(workflow_results) and all(
                item.complete for item in workflow_results
            ) and not workflow_rebuild_failure and len(rebuilt_workflow) == len(supplied_workflow) \
                and enrichment_worker_completed \
                and not pipeline_runtime_blocked \
                and not recording_conflicts and not migration_failure \
                and not integrity_failure and status != "blocked"
            if full_learning_pipeline_completed:
                status = "completed"
                reasons = ()
            readiness_workflow = tuple(rebuilt_workflow)
            if rebuild_failures:
                readiness_workflow = readiness_workflow + ({"_durable_rebuild_failure": True},)
            readiness = evaluate_stage10_readiness(
                discovery_safety={
                    "deterministic_replay": "passed" if manifest.mode == "deterministic" else "partial",
                    "provider_calls": provider_calls,
                    "network_calls": 0,
                    "replay_status": status,
                    "replay_blocked_reasons": tuple(reasons),
                    "recording_conflicts": tuple(recording_conflicts),
                    "pipeline_failure": pipeline_failure,
                    "snapshot_integrity_failure": integrity_failure,
                    "migration_failure": migration_failure,
                    "workflow_rebuild_failure": workflow_rebuild_failure,
                },
                workflow_evidence=readiness_workflow,
                semantic_metrics=metrics,
                human_gold_status=(
                    str(sample_payload.get("human_gold_status") or "pending")
                    if isinstance(sample_payload, Mapping) else "pending"
                ),
            )
            funnel = {
                "replay_status": status,
                "recording_conflicts": list(recording_conflicts),
                "pipeline_failure": pipeline_failure,
                "snapshot_integrity_failure": integrity_failure,
                "migration_failure": migration_failure,
                "workflow_rebuild_failure": workflow_rebuild_failure,
                "pipeline_invoked": pipeline_invoked,
                # Compatibility field: this has always meant discovery entry
                # completion, not enrichment/review/admission completion.
                "pipeline_completed": pipeline_completed,
                "discovery_pipeline_completed": pipeline_completed,
                "full_learning_pipeline_completed": full_learning_pipeline_completed,
                "enrichment_complete": full_learning_pipeline_completed,
                "review_complete": full_learning_pipeline_completed,
                "admission_complete": full_learning_pipeline_completed,
                "publish_complete": full_learning_pipeline_completed,
                "retrieval_complete": full_learning_pipeline_completed,
                "workflow_evidence_status": "complete" if full_learning_pipeline_completed else "incomplete",
                "stage10_readiness": readiness.to_dict(),
                "no_work_available": no_work_available,
                "pipeline_executed": bool(pipeline_executed),
                "provider_calls": provider_calls,
                "recording_count": len(recordings),
                "pipeline_reports": pipeline_reports,
                "tables": durable_catalog,
            }
            artifacts: dict[str, str] = {}
            for name in _REQUIRED_ARTIFACTS:
                artifacts[name] = str(output / name)
            hashable_metrics = [
                {
                    **item.to_dict(),
                    "window_start": "<replay-window>",
                    "window_end": "<replay-window>",
                }
                for item in metrics
            ]
            replay_payload = {
                "run_id": manifest.run_id,
                "status": status,
                "provider_calls": provider_calls,
                "source_integrity_before": _hashable_integrity(before),
                "source_integrity_after": _hashable_integrity(after),
                "memory_v2_integrity_before": _hashable_integrity(memory_before or {}),
                "memory_v2_integrity_after": _hashable_integrity(memory_after or {}),
                "funnel": funnel,
                # Window timestamps are reporting metadata. They may be
                # derived from a snapshot clock, but must not make identical
                # replay content produce different result hashes.
                "metrics": hashable_metrics,
                "unknowns": unknowns,
                "blocked_reasons": list(reasons),
            }
            result_hash = canonical_hash(replay_payload)
            result = ReplayResult(
                run_id=manifest.run_id, status=status, provider_calls=provider_calls,
                source_integrity_before=before, source_integrity_after=after,
                artifacts=artifacts, funnel=funnel, metrics=metrics,
                unknowns=tuple(unknowns), blocked_reasons=reasons,
                memory_v2_integrity_before=memory_before or {},
                memory_v2_integrity_after=memory_after or {}, result_hash=result_hash,
            )
            manifest_out = replace(manifest, result_hash=result_hash)
            _write_json(output / "gold_set_manifest.json", _load_json(Path(manifest.sample_manifest_path)) if manifest.sample_manifest_path else {"status": "unavailable"})
            _write_json(output / "replay_manifest.json", manifest_out.to_dict())
            _write_json(output / "replay_result.json", result.to_dict())
            _write_json(output / "funnel.json", funnel)
            _write_json(output / "metrics.json", {"metrics": [item.to_dict() for item in metrics]})
            _write_jsonl(output / "unknowns.jsonl", unknowns)
            _write_jsonl(output / "blocked.jsonl", blocked)
            _write_jsonl(output / "gold_labels.jsonl", ())
            _write_jsonl(output / "adjudication_log.jsonl", ())
            _write_json(output / "query_catalog.json", {"tables": durable_catalog})
            _write_json(output / "source_integrity_before.json", before)
            _write_json(output / "source_integrity_after.json", after)
            _write_json(output / "memory_v2_integrity_before.json", memory_before or {})
            _write_json(output / "memory_v2_integrity_after.json", memory_after or {})
            return result
    except ReplaySecurityError:
        raise
    except ReplayInputError:
        raise
    except (OSError, sqlite3.Error) as exc:
        raise ReplaySecurityError(type(exc).__name__) from exc


__all__ = [
    "ReplayInputError", "ReplaySecurityError", "recorded_request_fingerprint",
    "run_replay", "sample_manifest_hash", "sha256_file", "snapshot_integrity",
    "validate_output_file",
]
