"""
Sys3Router — SubAgent 路由注册中心 (满血动态版)

核心职责：
1. 静态注册核心 SubAgent (CronAgent, ComputerAgent)
2. 【核心升级】动态嗅探并挂载 WebUI 中配置的所有扩展智能体 (HandoffTool)
3. 为 System 2 主脑的 Planner 提供轻量级工具索引 (Context Compression)
4. 为纯任务指令提供全量工具集暴露
"""
from astrbot.core.agent.tool import ToolSet
from astrbot.api import logger
from .subagents.cron_agent import CronAgent
from .subagents.computer_agent import ComputerAgent

class Sys3Router:
    """SubAgent 路由注册中心 (满血动态版)"""

    def __init__(self, plugin_config, context, db_service=None):
        self.plugin_config = plugin_config
        self.context = context
        self.db_service = db_service  # 🟢 [新增] 挂载数据库服务
        
        # 静态内置 SubAgent 挂载池
        self._static_agents = [
            CronAgent(db_service=self.db_service),  # 🟢 [修改] 注入给需要持久化的子智能体
            ComputerAgent(),
        ]
        self._dynamic_agents = []
        self._dynamic_loaded = False
        logger.info("[Sys3Router] 🚦 Sys3 路由中枢已初始化，准备进行动态嗅探。")

    async def _try_load_dynamic_agents(self):
        """【核心突破】深度嗅探 SubAgentOrchestrator，实现 WebUI 动态智能体满血挂载"""
        if self._dynamic_loaded:
            return
        self._dynamic_loaded = True
        
        try:
            # 1. 寻址探针发现的 Orchestrator
            subagent_orch = getattr(self.context, "subagent_orchestrator", None)
            if not subagent_orch:
                pm = getattr(self.context, "provider_manager", None)
                if pm:
                    subagent_orch = getattr(pm, "subagent_orchestrator", None)
            
            if not subagent_orch or not hasattr(subagent_orch, "handoffs"):
                logger.warning("[Sys3Router] ⚠️ 未找到 subagent_orchestrator，保持纯静态模式。")
                return
            
            handoffs = subagent_orch.handoffs
            if not handoffs:
                return
            
            static_names = {a.name for a in self._static_agents}
            
            # 2. 遍历并挂载 WebUI 中的 HandoffTools
            for handoff in handoffs:
                agent_name = getattr(handoff, "name", "")
                if not agent_name or agent_name in static_names:
                    continue
                
                # 直接将框架原生的 HandoffTool 作为我们的 SubAgent 挂载，
                # 这样可以 100% 继承其在面板中配置的独立提示词与工具链。
                self._dynamic_agents.append(handoff)
                
                provider = getattr(handoff, "provider_id", "跟随全局")
                logger.info(f"[Sys3Router] 🔌 满血挂载 WebUI 动态 SubAgent: [{agent_name}] | 独立 Provider: [{provider}]")
                
        except Exception as e:
            logger.warning(f"[Sys3Router] 动态 SubAgent 加载失败（已无损降级）: {e}")

    async def get_all_agents(self) -> list:
        """获取当前系统中挂载的所有 SubAgent（自动拉起动态嗅探）"""
        await self._try_load_dynamic_agents()
        return self._static_agents + self._dynamic_agents

    async def get_light_tools_for_planner(self) -> ToolSet:
        """供给 Planner 使用：仅返回包含 name + description 的轻量级工具索引。"""
        all_agents = await self.get_all_agents()
        full_set = ToolSet(all_agents)
        return full_set.get_light_tool_set()

    async def get_full_tools_for_direct_entry(self) -> ToolSet:
        """供给 /work 直通命令使用：返回挂载了完整 Schema 与运行逻辑的全量工具集"""
        all_agents = await self.get_all_agents()
        return ToolSet(all_agents)

    def get_static_agent_names(self) -> list[str]:
        return [a.name for a in self._static_agents]