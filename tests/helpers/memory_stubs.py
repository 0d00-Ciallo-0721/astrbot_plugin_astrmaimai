import sys
import types


def install_memory_stubs():
    processor_mod = types.ModuleType("astrmai.memory.processor")

    class MemoryProcessor:
        def __init__(self, gateway):
            self.gateway = gateway

        async def process_conversation(self, text):
            return {
                "summary": "summary",
                "key_facts": ["fact"],
                "topics": ["topic"],
                "sentiment": "neutral",
                "reflection": "",
                "nodes": [],
                "importance": 0.4,
            }

    topic_mod = types.ModuleType("astrmai.memory.topic_summarizer")

    class TopicSummarizer:
        def __init__(self, gateway, config):
            self.gateway = gateway
            self.config = config
            self.calls = []

        async def process_history(self, messages, session_id=""):
            self.calls.append((messages, session_id))
            return []

    processor_mod.MemoryProcessor = MemoryProcessor
    topic_mod.TopicSummarizer = TopicSummarizer

    sys.modules["astrmai.memory.processor"] = processor_mod
    sys.modules["astrmai.memory.topic_summarizer"] = topic_mod
