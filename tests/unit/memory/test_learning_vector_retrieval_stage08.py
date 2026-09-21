from __future__ import annotations

import sqlite3
import hashlib
import json
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from astrmai.infrastructure.persistence.architecture_migration_audit import (
    LATEST_ARCHITECTURE_SCHEMA_VERSION,
    inspect_architecture_migration,
)
from astrmai.infrastructure.persistence.persistence_schema import _run_migrations
from astrmai.infrastructure.persistence.sqlite_helpers import connect_aiosqlite
from astrmai.memory.contracts.learning_retrieval import (
    LearningAssetProvenance,
    LearningAssetVersion,
    LearningIndexMembership,
    LearningRetrievalCandidate,
    LearningRetrievalEvent,
    LearningFocusContext,
    LearningTurnCorrelation,
)
from astrmai.memory.retrieval.learning_retrieval_events import (
    LearningRetrievalEventWriter,
)
from astrmai.memory.retrieval.learning_retrieval_selector import (
    LearningRetrievalSelector,
)
from astrmai.memory.services.memory_vector_reconciliation import validate_vector_identity
from astrmai.memory.services.v2_store import MemoryV2Store
from tests.helpers.astrbot_stubs import install_astrbot_stubs


def test_main_schema_v171_is_append_only_and_ready(tmp_path):
    path = tmp_path / "main.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 169")
        _run_migrations(db)
        db.commit()
        assert db.execute("PRAGMA user_version").fetchone() == (171,)
        report = inspect_architecture_migration(db)
        assert "learning_retrieval_event" not in report.missing_tables
        assert "learning_retrieval_event" not in report.missing_columns
        db.execute(
            """INSERT INTO learning_retrieval_event(
               event_id,idempotency_key,turn_id,correlation_id,source_layer,stage,
               event_status,scope_id,policy_version,created_at
            ) VALUES ('e','k','t','c','memory_injection','eligible','observed','qq:group:1','retrieval-v1',1)"""
        )
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("UPDATE learning_retrieval_event SET reason_code='changed' WHERE event_id='e'")
        with pytest.raises(sqlite3.IntegrityError):
            db.execute("DELETE FROM learning_retrieval_event WHERE event_id='e'")


def test_main_readiness_rejects_retrieval_table_with_drifted_checks(tmp_path):
    path = tmp_path / "drifted.db"
    with sqlite3.connect(path) as db:
        db.execute(
            "CREATE TABLE learning_retrieval_event(event_id TEXT PRIMARY KEY, source_layer TEXT CHECK(source_layer IN ('memory_injection')))"
        )
        db.execute("PRAGMA user_version = 171")
        report = inspect_architecture_migration(db)
    assert report.ready is False
    assert "learning_retrieval_event:reply_commit" in report.invalid_constraints
    assert "learning_retrieval_event:asset_provenance_json_valid" in report.invalid_constraints


def test_stage08_feature_flags_are_independent_and_default_false():
    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    evolution = schema["evolution"]["items"]
    for name in (
        "learning_vector_build_enabled",
        "learning_vector_publish_enabled",
        "learning_retrieval_shadow_enabled",
        "learning_prompt_injection_enabled",
    ):
        assert evolution[name]["default"] is False


@pytest.mark.asyncio
async def test_memory_v2_v4_schema_is_independent_and_ready(tmp_path):
    store = MemoryV2Store(str(tmp_path / "memory-v2.db"), data_path=tmp_path)
    await store.initialize()
    report = await store.learning_schema_readiness()
    assert report["ready"] is True
    assert report["schema_version"] == 4
    assert report["missing_tables"] == []
    with sqlite3.connect(store.db_path) as db:
        assert db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert db.execute("PRAGMA foreign_key_check").fetchall() == []
        foreign_tables = {
            row[2]
            for table in ("learning_asset_version", "learning_index_membership")
            for row in db.execute(f"PRAGMA foreign_key_list({table})")
        }
        assert "learning_candidate" not in foreign_tables


async def _stage_asset(
    store: MemoryV2Store,
    *,
    suffix: str,
    generation: int,
    resource_id: str | None = None,
):
    now = float(generation)
    memory_id = f"memory-{suffix}"
    async with connect_aiosqlite(store.db_path) as db:
        await db.execute(
            "INSERT INTO canonical_memories(id,status,create_time,update_time) VALUES (?,?,?,?)",
            (memory_id, "active", now, now),
        )
        await db.commit()
    asset = LearningAssetVersion(
        asset_id=f"asset-{suffix}", asset_revision=1,
        canonical_memory_id=memory_id, candidate_id=f"candidate-{suffix}",
        candidate_revision=1, admission_revision=1, scope_id="qq:group:g1",
        speaker_scope_id="", fingerprint=f"fp-{suffix}", fingerprint_version=1,
        lifecycle_status="candidate", provenance_hash="sha256:v1:" + "a" * 64,
        created_at=now, updated_at=now,
    )
    membership = LearningIndexMembership(
        asset_id=asset.asset_id, asset_revision=1, generation=generation,
        resource_id=resource_id or f"resource-{suffix}", mapping_ordinal=0, vector_id=f"vector-{suffix}",
        mapping_hash="b" * 64, index_hash="c" * 64,
        membership_status="candidate", created_at=now, updated_at=now,
    )
    assert (await store.save_learning_candidate_asset(asset, membership)).applied is True
    return asset, membership


def test_manifest_promotion_idempotence_requires_complete_hash_identity(tmp_path):
    install_astrbot_stubs(str(tmp_path))
    from astrmai.memory.services.memory_engine import MemoryEngine

    config = SimpleNamespace(
        provider=SimpleNamespace(embedding_models=["fixture-embedding-v1"]),
        evolution=SimpleNamespace(),
    )
    engine = MemoryEngine(SimpleNamespace(), SimpleNamespace(config=config), config=config)
    engine.data_path = tmp_path
    index_path = tmp_path / "candidate.index"
    index_path.write_bytes(b"candidate-index")
    index_hash = hashlib.sha256(index_path.read_bytes()).hexdigest()
    mapping_hash = "a" * 64
    engine._write_learning_vector_candidate_manifest(
        index_path,
        {
            "generation": 2,
            "embedding_model": "fixture-embedding-v1",
            "provider_source": "fixture-provider",
            "api_base_fingerprint": "sha256:v1:" + "b" * 64,
            "physical_dimension": 3,
            "document_count": 1,
            "vector_count": 1,
            "asset_revision_digest": "asset-digest",
            "configured_dimension": 3,
            "mapping_hash": mapping_hash,
            "index_hash": index_hash,
        },
        rollback_parent_generation=1,
    )
    current = json.loads(engine._learning_vector_candidate_manifest_path.read_text(encoding="utf-8"))
    current["role"] = "current"
    current["mapping_hash"] = "wrong-mapping"
    current["index_hash"] = "wrong-index"
    engine._vector_manifest_path.write_text(json.dumps(current), encoding="utf-8")
    engine._vector_generation = 2

    result = engine._promote_learning_vector_candidate_manifest(
        expected_current_generation=1,
        expected_target_generation=2,
        expected_resource_id=current["resource_id"],
        expected_mapping_hash=mapping_hash,
        expected_index_hash=index_hash,
        expected_asset_revision_digest="asset-digest",
    )
    assert result["idempotent"] is False
    assert result["failure_kind"] == "current_manifest_identity_conflict"


@pytest.mark.asyncio
async def test_retrieval_is_closed_while_current_generation_settlement_is_pending(tmp_path):
    install_astrbot_stubs(str(tmp_path))
    from astrmai.memory.services.memory_engine import MemoryEngine

    config = SimpleNamespace(
        provider=SimpleNamespace(embedding_models=["fixture-embedding-v1"]),
        memory=SimpleNamespace(recall_top_k=5),
        evolution=SimpleNamespace(
            learning_retrieval_shadow_enabled=True,
            learning_prompt_injection_enabled=False,
        ),
    )
    engine = MemoryEngine(SimpleNamespace(), SimpleNamespace(config=config), config=config)
    engine.data_path = tmp_path
    engine.v2_db_path = str(tmp_path / "memory-v2.db")
    engine.v2_store = MemoryV2Store(engine.v2_db_path, data_path=tmp_path)
    await engine.v2_store.initialize()
    asset, membership = await _stage_asset(engine.v2_store, suffix="pending-current", generation=1)
    revision_set = ((asset.asset_id, 1, 0, 0),)
    reserved = await engine.v2_store.reserve_learning_generation(
        asset_id=asset.asset_id,
        asset_revision=1,
        generation=1,
        resource_id=membership.resource_id,
        expected_current_generation=0,
        settlement_payload={"generation": 1, "resource_id": membership.resource_id},
        asset_revision_set=revision_set,
    )
    assert reserved.applied
    activated = await engine.v2_store.activate_learning_generation(
        asset_id=asset.asset_id,
        asset_revision=1,
        generation=1,
        expected_current_generation=0,
        expected_asset_revision=0,
        expected_membership_revision=0,
        mapping_hash=membership.mapping_hash,
        index_hash=membership.index_hash,
        now=2.0,
        asset_revision_set=revision_set,
    )
    assert activated.applied
    engine._vector_generation = 1
    selection = await engine.select_learning_retrieval_assets(
        scope_id="qq:group:g1", speaker_id="speaker-1", current_generation=1
    )
    assert selection.selected_ids == ()


