import asyncio
from pathlib import Path

from tests.helpers.astrbot_stubs import install_astrbot_stubs


def test_admin_app_uses_safe_fetch_for_empty_api_fallbacks():
    root = Path(__file__).resolve().parents[1]
    source = (root / "pages" / "admin" / "app.js").read_text(encoding="utf-8")

    assert "function safeFetch" in source
    assert ".catch(() => ({}))" not in source
    assert ".catch(() => ({ items: [] }))" not in source


def test_expression_review_conflict_keeps_explicit_status(tmp_path):
    install_astrbot_stubs(str(tmp_path))
    from astrmai.memory.contracts.memory_query import MemoryCandidate
    from astrmai.memory.services.expression_pattern_service import ExpressionPatternService

    class _Store:
        def __init__(self):
            self.updated_metadata = None
            self.candidate = MemoryCandidate(
                id="expr-1",
                kind="expression_pattern",
                source="learning_expression_pattern",
                summary="hello",
                content="hello",
                session_id="group-1",
                status="review_pending",
                visibility="maintenance_only",
                metadata={"review_status": "pending", "weight": 1.0},
            )

        async def get_canonical(self, _memory_id, include_inactive=False):
            return self.candidate

        async def update_memory(self, _memory_id, **kwargs):
            self.updated_metadata = dict(kwargs["metadata"])
            self.candidate.metadata = self.updated_metadata
            self.candidate.status = kwargs["status"]
            self.candidate.visibility = kwargs["visibility"]
            self.candidate.content = kwargs["content"]
            self.candidate.summary = kwargs["summary"]
            return 1

    store = _Store()
    service = ExpressionPatternService(store, write_service=None)

    updated = asyncio.run(
        service.update_review(
            "expr-1",
            checked=True,
            rejected=True,
            review_status="approved",
        )
    )

    assert updated.review_status == "approved"
    assert store.updated_metadata["review_status"] == "approved"


def test_memory_store_exposes_projector_slot(tmp_path):
    install_astrbot_stubs(str(tmp_path))
    from astrmai.memory.services.v2_store import MemoryV2Store

    store = MemoryV2Store(str(tmp_path / "memory.db"))
    projector = object()
    store.index_projector = projector

    assert store.index_projector is projector
