### 📄 core/goals.py
import time
from typing import List, Dict, Optional
from ..datamodels import Goal

class GoalStateMachine:
    """
    目标状态机 (Goal State Machine)
    维护当前对话的短期目标列表。
    """
    def __init__(self, goals: List[Goal] = None):
        self.goals = goals or []

    def get_active_goals(self) -> List[Goal]:
        """获取当前活跃目标"""
        return [g for g in self.goals if g.status == "active"]

    def get_goals_description(self) -> str:
        """获取目标的文本描述（供 Prompt 使用）"""
        active_goals = self.get_active_goals()
        if not active_goals:
            return "无明确目标 (No specific goal)"
        
        desc_list = [f"- {g.description}" for g in active_goals]
        return "\n".join(desc_list)

    def update_goals(self, updates: List[Dict]) -> List[Goal]:
        """
        根据 LLM 的决策更新目标，并返回更新后的列表
        updates 结构示例: 
        [
            {"action": "add", "description": "安慰用户"}, 
            {"action": "complete", "description": "打招呼"},
            {"action": "clear"} 
        ]
        """
        if not updates:
            return self.goals

        for op in updates:
            action = op.get("action")
            description = op.get("description")
            
            if action == "add" and description:
                # 查重
                if not any(g.description == description and g.status == "active" for g in self.goals):
                    new_goal = Goal(
                        id=f"g_{int(time.time())}_{len(self.goals)}",
                        description=description
                    )
                    self.goals.append(new_goal)
            
            elif action == "complete" or action == "remove":
                # 模糊匹配描述
                for g in self.goals:
                    if description and description in g.description:
                        g.status = "completed" if action == "complete" else "failed"
            
            elif action == "clear":
                for g in self.goals:
                    g.status = "completed"
        
        return self.goals