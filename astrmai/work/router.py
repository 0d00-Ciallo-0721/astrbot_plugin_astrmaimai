"""
Sys3Router — SubAgent 路由注册中心 (安全降级版)

基于探针结果：由于当前 AstrBot 框架版本未暴露出 WebUI 的 SubAgent 动态配置 API，
本路由中枢已采用纯静态路由模式。
核心职责：
1. 静态注册核心 SubAgent (CronAgent, ComputerAgent)
2. 为 System 2 主脑的 Planner 提供轻量级工具索引 (Context Compression)
3. 为纯任务指令提供全量工具集暴露
"""
from astrbot.core.agent.tool import ToolSet
from astrbot.api import logger
from .subagents.cron_agent import CronAgent
from .subagents.computer_agent import ComputerAgent

class Sys3Router:
    """SubAgent 路由注册中心 (纯静态坚固版)"""

    def __init__(self, plugin_config, context):
        self.plugin_config = plugin_config
        self.context = context
        
        # 静态内置 SubAgent 挂载池
        self._static_agents = [
            CronAgent(),
            ComputerAgent(),
        ]
        logger.info("[Sys3Router] 🚦 已加载纯静态 SubAgent 路由模式。")

    async def get_all_agents(self) -> list:
        """获取当前系统中挂载的所有 SubAgent"""
        return self._static_agents

    async def get_light_tools_for_planner(self) -> ToolSet:
        """
        【核心上下文压缩机制】
        供给 Planner 使用：仅返回包含 name + description 的轻量级工具索引。
        确保 Main Agent 在判断意图时，不会被庞大的 parameters JSON schema 撑爆上下文 Token。
        """
        full_set = ToolSet(self._static_agents)
        return full_set.get_light_tool_set()

    async def get_full_tools_for_direct_entry(self) -> ToolSet:
        """供给 /work 直通命令使用：返回挂载了完整 Schema 与运行逻辑的全量工具集"""
        return ToolSet(self._static_agents)

    def get_static_agent_names(self) -> list[str]:
        """获取子智能体名称列表（供日志与调试态查阅）"""
        return [a.name for a in self._static_agents]