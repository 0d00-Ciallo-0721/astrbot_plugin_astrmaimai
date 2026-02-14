import json
import os
from typing import List, Dict
from astrbot.api import logger

class OptimizationService:
    """
    风格优化服务 (Evolution Backend)
    职责：记录并管理用户的负面反馈，用于 Few-Shot 修正。
    """
    
    def __init__(self, data_dir: str):
        self.data_dir = data_dir
        self.bad_cases_path = os.path.join(data_dir, "bad_cases.json")
        self._load_data()

    def _load_data(self):
        if not os.path.exists(self.bad_cases_path):
            self.bad_cases = []
        else:
            try:
                with open(self.bad_cases_path, "r", encoding="utf-8") as f:
                    self.bad_cases = json.load(f)
            except Exception:
                self.bad_cases = []

    def _save_data(self):
        try:
            with open(self.bad_cases_path, "w", encoding="utf-8") as f:
                json.dump(self.bad_cases, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"Failed to save bad cases: {e}")

    def record_negative_feedback(self, 
                               user_input: str, 
                               bot_reply: str, 
                               feedback: str):
        """
        记录一次负面反馈
        """
        record = {
            "user": user_input,
            "bot": bot_reply,
            "feedback": feedback, # 用户的批评，如"太啰嗦了"
            "timestamp": __import__("time").time()
        }
        self.bad_cases.append(record)
        # 保持最新的 50 条
        if len(self.bad_cases) > 50:
            self.bad_cases.pop(0)
        self._save_data()
        logger.info(f"🧬 [Evolution] Recorded negative feedback: {feedback}")

    def get_negative_examples_prompt(self, limit: int = 3) -> str:
        """
        获取最近的负面教材，用于 System Prompt 的 Negative Constraints
        """
        if not self.bad_cases:
            return ""
            
        examples = self.bad_cases[-limit:]
        prompt = "\n[Previous Mistakes to Avoid]\n"
        for ex in examples:
            prompt += f"- User said: '{ex['user']}'. I replied: '{ex['bot']}'. Feedback: '{ex['feedback']}'. AVOID THIS STYLE.\n"
        
        return prompt