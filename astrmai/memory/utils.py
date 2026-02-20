import re
from dataclasses import dataclass
from typing import List, Any, Dict, Optional
from astrbot.api import logger

try:
    import jieba
    JIEBA_AVAILABLE = True
except ImportError:
    JIEBA_AVAILABLE = False

@dataclass
class SearchResult:
    """通用检索结果对象"""
    doc_id: int
    score: float
    content: str
    metadata: Dict[str, Any]
    source: str = "unknown" # bm25 or vector

class TextProcessor:
    """
    简易文本处理器 (System 2 Support)
    用于分词和预处理
    """
    def __init__(self):
        self.stopwords = {"的", "了", "和", "是", "就", "都", "而", "及", "与"}
    
    def tokenize(self, text: str, remove_stopwords: bool = True) -> List[str]:
        if not text:
            return []
        
        # 1. 简单清洗
        text = re.sub(r'[^\w\s]', ' ', text)
        
        # 2. 分词
        if JIEBA_AVAILABLE:
            tokens = list(jieba.cut_for_search(text))
        else:
            tokens = text.split()
            
        # 3. 停用词过滤
        if remove_stopwords:
            tokens = [t for t in tokens if t not in self.stopwords and len(t.strip()) > 0]
            
        return tokens

class RRFFusion:
    """
    Reciprocal Rank Fusion (RRF) 算法
    Reference: LivingMemory/core/retrieval/rrf_fusion.py
    """
    def __init__(self, k: int = 60):
        self.k = k

    def fuse(self, bm25_results: List[SearchResult], vector_results: List[SearchResult], top_k: int) -> List[SearchResult]:
        """融合两路检索结果"""
        all_doc_ids = set()
        for r in bm25_results: all_doc_ids.add(r.doc_id)
        for r in vector_results: all_doc_ids.add(r.doc_id)
        
        # 建立映射
        bm25_map = {r.doc_id: (i, r) for i, r in enumerate(bm25_results)}
        vector_map = {r.doc_id: (i, r) for i, r in enumerate(vector_results)}
        
        fused_scores = {}
        doc_info = {} # 存储 content 和 metadata
        
        for doc_id in all_doc_ids:
            score = 0.0
            
            # BM25 贡献
            if doc_id in bm25_map:
                rank, res = bm25_map[doc_id]
                score += 1.0 / (self.k + rank + 1)
                doc_info[doc_id] = res
            
            # Vector 贡献
            if doc_id in vector_map:
                rank, res = vector_map[doc_id]
                score += 1.0 / (self.k + rank + 1)
                if doc_id not in doc_info: doc_info[doc_id] = res
                
            fused_scores[doc_id] = score
            
        # 排序
        sorted_ids = sorted(all_doc_ids, key=lambda x: fused_scores[x], reverse=True)[:top_k]
        
        results = []
        for doc_id in sorted_ids:
            info = doc_info[doc_id]
            results.append(SearchResult(
                doc_id=doc_id,
                score=fused_scores[doc_id],
                content=info.content,
                metadata=info.metadata,
                source="hybrid"
            ))
            
        return results