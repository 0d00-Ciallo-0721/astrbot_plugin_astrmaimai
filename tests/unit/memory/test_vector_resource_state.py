from __future__ import annotations

import unittest

from astrmai.memory.contracts.vector_resource_state import (
    VectorResourceSnapshot,
    VectorResourceState,
    transition_vector_resource_state,
)


class VectorResourceStateTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
