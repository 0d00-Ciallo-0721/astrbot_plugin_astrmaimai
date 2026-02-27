import json
import aiosqlite
from typing import List, Optional, Any
from astrbot.api import logger
from .utils import TextProcessor, SearchResult

class BM25Retriever:
    """
    BM25 稀疏检索器
    升级：添加 metadata 过滤支持 (session_id 和 persona_id)。
    """
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.processor = TextProcessor()
        self.table = "memories_fts"
        self.doc_table = "documents" # 依赖 Faiss 底层共享的 documents 表

    async def initialize(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"""
                CREATE VIRTUAL TABLE IF NOT EXISTS {self.table}
                USING fts5(content, doc_id UNINDEXED, tokenize='unicode61')
            """)
            await db.commit()

    async def add_document(self, doc_id: int, content: str):
        tokens = self.processor.tokenize(content)
        processed = " ".join(tokens)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(
                f"INSERT INTO {self.table}(doc_id, content) VALUES (?, ?)",
                (doc_id, processed)
            )
            await db.commit()

    async def search(self, query: str, k: int = 20, session_id: Optional[str] = None, persona_id: Optional[str] = None) -> List[SearchResult]:
        tokens = self.processor.tokenize(query)
        if not tokens: return []
        
        escaped_tokens = []
        for t in tokens:
            escaped = t.replace('"', '""')
            escaped_tokens.append(f'"{escaped}"')
            
        fts_query = " OR ".join(escaped_tokens)
        
        async with aiosqlite.connect(self.db_path) as db:
            cursor = await db.execute(
                f"""
                SELECT doc_id, bm25({self.table}) as score
                FROM {self.table}
                WHERE {self.table} MATCH ?
                ORDER BY score DESC
                LIMIT ?
                """,
                (fts_query, k * 2) 
            )
            fts_results = await cursor.fetchall()
            
            if not fts_results: return []
            
            doc_ids = [r[0] for r in fts_results]
            placeholders = ",".join("?" * len(doc_ids))
            
            # 从主表拉取全量数据以供过滤
            cursor = await db.execute(
                f"SELECT id, text, metadata FROM {self.doc_table} WHERE id IN ({placeholders})", doc_ids
            )
            
            docs = {}
            async for r in cursor:
                doc_id, text, metadata_json = r
                metadata = json.loads(metadata_json) if metadata_json else {}
                docs[doc_id] = {"text": text, "metadata": metadata}
                
            results = []
            for doc_id, bm25_score in fts_results:
                if doc_id not in docs: continue
                
                doc = docs[doc_id]
                metadata = doc["metadata"]
                
                # 执行精确隔离过滤
                if session_id is not None and metadata.get("session_id") != session_id:
                    continue
                if persona_id is not None and metadata.get("persona_id") != persona_id:
                    continue
                    
                results.append(SearchResult(
                    doc_id=doc_id,
                    score=bm25_score,
                    content=doc["text"],
                    metadata=metadata,
                    source="bm25"
                ))
                
                if len(results) >= k: break
                
            # 分数归一化处理
            if results:
                scores = [r.score for r in results]
                max_score, min_score = max(scores), min(scores)
                score_range = max_score - min_score if max_score != min_score else 1.0
                for r in results:
                    r.score = (r.score - min_score) / score_range
                    
            return results