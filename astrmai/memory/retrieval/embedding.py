import numpy as np
import asyncio
import inspect
from typing import List, Optional, Any
from astrbot.api.star import Context
from astrbot.api import logger


def normalize_embedding_result(result: Any) -> Optional[List[float]]:
    if result is None:
        return None
    if isinstance(result, dict):
        for key in ("embedding", "embeddings", "vector", "data"):
            if key in result:
                result = result[key]
                break
    if isinstance(result, (list, tuple)) and result and isinstance(result[0], (list, tuple)):
        result = result[0]
    if hasattr(result, "tolist") and not isinstance(result, list):
        result = result.tolist()
    if isinstance(result, (list, tuple)) and result and isinstance(result[0], (list, tuple)):
        result = result[0]
    if not isinstance(result, (list, tuple)):
        return None
    values = list(result)
    array = np.asarray(values, dtype=np.float32)
    if array.ndim != 1 or array.size <= 0 or not np.isfinite(array).all():
        return None
    return [float(value) for value in array]


async def invoke_embedding(provider: Any, text: str, *, timeout_sec: float = 30.0) -> List[float]:
    """Call one embedding provider across AstrBot's supported API shapes."""
    methods = ("get_embeddings", "embeddings", "text_embedding", "get_embedding", "embedding")
    timeout = max(0.1, float(timeout_sec or 30.0))
    deadline = asyncio.get_running_loop().time() + timeout
    last_error: Exception | None = None
    for method_name in methods:
        method = getattr(provider, method_name, None)
        if not callable(method):
            continue
        for payload in ([text], text):
            try:
                async def _call() -> Any:
                    is_async = inspect.iscoroutinefunction(method) or inspect.iscoroutinefunction(
                        getattr(method, "__call__", None)
                    )
                    if is_async:
                        result = method(payload)
                    else:
                        result = await asyncio.to_thread(method, payload)
                    if inspect.isawaitable(result):
                        return await result
                    return result

                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0.0:
                    raise asyncio.TimeoutError
                result = await asyncio.wait_for(_call(), timeout=remaining)
                vector = normalize_embedding_result(result)
                if vector is not None:
                    return vector
                last_error = ValueError("invalid_embedding")
            except asyncio.TimeoutError as exc:
                last_error = exc
                break
            except Exception as exc:
                last_error = exc
                continue
        if isinstance(last_error, asyncio.TimeoutError):
            break
    raise RuntimeError(
        f"embedding provider call failed: {type(last_error).__name__}: {last_error!r}"
        if last_error is not None
        else "embedding provider method unavailable"
    )

class EmbeddingClient:
    """
    标准化的 Embedding 客户端
    支持手动配置优先 + 自动获取兜底的寻找策略。
    """
    def __init__(self, context: Context, embedding_models: list = None, config=None):
        self.context = context
        # 使用 embedding_models 列表替代原先的单字符串 provider_id
        self.embedding_models = embedding_models or []
        timing = getattr(config, "timing", None)
        try:
            configured_timeout = getattr(timing, "embedding_timeout_sec", 30.0) or 30.0
            self._timeout_sec = max(0.1, float(configured_timeout))
        except (TypeError, ValueError):
            self._timeout_sec = 30.0
        # 🟢 引入独立的轮询指针状态
        self._cursor = 0

    async def get_vector(self, text: str) -> Optional[List[float]]:
        """获取文本的 Embedding 向量 (实现独立池重试与轮询切换机制)"""
        clean_models = [m.strip() for m in self.embedding_models if m and m.strip()]
        unique_models = list(dict.fromkeys(clean_models))
        # 决定最大尝试次数，若为空则为1(给自动兜底一次机会)
        max_attempts = len(unique_models) if unique_models else 1
        
        for attempt in range(max_attempts):
            provider = self._find_provider()
            if not provider:
                continue

            try:
                return await invoke_embedding(
                    provider,
                    text,
                    timeout_sec=self._timeout_sec,
                )
            except Exception as e:
                logger.debug(
                    f"[Embedding] Provider 调用失败: "
                    f"{type(e).__name__}: {e!r}"
                )
                continue
        
        # 严格确保它失败后直接 return None，不与 LLM 总模型池 (fallback_models) 产生任何交集
        logger.error("[Embedding] 🚨 所有可用的 Embedding 模型尝试失败，已放弃向量化。")
        return None

    def _find_provider(self) -> Optional[Any]:
        """智能寻找 Provider：状态轮询手动配置 -> 自动兜底"""
        clean_models = [m.strip() for m in self.embedding_models if m and m.strip()]
        
        if clean_models:
            unique_models = list(dict.fromkeys(clean_models))
            # 指针超限归零
            if self._cursor >= len(unique_models):
                self._cursor = 0
                
            provider_id = unique_models[self._cursor]
            # 推进轮询指针
            self._cursor = (self._cursor + 1) % len(unique_models)
            
            # 尝试通过 get_provider_by_id 获取
            get_provider_func = getattr(self.context, 'get_provider_by_id', None)
            if get_provider_func:
                p = get_provider_func(provider_id)
                if p: return p
            
            # 尝试遍历获取
            if hasattr(self.context, 'get_all_embedding_providers'):
                for p in self.context.get_all_embedding_providers():
                    if getattr(p, 'id', '') == provider_id or getattr(getattr(p, 'meta', None), 'name', '') == provider_id:
                        return p
            
            logger.warning(f"[Embedding] 轮询到的 Provider '{provider_id}' 无效或未开启。")
            return None

        # 2. 自动获取兜底逻辑 (仅在用户完全未配置 embedding_models 时触发)
        if hasattr(self.context, 'get_all_embedding_providers'):
            providers = self.context.get_all_embedding_providers()
            if providers:
                p = providers[0]
                safe_id = getattr(p, 'id', None) or getattr(getattr(p, 'meta', None), 'name', 'Unknown')
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
