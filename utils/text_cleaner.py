import re

class TextCleaner:
    """
    文本清洗工具 (v2.0)
    职责：清洗 LLM 输出的幻觉前缀、动作标记和格式噪音
    """

    @staticmethod
    def clean_reply(text: str, bot_name: str = "") -> str:
        """
        清洗回复内容
        """
        if not text: return ""
        
        # 1. 去除 "Assistant:" 或 "BotName:" 前缀
        # 匹配模式：名字后面跟着冒号或直接开始
        if bot_name:
            text = re.sub(f"^{re.escape(bot_name)}[：:]\\s*", "", text, flags=re.IGNORECASE)
        
        text = re.sub(r"^(Assistant|AI|Bot)[：:]\s*", "", text, flags=re.IGNORECASE)
        
        # 2. 去除首尾引号 (常见于 LLM 输出)
        text = text.strip().strip('"').strip("'")
        
        # 3. 去除可能的时间戳幻觉 [12:00]
        text = re.sub(r"^\[\d{2}:\d{2}(:\d{2})?\]\s*", "", text)
        
        return text.strip()

    @staticmethod
    def extract_actions(text: str) -> list:
        """
        提取动作标记 (HeartCore 2.0)
        例如: (poke), (sigh), (wav)
        """
        # 匹配英文或中文括号内的特定关键词
        # 关键词列表应与 Prompt 中的 Instruction 保持一致
        pattern = r'[\(\（](poke|戳一戳|sigh|叹气|wink|眨眼|facepalm|扶额)[\)\）]'
        return re.findall(pattern, text, flags=re.IGNORECASE)

    @staticmethod
    def remove_actions(text: str) -> str:
        """
        移除动作标记，仅保留纯文本用于发送
        """
        pattern = r'[\(\（](poke|戳一戳|sigh|叹气|wink|眨眼|facepalm|扶额)[\)\）]'
        return re.sub(pattern, "", text, flags=re.IGNORECASE).strip()