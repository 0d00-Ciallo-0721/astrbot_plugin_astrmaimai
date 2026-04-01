# astrmai/work/subagents/cron_agent.py
from typing import Any
from pydantic.dataclasses import dataclass
from astrbot.core.agent.tool import ToolSet, ToolExecResult
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.astr_agent_context import AstrAgentContext
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

    # 🟢 [新增] 数据库服务注入点，供双写使用
    db_service: Any = None

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

    # ─── 🟢 [新增] 覆写 call：拦截执行后生命周期触发双写 ──────────────

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        # 1. 正常执行父类的 ReAct 循环（LLM 在此期间调用框架内置的增删查工具）
        result = await super().call(context, **kwargs)

        # 2. 循环结束后，立刻进行双写同步
        try:
            await self._sync_dual_write(context)
        except Exception as e:
            logger.error(f"[Sys3/CronAgent] ⚠️ 定时任务双写快照同步失败: {e}")

        return result

    # ─── 🟢 [新增] 执行正反向快照对齐 ──────────────────────────────────

    async def _sync_dual_write(self, context: ContextWrapper[AstrAgentContext]):
        """
        增量对齐策略（AOP 切面）：
        在 CronAgent ReAct 循环结束后，比对框架内存调度器与本地 SQLite，
        正向同步新创建的任务，逆向清理被 LLM 删除的任务。
        """
        if not self.db_service:
            return

        ctx = context.context.context
        event = context.context.event
        cron_mgr = getattr(ctx, "cron_manager", None)

        if not cron_mgr:
            return

        import time
        import json
        from ...infra.datamodels import CronSnapshot

        # 获取框架内存中当前所有的活跃任务
        active_jobs = await cron_mgr.list_jobs()
        current_umo = str(event.unified_msg_origin)

        synced_count = 0
        active_job_ids = set()

        # ── 动作 1：正向同步 (将内存中属于当前会话的 Job 更新至 SQLite) ──
        for job in active_jobs:
            payload = getattr(job, "payload", {}) or {}
            session = str(payload.get("session", ""))

            # 仅处理当前会话的任务
            if session != current_umo:
                continue

            job_id = str(getattr(job, "id", getattr(job, "job_id", "")))
            if not job_id:
                continue

            active_job_ids.add(job_id)

            run_at_dt = getattr(job, "run_at", None)
            run_at_ts = run_at_dt.timestamp() if run_at_dt else None

            snapshot = CronSnapshot(
                job_id=job_id,
                name=getattr(job, "name", ""),
                cron_expression=getattr(job, "cron_expression", None),
                run_at=run_at_ts,
                run_once=getattr(job, "run_once", False),
                target_origin=session,
                payload=json.dumps(payload, ensure_ascii=False),
                note="由 CronAgent 任务执行后双写注入",
                is_active=True,
                updated_at=time.time()
            )
            await self.db_service.save_cron_snapshot(snapshot)
            synced_count += 1

        # ── 动作 2：逆向清理 (将在框架中被 LLM 删除的 Job 在快照中标记为失效) ──
        db_snapshots = await self.db_service.get_all_active_cron_snapshots()
        cleaned_count = 0
        for snap in db_snapshots:
            if snap.target_origin == current_umo and snap.job_id not in active_job_ids:
                await self.db_service.deactivate_cron_snapshot(snap.job_id)
                cleaned_count += 1

        if synced_count > 0 or cleaned_count > 0:
            logger.info(f"[Sys3/CronAgent] 💾 智能体挂起前快照同步完成：新增/更新 {synced_count} 个，清理 {cleaned_count} 个。")