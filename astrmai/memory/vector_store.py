import time
from typing import List, Dict, Any
from astrbot.core.db.vec_db.faiss_impl.vec_db import FaissVecDB
from .utils import SearchResult, TextProcessor

class VectorRetriever:
    """
    向量检索器 (Custom Faiss implementation)
    """
    def __init__(self, data_path: str, embedding_client: EmbeddingClient):
        self.data_path = data_path
        self.embedding_client = embedding_client
        self.processor = TextProcessor()
        
        # 索引与元数据存储路径
        self.index_path = os.path.join(data_path, "faiss_index.bin")
        self.meta_path = os.path.join(data_path, "faiss_meta.json")
        
        self.index = None
        self.metadata_store = {}
        self.next_id = 0
        
        self._load()

    def _load(self):
        """加载本地索引与元数据"""
        if os.path.exists(self.meta_path):
            try:
                with open(self.meta_path, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    self.metadata_store = {int(k): v for k, v in data.items()}
                    self.next_id = max(self.metadata_store.keys()) + 1 if self.metadata_store else 0
            except Exception as e:
                logger.error(f"[VectorStore] 元数据加载失败: {e}")

        if os.path.exists(self.index_path):
            try:
                self.index = faiss.read_index(self.index_path)
            except Exception as e:
                logger.error(f"[VectorStore] Faiss 索引加载失败: {e}")

    def _save(self):
        """持久化保存"""
        if self.index is not None:
            faiss.write_index(self.index, self.index_path)
        with open(self.meta_path, 'w', encoding='utf-8') as f:
            json.dump(self.metadata_store, f, ensure_ascii=False)

    async def add_document(self, content: str, metadata: Dict[str, Any]) -> int:
        """存入文本，返回 document id"""
        if "create_time" not in metadata:
            metadata["create_time"] = time.time()
            
        # 1. 显式调用标准化 Embedding 客户端获取向量
        vector = await self.embedding_client.get_vector(content)
        if not vector:
            logger.error("[VectorStore] 无法获取文本向量，跳过存入")
            return -1
            
        # 2. 转换为 Numpy 数组并做 L2 归一化 (为后续计算 Cosine Similarity 做准备)
        vec_np = np.array([vector], dtype=np.float32)
        faiss.normalize_L2(vec_np)
        
        # 3. 动态初始化 Index (解决不同模型维度不同的痛点)
        if self.index is None:
            dim = vec_np.shape[1]
            self.index = faiss.IndexFlatIP(dim) # 内积 (Inner Product) 在归一化后等同于余弦相似度
            logger.info(f"[VectorStore] 已根据提供商动态初始化 Faiss 索引，维度: {dim}")
            
        # 4. 插入与保存
        doc_id = self.next_id
        self.index.add(vec_np)
        
        self.metadata_store[doc_id] = {
            "content": content,
            "metadata": metadata
        }
        self.next_id += 1
        self._save()
        
        return doc_id


    async def search(self, query: str, k: int = 20) -> List[SearchResult]:
        """检索文本片段"""
        if self.index is None or self.index.ntotal == 0:
            return []
            
        # 预处理查询
        tokens = self.processor.tokenize(query)
        processed_query = " ".join(tokens) if tokens else query
        
        # 获取查询向量
        vector = await self.embedding_client.get_vector(processed_query)
        if not vector:
            return []
            
        vec_np = np.array([vector], dtype=np.float32)
        faiss.normalize_L2(vec_np)
        
        # 检索 (限制搜索范围防止超限)
        fetch_k = min(k * 2, self.index.ntotal)
        distances, indices = self.index.search(vec_np, fetch_k)
        
        out = []
        for i, doc_id in enumerate(indices[0]):
            doc_id = int(doc_id) # faiss 返回的是 int64
            if doc_id == -1 or doc_id not in self.metadata_store:
                continue
            
            score = float(distances[0][i]) # IndexFlatIP 归一化后此处分数介于 -1 到 1 之间 (余弦相似度)
            data = self.metadata_store[doc_id]
            
            out.append(SearchResult(
                doc_id=doc_id,
                score=score,
                content=data["content"],
                metadata=data["metadata"],
                source="vector"
            ))
            
        return out