@pytest.mark.asyncio
async def test_learning_generation_cas_survives_twenty_independent_connection_races(tmp_path):
    import asyncio

    for round_no in range(20):
        round_path = tmp_path / f"race-{round_no}"
        round_path.mkdir()
        first = MemoryV2Store(str(round_path / "memory-v2.db"), data_path=round_path)
        second = MemoryV2Store(str(round_path / "memory-v2.db"), data_path=round_path)
        await first.initialize()
        asset, membership = await _stage_asset(first, suffix=str(round_no), generation=1)

        reservations = await asyncio.gather(
            first.reserve_learning_generation(
                asset_id=asset.asset_id, asset_revision=asset.asset_revision,
                generation=membership.generation, resource_id=membership.resource_id,
                expected_current_generation=0,
            ),
            second.reserve_learning_generation(
                asset_id=asset.asset_id, asset_revision=asset.asset_revision,
                generation=membership.generation, resource_id=membership.resource_id,
                expected_current_generation=0,
            ),
        )
        assert sum(result.applied for result in reservations) == 1
        assert sum(result.idempotent for result in reservations) == 1

        async def publish(store):
            return await store.activate_learning_generation(
                asset_id=asset.asset_id, asset_revision=asset.asset_revision,
                generation=membership.generation, expected_current_generation=0,
                expected_asset_revision=0, expected_membership_revision=0,
                mapping_hash=membership.mapping_hash, index_hash=membership.index_hash,
                now=100.0,
            )

        results = await asyncio.gather(publish(first), publish(second))
        assert sum(result.applied for result in results) == 1
        assert all(result.applied or result.idempotent or result.conflict for result in results)
        with sqlite3.connect(first.db_path) as db:
            assert db.execute(
                "SELECT value FROM memory_v2_meta WHERE key='learning_current_generation'"
            ).fetchone() == ("1",)
            assert db.execute(
                "SELECT COUNT(*) FROM learning_index_membership WHERE membership_status='current'"
            ).fetchone() == (1,)


@pytest.mark.asyncio
async def test_generation_activation_uses_complete_frozen_asset_set(tmp_path):
    store = MemoryV2Store(str(tmp_path / "memory-v2.db"), data_path=tmp_path)
    await store.initialize()
    first, first_membership = await _stage_asset(
        store,
        suffix="first",
        generation=1,
        resource_id="resource-g1",
    )
    assert (await store.reserve_learning_generation(
        asset_id=first.asset_id,
        asset_revision=first.asset_revision,
        generation=1,
        resource_id="resource-g1",
        expected_current_generation=0,
    )).applied
    assert (await store.activate_learning_generation(
        asset_id=first.asset_id,
        asset_revision=first.asset_revision,
        generation=1,
        expected_current_generation=0,
        expected_asset_revision=0,
        expected_membership_revision=0,
        mapping_hash=first_membership.mapping_hash,
        index_hash=first_membership.index_hash,
        now=2.0,
    )).applied

    async with connect_aiosqlite(store.db_path) as db:
        await db.execute(
            "INSERT INTO canonical_memories(id,status,create_time,update_time) VALUES ('memory-second','active',2,2)"
        )
        await db.commit()
    active_first = replace(first, lifecycle_status="active", revision=1, valid_at=2.0, updated_at=2.0)
    second = LearningAssetVersion(
        asset_id="asset-second", asset_revision=1, canonical_memory_id="memory-second",
        candidate_id="candidate-second", candidate_revision=1, admission_revision=1,
        scope_id="qq:group:g1", speaker_scope_id="", fingerprint="fp-second",
        fingerprint_version=1, lifecycle_status="candidate",
        provenance_hash="sha256:v1:" + "d" * 64, created_at=2.0, updated_at=2.0,
    )
    memberships = (
        LearningIndexMembership(
            asset_id=active_first.asset_id, asset_revision=1, generation=2,
            resource_id="resource-g2", mapping_ordinal=0, vector_id="vector-first-g2",
            mapping_hash="e" * 64, index_hash="f" * 64,
            membership_status="candidate", created_at=2.0, updated_at=2.0,
        ),
        LearningIndexMembership(
            asset_id=second.asset_id, asset_revision=1, generation=2,
            resource_id="resource-g2", mapping_ordinal=1, vector_id="vector-second-g2",
            mapping_hash="e" * 64, index_hash="f" * 64,
            membership_status="candidate", created_at=2.0, updated_at=2.0,
        ),
    )
    staged = await store.save_learning_generation_candidates(
        (active_first, second), memberships,
    )
    assert staged.applied is True
    frozen_set = (
        (active_first.asset_id, 1, 1, 0),
        (second.asset_id, 1, 0, 0),
    )
    reserved = await store.reserve_learning_generation(
        asset_id=active_first.asset_id,
        asset_revision=1,
        generation=2,
        resource_id="resource-g2",
        expected_current_generation=1,
        asset_revision_set=frozen_set,
    )
    assert reserved.applied is True
    activated = await store.activate_learning_generation(
        asset_id=active_first.asset_id,
        asset_revision=1,
        generation=2,
        expected_current_generation=1,
        expected_asset_revision=1,
        expected_membership_revision=0,
        mapping_hash="e" * 64,
        index_hash="f" * 64,
        now=3.0,
        asset_revision_set=frozen_set,
    )
    assert activated.applied is True
    with sqlite3.connect(store.db_path) as db:
        assert db.execute(
            "SELECT COUNT(*) FROM learning_index_membership WHERE generation=2 AND membership_status='current'"
        ).fetchone() == (2,)
        assert db.execute(
            "SELECT COUNT(*) FROM learning_asset_version WHERE lifecycle_status='active'"
        ).fetchone() == (2,)

    third, third_membership = await _stage_asset(
        store,
        suffix="third",
        generation=3,
        resource_id="resource-g3",
    )
    incomplete_reservation = await store.reserve_learning_generation(
        asset_id=third.asset_id,
        asset_revision=third.asset_revision,
        generation=3,
        resource_id=third_membership.resource_id,
        expected_current_generation=2,
    )
    assert incomplete_reservation.conflict is True
    assert incomplete_reservation.failure_kind == "generation_asset_set_incomplete"
    assert (await store.get_learning_generation_state())["current_generation"] == 2


def test_vector_identity_gate_rejects_unknown_and_type_coercion():
    valid = {
        "generation": 2,
        "revision": 3,
        "asset_revision_digest": "sha256:v1:" + "a" * 64,
        "index_file": "vectors.g2.index",
        "embedding_model": "fixture-embedding-v1",
        "provider_source": "fixture-provider",
        "api_base_fingerprint": "sha256:v1:" + "b" * 64,
        "physical_dimension": 3,
        "configured_dimension": 3,
        "document_count": 2,
        "vector_count": 2,
        "mapping_hash": "sha256:v1:" + "c" * 64,
        "index_hash": "sha256:v1:" + "d" * 64,
    }
    assert validate_vector_identity(valid)["publish_allowed"] is True
    for field, bad in (
        ("physical_dimension", None),
        ("physical_dimension", True),
        ("physical_dimension", 3.0),
        ("generation", "2"),
        ("index_file", "C:/secret/vectors.index"),
        ("provider_source", ""),
    ):
        result = validate_vector_identity(dict(valid, **{field: bad}))
        assert result["publish_allowed"] is False
        assert result["failure_stage"] == "vector_identity"
        assert result["failure_kind"]


@pytest.mark.asyncio
async def test_learning_manifest_publish_is_flagged_hashed_and_generation_fenced(tmp_path):
    install_astrbot_stubs(str(tmp_path))
    from astrmai.memory.services.memory_engine import MemoryEngine

    evolution = SimpleNamespace(
        learning_vector_build_enabled=True,
        learning_vector_publish_enabled=True,
    )
    config = SimpleNamespace(
        provider=SimpleNamespace(embedding_models=["fixture-embedding-v1"]),
        memory=SimpleNamespace(recall_top_k=5),
        evolution=evolution,
    )
    engine = MemoryEngine(SimpleNamespace(), SimpleNamespace(config=config), config=config)
    engine.data_path = Path(tmp_path)
    engine.v2_db_path = str(tmp_path / "memory-v2.db")
    index_path = tmp_path / "candidate.index"
    index_path.write_bytes(b"deterministic-index")
    engine.v2_store = MemoryV2Store(engine.v2_db_path, data_path=tmp_path)
    await engine.v2_store.initialize()
    asset, membership = await _stage_asset(
        engine.v2_store,
        suffix="manifest",
        generation=1,
        resource_id=engine._vector_resource_id(index_path),
    )
    reservation = await engine.v2_store.reserve_learning_generation(
        asset_id=asset.asset_id,
        asset_revision=asset.asset_revision,
        generation=membership.generation,
        resource_id=membership.resource_id,
        expected_current_generation=0,
    )
    assert reservation.applied is True
    index_hash = hashlib.sha256(index_path.read_bytes()).hexdigest()
    descriptor = {
        "generation": 1,
        "revision": 1,
        "asset_revision_digest": "sha256:v1:" + "a" * 64,
        "embedding_model": "fixture-embedding-v1",
        "provider_source": "fixture-provider",
        "api_base_fingerprint": "sha256:v1:" + "b" * 64,
        "physical_dimension": 3,
        "configured_dimension": 3,
        "document_count": 1,
        "vector_count": 1,
        "mapping_hash": "sha256:v1:" + "c" * 64,
        "index_hash": "sha256:v1:" + index_hash,
    }
    result = engine.publish_learning_vector_index_manifest(
        index_path, descriptor, expected_current_generation=0,
    )
    assert result["applied"] is True
    assert result["manifest"]["index_file"] == "candidate.index"
    assert str(tmp_path) not in __import__("json").dumps(result["manifest"])
    assert result["manifest"]["role"] == "candidate"
    assert engine._vector_generation == 0
    assert not engine._vector_manifest_path.exists()
    assert json.loads(
        engine._learning_vector_candidate_manifest_path.read_text(encoding="utf-8")
    )["generation"] == 1
    stale = engine.publish_learning_vector_index_manifest(
        index_path, dict(descriptor, generation=2), expected_current_generation=0,
    )
    assert stale["failure_kind"] == "publish_reservation_missing"


