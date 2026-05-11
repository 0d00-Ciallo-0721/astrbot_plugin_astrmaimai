from importlib import import_module

_EXPORTS = {
    "BM25Retriever": ".bm25",
    "EmbeddingClient": ".embedding",
    "HybridRetriever": ".hybrid_retriever",
    "ReActRetriever": ".react_retriever",
    "VectorStore": ".vector_store",
}

__all__ = list(_EXPORTS)


def __getattr__(name):
    try:
        module_name = _EXPORTS[name]
    except KeyError as exc:  # pragma: no cover
        raise AttributeError(name) from exc
    module = import_module(f"{__name__}{module_name}")
    value = getattr(module, name)
    globals()[name] = value
    return value


def __dir__():
    return sorted(set(globals()) | set(__all__))