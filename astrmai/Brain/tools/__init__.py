from astrbot.api.event import AstrMessageEvent
from astrbot.api.all import Context

class ToolRegistry:
    """
    工具注册表 (System 2)
    负责管理和发现 AstrBot 中注册的所有 Tools
    """
    def __init__(self, context: Context):
        self.context = context

    def get_all_tool_descs(self) -> list:
        """获取所有可用工具的描述 (用于 Prompt)"""
        # AstrBot v4.12: context.get_all_tools() 或 provider_manager
        # 这里假设从 context 获取，具体需适配 API
        tools = []
        # TODO: 适配 AstrBot 工具列表获取逻辑
        return tools