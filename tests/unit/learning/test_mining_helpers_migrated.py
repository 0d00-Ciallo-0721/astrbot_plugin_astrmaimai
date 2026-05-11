import asyncio
import unittest
from types import SimpleNamespace

from astrmai.learning.mining.jargon_miner import JargonMiner
from astrmai.learning.mining.social_relation_miner import SocialRelationMiner


class _FakeExpressionMiner:
    def __init__(self):
        self.calls = []

    async def mine_jargons(self, group_id, messages):
        self.calls.append((group_id, list(messages)))
        return ['ok']


class _FakeStateEngine:
    def __init__(self):
        self.calls = []

    async def update_social_score_from_fact(self, user_id, impact_score):
        self.calls.append((user_id, impact_score))


class MiningHelpersMigratedTests(unittest.TestCase):
    def test_jargon_miner_filters_blank_messages_before_delegating(self):
        miner = _FakeExpressionMiner()
        jargon_miner = JargonMiner(miner, min_messages=1)

        async def _run():
            return await jargon_miner.mine(
                'group-1',
                [
                    SimpleNamespace(content='hello'),
                    SimpleNamespace(content='   '),
                    SimpleNamespace(content='world'),
                ],
            )

        result = asyncio.run(_run())
        self.assertEqual(result, ['ok'])
        self.assertEqual([msg.content for msg in miner.calls[0][1]], ['hello', 'world'])

    def test_social_relation_miner_normalizes_score_and_ignores_empty_input(self):
        state_engine = _FakeStateEngine()
        miner = SocialRelationMiner(state_engine)

        async def _run():
            await miner.record_affection_fact('', 1)
            await miner.record_affection_fact('user-1', 0)
            await miner.record_affection_fact('user-1', '1.5')

        asyncio.run(_run())
        self.assertEqual(state_engine.calls, [('user-1', 1.5)])


if __name__ == '__main__':
    unittest.main()
