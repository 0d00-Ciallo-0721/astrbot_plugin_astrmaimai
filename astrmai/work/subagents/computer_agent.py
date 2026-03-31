# astrmai/sys3/subagents/computer_agent.py
from pydantic.dataclasses import dataclass
from astrbot.core.agent.tool import ToolSet
from astrbot.api import logger
from .base_agent import AstrMaiBaseSubAgent

# 尝试导入代码执行工具
try:
    from astrbot.core.computer.tools.python import LocalPythonTool
    from astrbot.core.computer.tools.shell import ExecuteShellTool
    _COMPUTER_TOOLS_AVAILABLE = True
    logger.info("[Sys3/ComputerAgent] ✅ 计算机工具加载成功")
except ImportError:
    _COMPUTER_TOOLS_AVAILABLE = False
    logger.warning("[Sys3/ComputerAgent] ⚠️ 计算机工具不可用，Computer 功能降级")


@dataclass 
class ComputerAgent(AstrMaiBaseSubAgent):
    """
    代码执行与系统操作子智能体
    能力：执行 Python 代码、Shell 命令（需要管理员权限）
    """
    name: str = "transfer_to_computer"
    description: str = (
        "代码执行与系统操作专家。"
        "当用户需要执行 Python 代码、运行系统命令、计算复杂数值或处理文件时使用。"
        "注意：此功能需要管理员权限。"
    )

    def get_max_steps(self) -> int:
        return 15  # 代码执行可能需要多步调试

    def get_timeout(self) -> int:
        return 120  # 代码执行超时时间较长

    async def get_system_prompt(self, ctx, event) -> str:
        # 获取当前用户是否为管理员（影响工具权限）
        sender_id = str(event.get_sender_id())
        return (
            f"你是一位专业的代码执行助手。当前操作者 ID: {sender_id}。\n\n"
            "执行规范：\n"
            "1. 执行前先分析需求，确认代码安全无副作用\n"
            "2. 不执行删除系统文件、发送网络请求到未知域名等高危操作\n"
            "3. 代码报错时，分析原因后修改重试（最多 3 次）\n"
            "4. 最终向用户汇报：执行结果 + 输出内容摘要\n"
            "5. 如果权限不足，如实告知用户并建议联系管理员"
        )

    async def get_tool_set(self, ctx, event) -> ToolSet:
        if not _COMPUTER_TOOLS_AVAILABLE:
            return ToolSet([])
        return ToolSet([
            LocalPythonTool(),
            ExecuteShellTool(is_local=True),
        ])

    async def _get_decline_reason(self) -> str:
        return "代码执行工具未能加载，请确认 AstrBot 计算环境配置正确，或考虑改用沙盒模式"