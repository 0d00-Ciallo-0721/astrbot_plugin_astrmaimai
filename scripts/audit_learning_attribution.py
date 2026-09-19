from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections import Counter
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Iterable
from urllib.parse import quote

if __package__ in (None, ""):
    repository_root = Path(__file__).resolve().parents[1]
    if str(repository_root) not in sys.path:
        sys.path.insert(0, str(repository_root))

from astrmai.learning.mining.learning_attribution import LearningAttributionAdapter
from astrmai.learning.mining.learning_input_policy import (
    LearningInputPolicy,
    LearningMessageView,
)


REPORT_VERSION = "learning-attribution-audit-v1"
REPORT_FILTERS = {
    "candidate_evidence": "eligible = 1 AND is_generated = 0",
    "legacy_messages": "event_schema_version <= 0",
    "speaker_eligibility": (
        "LearningInputPolicy acceptance AND LearningAttributionAdapter eligibility"
    ),
    "structured_messages": "event_schema_version > 0",
    "time_window": "timestamp >= start_inclusive AND timestamp < end_exclusive",
    "trace_success": "propagation_status = available with durable identity match",
}


MESSAGE_COLUMNS = (
    "id",
    "group_id",
    "sender_id",
    "sender_name",
    "content",
    "timestamp",
    "event_id",
    "event_schema_version",
    "platform_message_id",
    "chat_kind",
    "role",
    "message_kind",
    "is_bot",
    "reply_target_event_id",
    "reply_target_actor_id",
    "reply_target_actor_name",
    "quote_event_id",
    "at_actor_ids",
    "topic_epoch",
    "causal_parent_event_id",
    "source_event_ids",
    "provenance",
    "image_refs",
    "interaction_kind",
    "recalled",
    "outcome",
)