@pytest.mark.asyncio
async def test_publish_saga_keeps_v2_candidate_when_admission_settlement_fails(tmp_path):
    install_astrbot_stubs(str(tmp_path))
    from astrmai.memory.services.memory_engine import MemoryEngine
    from astrmai.learning.review.admission import VectorPublishProof

    config = SimpleNamespace(
        provider=SimpleNamespace(embedding_models=["fixture-embedding-v1"]),
        memory=SimpleNamespace(recall_top_k=5),
        evolution=SimpleNamespace(
            learning_vector_build_enabled=True,
            learning_vector_publish_enabled=True,
        ),
    )
    engine = MemoryEngine(SimpleNamespace(), SimpleNamespace(config=config), config=config)
    engine.data_path = tmp_path
    engine.v2_db_path = str(tmp_path / "memory-v2.db")
    engine.v2_store = MemoryV2Store(engine.v2_db_path, data_path=tmp_path)
    await engine.v2_store.initialize()
    async with connect_aiosqlite(engine.v2_db_path) as db:
        await db.execute(
            "INSERT INTO canonical_memories(id,status) VALUES ('memory-saga','active')"
        )
        await db.commit()
    index_path = tmp_path / "saga.index"
    index_path.write_bytes(b"saga-index")
    plain_index_hash = hashlib.sha256(index_path.read_bytes()).hexdigest()
    resource_id = engine._vector_resource_id(index_path)
    asset = LearningAssetVersion(
        asset_id="asset-saga", asset_revision=1, canonical_memory_id="memory-saga",
        candidate_id="candidate-saga", candidate_revision=1, admission_revision=1,
        scope_id="qq:group:g1", speaker_scope_id="", fingerprint="fp-saga",
        fingerprint_version=1, lifecycle_status="candidate",
        provenance_hash="d" * 64, created_at=1.0, updated_at=1.0,
    )
    membership = LearningIndexMembership(
        asset_id=asset.asset_id, asset_revision=1, generation=1,
        resource_id=resource_id, mapping_ordinal=0, vector_id="vector-saga",
        mapping_hash="c" * 64, index_hash=plain_index_hash,
        membership_status="candidate", created_at=1.0, updated_at=1.0,
    )
    proof = VectorPublishProof.signed(
        candidate_id=asset.candidate_id, candidate_revision=1, admission_revision=1,
        review_decision_ids=("decision-saga",), canonical_revision=1,
        owner_id="fixture-owner", asset_id=asset.asset_id, asset_revision=1,
        membership_id="membership-saga", index_generation="1",
        provider_id="fixture-provider", model_id="fixture-embedding-v1",
        vector_dimension=3, vector_count=1,
        mapping_digest=membership.mapping_hash, index_hash=plain_index_hash,
        provenance_digest=asset.provenance_hash,
    )
    descriptor = {
        "generation": 1, "revision": 1,
        "asset_revision_digest": engine._learning_asset_revision_digest((asset,)),
        "embedding_model": "fixture-embedding-v1", "provider_source": "fixture-provider",
        "api_base_fingerprint": "sha256:v1:" + "b" * 64,
        "physical_dimension": 3, "configured_dimension": 3,
        "document_count": 1, "vector_count": 1,
        "mapping_hash": "sha256:v1:" + membership.mapping_hash,
        "index_hash": "sha256:v1:" + plain_index_hash,
    }

    class _Admission:
        async def mark_published(self, *_args, **_kwargs):
            return SimpleNamespace(
                applied=False, idempotent=False, failure_kind="admission_cas_conflict",
                admission=None,
            )

    result = await engine.publish_learning_vector_candidate(
        index_path=index_path, descriptor=descriptor, asset=asset,
        membership=membership, proof=proof, admission_repository=_Admission(),
        submitting_owner_id="fixture-owner", expected_admission_record_revision=1,
        expected_current_generation=0, now=2.0,
    )
    assert result["status"] == "settlement_pending"
    assert result["failure_kind"] == "admission_cas_conflict"
    assert engine._vector_generation == 0
    assert not engine._vector_manifest_path.exists()
    assert json.loads(
        engine._learning_vector_candidate_manifest_path.read_text(encoding="utf-8")
    )["generation"] == 1
    with sqlite3.connect(engine.v2_db_path) as db:
        assert db.execute(
            "SELECT lifecycle_status FROM learning_asset_version WHERE asset_id='asset-saga'"
        ).fetchone() == ("candidate",)
        assert db.execute(
            "SELECT membership_status FROM learning_index_membership WHERE asset_id='asset-saga'"
        ).fetchone() == ("candidate",)
        assert db.execute(
            "SELECT value FROM memory_v2_meta WHERE key='learning_current_generation'"
        ).fetchone() == ("0",)
        assert db.execute(
            "SELECT value <> '' FROM memory_v2_meta WHERE key='learning_pending_publish_json'"
        ).fetchone() == (1,)
    assert await engine.v2_store.list_learning_retrieval_candidates() == []

    class _RecoveredAdmission:
        async def mark_published(self, recovered_proof, **_kwargs):
            assert recovered_proof == proof
            return SimpleNamespace(
                applied=True, idempotent=False, failure_kind="",
                admission=SimpleNamespace(revision=2),
            )

    restarted = MemoryEngine(SimpleNamespace(), SimpleNamespace(config=config), config=config)
    restarted.data_path = tmp_path
    restarted.v2_db_path = str(tmp_path / "memory-v2.db")
    restarted.v2_store = MemoryV2Store(restarted.v2_db_path, data_path=tmp_path)
    restarted.bind_learning_admission_repository(_RecoveredAdmission())
    await restarted._start_learning_publish_runtime(recover=True)
    recovered = restarted._learning_publish_recovery_result
    assert recovered["status"] == "published", recovered
    assert recovered["recovered"] is True
    with sqlite3.connect(restarted.v2_db_path) as db:
        assert db.execute(
            "SELECT lifecycle_status FROM learning_asset_version WHERE asset_id='asset-saga'"
        ).fetchone() == ("active",)
        assert db.execute(
            "SELECT membership_status FROM learning_index_membership WHERE asset_id='asset-saga'"
        ).fetchone() == ("current",)
        assert db.execute(
            "SELECT value FROM memory_v2_meta WHERE key='learning_pending_publish_json'"
        ).fetchone() == ("",)
    generation_state = await MemoryV2Store(
        restarted.v2_db_path, data_path=tmp_path
    ).get_learning_generation_state()
    assert generation_state == {
        "ready": True,
        "current_generation": 1,
        "pending_generation": None,
        "pending_resource_id": "",
        "failure_kind": "",
    }
    await restarted.stop_background_producers()


