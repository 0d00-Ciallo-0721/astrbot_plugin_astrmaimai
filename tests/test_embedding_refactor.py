import asyncio
import tempfile
import unittest

from tests.helpers.astrbot_stubs import install_astrbot_stubs

install_astrbot_stubs(tempfile.mkdtemp(prefix="astrmai-embedding-"))

from astrmai.memory.retrieval.embedding import EmbeddingClient


class _Provider:
    def __init__(self, provider_id, vector):
        self.id = provider_id
        self.vector = vector
        self.calls = 0

    async def get_embedding(self, text):
        self.calls += 1
        return self.vector


class _Context:
    def __init__(self):
        self.providers = {
            "a": _Provider("a", [1.0, 0.0]),
            "b": _Provider("b", [0.0, 1.0]),
        }
        self.auto_provider = _Provider("auto", [0.5, 0.5])

    def get_provider_by_id(self, provider_id):
        return self.providers.get(provider_id)

    def get_all_embedding_providers(self):
        return [self.auto_provider]


class EmbeddingRefactorTests(unittest.TestCase):
    def test_round_robin_configured_embedding_models(self):
        context = _Context()
        client = EmbeddingClient(context, embedding_models=["a", "b"])

        async def _run():
            first = await client.get_vector("hello")
            second = await client.get_vector("hello")
            return first, second

        first, second = asyncio.run(_run())
        self.assertEqual(first, [1.0, 0.0])
        self.assertEqual(second, [0.0, 1.0])
        self.assertEqual(context.providers["a"].calls, 1)
        self.assertEqual(context.providers["b"].calls, 1)

    def test_auto_fallback_only_when_no_models_configured(self):
        context = _Context()
        client = EmbeddingClient(context, embedding_models=[])
        self.assertEqual(asyncio.run(client.get_vector("hello")), [0.5, 0.5])

        configured_missing = EmbeddingClient(context, embedding_models=["missing"])
        self.assertIsNone(asyncio.run(configured_missing.get_vector("hello")))
        self.assertEqual(context.auto_provider.calls, 1)

    def test_cosine_similarity(self):
        self.assertAlmostEqual(EmbeddingClient.cosine_similarity([1, 0], [1, 0]), 1.0)
        self.assertEqual(EmbeddingClient.cosine_similarity([1, 0], [0, 1]), 0.0)
        self.assertEqual(EmbeddingClient.cosine_similarity([1], [1, 2]), 0.0)


if __name__ == "__main__":
    unittest.main()