def _connect_ro(path: Path) -> sqlite3.Connection:
    resolved = path.resolve(strict=True)
    uri = f"file:{quote(resolved.as_posix(), safe='/:%')}?mode=ro"
    connection = sqlite3.connect(uri, uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def _source_descriptor(path: Path) -> dict[str, Any]:
    resolved = path.resolve(strict=True)
    digest = hashlib.sha256()
    with resolved.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    return {
        "descriptor": f"sha256:{digest.hexdigest()}",
        "descriptor_kind": "file_sha256",
        "mode": "read_only",
        "size_bytes": resolved.stat().st_size,
    }


def _validate_output_path(
    output: Path,
    database: Path,
    memory_database: Path | None,
) -> None:
    output_resolved = output.resolve(strict=False)
    for input_path in (database, memory_database):
        if input_path is None:
            continue
        input_resolved = input_path.resolve(strict=True)
        overlaps = output_resolved == input_resolved
        if not overlaps and output.exists():
            try:
                overlaps = os.path.samefile(output, input_path)
            except OSError:
                overlaps = False
        if overlaps:
            raise ValueError("output path overlaps input database")


def _candidate_persistence_id(
    candidate_id: str, evidence_ids: Iterable[str]
) -> str:
    encoded = json.dumps(
        {
            "candidate_id": str(candidate_id),
            "source_evidence_ids": sorted(
                str(item) for item in evidence_ids if str(item or "").strip()
            ),
            "version": 1,
        },
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"candidate-enrichment:{hashlib.sha256(encoded).hexdigest()}"


def _tables(connection: sqlite3.Connection) -> set[str]:
    return {
        str(row[0]).lower()
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        )
    }


def _columns(connection: sqlite3.Connection, table: str) -> set[str]:
    return {
        str(row[1])
        for row in connection.execute(f'PRAGMA table_info("{table}")')
    }


def _metric(numerator: int, denominator: int, unknown_count: int = 0) -> dict[str, Any]:
    return {
        "numerator": int(numerator),
        "denominator": int(denominator),
        "unknown_count": int(unknown_count),
        "ratio": (float(numerator) / denominator) if denominator else None,
    }


def _counter(values: Iterable[str], denominator: int) -> dict[str, dict[str, Any]]:
    counts = Counter(str(value or "unknown") for value in values)
    unknown_count = counts.get("unknown", 0)
    return {
        key: _metric(count, denominator, unknown_count)
        for key, count in sorted(counts.items())
    }


def _scoped_coverage(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    adapter = LearningAttributionAdapter()
    groups: dict[tuple[str, str, str], list[sqlite3.Row]] = {}
    for row in rows:
        view = adapter.attribute(dict(row))
        scope_hash = (
            hashlib.sha256(view.scope_id.encode("utf-8")).hexdigest()[:16]
            if view.scope_id
            else "unknown"
        )
        key = (view.platform_id or "unknown", view.chat_kind or "unknown", scope_hash)
        groups.setdefault(key, []).append(row)
    return [
        {
            "platform_id": platform,
            "chat_kind": kind,
            "scope_hash": scope_hash,
            "coverage": _coverage(group_rows),
        }
        for (platform, kind, scope_hash), group_rows in sorted(groups.items())
    ]


def _coverage(rows: list[sqlite3.Row]) -> dict[str, Any]:
    adapter = LearningAttributionAdapter()
    source_rows = [SimpleNamespace(**dict(row)) for row in rows]
    accepted = LearningInputPolicy().normalize(source_rows)
    accepted_by_source = {id(item._source): item for item in accepted}
    policy_views = [
        accepted_by_source.get(id(source))
        or LearningMessageView(
            source,
            str(getattr(source, "content", "") or ""),
            context_content=str(getattr(source, "content", "") or ""),
            evidence_eligible=False,
        )
        for source in source_rows
    ]
    views = [adapter.attribute(item) for item in policy_views]
    total = len(views)
    accepted_count = len(accepted)

    def present(name: str) -> dict[str, Any]:
        count = sum(getattr(view, name) not in (None, "", (), []) for view in views)
        return _metric(count, total, total - count)

    relation_names = {
        "reply": "reply_target_actor_id",
        "quote": "quote_event_id",
        "at": "at_actor_ids",
        "topic": "topic_epoch",
    }
    relations: dict[str, Any] = {}
    for label, key in relation_names.items():
        if key == "topic_epoch":
            count = sum(view.topic_epoch is not None for view in views)
        elif key == "at_actor_ids":
            count = sum(
                bool(view.relation_payload.get("at_actor_ids")) for view in views
            )
        else:
            count = sum(bool(view.relation_payload.get(key)) for view in views)
        relations[label] = _metric(count, total, total - count)
    unknown_reasons = Counter(
        reason for view in views for reason in view.unknown_reasons
    )
    return {
        "row_count": total,
        "text": _metric(
            sum(bool(str(row["content"] or "").strip()) for row in rows), total
        ),
        "input_policy_accepted": _metric(accepted_count, total, total - accepted_count),
        "source_row": present("source_row_id"),
        "source_message": present("source_message_id"),
        "event": present("event_id"),
        "platform_message": present("platform_message_id"),
        "speaker": present("speaker_scope_id"),
        "scope": present("scope_id"),
        "relations": relations,
        "source_types": _counter((view.source_type for view in views), total),
        "evidence_qualities": _counter((view.evidence_quality for view in views), total),
        "group_shadow_eligibility": _metric(
            sum(view.eligible_for_group_shadow for view in views), total
        ),
        "speaker_stats_eligibility": _metric(
            sum(view.eligible_for_speaker_stats for view in views), total
        ),
        "unknown_reasons": {
            key: _metric(count, total, total - count)
            for key, count in sorted(unknown_reasons.items())
        },
    }


def _propagation(
    plugin: sqlite3.Connection,
    memory: sqlite3.Connection | None,
    *,
    start: float,
    end: float,
) -> dict[str, Any]:
    plugin_tables = _tables(plugin)
    candidate_evidence: dict[str, set[str]] = {}
    evidence_count = 0
    if "learning_candidate_evidence" in plugin_tables:
        rows = plugin.execute(
            """
            SELECT candidate_id, evidence_id FROM learning_candidate_evidence
            WHERE created_at >= ? AND created_at < ?
              AND is_generated = 0 AND eligible = 1
            """,
            (start, end),
        ).fetchall()
        evidence_count = len(rows)
        window_candidate_ids: set[str] = set()
        for row in rows:
            candidate_id = str(row[0] or "")
            if candidate_id:
                window_candidate_ids.add(candidate_id)
        for row in plugin.execute(
            """
            SELECT candidate_id, evidence_id
            FROM learning_candidate_evidence
            WHERE is_generated = 0 AND eligible = 1
            """
        ):
            candidate_id = str(row[0] or "")
            evidence_id = str(row[1] or "")
            if candidate_id in window_candidate_ids and evidence_id:
                candidate_evidence.setdefault(candidate_id, set()).add(evidence_id)

    expected: dict[str, dict[str, Any]] = {}
    mismatch_reasons = Counter()
    if candidate_evidence and "learning_candidate_attempt" in plugin_tables:
        candidate_revisions: dict[str, int] = {}
        candidate_evidence_digests: dict[str, str] = {}
        if "learning_candidate" in plugin_tables:
            candidate_columns = _columns(plugin, "learning_candidate")
            if {"candidate_id", "revision"} <= candidate_columns:
                selected_columns = "candidate_id, revision"
                if "source_payload_json" in candidate_columns:
                    selected_columns += ", source_payload_json"
                for row in plugin.execute(
                    f"SELECT {selected_columns} FROM learning_candidate"
                ):
                    candidate_id = str(row[0])
                    candidate_revisions[candidate_id] = int(row[1])
                    if len(row) < 3:
                        continue
                    try:
                        source_payload = json.loads(str(row[2] or "{}"))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        mismatch_reasons["candidate_payload_invalid"] += 1
                        continue
                    if isinstance(source_payload, dict):
                        evidence_digest = str(
                            source_payload.get("evidence_digest") or ""
                        )
                        if evidence_digest:
                            candidate_evidence_digests[candidate_id] = evidence_digest
        attempts: dict[str, tuple[int, dict[str, Any], list[str]]] = {}
        for row in plugin.execute(
            """
            SELECT candidate_id, revision, diagnostics_json, canonical_ids_json
            FROM learning_candidate_attempt
            WHERE status = 'completed'
            ORDER BY revision DESC
            """
        ):
            candidate_id = str(row[0] or "")
            if candidate_id not in candidate_evidence or candidate_id in attempts:
                continue
            try:
                diagnostics = json.loads(str(row[2] or "{}"))
                canonical_ids = json.loads(str(row[3] or "[]"))
            except (TypeError, ValueError, json.JSONDecodeError):
                mismatch_reasons["attempt_diagnostics_invalid"] += 1
                continue
            if not isinstance(diagnostics, dict) or not isinstance(canonical_ids, list):
                mismatch_reasons["attempt_diagnostics_invalid"] += 1
                continue
            attempts[candidate_id] = (
                int(row[1]), diagnostics, [str(item) for item in canonical_ids]
            )
        for candidate_id, evidence_ids in candidate_evidence.items():
            attempt = attempts.get(candidate_id)
            if attempt is None:
                mismatch_reasons["completed_attempt_missing"] += 1
                continue
            revision, diagnostics, canonical_ids = attempt
            persistence_id = _candidate_persistence_id(candidate_id, evidence_ids)
            if str(diagnostics.get("canonical_persistence_id") or "") != persistence_id:
                mismatch_reasons["evidence_identity_mismatch"] += 1
                continue
            current_revision = candidate_revisions.get(candidate_id)
            if current_revision is not None and current_revision != revision + 1:
                mismatch_reasons["candidate_revision_advanced"] += 1
                continue
            expected[candidate_id] = {
                "revision": revision,
                "persistence_id": persistence_id,
                "canonical_ids": set(canonical_ids),
                "evidence_digest": candidate_evidence_digests.get(candidate_id, ""),
            }

    verified_canonical: dict[str, dict[str, Any]] = {}
    canonical_with_attribution = 0
    if memory is not None and "canonical_memories" in _tables(memory):
        for row in memory.execute(
            "SELECT id, metadata FROM canonical_memories",
        ):
            try:
                metadata = json.loads(str(row[1] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(metadata, dict):
                continue
            candidate_id = str(metadata.get("candidate_id") or "")
            if (
                candidate_id in candidate_evidence
                and metadata.get("source_attributions")
            ):
                canonical_with_attribution += 1
            identity = expected.get(candidate_id)
            if identity is None:
                continue
            if (
                not metadata.get("source_attributions")
                or not str(metadata.get("evidence_digest") or "")
            ):
                mismatch_reasons["canonical_attribution_missing"] += 1
                continue
            try:
                revision = int(metadata.get("candidate_revision"))
            except (TypeError, ValueError):
                mismatch_reasons["canonical_revision_missing"] += 1
                continue
            memory_id = str(row[0] or "")
            if (
                revision != identity["revision"]
                or str(metadata.get("candidate_persistence_id") or "")
                != identity["persistence_id"]
                or memory_id not in identity["canonical_ids"]
                or (
                    identity["evidence_digest"]
                    and str(metadata.get("evidence_digest") or "")
                    != identity["evidence_digest"]
                )
            ):
                mismatch_reasons["canonical_identity_mismatch"] += 1
                continue
            verified_canonical[memory_id] = metadata

    trace_count = trace_with_attribution = 0
    if "memoryretrievaltrace" in plugin_tables:
        for row in plugin.execute(
            """
            SELECT trace_summary FROM memoryretrievaltrace
            WHERE created_at >= ? AND created_at < ?
            """,
            (start, end),
        ):
            trace_count += 1
            try:
                summary = json.loads(str(row[0] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(summary, dict):
                continue
            for item in summary.get("selected_attribution") or ():
                if not isinstance(item, dict) or item.get("propagation_status") != "available":
                    continue
                metadata = verified_canonical.get(str(item.get("memory_id") or ""))
                if metadata is None:
                    continue
                try:
                    revision_matches = int(item.get("candidate_revision")) == int(
                        metadata.get("candidate_revision")
                    )
                except (TypeError, ValueError):
                    revision_matches = False
                if (
                    str(item.get("candidate_id") or "")
                    == str(metadata.get("candidate_id") or "")
                    and revision_matches
                    and str(item.get("candidate_persistence_id") or "")
                    == str(metadata.get("candidate_persistence_id") or "")
                    and str(item.get("evidence_digest") or "")
                    == str(metadata.get("evidence_digest") or "")
                ):
                    trace_with_attribution += 1
                    break

    verified_candidate_ids = {
        str(metadata.get("candidate_id") or "")
        for metadata in verified_canonical.values()
    }
    candidate_to_canonical = (
        _metric(
            len(verified_candidate_ids),
            len(candidate_evidence),
            len(candidate_evidence) - len(verified_candidate_ids),
        )
        if memory is not None
        else {
            "numerator": None,
            "denominator": len(candidate_evidence),
            "unknown_count": len(candidate_evidence),
            "ratio": None,
        }
    )
    return {
        "eligible_candidate_evidence": evidence_count,
        "candidate_to_canonical": candidate_to_canonical,
        "candidate_to_canonical_mismatch_reasons": dict(
            sorted(mismatch_reasons.items())
        ),
        "canonical_with_attribution": canonical_with_attribution,
        "retrieval_trace_attribution": (
            _metric(
                trace_with_attribution,
                trace_count,
                trace_count - trace_with_attribution,
            )
            if memory is not None
            else {
                "numerator": None,
                "denominator": trace_count,
                "unknown_count": trace_count,
                "ratio": None,
            }
        ),
        "memory_database_status": "observed" if memory is not None else "unavailable",
        "memory_database_unavailable_reason": (
            "" if memory is not None else "memory_database_not_supplied"
        ),
    }


def audit_database(
    database: Path,
    *,
    start: float,
    end: float,
    memory_database: Path | None = None,
) -> dict[str, Any]:
    if end <= start:
        raise ValueError("end must be greater than start")
    source = {
        "plugin_database": _source_descriptor(database),
        "memory_database": (
            _source_descriptor(memory_database)
            if memory_database is not None
            else {"descriptor": "unavailable", "mode": "not_supplied"}
        ),
    }
    with _connect_ro(database) as plugin:
        if "messagelog" not in _tables(plugin):
            raise RuntimeError("messagelog table is unavailable")
        available = _columns(plugin, "messagelog")
        missing = [name for name in MESSAGE_COLUMNS if name not in available]
        if missing:
            raise RuntimeError("messagelog missing columns: " + ",".join(missing))
        column_sql = ", ".join(f'"{name}"' for name in MESSAGE_COLUMNS)
        rows = plugin.execute(
            f"SELECT {column_sql} FROM messagelog WHERE timestamp >= ? AND timestamp < ? ORDER BY id",
            (start, end),
        ).fetchall()
        structured = [row for row in rows if int(row["event_schema_version"] or 0) > 0]
        legacy = [row for row in rows if int(row["event_schema_version"] or 0) <= 0]
        memory = _connect_ro(memory_database) if memory_database is not None else None
        try:
            propagation = _propagation(
                plugin, memory, start=start, end=end
            )
        finally:
            if memory is not None:
                memory.close()
    return {
        "report_version": REPORT_VERSION,
        "source": source,
        "window": {"start_inclusive": start, "end_exclusive": end},
        "filters": dict(REPORT_FILTERS),
        "database_mode": "read_only",
        "structured": _coverage(structured),
        "legacy": _coverage(legacy),
        "structured_by_scope": _scoped_coverage(structured),
        "legacy_by_scope": _scoped_coverage(legacy),
        "propagation": propagation,
        "privacy": {
            "message_content_emitted": False,
            "raw_user_ids_emitted": False,
            "secrets_emitted": False,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Read-only learning attribution coverage audit")
    parser.add_argument("database", type=Path)
    parser.add_argument("--memory-database", type=Path)
    parser.add_argument("--start", type=float, required=True)
    parser.add_argument("--end", type=float, required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    if args.output:
        _validate_output_path(
            args.output, args.database, args.memory_database
        )
    report = audit_database(
        args.database,
        start=args.start,
        end=args.end,
        memory_database=args.memory_database,
    )
    encoded = json.dumps(report, ensure_ascii=False, indent=2, sort_keys=True)
    if args.output:
        _validate_output_path(
            args.output, args.database, args.memory_database
        )
        args.output.write_text(encoded + "\n", encoding="utf-8")
    else:
        print(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