@pytest.mark.asyncio
async def test_admission_failure_preserves_existing_current_generation_end_to_end(tmp_path):
    install_astrbot_stubs(str(tmp_path))
    from astrmai.learning.review.admission import VectorPublishProof
    from astrmai.memory.services.memory_engine import MemoryEngine

    config = SimpleNamespace(
        provider=SimpleNamespace(embedding_models=["fixture-embedding-v1"]),
        memory=SimpleNamespace(recall_top_k=5),
        evolution=SimpleNamespace(
            learning_vector_build_enabled=True,
            learning_vector_publish_enabled=True,
            learning_retrieval_shadow_enabled=True,
            learning_prompt_injection_enabled=False,
        ),
    )
    engine = MemoryEngine(SimpleNamespace(), SimpleNamespace(config=config), config=config)
    engine.data_path = tmp_path
    engine.v2_db_path = str(tmp_path / "memory-v2.db")
    engine.v2_store = MemoryV2Store(engine.v2_db_path, data_path=tmp_path)
    await engine.v2_store.initialize()

    old_asset, old_membership = await _stage_asset(
        engine.v2_store,
        suffix="old-current",
        generation=1,
        resource_id="resource-old-current",
    )
    old_revision_set = ((old_asset.asset_id, 1, 0, 0),)
    assert (await engine.v2_store.reserve_learning_generation(
        asset_id=old_asset.asset_id,
        asset_revision=1,
        generation=1,
        resource_id=old_membership.resource_id,
        expected_current_generation=0,
        asset_revision_set=old_revision_set,
    )).applied
    assert (await engine.v2_store.activate_learning_generation(
        asset_id=old_asset.asset_id,
        asset_revision=1,
        generation=1,
        expected_current_generation=0,
        expected_asset_revision=0,
        expected_membership_revision=0,
        mapping_hash=old_membership.mapping_hash,
        index_hash=old_membership.index_hash,
        now=1.5,
        asset_revision_set=old_revision_set,
    )).applied
    initial_completion = await engine.v2_store.complete_learning_generation_settlement(
        generation=1,
        resource_id=old_membership.resource_id,
        asset_revision_set=old_revision_set,
    )
    assert initial_completion.applied or initial_completion.idempotent

    old_index = tmp_path / "old-current.index"
    old_index.write_bytes(b"old-current-index")
    old_index_hash = hashlib.sha256(old_index.read_bytes()).hexdigest()
    engine._publish_vector_index_manifest(
        old_index,
        ["fixture-embedding-v1"],
        dimension=3,
        provider_source_id="fixture-provider",
        api_base_fingerprint="sha256:v1:" + "1" * 64,
        document_count=1,
        vector_count=1,
        generation=1,
        asset_revision_digest=engine._learning_asset_revision_digest((old_asset,)),
        configured_dimension=3,
        mapping_hash="sha256:v1:" + old_membership.mapping_hash,
        index_hash="sha256:v1:" + old_index_hash,
        rollback_parent_generation=0,
    )
    engine._vector_generation = 1
    current_manifest_before = engine._vector_manifest_path.read_bytes()

    async with connect_aiosqlite(engine.v2_db_path) as db:
        await db.execute(
            "INSERT INTO canonical_memories(id,status,create_time,update_time) "
            "VALUES ('memory-new-candidate','active',2,2)"
        )
        await db.commit()
    active_old = replace(
        old_asset,
        lifecycle_status="active",
        revision=1,
        valid_at=1.5,
        updated_at=1.5,
    )
    new_asset = LearningAssetVersion(
        asset_id="asset-new-candidate", asset_revision=1,
        canonical_memory_id="memory-new-candidate",
        candidate_id="candidate-new-candidate", candidate_revision=1,
        admission_revision=1, scope_id="qq:group:g1", speaker_scope_id="",
        fingerprint="fp-new-candidate", fingerprint_version=1,
        lifecycle_status="candidate", provenance_hash="sha256:v1:" + "d" * 64,
        created_at=2.0, updated_at=2.0,
    )
    target_index = tmp_path / "target-g2.index"
    target_index.write_bytes(b"target-generation-two")
    target_index_hash = hashlib.sha256(target_index.read_bytes()).hexdigest()
    target_resource = engine._vector_resource_id(target_index)
    target_mapping_hash = "e" * 64
    target_memberships = (
        LearningIndexMembership(
            asset_id=active_old.asset_id, asset_revision=1, generation=2,
            resource_id=target_resource, mapping_ordinal=0,
            vector_id="vector-old-g2", mapping_hash=target_mapping_hash,
            index_hash=target_index_hash, membership_status="candidate",
            created_at=2.0, updated_at=2.0,
        ),
        LearningIndexMembership(
            asset_id=new_asset.asset_id, asset_revision=1, generation=2,
            resource_id=target_resource, mapping_ordinal=1,
            vector_id="vector-new-g2", mapping_hash=target_mapping_hash,
            index_hash=target_index_hash, membership_status="candidate",
            created_at=2.0, updated_at=2.0,
        ),
    )
    assets = (active_old, new_asset)
    proof = VectorPublishProof.signed(
        candidate_id=new_asset.candidate_id, candidate_revision=1,
        admission_revision=1, review_decision_ids=("decision-new-candidate",),
        canonical_revision=1, owner_id="fixture-owner",
        asset_id=new_asset.asset_id, asset_revision=1,
        membership_id="membership-new-candidate", index_generation="2",
        provider_id="fixture-provider", model_id="fixture-embedding-v1",
        vector_dimension=3, vector_count=2,
        mapping_digest=target_mapping_hash, index_hash=target_index_hash,
        provenance_digest=new_asset.provenance_hash,
    )
    descriptor = {
        "generation": 2, "revision": 1,
        "asset_revision_digest": engine._learning_asset_revision_digest(assets),
        "embedding_model": "fixture-embedding-v1",
        "provider_source": "fixture-provider",
        "api_base_fingerprint": "sha256:v1:" + "2" * 64,
        "physical_dimension": 3, "configured_dimension": 3,
        "document_count": 2, "vector_count": 2,
        "mapping_hash": "sha256:v1:" + target_mapping_hash,
        "index_hash": "sha256:v1:" + target_index_hash,
    }

    class _RejectedAdmission:
        async def mark_published(self, *_args, **_kwargs):
            return SimpleNamespace(
                applied=False, idempotent=False,
                failure_kind="admission_cas_conflict", admission=None,
            )

    result = await engine.publish_learning_vector_generation(
        index_path=target_index, descriptor=descriptor, assets=assets,
        memberships=target_memberships, proofs=(proof,),
        admission_repository=_RejectedAdmission(),
        submitting_owner_id="fixture-owner",
        expected_admission_record_revisions={new_asset.candidate_id: 1},
        expected_current_generation=1, now=3.0,
    )

    assert result["status"] == "settlement_pending"
    assert result["failure_kind"] == "admission_cas_conflict"
    assert engine._vector_generation == 1
    assert engine._vector_manifest_path.read_bytes() == current_manifest_before
    assert json.loads(
        engine._learning_vector_candidate_manifest_path.read_text(encoding="utf-8")
    )["generation"] == 2
    generation_state = await engine.v2_store.get_learning_generation_state()
    assert generation_state["current_generation"] == 1
    assert generation_state["pending_generation"] == 2
    selection = await engine.select_learning_retrieval_assets(
        scope_id="qq:group:g1", speaker_id="speaker-1",
        current_generation=engine._vector_generation,
    )
    assert selection.selected_ids == (old_asset.asset_id,)


@pytest.mark.asyncio
async def test_runtime_publish_worker_uses_authoritative_verifier_and_owner_fence(tmp_path):
    install_astrbot_stubs(str(tmp_path))
    from astrmai.infrastructure.runtime.background_task_owner_registry import (
        BackgroundTaskOwnerRegistry,
    )
    from astrmai.learning.review.admission import VectorPublishProof
    from astrmai.memory.services.memory_engine import MemoryEngine

    config = SimpleNamespace(
        provider=SimpleNamespace(embedding_models=["fixture-embedding-v1"]),
        memory=SimpleNamespace(recall_top_k=5),
        evolution=SimpleNamespace(
            learning_vector_build_enabled=True,
            learning_vector_publish_enabled=True,
        ),
    )
    registry = BackgroundTaskOwnerRegistry()
    engine = MemoryEngine(
        SimpleNamespace(), SimpleNamespace(config=config), config=config,
        owner_registry=registry,
    )
    engine.data_path = tmp_path
    engine.v2_db_path = str(tmp_path / "memory-v2.db")
    engine.v2_store = MemoryV2Store(engine.v2_db_path, data_path=tmp_path)
    await engine.v2_store.initialize()
    async with connect_aiosqlite(engine.v2_db_path) as db:
        await db.execute(
            "INSERT INTO canonical_memories(id,status) VALUES ('memory-worker','active')"
        )
        await db.commit()
    index_path = tmp_path / "worker.index"
    index_path.write_bytes(b"worker-index")
    plain_index_hash = hashlib.sha256(index_path.read_bytes()).hexdigest()
    asset = LearningAssetVersion(
        asset_id="asset-worker", asset_revision=1, canonical_memory_id="memory-worker",
        candidate_id="candidate-worker", candidate_revision=1, admission_revision=1,
        scope_id="qq:group:g1", speaker_scope_id="", fingerprint="fp-worker",
        fingerprint_version=1, lifecycle_status="candidate",
        provenance_hash="d" * 64, created_at=1.0, updated_at=1.0,
    )
    membership = LearningIndexMembership(
        asset_id=asset.asset_id, asset_revision=1, generation=1,
        resource_id=engine._vector_resource_id(index_path), mapping_ordinal=0,
        vector_id="vector-worker", mapping_hash="c" * 64,
        index_hash=plain_index_hash, membership_status="candidate",
        created_at=1.0, updated_at=1.0,
    )
    proof = VectorPublishProof.signed(
        candidate_id=asset.candidate_id, candidate_revision=1, admission_revision=1,
        review_decision_ids=("decision-worker",), canonical_revision=1,
        owner_id="fixture-owner", asset_id=asset.asset_id, asset_revision=1,
        membership_id="membership-worker", index_generation="1",
        provider_id="fixture-provider", model_id="fixture-embedding-v1",
        vector_dimension=3, vector_count=1,
        mapping_digest=membership.mapping_hash, index_hash=plain_index_hash,
        provenance_digest=asset.provenance_hash,
    )
    descriptor = {
        "generation": 1, "revision": 1,
        "asset_revision_digest": engine._learning_asset_revision_digest((asset,)),
        "embedding_model": "fixture-embedding-v1", "provider_source": "fixture-provider",
        "api_base_fingerprint": "sha256:v1:" + "b" * 64,
        "physical_dimension": 3, "configured_dimension": 3,
        "document_count": 1, "vector_count": 1,
        "mapping_hash": "sha256:v1:" + membership.mapping_hash,
        "index_hash": "sha256:v1:" + plain_index_hash,
    }

    class _Admission:
        publish_proof_verifier = None

        async def mark_published(self, submitted, **_kwargs):
            verified = await engine.verify(
                submitted,
                submitting_owner_id="fixture-owner",
            )
            assert verified.verified is True
            assert len(verified.verification_digest) == 64
            forged = replace(submitted, membership_id="forged-membership")
            rejected = await engine.verify(
                forged,
                submitting_owner_id="fixture-owner",
            )
            assert rejected.verified is False
            assert rejected.failure_kind == "publish_proof_not_reserved"
            return SimpleNamespace(
                applied=True, idempotent=False, failure_kind="",
                admission=SimpleNamespace(revision=2),
            )

    repository = _Admission()
    engine.bind_learning_admission_repository(repository)
    result = await engine.submit_learning_vector_generation(
        index_path=index_path,
        descriptor=descriptor,
        assets=(asset,),
        memberships=(membership,),
        proofs=(proof,),
        submitting_owner_id="fixture-owner",
        expected_admission_record_revisions={asset.candidate_id: 1},
        expected_current_generation=0,
        now=2.0,
    )
    assert result["status"] == "published", result
    assert result["asset_count"] == 1
    assert repository.publish_proof_verifier is engine
    replay = await engine.submit_learning_vector_generation(
        index_path=index_path,
        descriptor=descriptor,
        assets=(asset,),
        memberships=(membership,),
        proofs=(proof,),
        submitting_owner_id="fixture-owner",
        expected_admission_record_revisions={asset.candidate_id: 1},
        expected_current_generation=0,
        now=3.0,
    )
    assert replay["status"] == "published"
    assert replay["idempotent"] is True
    assert any(
        record.task_family == "memory.learning_vector.publish"
        for record in registry._records.values()
    )
    await engine.stop_background_producers()
    assert engine._learning_publish_worker_task is None


