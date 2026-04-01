# astrmai/work/cron_guard/heartbeat.py
"""
CronHeartbeatGuard — 定时任务健壮性守护进程 (满血自愈版)

基于探针突破：我们成功寻址到 self.context.cron_manager，
现在可以正式启用工业级防掉线机制！
开机或后台运行期间，本守护进程会将 SQLite 快照与内存调度器进行比对，
如发现因崩溃或重启导致的内存任务遗失，将主动构造 CronJob 对象将其强行逆向注入。
"""
import asyncio
import time
import json
from datetime import datetime
from astrbot.api import logger

class CronHeartbeatGuard:
    """定时任务快照守护进程 (满血自愈版)"""

    HEARTBEAT_INTERVAL = 60  # 满血版恢复高频检测，每 60 秒轮询心跳

    def __init__(self, db_service, context):
        self.db_service = db_service
        self.context = context
        self._is_running = True

    # ────────────────────────────────────────────────────────
    # 公共接口
    # ────────────────────────────────────────────────────────

    async def reload_all_lost_jobs(self) -> int:
        """开机扫描与比对，满血复活遗失任务"""
        cron_mgr = getattr(self.context, "cron_manager", None)
        if not cron_mgr:
            logger.warning("[CronGuard] ⚠️ 未找到 CronJobManager，退回降级版清理模式。")
            await self._clean_expired_snapshots()
            return 0

        logger.info("[CronGuard] 🔍 满血开机扫描：开始比对 SQLite 快照与内存调度器...")
        try:
            snapshots = await self.db_service.get_all_active_cron_snapshots()
            if not snapshots:
                return 0

            # 获取框架内存中的现有 jobs
            active_jobs = await cron_mgr.list_jobs()
            active_job_ids = {str(getattr(j, 'id', getattr(j, 'job_id', j))) for j in active_jobs}

            revived = 0
            now = time.time()
            for snap in snapshots:
                # 1. 正常清理过期的一次性任务快照
                if snap.run_once and snap.run_at and snap.run_at < now:
                    await self.db_service.deactivate_cron_snapshot(snap.job_id)
                    continue
                
                # 2. 发现内存缺失，触发自愈注入
                if snap.job_id not in active_job_ids:
                    success = await self._revive_job(cron_mgr, snap)
                    if success:
                        revived += 1

            if revived > 0:
                logger.info(f"[CronGuard] 🔁 满血复活完成，共从快照中抢救了 {revived} 个遗失任务！")
            else:
                logger.info(f"[CronGuard] ✅ 所有计划任务均安全在线，无需抢救。")
            return revived
        except Exception as e:
            logger.error(f"[CronGuard] 开机扫描异常: {e}")
            return 0

    async def run_heartbeat(self):
        """后台心跳自愈协程"""
        logger.info(f"[CronGuard] 💓 满血版自愈守护已启动（心跳间隔 {self.HEARTBEAT_INTERVAL}s）。")
        while self._is_running:
            try:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                await self._heartbeat_tick()
            except asyncio.CancelledError:
                logger.info("[CronGuard] 🛑 守护收到终止信号，安全退出。")
                raise
            except Exception as e:
                logger.error(f"[CronGuard] 快照清理检测异常（不影响系统）: {e}")

    def stop(self):
        self._is_running = False

    # ────────────────────────────────────────────────────────
    # 内部实现
    # ────────────────────────────────────────────────────────

    async def _clean_expired_snapshots(self):
        """静默清理已过期的一次性任务快照 (降级兜底)"""
        try:
            snapshots = await self.db_service.get_all_active_cron_snapshots()
            if not snapshots:
                return

            now = time.time()
            cleaned = 0
            for snap in snapshots:
                if snap.run_once and snap.run_at and snap.run_at < now:
                    await self.db_service.deactivate_cron_snapshot(snap.job_id)
                    cleaned += 1

            if cleaned > 0:
                logger.info(f"[CronGuard] 🧹 数据库保养：已自动清理 {cleaned} 个过期的快照。")
        except Exception as e:
            logger.debug(f"[CronGuard] 清理过期快照底层异常: {e}")

    async def _heartbeat_tick(self):
        """单次心跳检测（运行中防护）"""
        cron_mgr = getattr(self.context, "cron_manager", None)
        if not cron_mgr:
            await self._clean_expired_snapshots()
            return

        try:
            snapshots = await self.db_service.get_all_active_cron_snapshots()
            if not snapshots: return
            
            active_jobs = await cron_mgr.list_jobs()
            active_job_ids = {str(getattr(j, 'id', getattr(j, 'job_id', j))) for j in active_jobs}
            
            now = time.time()
            need_revive = []
            for snap in snapshots:
                if snap.run_once and snap.run_at and snap.run_at < now:
                    await self.db_service.deactivate_cron_snapshot(snap.job_id)
                    continue
                if snap.job_id not in active_job_ids:
                    need_revive.append(snap)
                    
            for snap in need_revive:
                logger.warning(f"[CronGuard] 💔 运行时心跳发现任务 {snap.job_id} 意外消失，立即注入复活...")
                await self._revive_job(cron_mgr, snap)
        except Exception as e:
            logger.debug(f"[CronGuard] 心跳检查内部异常: {e}")

    async def _revive_job(self, cron_mgr, snap) -> bool:
        """核心抢救手段：构造 ORM 实体并逆向注入框架"""
        try:
            # 依赖探针发现的底层 API
            if hasattr(cron_mgr, 'add_job'):
                from astrbot.core.db.po import CronJob
                
                # 重建 CronJob ORM 对象
                job = CronJob(
                    id=snap.job_id,
                    name=snap.name,
                    cron_expression=snap.cron_expression,
                    run_at=datetime.fromtimestamp(snap.run_at) if snap.run_at else None,
                    run_once=snap.run_once,
                    payload=snap.payload
                )
                await cron_mgr.add_job(job)
                return True
            else:
                logger.warning(f"[CronGuard] 框架缺乏 add_job 方法，无法逆向复活任务。")
                return False
        except Exception as e:
            logger.error(f"[CronGuard] 逆向复活任务 {snap.job_id} 失败: {e}")
            return False