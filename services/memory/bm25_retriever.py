### 📄 services/memory/bm25_retriever.py
import json
import aiosqlite
from .text_processor import TextProcessor
from astrbot.api import logger

class BM25Retriever:
    def __init__(self, db_path: str, text_processor: TextProcessor):
        self.db_path = db_path
        self.text_processor = text_processor
        self.fts_table = "memories_fts"

    async def initialize(self):
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS {self.fts_table} USING fts5(content, doc_id UNINDEXED, tokenize='unicode61')")
            await db.commit()

    async def add_document(self, doc_id: int, content: str):
        tokens = self.text_processor.tokenize(content)
        processed_content = " ".join(tokens)
        async with aiosqlite.connect(self.db_path) as db:
            await db.execute(f"INSERT INTO {self.fts_table}(doc_id, content) VALUES (?, ?)", (doc_id, processed_content))
            await db.commit()

    async def search(self, query: str, limit: int = 50):
        tokens = self.text_processor.tokenize(query)
        if not tokens: return []
        
        fts_query = " OR ".join([f'"{t}"' for t in tokens])
        
        async with aiosqlite.connect(self.db_path) as db:
            # 获取 ID 和分数
            cursor = await db.execute(
                f"SELECT doc_id, bm25({self.fts_table}) as score FROM {self.fts_table} WHERE {self.fts_table} MATCH ? ORDER BY score DESC LIMIT ?",
                (fts_query, limit * 2)
            )
            return await cursor.fetchall() # 返回原始元组 (doc_id, score)，后续由 HybridRetriever 组装
            
    async def delete_document(self, doc_id: int):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                await db.execute(f"DELETE FROM {self.fts_table} WHERE doc_id = ?", (doc_id,))
                await db.commit()
            return True
        except Exception:
            return False