def _candidate(
    asset_id: str,
    *,
    scope_id: str = "qq:group:g1",
    speaker_scope_id: str = "",
    fingerprint: str | None = None,
    relevance: float = 1.0,
    generation: int = 7,
    membership_status: str = "current",
) -> LearningRetrievalCandidate:
    return LearningRetrievalCandidate(
        asset_id=asset_id,
        asset_revision=1,
        candidate_revision=4,
        admission_revision=2,
        canonical_memory_id=f"mem-{asset_id}",
        scope_id=scope_id,
        speaker_scope_id=speaker_scope_id,
        topic_scope_id="",
        fingerprint=fingerprint or f"fp-{asset_id}",
        lifecycle_status="active",
        membership_status=membership_status,
        generation=generation,
        relevance=relevance,
        asset_weight=1.0,
        support=1.0,
        decay=1.0,
        evidence_quality=1.0,
        provenance_hash="sha256:v1:" + "e" * 64,
        review_revision=3,
    )


def test_selector_is_stable_and_enforces_current_scope_and_quotas():
    selector = LearningRetrievalSelector()
    candidates = [
        _candidate("d", scope_id="qq:group:other"),
        _candidate("c", membership_status="stale"),
        _candidate("b", fingerprint="same"),
        _candidate("a", fingerprint="same"),
        _candidate("e"),
        _candidate("f", scope_id="qq:group:g1:topic:t1"),
    ]
    forward = selector.select(
        candidates,
        scope_id="qq:group:g1",
        speaker_id="",
        current_generation=7,
        shadow_enabled=True,
    )
    backward = selector.select(
        list(reversed(candidates)),
        scope_id="qq:group:g1",
        speaker_id="",
        current_generation=7,
        shadow_enabled=True,
    )
    assert forward.selected_ids == backward.selected_ids
    assert len(forward.selected_ids) <= 3
    assert len({item.fingerprint for item in forward.selected}) == len(forward.selected)
    assert sum(item.scope_id == "qq:group:g1" for item in forward.selected) <= 2
    blocked = {item.asset_id: item.blocked_reason for item in forward.blocked}
    assert blocked["c"] == "membership_not_current"
    assert blocked["d"] == "scope_mismatch"


def test_selector_blocks_non_finite_scores_independent_of_input_order():
    selector = LearningRetrievalSelector()
    candidates = (
        _candidate("finite-a", relevance=0.8),
        _candidate("nan", relevance=float("nan")),
        _candidate("inf", relevance=float("inf")),
        _candidate("finite-b", relevance=0.7),
    )
    forward = selector.select(
        candidates,
        scope_id="qq:group:g1",
        speaker_id="",
        current_generation=7,
        shadow_enabled=True,
    )
    reverse = selector.select(
        reversed(candidates),
        scope_id="qq:group:g1",
        speaker_id="",
        current_generation=7,
        shadow_enabled=True,
    )
    assert forward.selected_ids == reverse.selected_ids == ("finite-a", "finite-b")
    blocked = {
        item.asset_id: item.blocked_reason for item in forward.blocked
    } | {
        item.asset_id: item.blocked_reason for item in reverse.blocked
    }
    assert blocked["nan"] == "score_input_invalid"
    assert blocked["inf"] == "score_input_invalid"


@pytest.mark.asyncio
async def test_event_writer_is_append_only_idempotent_and_conflict_safe(tmp_path):
    path = tmp_path / "events.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 169")
        _run_migrations(db)
        db.commit()
    writer = LearningRetrievalEventWriter(path)
    event = LearningRetrievalEvent(
        correlation=LearningTurnCorrelation(
            turn_id="turn-1",
            correlation_id="corr-1",
            prompt_revision="prompt-1",
            scope_id="qq:group:g1",
            sender_id="speaker-1",
            query_fingerprint="sha256:v1:" + "f" * 64,
        ),
        source_layer="memory_injection",
        stage="selected",
        event_status="observed",
        policy_version="retrieval-v1",
        asset_revision_ids=("asset-a:1",),
        selected_ids=("asset-a",),
        generation=7,
        created_at=10.0,
    )
    first = await writer.append(event)
    replay = await writer.append(event)
    conflict = await writer.append(
        replace(event, reason_code="different", idempotency_key=first.idempotency_key)
    )
    assert first.applied is True
    assert replay.idempotent is True
    assert conflict.conflict is True
    with sqlite3.connect(path) as db:
        assert db.execute("SELECT COUNT(*) FROM learning_retrieval_event").fetchone() == (1,)


@pytest.mark.asyncio
async def test_event_retry_ignores_wall_clock_but_preserves_logical_conflicts(tmp_path):
    path = tmp_path / "event-retry.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 169")
        _run_migrations(db)
        db.commit()
    writer = LearningRetrievalEventWriter(path)
    event = LearningRetrievalEvent(
        correlation=LearningTurnCorrelation(
            turn_id="turn-retry",
            correlation_id="corr-retry",
            scope_id="qq:group:g1",
        ),
        source_layer="memory_injection",
        stage="selected",
        event_status="observed",
        policy_version="retrieval-v1",
        asset_revision_ids=("asset-a:1",),
        selected_ids=("asset-a",),
        candidate_revision=4,
        review_revision=3,
        admission_revision=2,
        generation=7,
        work_id="turn-retry:memory:selected",
        created_at=1.0,
    )
    assert (await writer.append(event)).applied is True
    replay = await writer.append(replace(event, created_at=2.0))
    assert replay.idempotent is True
    conflict = await writer.append(replace(event, selected_ids=("asset-b",), created_at=3.0))
    assert conflict.conflict is True
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT COUNT(*),candidate_revision,review_revision,admission_revision "
            "FROM learning_retrieval_event"
        ).fetchone() == (1, 4, 3, 2)


@pytest.mark.asyncio
async def test_observed_event_without_durable_correlation_fails_closed(tmp_path):
    writer = LearningRetrievalEventWriter(tmp_path / "missing.db")
    event = LearningRetrievalEvent(
        correlation=LearningTurnCorrelation(scope_id="qq:group:g1"),
        source_layer="react_retriever",
        stage="eligible",
        event_status="observed",
        policy_version="retrieval-v1",
        created_at=1.0,
    )
    result = await writer.append(event)
    assert result.applied is False
    assert result.conflict is True
    assert result.failure_kind == "correlation_unavailable"

    visibility = LearningRetrievalEventWriter.prompt_visibility_event(
        correlation=LearningTurnCorrelation(
            turn_id="turn-no-prompt",
            correlation_id="corr-no-prompt",
            scope_id="qq:group:g1",
        ),
        asset_revision_ids=("asset-a:1",),
        accepted_ids=("asset-a",),
        visible_ids=("asset-a",),
        generation=7,
        policy_version="retrieval-v1",
        created_at=2.0,
    )
    assert visibility.event_status == "blocked"
    assert visibility.visible_ids == ()
    assert visibility.reason_code == "prompt_revision_unavailable"


def test_selection_visibility_and_reply_outcome_remain_separate_stages():
    correlation = LearningTurnCorrelation(
        turn_id="turn-1", correlation_id="corr-1", prompt_revision="prompt-1",
        scope_id="qq:group:g1", sender_id="speaker-1",
    )
    selection = LearningRetrievalEventWriter.selection_events(
        correlation=correlation, source_layer="memory_injection",
        asset_revision_ids=("asset-a:1",), selected_ids=("asset-a",),
        accepted_ids=(), generation=7, policy_version="retrieval-v1",
        created_at=1.0, accepted=False,
    )
    assert [event.stage for event in selection] == [
        "eligible", "selected", "accepted_for_prompt",
    ]
    assert all(not event.visible_ids for event in selection)
    assert selection[-1].event_status == "blocked"
    visible = LearningRetrievalEventWriter.prompt_visibility_event(
        correlation=correlation, asset_revision_ids=("asset-a:1",),
        accepted_ids=("asset-a",), visible_ids=(), generation=7,
        policy_version="retrieval-v1", created_at=2.0,
        budget_chars=0, trimmed_reason="budget_zero",
    )
    assert visible.stage == "prompt_visible"
    assert visible.event_status == "blocked"
    assert visible.trimmed_reason == "budget_zero"
    terminal = LearningRetrievalEventWriter.reply_outcome_event(
        correlation=correlation, asset_revision_ids=("asset-a:1",), generation=7,
        policy_version="retrieval-v1", outcome="reply_sent", reply_id="reply-1",
        created_at=3.0,
    )
    assert terminal.stage == "reply_outcome"
    assert terminal.outcome == "reply_sent"


def test_focus_and_repetition_guard_fail_closed_after_one_retry():
    synthetic = LearningFocusContext(
        source_event_id="", scope_id="qq:group:g1", speaker_id="",
        focus_message_fingerprint="", synthetic=True, context_revision=1,
    )
    real = LearningFocusContext(
        source_event_id="event-1", scope_id="qq:group:g1", speaker_id="speaker-1",
        focus_message_fingerprint="sha256:v1:" + "a" * 64,
        synthetic=False, context_revision=1,
    )
    assert synthetic.attribution_eligible is False
    assert real.attribution_eligible is True
    first = LearningRetrievalSelector.repetition_guard(
        source_examples=("please deploy the service carefully",),
        model_output="please deploy the service carefully", attempt=0,
    )
    second = LearningRetrievalSelector.repetition_guard(
        source_examples=("please deploy the service carefully",),
        model_output="please deploy the service carefully", attempt=1,
    )
    assert first.regenerate is True and first.blocked is False
    assert second.regenerate is False and second.blocked is True


