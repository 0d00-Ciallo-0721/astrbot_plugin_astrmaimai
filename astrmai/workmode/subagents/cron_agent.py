import json
from datetime import datetime
from typing import Any

from pydantic.dataclasses import dataclass

from astrbot.api import logger
from astrbot.core.agent.run_context import ContextWrapper
from astrbot.core.agent.tool import ToolExecResult, ToolSet
from astrbot.core.astr_agent_context import AstrAgentContext

from .base_agent import AstrMaiBaseSubAgent

try:
    from astrbot.core.tools.cron_tools import (
        CREATE_CRON_JOB_TOOL,
        DELETE_CRON_JOB_TOOL,
        LIST_CRON_JOBS_TOOL,
    )
    _CRON_TOOLS_AVAILABLE = True
    logger.info("[Sys3/CronAgent] 框架内置 CronTools 加载成功")
except ImportError:
    _CRON_TOOLS_AVAILABLE = False
    logger.warning("[Sys3/CronAgent] 框架内置 CronTools 不可用，Cron 功能降级")


@dataclass
class CronAgent(AstrMaiBaseSubAgent):
    """定时任务与未来计划管理子智能体。"""

    name: str = "transfer_to_cron"
    description: str = (
        "定时任务与未来计划管理专家。"
        "当用户需要在未来某个时间点执行某事、设置重复提醒或计划任务时使用。"
        "例如：‘明天 8 点提醒我开会’、‘每周五下午 6 点总结工作’、‘10 分钟后提醒我喝水’。"
    )
    db_service: Any = None

    def get_max_steps(self) -> int:
        return 8

    async def get_system_prompt(self, ctx, event) -> str:
        return (
            "你是一位专业的时间管理助手，专门帮助用户设置和管理定时提醒。\n\n"
            "执行规范：\n"
            "1. 创建任务时，cron_expression 使用标准 5 段格式（分 时 日 月 周），如 '0 8 * * *' 表示每天 8 点\n"
            "2. 一次性任务使用 run_at 参数（ISO 8601 格式，含时区）并设 run_once=true\n"
            "3. note 字段必填，用自然语言描述任务内容\n"
            "4. 任务创建成功后，用自然语言确认：时间、频率、具体内容三要素\n"
            "5. 如果用户说的时间模糊（如‘等会儿’），礼貌地请求明确时间"
        )

    async def get_tool_set(self, ctx, event) -> ToolSet:
        if not _CRON_TOOLS_AVAILABLE:
            return ToolSet([])
        return ToolSet([
            CREATE_CRON_JOB_TOOL,
            DELETE_CRON_JOB_TOOL,
            LIST_CRON_JOBS_TOOL,
        ])

    async def _get_decline_reason(self) -> str:
        return "框架定时任务工具 (cron_tools) 未能成功加载，请检查 AstrBot 版本是否 >= v4.14.0"

    async def call(self, context: ContextWrapper[AstrAgentContext], **kwargs) -> ToolExecResult:
        result = await super().call(context, **kwargs)
        try:
            await self._sync_dual_write(context)
        except Exception as exc:
            logger.error(f"[Sys3/CronAgent] 定时任务双写快照同步失败: {exc}")
        return result

    async def _sync_dual_write(self, context: ContextWrapper[AstrAgentContext]):
        if not self.db_service:
            return

        astr_agent_ctx = context.context
        if astr_agent_ctx is None:
            logger.error("[AstrMai-cron] ContextWrapper.context is None; AstrBot API may have changed")
            return
        ctx = astr_agent_ctx.context
        event = astr_agent_ctx.event
        if ctx is None or event is None:
            logger.error(f"[AstrMai-cron] astr_agent_ctx.context={ctx}, .event={event}")
            return
        cron_mgr = getattr(ctx, "cron_manager", None)
        if not cron_mgr:
            return

        import time

        try:
            from ...infrastructure.persistence.orm_models import CronSnapshot
        except Exception:
            logger.exception("[AstrMai-cron] CronSnapshot import failed", exc_info=True)
            CronSnapshot = None

        if CronSnapshot is None:
            return

        active_jobs = await cron_mgr.list_jobs()
        current_umo = str(event.unified_msg_origin)
        synced_count = 0
        active_job_ids = set()

        for job in active_jobs:
            payload = getattr(job, "payload", {}) or {}
            if isinstance(payload, str):
                try:
                    payload = json.loads(payload)
                except (json.JSONDecodeError, TypeError):
                    payload = {}
            if not isinstance(payload, dict):
                payload = {}
            session = str(payload.get("session", ""))
            if session != current_umo:
                continue

            job_id = str(getattr(job, "id", getattr(job, "job_id", "")))
            if not job_id:
                continue

            active_job_ids.add(job_id)
            run_at_dt = getattr(job, "run_at", None)
            # fallback: parse run_at from payload for framework versions storing it there
            if not run_at_dt:
                raw_run_at = (payload or {}).get("run_at")
                if raw_run_at:
                    try:
                        run_at_dt = datetime.fromisoformat(str(raw_run_at))
                    except (ValueError, TypeError):
                        pass
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
                updated_at=time.time(),
            )
            await self.db_service.save_cron_snapshot(snapshot)
            synced_count += 1

        db_snapshots = await self.db_service.get_all_active_cron_snapshots()
        cleaned_count = 0
        for snapshot in db_snapshots:
            if snapshot.target_origin == current_umo and snapshot.job_id not in active_job_ids:
                await self.db_service.deactivate_cron_snapshot(snapshot.job_id)
                cleaned_count += 1

        if synced_count > 0 or cleaned_count > 0:
            logger.info(
                f"[Sys3/CronAgent] 智能体挂起前快照同步完成：新增/更新 {synced_count} 个，清理 {cleaned_count} 个。"
            )


__all__ = ["CronAgent"]
