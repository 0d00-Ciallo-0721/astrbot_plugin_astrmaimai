import ast
import json
import re
from typing import Tuple
from astrbot.api import logger
from ..infra.gateway import GlobalModelGateway

class MoodManager:
    """
    情绪管理器 (System 1)
    职责: 调用 LLM 分析文本对机器人的情绪影响，输出情绪标签与数值变化。
    Reference: HeartFlow/core/mood_manager.py
    """
    def __init__(self, gateway: GlobalModelGateway, config=None):
        self.gateway = gateway
        self.config = config if config else gateway.config
        
        self.emotion_mapping = {}
        
        # [修改] 将配置中的 List 动态解析为字典，兼容中英文冒号
        if hasattr(self.config, 'reply') and hasattr(self.config.reply, 'emotion_mapping'):
            mapping_list = self.config.reply.emotion_mapping
            for item in mapping_list:
                if ":" in item:
                    k, v = item.split(":", 1)
                    self.emotion_mapping[k.strip()] = v.strip()
                elif "：" in item: # 兼容全角冒号
                    k, v = item.split("：", 1)
                    self.emotion_mapping[k.strip()] = v.strip()

        # 兜底默认值
        if not self.emotion_mapping:
            self.emotion_mapping = {
                "happy": "积极、开心、感谢",
                "sad": "悲伤、遗憾、道歉",
                "angry": "生气、抱怨、攻击",
                "neutral": "平静、客观、陈述",
                "curious": "好奇、提问、困惑",
                "surprise": "惊讶、意外"
            }
    async def analyze_mood(self, text: str, current_mood: float, user_affection: float = 0.0) -> Tuple[str, float]:
        """
        [修改] 分析情绪时，引入当前用户的好感度 (Affection) 维度，模拟更真实的复合心理状态。
        同时增强对 AI 异常输出格式的极端容错解析能力。
        """
        if not text or len(text) < 2:
            return "neutral", current_mood

        # 构造 Prompt
        mapping_desc = "\n".join([f"- {k}: {v}" for k, v in self.emotion_mapping.items()])
        prompt = f"""
你的任务是分析[待分析文本]，并评估它对“我”（你自己）的情绪影响。

[我的当前情绪]
{current_mood:.2f} (范围 -1.0[极度沮丧] ~ 1.0[极度开心]，0.0 为平静)

[当前用户对我的好感度]
{user_affection:.2f} (范围 -100 ~ 100。正数代表关系亲密，负数代表关系恶劣)
*提示：对于高好感度用户，你可以表现得更包容、更易开心；对于低好感度用户，你的情绪更容易受负面影响。*

[可用情绪标签]
{mapping_desc}

[待分析文本]
"{text}"

[任务]
请基于当前情绪和用户好感度，综合分析这段文本会让我产生什么感觉？
返回一个 JSON，包含：
1. "mood_tag": (字符串) 从可用标签中选一个最匹配的。
2. "mood_value": (浮点数) 计算新的情绪值 (-1.0 到 1.0)。
   - 夸奖/感谢/有趣 -> 升高
   - 辱骂/指责/悲伤 -> 降低
   - 平常对话 -> 向 0.0 缓慢回归 (衰减)

严格返回 JSON: {{"mood_tag": "string", "mood_value": float}}
"""
        try:
            # 使用专属的情绪分析任务接口
            result = await self.gateway.call_mood_task(prompt, system_prompt="你是一个具备丰富情感和社交包容度的专业分析师。")
            
            data = {}
            # 1. 理想情况：网关已经直接返回了字典对象
            if isinstance(result, dict):
                data = result
            else:
                raw_str = str(result).strip()
                
                # 预处理：清洗掉可能存在的 markdown 代码块符号
                clean_str = re.sub(r'```(?:json)?', '', raw_str, flags=re.IGNORECASE).strip()

                parsed_successfully = False
                
                # 2. 尝试提取 {} 或 [] 内的结构化内容 (应对带有 "回答是：" 前缀的情况)
                match = re.search(r'(\{.*\}|\[.*\])', clean_str, re.DOTALL)
                if match:
                    json_str = match.group(1)
                    try:
                        # 尝试标准 JSON 解析
                        parsed_data = json.loads(json_str)
                        # 如果 AI 输出的是 [{...}] 格式，提取列表中的第一个字典
                        if isinstance(parsed_data, list) and len(parsed_data) > 0 and isinstance(parsed_data[0], dict):
                            data = parsed_data[0]
                        elif isinstance(parsed_data, dict):
                            data = parsed_data
                        
                        if data:
                            parsed_successfully = True
                    except json.JSONDecodeError as e:
                        logger.debug(f"[MoodManager] 标准 JSON 解析失败，尝试 AST 容错解析: {e}")
                        try:
                            # 尝试 AST 解析 (应对使用了单引号 {'mood_tag': 'happy'} 的脏数据)
                            eval_data = ast.literal_eval(json_str)
                            if isinstance(eval_data, list) and len(eval_data) > 0 and isinstance(eval_data[0], dict):
                                data = eval_data[0]
                            elif isinstance(eval_data, dict):
                                data = eval_data
                                
                            if data:
                                parsed_successfully = True
                        except Exception:
                            pass
                
                # 3. 终极降级防线：正则暴力键值对提取
                # 应对格式彻底损坏、或者被包裹在圆括号 (mood_tag: happy) 中的情况
                if not parsed_successfully or ("mood_tag" not in data and "mood_value" not in data):
                    logger.debug(f"[MoodManager] 结构化解析均失败，启动正则暴力提取: {raw_str[:50]}...")
                    
                    # 匹配 mood_tag，支持冒号、等号，以及是否带有引号
                    tag_match = re.search(r'(?:"|\')?mood_tag(?:"|\')?\s*[:：=]\s*(?:"|\')?([a-zA-Z0-9_]+)(?:"|\')?', clean_str, re.IGNORECASE)
                    if tag_match:
                        data["mood_tag"] = tag_match.group(1).lower()

                    # 匹配 mood_value，提取浮点数或整数
                    val_match = re.search(r'(?:"|\')?mood_value(?:"|\')?\s*[:：=]\s*([-+]?\d*\.?\d+)', clean_str, re.IGNORECASE)
                    if val_match:
                        try:
                            data["mood_value"] = float(val_match.group(1))
                        except ValueError:
                            pass

            # 取值并应用兜底默认值
            mood_tag = data.get("mood_tag", "neutral")
            mood_value = float(data.get("mood_value", current_mood))
            
            # 严格限幅
            mood_value = max(-1.0, min(1.0, mood_value))
            return mood_tag, mood_value
            
        except Exception as e:
            logger.warning(f"[MoodManager] ⚠️ LLM 情绪分析彻底失败，触发本地算法降级防线。原因: {e}")
            
            # 🟢 [增强版] 情感降级算法，扩充口语词库
            fallback_text = text.lower()
            if any(w in fallback_text for w in ["哈哈", "嘿嘿", "贴贴", "喜欢", "好棒", "谢谢", "开心", "做饭", "喵", "来啦"]):
                return "happy", min(1.0, current_mood + 0.1)
            elif any(w in fallback_text for w in ["滚", "气死", "烦", "闭嘴", "傻", "笨", "打你", "饿"]):
                return "angry", max(-1.0, current_mood - 0.2)
            elif any(w in fallback_text for w in ["呜呜", "难过", "惨", "抱歉", "对不起", "唉", "叹气"]):
                return "sad", max(-1.0, current_mood - 0.1)
            elif any(w in fallback_text for w in ["?", "？", "啊", "怎么会", "啥", "什么"]):
                return "surprise", current_mood
                
            return "neutral", current_mood

    async def analyze_text_mood(self, text: str, current_mood: float, user_affection: float = 0.0) -> Tuple[str, float]:
        return await self.analyze_mood(text, current_mood, user_affection=user_affection)