@pytest.mark.asyncio
async def test_mixed_revision_prompt_visibility_requires_per_asset_provenance(tmp_path):
    path = tmp_path / "mixed-provenance.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 169")
        _run_migrations(db)
        db.commit()
    writer = LearningRetrievalEventWriter(path)
    correlation = LearningTurnCorrelation(
        turn_id="turn-mixed",
        correlation_id="corr-mixed",
        prompt_revision="prompt-mixed",
        scope_id="qq:group:g1",
    )
    incomplete = LearningRetrievalEventWriter.prompt_visibility_event(
        correlation=correlation,
        asset_revision_ids=("asset-a:1", "asset-b:1"),
        accepted_ids=("asset-a", "asset-b"),
        visible_ids=("asset-a", "asset-b"),
        generation=7,
        policy_version="retrieval-v1",
        created_at=1.0,
    )
    assert incomplete.event_status == "blocked"
    assert incomplete.reason_code == "asset_provenance_unavailable"

    provenance = (
        LearningAssetProvenance(
            asset_id="asset-a", asset_revision=1, generation=7,
            candidate_revision=4, review_revision=3, admission_revision=2,
            canonical_memory_id="memory-a", provenance_hash="sha256:v1:" + "a" * 64,
            source_evidence_ids=("row:101",),
        ),
        LearningAssetProvenance(
            asset_id="asset-b", asset_revision=1, generation=7,
            candidate_revision=9, review_revision=8, admission_revision=7,
            canonical_memory_id="memory-b", provenance_hash="sha256:v1:" + "b" * 64,
            source_evidence_ids=("row:202",),
        ),
    )
    observed = LearningRetrievalEventWriter.prompt_visibility_event(
        correlation=correlation,
        asset_revision_ids=("asset-a:1", "asset-b:1"),
        accepted_ids=("asset-a", "asset-b"),
        visible_ids=("asset-a", "asset-b"),
        generation=7,
        policy_version="retrieval-v1",
        created_at=2.0,
        asset_provenance=provenance,
    )
    assert observed.event_status == "observed"
    assert (await writer.append(observed)).applied is True
    with sqlite3.connect(path) as db:
        stored = json.loads(db.execute(
            "SELECT asset_provenance_json FROM learning_retrieval_event"
        ).fetchone()[0])
    assert [(item["asset_id"], item["candidate_revision"]) for item in stored] == [
        ("asset-a", 4), ("asset-b", 9),
    ]


@pytest.mark.asyncio
async def test_prompt_refiner_executes_one_bounded_repetition_regeneration(tmp_path):
    path = tmp_path / "repetition-regeneration.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 169")
        _run_migrations(db)
        db.commit()
    install_astrbot_stubs(str(tmp_path))
    from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope
    from astrmai.conversation.planning.prompt_refiner import PromptRefiner

    writer = LearningRetrievalEventWriter(path)
    candidate = replace(
        _candidate("repeat"),
        prompt_text="please deploy the service carefully",
        prompt_text_provenance="model_generated",
        source_examples=("please deploy the service carefully",),
        source_example_ids=("row:301",),
        model_example_id="model:repeat:1",
    )
    provenance = LearningRetrievalEventWriter.asset_provenance((candidate,), generation=7)
    calls = []

    async def regenerate(**request):
        calls.append(request)
        return "use a cautious rollout with health checks"

    class _Event:
        message_str = "current question"
        unified_msg_origin = "qq:group:g1"

        def __init__(self):
            self._extras = {
                "astrmai_turn_identity": SimpleNamespace(turn_id="turn-repeat"),
                "astrmai_trace_id": "corr-repeat",
                "astrmai_learning_retrieval_shadow": {
                    "correlation": LearningTurnCorrelation(
                        turn_id="turn-repeat", correlation_id="corr-repeat",
                        scope_id="qq:group:g1", sender_id="speaker-1",
                    ),
                    "asset_revision_ids": ("repeat:1",),
                    "asset_provenance": provenance,
                    "selected_ids": ("repeat",),
                    "accepted_ids": ("repeat",),
                    "generation": 7,
                    "policy_version": "retrieval-v1",
                    "selected": (candidate,),
                    "created_at": 10.0,
                },
            }

        def get_extra(self, key, default=None):
            return self._extras.get(key, default)

        def set_extra(self, key, value):
            self._extras[key] = value

        def get_sender_id(self):
            return "speaker-1"

    event = _Event()
    config = SimpleNamespace(
        memory=SimpleNamespace(), conversation=SimpleNamespace(),
        global_settings=SimpleNamespace(debug_mode=False),
    )
    refiner = PromptRefiner(
        memory_engine=SimpleNamespace(
            learning_retrieval_event_writer=writer,
            injection_service=None,
            retrieval_service=None,
        ),
        config=config,
        learning_prompt_regenerator=regenerate,
    )
    _system, prompt = await refiner.refine_prompt(
        event, "system", prompt="current question", context={},
        prompt_envelope=PromptEnvelope(raw_user_text="current question"),
    )
    assert len(calls) == 1
    assert calls[0]["attempt"] == 1
    assert "use a cautious rollout with health checks" in prompt
    with sqlite3.connect(path) as db:
        diagnostics = [
            json.loads(row[0])
            for row in db.execute(
                "SELECT diagnostics_json FROM learning_retrieval_event "
                "WHERE source_layer='prompt_refiner' ORDER BY stage"
            )
        ]
    attempts = diagnostics[0]["repetition_attempts"][0]
    assert attempts["attempt_count"] == 2
    assert attempts["first_trigram_overlap"] > 0.5
    assert attempts["second_trigram_overlap"] <= 0.5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turn_id", "correlation_id"),
    (("", "corr-1"), ("turn-1", ""), ("", "")),
)
async def test_prompt_refiner_blocks_regeneration_without_durable_turn_identity(
    turn_id, correlation_id
):
    from astrmai.conversation.planning.prompt_refiner import PromptRefiner

    calls = []

    async def regenerate(**request):
        calls.append(request)
        return "should not be called"

    refiner = PromptRefiner(
        memory_engine=SimpleNamespace(),
        learning_prompt_regenerator=regenerate,
    )
    result = await refiner._regenerate_learning_prompt_text(
        candidate=SimpleNamespace(asset_id="asset-identity"),
        source_examples=("source",),
        attempt=1,
        turn_id=turn_id,
        correlation_id=correlation_id,
    )
    assert result == ""
    assert calls == []


@pytest.mark.asyncio
async def test_formal_repetition_regenerator_uses_unique_learning_provider_adapter():
    from astrmai.learning.evolution_manager import EvolutionManager

    calls = []

    class _Adapter:
        async def call(self, **request):
            calls.append(request)
            return SimpleNamespace(
                ok=True,
                value={"prompt_text": "use a cautious rollout with health checks"},
            )

    manager = EvolutionManager.__new__(EvolutionManager)
    manager.config = SimpleNamespace(
        evolution=SimpleNamespace(
            learning_enrichment_enabled=True,
            learning_pipeline_timeout_sec=60.0,
            learning_enrichment_concurrency=1,
            learning_enrichment_queue_max=8,
            learning_enrichment_wait_timeout_sec=10.0,
            learning_enrichment_execution_timeout_sec=45.0,
        )
    )
    manager.provider_adapter = _Adapter()
    manager.last_prompt_regeneration_attempt = None
    candidate = replace(
        _candidate("formal-regeneration"),
        prompt_text="please deploy the service carefully",
        source_example_ids=("row:401",),
        model_example_id="model:formal-regeneration:1",
    )

    first = await manager.regenerate_learning_prompt_asset(
        candidate=candidate,
        source_examples=("please deploy the service carefully",),
        attempt=1,
        profile_version="retrieval-v1",
        turn_id="turn-1",
        correlation_id="corr-1",
    )
    second = await manager.regenerate_learning_prompt_asset(
        candidate=candidate,
        source_examples=("please deploy the service carefully",),
        attempt=1,
        profile_version="retrieval-v1",
        turn_id="turn-1",
        correlation_id="corr-1",
    )
    third = await manager.regenerate_learning_prompt_asset(
        candidate=candidate,
        source_examples=("please deploy the service carefully",),
        attempt=1,
        profile_version="retrieval-v1",
        turn_id="turn-2",
        correlation_id="corr-2",
    )

    assert first == second == "use a cautious rollout with health checks"
    assert third == first
    assert len(calls) == 3
    assert {call["task_name"] for call in calls} == {
        "learning.retrieval_regeneration"
    }
    assert calls[0]["work_attempt_id"] == calls[1]["work_attempt_id"]
    assert calls[0]["run_id"] == calls[1]["run_id"]
    assert calls[2]["work_attempt_id"] != calls[0]["work_attempt_id"]
    assert calls[2]["run_id"] != calls[0]["run_id"]
    assert calls[0]["candidate_work_attempt"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("turn_id", "correlation_id"),
    (("", "corr-1"), ("turn-1", ""), ("", "")),
)
async def test_formal_repetition_regenerator_requires_durable_turn_identity(
    turn_id, correlation_id
):
    from astrmai.learning.evolution_manager import EvolutionManager

    calls = []

    class _Adapter:
        async def call(self, **request):
            calls.append(request)
            return SimpleNamespace(ok=True, value={"prompt_text": "rewritten"})

    manager = EvolutionManager.__new__(EvolutionManager)
    manager.config = SimpleNamespace(
        evolution=SimpleNamespace(
            learning_enrichment_enabled=True,
            learning_pipeline_timeout_sec=60.0,
            learning_enrichment_concurrency=1,
            learning_enrichment_queue_max=8,
            learning_enrichment_wait_timeout_sec=10.0,
            learning_enrichment_execution_timeout_sec=45.0,
        )
    )
    manager.provider_adapter = _Adapter()
    manager.last_prompt_regeneration_attempt = None
    result = await manager.regenerate_learning_prompt_asset(
        candidate=replace(
            _candidate("identity-gate"),
            prompt_text="please deploy carefully",
            source_example_ids=("row:1",),
            model_example_id="model:identity-gate:1",
        ),
        source_examples=("please deploy carefully",),
        attempt=1,
        profile_version="retrieval-v1",
        turn_id=turn_id,
        correlation_id=correlation_id,
    )
    assert result == ""
    assert calls == []
    assert manager.last_prompt_regeneration_attempt["failure_kind"] == (
        "turn_identity_unavailable"
    )


