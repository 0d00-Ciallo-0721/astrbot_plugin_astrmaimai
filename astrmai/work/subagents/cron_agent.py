# astrmai/sys3/subagents/cron_agent.py
from pydantic.dataclasses import dataclass
from astrbot.core.agent.tool import ToolSet
from astrbot.api import logger
from .base_agent import AstrMaiBaseSubAgent

# 尝试从框架导入内置 Cron 工具
try:
    from astrbot.core.tools.cron_tools import (
        CREATE_CRON_JOB_TOOL,
        DELETE_CRON_JOB_TOOL,
        LIST_CRON_JOBS_TOOL,
    )
    _CRON_TOOLS_AVAILABLE = True
    logger.info("[Sys3/CronAgent] ✅ 框架内置 CronTools 加载成功")
except ImportError:
    _CRON_TOOLS_AVAILABLE = False
    logger.warning("[Sys3/CronAgent] ⚠️ 框架内置 CronTools 不可用，Cron 功能降级")


@dataclass
class CronAgent(AstrMaiBaseSubAgent):
    """
    定时任务与未来计划管理子智能体
    能力：创建、查询、删除定时提醒与周期性任务
    """
    name: str = "transfer_to_cron"
    description: str = (
        "定时任务与未来计划管理专家。"
        "当用户需要在未来某个时间点执行某事、设置重复提醒或计划任务时使用。"
        "例如：'明天早8点提醒我开会'、'每周五下午5点总结工作'、'10分钟后提醒我喝水'。"
    )

    def get_max_steps(self) -> int:
        return 8  # Cron 任务逻辑简单，无需太多步骤

    async def get_system_prompt(self, ctx, event) -> str:
        return (
            "你是一位专业的时间管理助手，专门帮助用户设置和管理定时提醒。\n\n"
            "执行规范：\n"
            "1. 创建任务时，cron_expression 使用标准 5 段格式（分 时 日 月 周），如 '0 8 * * *' 表示每天早8点\n"
            "2. 一次性任务使用 run_at 参数（ISO 8601 格式，含时区）并设 run_once=true\n"
            "3. note 字段必填，用自然语言描述任务内容\n"
            "4. 任务创建成功后，用自然语言确认：时间、频率、具体内容三要素\n"
            "5. 如果用户说的时间模糊（如'等会儿'），礼貌地请求明确时间"
        )

    async def get_tool_set(self, ctx, event) -> ToolSet:
        if not _CRON_TOOLS_AVAILABLE:
            return ToolSet([])  # 触发 base_agent 的优雅退回
        return ToolSet([
            CREATE_CRON_JOB_TOOL,
            DELETE_CRON_JOB_TOOL,
            LIST_CRON_JOBS_TOOL,
        ])

    async def _get_decline_reason(self) -> str:
        return "框架定时任务工具 (cron_tools) 未能成功加载，请检查 AstrBot 版本是否 ≥ v4.14.0"