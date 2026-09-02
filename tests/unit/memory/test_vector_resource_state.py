from __future__ import annotations

import unittest
import tempfile
import uuid
from pathlib import Path
from types import SimpleNamespace

from astrmai.memory.contracts.vector_resource_state import (
    VectorResourceSnapshot,
    VectorResourceState,
    transition_vector_resource_state,
)


class VectorResourceStateTests(unittest.TestCase):
    def _engine(self):
        from astrmai.memory.services.memory_engine import MemoryEngine

        config = SimpleNamespace(
            provider=SimpleNamespace(embedding_models=[]),
            memory=SimpleNamespace(recall_top_k=5),
        )
        engine = MemoryEngine(SimpleNamespace(), SimpleNamespace(config=config), config=config)
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        engine.data_path = Path(self._tmp.name)
        engine.db_path = str(engine.data_path / "docs.db")
        engine.v2_db_path = str(engine.data_path / "memory_v2.db")
        engine._vector_resource_descriptors.clear()
        engine._vector_resource_state_overrides.clear()
        engine._vector_candidate_paths.clear()
        engine._vector_shutdown_generation = 0
        return engine

    def test_allowed_and_terminal_transitions(self):
        self.assertEqual(
            transition_vector_resource_state("candidate_building", "candidate_ready"),
            (True, "allowed"),
        )
        self.assertEqual(
            transition_vector_resource_state("retired_closing", "closed"),
            (True, "allowed"),
        )
        self.assertEqual(
            transition_vector_resource_state("closed", "active"),
            (False, "terminal_state"),
        )

    def test_invalid_and_unknown_transitions_fail_closed(self):
        self.assertEqual(
            transition_vector_resource_state("active", "closed"),
            (False, "invalid_transition"),
        )
        self.assertEqual(
            transition_vector_resource_state("bogus", "active"),
            (False, "unknown_state"),
        )

    def test_snapshot_is_serializable_and_transition_guarded(self):
        snapshot = VectorResourceSnapshot(resource_id="r1", state=VectorResourceState.RETIRED_PENDING)
        self.assertEqual(snapshot.to_dict()["state"], "retired_pending")
        changed = snapshot.evolve("retired_closing")
        self.assertEqual(changed.state, VectorResourceState.RETIRED_CLOSING)
        with self.assertRaises(ValueError):
            changed.evolve("active")

    def test_engine_transition_drives_candidate_cutover(self):
        engine = self._engine()
        rid = f"candidate:test-{uuid.uuid4().hex}"
        candidate_path = str(Path(self._tmp.name) / "candidate.index")
        result = engine._transition_vector_resource(
            rid, VectorResourceState.CANDIDATE_BUILDING,
            role="candidate", generation=1, index_path=candidate_path,
        )
        self.assertTrue(result["allowed"], result)
        self.assertTrue(engine._transition_vector_resource(
            rid, VectorResourceState.CANDIDATE_READY,
        )["allowed"])
        self.assertTrue(engine._transition_vector_resource(
            rid, VectorResourceState.CUTOVER_PENDING,
        )["allowed"])
        result = engine._transition_vector_resource(rid, VectorResourceState.ACTIVE, role="active")
        self.assertTrue(result["allowed"])
        self.assertEqual(engine.describe_vector_resource_states()["active_resource_id"], rid)
        self._tmp.cleanup()

    def test_engine_rejects_shutdown_generation_and_records_diagnostic(self):
        engine = self._engine()
        result = engine._transition_vector_resource(
            "candidate:shutdown", VectorResourceState.CANDIDATE_BUILDING,
            role="candidate", generation=1, shutdown_generation=99,
        )
        self.assertFalse(result["allowed"])
        self.assertEqual(result["reason"], "shutdown_generation_mismatch")
        self.assertEqual(engine.describe_vector_resource_states()["invalid_transition_count"], 1)
        self.assertGreaterEqual(engine._vector_state_transition_rejected_total, 1)
        self._tmp.cleanup()

    def test_projection_mismatch_enters_degraded_fail_closed(self):
        engine = self._engine()
        rid = "candidate:mismatch"
        path = str(Path(self._tmp.name) / "candidate.index")
        self.assertTrue(engine._transition_vector_resource(
            rid, VectorResourceState.CANDIDATE_BUILDING,
            role="candidate", generation=1, index_path=path,
        )["allowed"])
        self.assertFalse(engine._vector_state_projection_matches(rid, role="candidate", index_path=path))
        self.assertEqual(engine._vector_state, "degraded")
        self.assertEqual(engine._vector_health_status, "degraded")
        snapshot = next(item for item in engine.describe_vector_resource_states()["resources"] if item["resource_id"] == rid)
        self.assertEqual(snapshot["state"], "candidate_building")
        diagnostics = engine.describe_vector_resource_states()["state_diagnostics"]["rejected_transitions"]
        self.assertEqual(diagnostics[-1]["reason"], "state_projection_mismatch")
        self._tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
