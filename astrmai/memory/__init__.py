from importlib import import_module

__all__ = [
    "MemoryEngine",
    "MemoryQuery",
    "PersonaSummarizer",
    "ReActRetriever",
    "RetrievalTrace",
]


def __getattr__(name):
    module_map = {
        "MemoryEngine": ".services.memory_engine",
        "MemoryQuery": ".contracts.memory_query",
        "PersonaSummarizer": ".persona.persona_summarizer",
        "ReActRetriever": ".retrieval.react_retriever",
        "RetrievalTrace": ".contracts.retrieval_trace",
    }
    module_name = module_map.get(name)
    if module_name is None:
        raise AttributeError(name)
    module = import_module(module_name, __name__)
    value = getattr(module, name)
    globals()[name] = value
    return value