@pytest.mark.asyncio
async def test_reply_commit_terminal_replay_is_idempotent(tmp_path):
    path = tmp_path / "reply-events.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 169")
        _run_migrations(db)
        db.commit()

    install_astrbot_stubs(str(tmp_path))
    from astrmai.conversation.execution.reply_commit_service import ReplyCommitService

    correlation = LearningTurnCorrelation(
        turn_id="turn-reply",
        correlation_id="corr-reply",
        prompt_revision="prompt-reply",
        scope_id="qq:group:g1",
    )

    class _Event:
        def get_extra(self, key, default=None):
            if key == "astrmai_learning_retrieval_shadow":
                return {
                    "correlation": correlation,
                    "asset_revision_ids": ("asset-a:1",),
                    "generation": 4,
                    "policy_version": "retrieval-v1",
                }
            return default

    committed = SimpleNamespace(
        send_status=SimpleNamespace(value="sent"),
        commit_id="reply-commit-1",
        sent_at=1234.5,
    )
    service = ReplyCommitService(learning_event_writer=LearningRetrievalEventWriter(path))
    await service._record_learning_reply_outcome(_Event(), committed)
    await service._record_learning_reply_outcome(_Event(), committed)

    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT outcome, reply_id, created_at FROM learning_retrieval_event "
            "WHERE stage='reply_outcome'"
        ).fetchall()
    assert rows == [("reply_sent", "reply-commit-1", 1234.5)]


@pytest.mark.asyncio
async def test_conflicting_terminal_outcome_preserves_first_fact(tmp_path):
    path = tmp_path / "terminal-conflict.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 169")
        _run_migrations(db)
        db.commit()
    writer = LearningRetrievalEventWriter(path)
    correlation = LearningTurnCorrelation(
        turn_id="turn-terminal",
        correlation_id="corr-terminal",
        prompt_revision="prompt-terminal",
        scope_id="qq:group:g1",
    )
    sent = LearningRetrievalEventWriter.reply_outcome_event(
        correlation=correlation,
        asset_revision_ids=("asset-a:1",),
        generation=4,
        policy_version="retrieval-v1",
        outcome="reply_sent",
        reply_id="reply-1",
        created_at=20.0,
    )
    failed = replace(sent, outcome="reply_failed", reason_code="")
    assert (await writer.append(sent)).applied is True
    conflict = await writer.append(failed)
    assert conflict.conflict is True
    assert conflict.failure_kind == "terminal_outcome_conflict"
    with sqlite3.connect(path) as db:
        assert db.execute(
            "SELECT outcome,COUNT(*) FROM learning_retrieval_event "
            "WHERE stage='reply_outcome' GROUP BY outcome"
        ).fetchall() == [("reply_sent", 1)]


def test_prompt_visibility_rebinds_generated_prompt_revision():
    install_astrbot_stubs(".")
    from astrmai.conversation.planning.prompt_refiner import PromptRefiner

    original = LearningTurnCorrelation(
        turn_id="turn-prompt",
        correlation_id="corr-prompt",
        scope_id="qq:group:g1",
    )
    rebound = PromptRefiner._correlation_for_prompt_visibility(
        original,
        prompt_revision="sha256:v1:" + "a" * 64,
    )
    assert rebound.turn_id == original.turn_id
    assert rebound.correlation_id == original.correlation_id
    assert rebound.prompt_revision == "sha256:v1:" + "a" * 64
    assert rebound.durable is True


@pytest.mark.asyncio
async def test_prompt_render_exception_records_failed_visibility_once(tmp_path):
    path = tmp_path / "prompt-events.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 169")
        _run_migrations(db)
        db.commit()
    install_astrbot_stubs(str(tmp_path))
    from astrmai.conversation.planning.prompt_refiner import PromptRefiner

    correlation = LearningTurnCorrelation(
        turn_id="turn-render",
        correlation_id="corr-render",
        scope_id="qq:group:g1",
    )

    class _Event:
        def get_extra(self, key, default=None):
            if key == "astrmai_learning_retrieval_shadow":
                return {
                    "correlation": correlation,
                    "asset_revision_ids": ("asset-a:1",),
                    "accepted_ids": ("asset-a",),
                    "generation": 4,
                    "policy_version": "retrieval-v1",
                    "created_at": 444.0,
                }
            return default

    refiner = PromptRefiner(
        memory_engine=SimpleNamespace(
            learning_retrieval_event_writer=LearningRetrievalEventWriter(path)
        )
    )

    async def _failed(**_kwargs):
        raise RuntimeError("fixture render failure")

    refiner._refine_prompt_impl = _failed
    for _ in range(2):
        with pytest.raises(RuntimeError, match="fixture render failure"):
            await refiner.refine_prompt(_Event(), "system")

    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT event_status, trimmed_reason, reason_code, created_at "
            "FROM learning_retrieval_event WHERE stage='prompt_visible'"
        ).fetchall()
    assert rows == [("failed", "error", "render_error", 444.0)]


@pytest.mark.asyncio
async def test_prompt_refiner_records_actual_visible_fast_and_zero_budget_states(tmp_path):
    path = tmp_path / "prompt-runtime.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 169")
        _run_migrations(db)
        db.commit()
    install_astrbot_stubs(str(tmp_path))
    from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope
    from astrmai.conversation.planning.prompt_refiner import PromptRefiner

    writer = LearningRetrievalEventWriter(path)
    candidate = _candidate("runtime")
    candidate = replace(
        candidate,
        prompt_text="fixture learning asset",
        prompt_text_provenance="model_generated",
        source_examples=("historical wording example",),
        source_example_ids=("row:101",),
        model_example_id="model:runtime:1",
    )

    class _Event:
        def __init__(self, suffix: str, *, fast: bool = False):
            self.message_str = "current question"
            self.unified_msg_origin = "qq:group:g1"
            self._extras = {
                "retrieve_keys": ["CORE_ONLY"] if fast else [],
                "astrmai_turn_identity": SimpleNamespace(turn_id=f"turn-{suffix}"),
                "astrmai_trace_id": f"corr-{suffix}",
            }
            correlation = LearningTurnCorrelation(
                turn_id=f"turn-{suffix}",
                correlation_id=f"corr-{suffix}",
                scope_id="qq:group:g1",
                sender_id="speaker-1",
            )
            self._extras["astrmai_learning_retrieval_shadow"] = {
                "correlation": correlation,
                "asset_revision_ids": ("runtime:1",),
                "asset_provenance": LearningRetrievalEventWriter.asset_provenance(
                    (candidate,), generation=7,
                ),
                "selected_ids": ("runtime",),
                "accepted_ids": ("runtime",),
                "generation": 7,
                "policy_version": "retrieval-v1",
                "selected": (candidate,),
                "created_at": 700.0,
            }

        def get_extra(self, key, default=None):
            return self._extras.get(key, default)

        def set_extra(self, key, value):
            self._extras[key] = value

        def get_sender_id(self):
            return "speaker-1"

    config = SimpleNamespace(
        memory=SimpleNamespace(),
        conversation=SimpleNamespace(),
        global_settings=SimpleNamespace(debug_mode=False),
    )
    engine = SimpleNamespace(
        learning_retrieval_event_writer=writer,
        injection_service=None,
        retrieval_service=None,
    )
    refiner = PromptRefiner(memory_engine=engine, config=config)

    normal = _Event("normal")
    _system, normal_prompt = await refiner.refine_prompt(
        normal,
        "system",
        prompt="current question",
        context={},
        prompt_envelope=PromptEnvelope(raw_user_text="current question"),
    )
    assert "fixture learning asset" in normal_prompt
    assert normal.get_extra("astrmai_learning_retrieval_shadow")["correlation"].prompt_revision

    fast = _Event("fast", fast=True)
    _system, fast_prompt = await refiner.refine_prompt(
        fast,
        "system",
        prompt="current question",
        context={},
        prompt_envelope=PromptEnvelope(raw_user_text="current question"),
    )
    assert "fixture learning asset" not in fast_prompt

    refiner.LEARNING_CONTEXT_BUDGET_CHARS = 0
    zero = _Event("zero")
    _system, zero_prompt = await refiner.refine_prompt(
        zero,
        "system",
        prompt="current question",
        context={},
        prompt_envelope=PromptEnvelope(raw_user_text="current question"),
    )
    assert "fixture learning asset" not in zero_prompt

    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT correlation_id,stage,event_status,trimmed_reason,prompt_revision "
            "FROM learning_retrieval_event "
            "WHERE source_layer='prompt_refiner' ORDER BY correlation_id,stage"
        ).fetchall()
    grouped = {row[0]: [] for row in rows}
    for row in rows:
        grouped[row[0]].append(row[1:])
    assert ("accepted_for_prompt", "observed", "", grouped["corr-normal"][0][3]) in grouped["corr-normal"]
    assert ("prompt_visible", "observed", "", grouped["corr-normal"][0][3]) in grouped["corr-normal"]
    assert any(row[:3] == ("prompt_visible", "blocked", "fast_mode") for row in grouped["corr-fast"])
    assert any(row[:3] == ("accepted_for_prompt", "blocked", "") for row in grouped["corr-zero"])
    assert any(row[:3] == ("prompt_visible", "blocked", "budget_zero") for row in grouped["corr-zero"])


