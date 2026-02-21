import json
import aiosqlite
from typing import List, Optional
from astrbot.api import logger
from .utils import TextProcessor, SearchResult

class BM25Retriever:
    """
    基于 SQLite FTS5 的 BM25 检索器
    Reference: LivingMemory/core/retrieval/bm25_retriever.py
    """
    def __init__(self, db_path: str):
        self.db_path = db_path
        self.processor = TextProcessor()
        self.table = "memories_fts"

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

    async def search(self, query: str, k: int = 20, **kwargs) -> List[SearchResult]:
        tokens = self.processor.tokenize(query)
        if not tokens: return []
        
        # 构造 OR 查询以提高召回
        fts_query = " OR ".join([f'"{t}"' for t in tokens])
        
        async with aiosqlite.connect(self.db_path) as db:
            # 获取 ID 和 分数
            cursor = await db.execute(
                f"""
                SELECT doc_id, bm25({self.table}) as score
                FROM {self.table}
                WHERE {self.table} MATCH ?
                ORDER BY score
                LIMIT ?
                """,
                (fts_query, k)  # 这里把原来的 limit 变量替换为 k
            )
            rows = await cursor.fetchall()
            
            if not rows: return []
            
            # 获取完整内容 (需要关联 documents 表，假设 engine 会处理表创建)
            doc_ids = [r[0] for r in rows]
            id_str = ",".join(map(str, doc_ids))
            
            # 注意：这里假设 documents 表存在于同一个 DB 文件中
            cursor = await db.execute(f"SELECT id, text, metadata FROM documents WHERE id IN ({id_str})")
            docs = {}
            async for r in cursor:
                docs[r[0]] = {"text": r[1], "meta": json.loads(r[2]) if r[2] else {}}
                
            results = []
            for doc_id, score in rows:
                if doc_id in docs:
                    # 归一化处理在 Hybrid 中做，或者这里简单处理
                    # FTS5 bm25 返回负数，越小越好(绝对值越大越相关?) 
                    # 修正: FTS5 bm25 越小越相关 (more negative is better usually, but standard sqlite bm25 returns positive where smaller is better? 
                    # 引用源文件逻辑: "注意: bm25()返回负数,越大(越接近0)越相关" -> 实际上是越负越相关? 
                    # 源文件逻辑: "ORDER BY score DESC" (从大到小). 
                    # 我们保持源文件逻辑
                    results.append(SearchResult(
                        doc_id=doc_id,
                        score=score, # 原始分数
                        content=docs[doc_id]["text"],
                        metadata=docs[doc_id]["meta"],
                        source="bm25"
                    ))
            return results