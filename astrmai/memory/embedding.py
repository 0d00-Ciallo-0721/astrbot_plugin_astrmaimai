import numpy as np
from typing import List, Optional, Any
from astrbot.api.star import Context
from astrbot.api import logger

class EmbeddingClient:
    """
    标准化的 Embedding 客户端
    支持手动配置优先 + 自动获取兜底的寻找策略。
    """
    def __init__(self, context: Context, provider_id: str = ""):
        self.context = context
        self.provider_id = provider_id

    async def get_vector(self, text: str) -> Optional[List[float]]:
        """获取文本的 Embedding 向量"""
        provider = self._find_provider()
        if not provider:
            logger.error("[Embedding] 未找到任何可用的 Embedding Provider，向量化失败")
            return None

        # 动态探测兼容的方法名
        candidate_methods = [
            'get_embeddings', 
            'embeddings',     
            'text_embedding', 
            'get_embedding',  
            'embedding'
        ]

        for method_name in candidate_methods:
            if hasattr(provider, method_name):
                method = getattr(provider, method_name)
                try:
                    # 尝试批处理格式
                    try:
                        result = await method([text]) 
                    except:
                        # 回退单文本格式
                        result = await method(text)   

                    # 结果标准化
                    if result:
                        if isinstance(result, list) and len(result) > 0 and isinstance(result[0], list):
                            return result[0]
                        elif isinstance(result, list):
                            return result
                        elif hasattr(result, 'tolist'):
                            return result.tolist()
                            
                except Exception as e:
                    logger.debug(f"[Embedding] 尝试方法 {method_name} 失败: {e}")
                    continue
        
        logger.error(f"[Embedding] Provider 实例中未找到可用的 Embedding 方法")
        return None

    def _find_provider(self) -> Optional[Any]:
        """智能寻找 Provider：手动配置优先 -> 自动兜底"""
        
        # 1. 尝试手动配置
        if self.provider_id:
            # 尝试通过 get_provider_by_id 获取
            get_provider_func = getattr(self.context, 'get_provider_by_id', None)
            if get_provider_func:
                p = get_provider_func(self.provider_id)
                if p: return p
            
            # 尝试遍历获取
            if hasattr(self.context, 'get_all_embedding_providers'):
                for p in self.context.get_all_embedding_providers():
                    if getattr(p, 'id', '') == self.provider_id or getattr(getattr(p, 'meta', None), 'name', '') == self.provider_id:
                        return p
            
            logger.warning(f"[Embedding] 手动配置的 Provider '{self.provider_id}' 无效或未开启，尝试自动获取兜底...")

        # 2. 自动获取兜底逻辑
        if hasattr(self.context, 'get_all_embedding_providers'):
            providers = self.context.get_all_embedding_providers()
            if providers:
                p = providers[0]
                safe_id = getattr(p, 'id', getattr(getattr(p, 'meta', None), 'name', 'Unknown'))
                logger.info(f"[Embedding] 兜底策略：已自动选择系统中可用的 Embedding Provider: {safe_id}")
                return p
                
        return None

    @staticmethod
    def cosine_similarity(v1: List[float], v2: List[float]) -> float:
        """计算余弦相似度"""
        if not v1 or not v2 or len(v1) != len(v2):
            return 0.0
        a, b = np.array(v1), np.array(v2)
        norm_a = np.linalg.norm(a)
        norm_b = np.linalg.norm(b)
        if norm_a == 0 or norm_b == 0:
            return 0.0
        return float(np.dot(a, b) / (norm_a * norm_b))