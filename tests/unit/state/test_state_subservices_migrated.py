import unittest
from unittest.mock import patch

from astrmai.state.energy.frequency_controller import FrequencyController
from astrmai.state.relationship.relationship_engine import (
    RelationshipEngine,
    RelationshipEvent,
)


class StateSubservicesMigratedTests(unittest.TestCase):
    def test_frequency_controller_honors_mentions(self):
        controller = FrequencyController()
        self.assertTrue(controller.should_reply('chat-1', is_mentioned=True))

    def test_frequency_controller_drops_dense_replies_when_probability_misses(self):
        controller = FrequencyController()
        record = controller._get_record('chat-1')
        record.reply_timestamps = [100.0, 120.0, 140.0]
        with patch('astrmai.state.energy.frequency_controller.time.time', return_value=150.0),                  patch('astrmai.state.energy.frequency_controller.random.random', return_value=0.95):
            self.assertFalse(controller.should_reply('chat-1', energy=0.2, mood=-0.6))

    def test_relationship_engine_process_event_updates_social_score(self):
        engine = RelationshipEngine()
        before = engine.get_social_score('user-1')
        after = engine.process_event('user-1', RelationshipEvent.HELPFUL_REPLY, intensity=1.0)
        self.assertGreater(after, before)
        self.assertGreater(engine.get_or_create('user-1').trust, 0.0)


if __name__ == '__main__':
    unittest.main()
