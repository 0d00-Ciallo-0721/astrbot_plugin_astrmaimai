# heartflow/utils/api_utils.py
import json
from json.decoder import JSONDecodeError
from astrbot.api import logger
from astrbot.api.star import Context
# [新增] 导入 AstrBot 的消息段，用于构建 system prompt (如果 llm_generate 不直接支持 system_prompt 参数)
from astrbot.api.message_components import Plain

async def elastic_simple_text_chat(context: Context, provider_names: list[str], prompt: str, system_prompt: str = "") -> str | None:
    """
    (v4.12 适配) 弹性文本调用
    使用 context.llm_generate 替代 provider.text_chat 以支持 Hooks
    """
    if not provider_names:
        return None
    
    unique_names = list(dict.fromkeys(provider_names))
    last_error = "No models"

    # 构造上下文 (适配 system_prompt)
    contexts = []
    if system_prompt:
        # v4.12 推荐通过 contexts 传递 system prompt [cite: 45]
        # 或者某些版本的 llm_generate 支持 system_prompt 参数，这里使用更通用的 contexts 方式
        # 注意：具体取决于 LLM Provider 的实现，通常传入 role=system 的 dict 即可
        # 但 astrbot 的 contexts 通常是 list[MessageSegment] 或 dict。
        # 安全起见，我们传递 dict 列表，AstrBot 会自动解析。
        contexts.append({"role": "system", "content": system_prompt})

    for name in unique_names:
        try:
            # [核心修改] 使用 llm_generate 
            # 注意：llm_generate 会触发 on_llm_request 等钩子
            resp = await context.llm_generate(
                chat_provider_id=name,
                prompt=prompt,
                contexts=contexts 
            )
            
            if resp and resp.completion_text and resp.completion_text.strip():
                return resp.completion_text.strip()
            
            last_error = f"Model {name} returned empty"
            logger.warning(f"ElasticTextChat: {name} 返回空，切换下一模型")
            
        except Exception as e:
            last_error = str(e)
            logger.warning(f"ElasticTextChat: {name} 调用失败: {e}")
            continue

    logger.error(f"ElasticTextChat: 所有模型均失败。Last error: {last_error}")
    return None

async def elastic_json_chat(context: Context, provider_names: list[str], prompt: str, max_retries: int, system_prompt: str = "") -> dict | None:
    """
    (v4.12 适配) 弹性 JSON 调用
    """
    if not provider_names:
        return None

    unique_names = list(dict.fromkeys(provider_names))
    last_error = "No models"
    
    contexts = []
    if system_prompt:
        contexts.append({"role": "system", "content": system_prompt})

    for name in unique_names:
        # 检查模型是否存在 (可选，llm_generate 内部也会检查，但这能避免无效调用)
        # 注意：get_provider_by_id 在 v4.12 依然可用，但仅用于检查存在性
        if not context.get_provider_by_id(name):
             continue

        logger.debug(f"ElasticJsonChat: 尝试模型 {name}")

        for attempt in range(max_retries + 1):
            try:
                # [核心修改] 使用 llm_generate
                resp = await context.llm_generate(
                    chat_provider_id=name,
                    prompt=prompt,
                    contexts=contexts
                )
                
                content = resp.completion_text
                if not content or not content.strip():
                    raise ValueError("Empty response")

                # 清洗 Markdown 标记
                content = content.strip()
                if content.startswith("```json"): content = content[7:-3].strip()
                elif content.startswith("```"): content = content[3:-3].strip()

                return json.loads(content) # 成功返回

            except (json.JSONDecodeError, JSONDecodeError):
                logger.warning(f"ElasticJsonChat: {name} JSON解析失败 (Try {attempt+1})")
                if attempt == max_retries: break # 换模型
            except Exception as e:
                logger.warning(f"ElasticJsonChat: {name} 异常: {e}")
                last_error = str(e)
                break # 换模型 (API 错误通常不值得重试)

    logger.error(f"ElasticJsonChat: 所有模型均失败。Last error: {last_error}")
    return None