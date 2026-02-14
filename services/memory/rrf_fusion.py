### 📄 services/memory/rrf_fusion.py
from dataclasses import dataclass
from typing import Any, List, Dict, Optional

@dataclass
class BM25Result:
    doc_id: int
    score: float
    content: str
    metadata: Dict[str, Any]

@dataclass
class VectorResult:
    doc_id: int
    score: float
    content: str
    metadata: Dict[str, Any]

@dataclass
class FusedResult:
    doc_id: int
    rrf_score: float
    bm25_score: Optional[float]
    vector_score: Optional[float]
    content: str
    metadata: Dict[str, Any]

class RRFFusion:
    """RRF 倒数排名融合算法"""
    def __init__(self, k: int = 60):
        self.k = k

    def fuse(self, bm25_results: List[BM25Result], vector_results: List[VectorResult], top_k: int = 10) -> List[FusedResult]:
        fused_scores = {}
        doc_content_map = {}
        doc_metadata_map = {}
        bm25_score_map = {}
        vector_score_map = {}

        # 处理 BM25
        for rank, res in enumerate(bm25_results):
            if res.doc_id not in fused_scores:
                fused_scores[res.doc_id] = 0.0
                doc_content_map[res.doc_id] = res.content
                doc_metadata_map[res.doc_id] = res.metadata
            fused_scores[res.doc_id] += 1.0 / (self.k + rank + 1)
            bm25_score_map[res.doc_id] = res.score

        # 处理 Vector
        for rank, res in enumerate(vector_results):
            if res.doc_id not in fused_scores:
                fused_scores[res.doc_id] = 0.0
                doc_content_map[res.doc_id] = res.content
                doc_metadata_map[res.doc_id] = res.metadata
            fused_scores[res.doc_id] += 1.0 / (self.k + rank + 1)
            vector_score_map[res.doc_id] = res.score

        # 排序
        sorted_ids = sorted(fused_scores.keys(), key=lambda x: fused_scores[x], reverse=True)
        
        results = []
        for doc_id in sorted_ids[:top_k]:
            results.append(FusedResult(
                doc_id=doc_id,
                rrf_score=fused_scores[doc_id],
                bm25_score=bm25_score_map.get(doc_id),
                vector_score=vector_score_map.get(doc_id),
                content=doc_content_map[doc_id],
                metadata=doc_metadata_map[doc_id]
            ))
        return results