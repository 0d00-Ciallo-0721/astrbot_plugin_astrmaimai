import asyncio
import sqlite3

from astrmai.infrastructure.persistence.persistence_schema import _MIGRATIONS, _run_migrations
from astrmai.infrastructure.persistence.relationship_ledger_persistence import RelationshipLedgerPersistenceMixin
from astrmai.state.relationship.relationship_ledger import RelationshipEventProposal, RelationshipLedgerEntry


def test_relationship_ledger_migrates_v88_and_has_required_indexes(tmp_path):
    path = tmp_path / "ledger-v88.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 88")
        _run_migrations(db)
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        indexes = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
        version = db.execute("PRAGMA user_version").fetchone()[0]

    assert version == len(_MIGRATIONS)
    assert "relationship_event_ledger" in tables
    assert {
        "ix_relationship_event_ledger_user_created",
        "ix_relationship_event_ledger_chat_created",
        "ix_relationship_event_ledger_turn_id",
    }.issubset(indexes)


def test_relationship_ledger_is_idempotent_and_preserves_audit_payload(tmp_path):
    path = tmp_path / "ledger.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 88")
        _run_migrations(db)

    class _Harness(RelationshipLedgerPersistenceMixin):
        db_path = path

    proposal = RelationshipEventProposal(
        event_type="compliment",
        user_id="user-a",
        chat_id="group-a",
        turn_id="turn-a",
        source_event_ids=("event-a",),
        evidence_codes=("direct_praise",),
    )
    entry = RelationshipLedgerEntry(
        proposal=proposal,
        policy_version="relationship-v1",
        disposition="applied",
        before_vector={"trust": 1.0},
        delta_vector={"trust": 0.5},
        after_vector={"trust": 1.5},
    )

    async def _run():
        store = _Harness()
        first = await store.append_relationship_ledger_entry(entry)
        second = await store.append_relationship_ledger_entry(entry)
        found = await store.get_relationship_ledger_entry(entry.proposal.idempotency_key)
        rows = await store.list_relationship_ledger_entries("user-a")
        return first, second, found, rows

    first, second, found, rows = asyncio.run(_run())
    assert first[0] is True
    assert second[0] is False
    assert found is not None
    assert found["event_id"] == entry.event_id
    assert len(rows) == 1
    assert rows[0]["event_type"] == "compliment"
    assert rows[0]["disposition"] == "applied"
