### 📄 core/goals.py
# heartflow/core/goals.py
# (HeartCore 2.0 - Goal State Machine)

import time
from typing import List, Dict, Optional
from ..datamodels import Goal

class GoalStateMachine:
    """
    目标状态机 (Goal State Machine)
    维护当前对话的短期目标列表（如：'安抚用户', '询问详情', '结束话题'）。
    """
    def __init__(self):
        self.goals: List[Goal] = []

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

    def update_goals(self, updates: List[Dict]):
        """
        根据 LLM 的决策更新目标
        updates 结构示例: 
        [
            {"action": "add", "description": "安慰用户"}, 
            {"action": "complete", "id": "goal_1"},
            {"action": "clear"} 
        ]
        """
        for op in updates:
            action = op.get("action")
            
            if action == "add":
                description = op.get("description")
                if description:
                    new_goal = Goal(
                        id=f"g_{int(time.time())}_{len(self.goals)}",
                        description=description
                    )
                    self.goals.append(new_goal)
            
            elif action == "complete" or action == "remove":
                # 简单实现：通过描述或ID匹配（LLM通常更擅长按描述操作）
                target_desc = op.get("description")
                target_id = op.get("id")
                
                for g in self.goals:
                    if (target_id and g.id == target_id) or \
                       (target_desc and target_desc in g.description):
                        g.status = "completed" if action == "complete" else "failed"
            
            elif action == "clear":
                # 结束话题时清空所有目标
                for g in self.goals:
                    g.status = "completed"

    def clear_all(self):
        self.goals.clear()