@pytest.mark.asyncio
async def test_react_only_shadow_closes_funnel_with_per_asset_visibility(tmp_path):
    path = tmp_path / "react-funnel.db"
    with sqlite3.connect(path) as db:
        db.execute("PRAGMA user_version = 169")
        _run_migrations(db)
        db.commit()
    install_astrbot_stubs(str(tmp_path))
    from astrmai.conversation.contracts.prompt_envelope import PromptEnvelope
    from astrmai.conversation.execution.reply_commit_service import ReplyCommitService
    from astrmai.conversation.planning.prompt_refiner import PromptRefiner

    writer = LearningRetrievalEventWriter(path)
    correlation = LearningTurnCorrelation(
        turn_id="turn-react-only",
        correlation_id="corr-react-only",
        scope_id="qq:group:g1",
        sender_id="speaker-1",
    )
    first = replace(
        _candidate("react-a"),
        review_revision=3,
        prompt_text="compact generated hint",
        prompt_text_provenance="model_generated",
        source_examples=("historical alpha wording",),
        source_example_ids=("row:201",),
        model_example_id="model:react-a:1",
    )
    second = replace(
        _candidate("react-b"),
        review_revision=3,
        prompt_text="a much longer generated guidance section that cannot fit the remaining prompt budget",
        prompt_text_provenance="model_generated",
        source_examples=("historical beta wording",),
        source_example_ids=("row:202",),
        model_example_id="model:react-b:1",
    )

    class _Event:
        message_str = "current question"
        unified_msg_origin = "qq:group:g1"

        def __init__(self):
            self._extras = {
                "astrmai_turn_identity": SimpleNamespace(turn_id=correlation.turn_id),
                "astrmai_trace_id": correlation.correlation_id,
            }

        def get_extra(self, key, default=None):
            return self._extras.get(key, default)

        def set_extra(self, key, value):
            self._extras[key] = value

        def get_sender_id(self):
            return "speaker-1"

    event = _Event()
    revision_facts = LearningRetrievalEventWriter.revision_facts((first, second))
    asset_provenance = LearningRetrievalEventWriter.asset_provenance(
        (first, second), generation=7,
    )
    for observed in LearningRetrievalEventWriter.selection_events(
        correlation=correlation,
        source_layer="react_retriever",
        asset_revision_ids=("react-a:1", "react-b:1"),
        selected_ids=("react-a", "react-b"),
        accepted_ids=("react-a", "react-b"),
        generation=7,
        policy_version="retrieval-v1",
        created_at=10.0,
        accepted=True,
        include_accepted=False,
        candidate_revision=revision_facts[0],
        review_revision=revision_facts[1],
        admission_revision=revision_facts[2],
        asset_provenance=asset_provenance,
    ):
        assert (await writer.append(observed)).applied
    LearningRetrievalEventWriter.merge_shadow(
        event,
        {
            "correlation": correlation,
            "asset_revision_ids": ("react-a:1", "react-b:1"),
            "asset_provenance": asset_provenance,
            "selected_ids": ("react-a", "react-b"),
            "accepted_ids": ("react-a", "react-b"),
            "generation": 7,
            "policy_version": "retrieval-v1",
            "created_at": 10.0,
            "selected": (first, second),
            "candidate_revision": revision_facts[0],
            "review_revision": revision_facts[1],
            "admission_revision": revision_facts[2],
        },
        source_layer="react_retriever",
    )
    config = SimpleNamespace(
        memory=SimpleNamespace(),
        conversation=SimpleNamespace(),
        global_settings=SimpleNamespace(debug_mode=False),
    )
    refiner = PromptRefiner(
        memory_engine=SimpleNamespace(
            learning_retrieval_event_writer=writer,
            injection_service=None,
            retrieval_service=None,
        ),
        config=config,
    )
    refiner.LEARNING_CONTEXT_BUDGET_CHARS = 30
    _system, prompt = await refiner.refine_prompt(
        event,
        "system",
        prompt="current question",
        context={},
        prompt_envelope=PromptEnvelope(raw_user_text="current question"),
    )
    assert "compact generated hint" in prompt
    assert "much longer generated guidance" not in prompt
    await ReplyCommitService(learning_event_writer=writer)._record_learning_reply_outcome(
        event,
        SimpleNamespace(
            send_status=SimpleNamespace(value="sent"),
            commit_id="reply-react-only",
            sent_at=11.0,
        ),
    )
    with sqlite3.connect(path) as db:
        rows = db.execute(
            "SELECT source_layer,stage,accepted_ids_json,visible_ids_json,outcome,"
            "candidate_revision,review_revision,admission_revision "
            "FROM learning_retrieval_event ORDER BY rowid"
        ).fetchall()
    assert [(row[0], row[1]) for row in rows] == [
        ("react_retriever", "eligible"),
        ("react_retriever", "selected"),
        ("prompt_refiner", "accepted_for_prompt"),
        ("prompt_refiner", "prompt_visible"),
        ("reply_commit", "reply_outcome"),
    ]
    visibility = next(row for row in rows if row[1] == "prompt_visible")
    assert json.loads(visibility[2]) == ["react-a"]
    assert json.loads(visibility[3]) == ["react-a"]
    assert visibility[5:] == (4, 3, 2)
    terminal = next(row for row in rows if row[1] == "reply_outcome")
    assert terminal[4] == "reply_sent"


@pytest.mark.asyncio
async def test_pending_publish_descriptor_survives_store_reload(tmp_path):
    first = MemoryV2Store(str(tmp_path / "pending.db"), data_path=tmp_path)
    await first.initialize()
    asset, membership = await _stage_asset(
        first,
        suffix="pending",
        generation=1,
    )
    payload = {
        "asset_id": asset.asset_id,
        "asset_revision": asset.asset_revision,
        "candidate_id": asset.candidate_id,
        "candidate_revision": asset.candidate_revision,
        "admission_revision": asset.admission_revision,
        "generation": membership.generation,
        "resource_id": membership.resource_id,
        "expected_current_generation": 0,
        "expected_admission_record_revision": 3,
        "submitting_owner_id": "fixture-owner",
        "proof": {"publish_proof": "a" * 64},
    }
    reserved = await first.reserve_learning_generation(
        asset_id=asset.asset_id,
        asset_revision=asset.asset_revision,
        generation=membership.generation,
        resource_id=membership.resource_id,
        expected_current_generation=0,
        settlement_payload=payload,
    )
    assert reserved.applied is True

    second = MemoryV2Store(str(tmp_path / "pending.db"), data_path=tmp_path)
    pending = await second.get_pending_learning_publish()
    assert pending == {
        **payload,
        "asset_revision_set": [[asset.asset_id, asset.asset_revision, 0, 0]],
    }


@pytest.mark.asyncio
async def test_incomplete_pending_publish_is_not_reported_as_empty(tmp_path):
    store = MemoryV2Store(str(tmp_path / "incomplete.db"), data_path=tmp_path)
    await store.initialize()
    async with connect_aiosqlite(store.db_path) as db:
        await db.execute(
            "UPDATE memory_v2_meta SET value='9' "
            "WHERE key='learning_pending_generation'"
        )
        await db.execute(
            "UPDATE memory_v2_meta SET value='resource-9' "
            "WHERE key='learning_pending_resource_id'"
        )
        await db.commit()
    pending = await store.get_pending_learning_publish()
    assert pending is not None
    assert pending["_failure_kind"] == "pending_publish_identity_incomplete"


@pytest.mark.asyncio
async def test_publish_recovery_rejects_asset_without_admission_proof(tmp_path):
    install_astrbot_stubs(str(tmp_path))
    from astrmai.memory.services.memory_engine import MemoryEngine

    config = SimpleNamespace(
        provider=SimpleNamespace(embedding_models=["fixture-embedding-v1"]),
        memory=SimpleNamespace(recall_top_k=5),
        evolution=SimpleNamespace(
            learning_vector_build_enabled=True,
            learning_vector_publish_enabled=True,
        ),
    )
    engine = MemoryEngine(SimpleNamespace(), SimpleNamespace(config=config), config=config)

    class _PendingStore:
        async def get_pending_learning_publish(self):
            return {
                "generation": 1,
                "resource_id": "resource-1",
                "mapping_hash": "b" * 64,
                "index_hash": "c" * 64,
                "expected_current_generation": 0,
                "submitting_owner_id": "fixture-owner",
                "asset_revision_set": [["asset-1", 1, 0, 0]],
                "items": [{"asset_id": "asset-1", "asset_revision": 1, "proof": None}],
            }

    engine.v2_store = _PendingStore()
    result = await engine.recover_learning_vector_publish(
        admission_repository=SimpleNamespace(),
        now=1.0,
    )
    assert result == {
        "status": "blocked",
        "failure_stage": "publish_recovery",
        "failure_kind": "pending_publish_proof_invalid",
    }
