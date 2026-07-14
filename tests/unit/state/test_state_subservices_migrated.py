import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
from types import SimpleNamespace

from astrmai.state.energy.energy_manager import EnergyManager
from astrmai.state.mood.mood_decay import apply_natural_decay
from astrmai.state.energy.frequency_controller import ChatReplyRecord, FrequencyController
from astrmai.state.relationship.relationship_engine import (
    RelationshipEngine,
    RelationshipEvent,
)


class StateSubservicesMigratedTests(unittest.TestCase):
    def test_frequency_controller_refreshes_derived_frequency_without_losing_records(self):
        old_config = SimpleNamespace(reply=SimpleNamespace(base_frequency=0.4))
        new_config = SimpleNamespace(reply=SimpleNamespace(base_frequency=0.9))
        controller = FrequencyController(old_config)
        record = controller._get_record('chat-1')

        controller.refresh_config(new_config)

        self.assertEqual(controller.BASE_FREQ, 0.9)
        self.assertIs(controller._get_record('chat-1'), record)

    def test_frequency_controller_honors_mentions(self):
        controller = FrequencyController()
        self.assertTrue(controller.should_reply('chat-1', is_mentioned=True))

    def test_frequency_controller_drops_dense_replies_when_probability_misses(self):
        controller = FrequencyController()
        record = controller._get_record('chat-1')
        record.reply_timestamps = [100.0, 120.0, 140.0]
        with patch('astrmai.state.energy.frequency_controller.time.time', return_value=150.0),                  patch('astrmai.state.energy.frequency_controller.random.random', return_value=0.95):
            self.assertFalse(controller.should_reply('chat-1', energy=0.2, mood=-0.6))

    def test_frequency_controller_concurrent_access_keeps_single_record(self):
        controller = FrequencyController()

        def worker(_):
            controller.on_message_received('chat-shared')
            result = controller.should_reply('chat-shared', energy=0.8, mood=0.0)
            return id(controller._get_record('chat-shared')), result

        with patch('astrmai.state.energy.frequency_controller.random.random', return_value=0.0):
            with ThreadPoolExecutor(max_workers=8) as executor:
                results = list(executor.map(worker, range(40)))

        record_ids = {record_id for record_id, _ in results}
        self.assertEqual(len(record_ids), 1)
        self.assertTrue(all(result for _, result in results))
        record = controller._get_record('chat-shared')
        self.assertEqual(len(record.reply_timestamps), 40)
        self.assertGreater(record.last_message_time, 0.0)

    def test_frequency_controller_concurrent_cleanup_and_updates_keep_records_valid(self):
        controller = FrequencyController()
        for idx in range(6):
            record = controller._get_record(f'stale-{idx}')
            record.last_message_time = 1.0
            record.reply_timestamps = [1.0, 2.0]

        def update_worker(index: int):
            chat_id = f'active-{index % 3}'
            controller.on_message_received(chat_id)
            controller.should_reply(chat_id, energy=0.8, mood=0.0)

        def cleanup_worker(_):
            controller.cleanup_inactive(max_age_hours=0.0)

        with patch('astrmai.state.energy.frequency_controller.random.random', return_value=0.0):
            with ThreadPoolExecutor(max_workers=8) as executor:
                futures = [executor.submit(update_worker, index) for index in range(30)]
                futures.extend(executor.submit(cleanup_worker, index) for index in range(10))
                for future in futures:
                    future.result()

        controller.on_message_received('post-cleanup')
        post_cleanup_record = controller._get_record('post-cleanup')
        self.assertIsInstance(post_cleanup_record, ChatReplyRecord)
        for chat_id, record in controller._records.items():
            self.assertIsInstance(chat_id, str)
            self.assertIsInstance(record, ChatReplyRecord)
            self.assertIsInstance(record.reply_timestamps, list)
            self.assertIsInstance(record.last_message_time, float)

    def test_relationship_engine_process_event_updates_social_score(self):
        engine = RelationshipEngine()
        before = engine.get_social_score('user-1')
        after = engine.process_event('user-1', RelationshipEvent.HELPFUL_REPLY, intensity=1.0)
        self.assertGreater(after, before)
        self.assertGreater(engine.get_or_create('user-1').trust, 0.0)

    def test_energy_manager_uses_safe_defaults_when_energy_config_missing(self):
        manager = EnergyManager(SimpleNamespace())
        state = SimpleNamespace(energy=0.05, is_dirty=False)

        with patch('astrmai.state.energy.energy_manager.random.random', return_value=0.0):
            should_drop = manager.should_drop_by_energy(state, msg_count=2)

        self.assertTrue(should_drop)
        self.assertAlmostEqual(manager.get_reply_cost(), 0.1)
        self.assertAlmostEqual(state.energy, 0.25)
        self.assertTrue(state.is_dirty)

    def test_apply_natural_decay_recovers_energy_only_once_per_silence_window(self):
        state = SimpleNamespace(
            energy=0.4,
            mood=0.2,
            last_reply_time=1200.0,  # 20 min ago, ensures valid recovery_anchor
            last_energy_recovery_time=0.0,
            last_passive_decay_time=0.0,
            is_dirty=False,
        )
        config = SimpleNamespace(
            energy=SimpleNamespace(recovery_silence_min=1),
            mood=SimpleNamespace(decay_interval=3600, decay_rate=0.05),
        )

        with patch('astrmai.state.mood.mood_decay.time.time', return_value=7200.0):
            apply_natural_decay(state, config)
            first_energy = state.energy
            first_decay_time = state.last_passive_decay_time
            apply_natural_decay(state, config)

        self.assertAlmostEqual(first_energy, 0.5)
        self.assertAlmostEqual(state.energy, 0.5)
        self.assertAlmostEqual(state.last_passive_decay_time, first_decay_time)

    def test_relationship_engine_streak_bonus_applies_once_per_event(self):
        engine = RelationshipEngine()
        vec = engine.get_or_create('user-streak')
        vec.positive_streak = 2
        before = vec.to_dict()

        after_score = engine.process_event('user-streak', RelationshipEvent.HELPFUL_REPLY, intensity=1.0)
        after = engine.get_or_create('user-streak').to_dict()

        streak_bonus = min(__import__('math').log2(3) * 0.3, 1.5)
        multiplier = 1.0 + streak_bonus
        expected_trust = before['trust'] + 1.5 * multiplier
        expected_familiarity = before['familiarity'] + 0.5 * multiplier
        expected_emotion = before['emotion_bond'] + 0.8 * multiplier
        expected_respect = before['respect'] + 1.2 * multiplier

        self.assertAlmostEqual(after['trust'], round(expected_trust, 2))
        self.assertAlmostEqual(after['familiarity'], round(expected_familiarity, 2))
        self.assertAlmostEqual(after['emotion_bond'], round(expected_emotion, 2))
        self.assertAlmostEqual(after['respect'], round(expected_respect, 2))
        self.assertGreater(after_score, 0.0)


if __name__ == '__main__':
    unittest.main()
