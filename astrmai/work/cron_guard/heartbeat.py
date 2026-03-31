# astrmai/sys3/cron_guard/heartbeat.py
"""
CronHeartbeatGuard — 定时任务健壮性守护进程 (降级版)

基于探针结果：由于当前 AstrBot 框架版本未暴露 CronJobManager 底层 API，
自动复活（自愈）机制已降级为纯数据库只读快照与清理机制。
用户的定时任务仍会双写到 SQLite 以防丢失记录，方便后续追溯。
本守护进程负责在开机和后台运行期间，定期清理已过期的一次性任务快照。
"""
import asyncio
import time
from astrbot.api import logger


class CronHeartbeatGuard:
    """定时任务快照守护进程 (降级安全模式)"""

    HEARTBEAT_INTERVAL = 3600  # 降级后无需高频检测，每小时清理一次过期快照即可

    def __init__(self, db_service, context):
        self.db_service = db_service
        self.context = context
        self._is_running = True

    # ────────────────────────────────────────────────────────
    # 公共接口
    # ────────────────────────────────────────────────────────

    async def reload_all_lost_jobs(self) -> int:
        """
        开机扫描快照 (降级版)。
        因无法获取 CronJobManager，放弃向 APScheduler 强行注入任务，仅清理已过期的历史快照。
        应在 AstrMaiPlugin.on_program_start() 中调用。
        """
        logger.info("[CronGuard] 🔍 开机扫描：当前运行在安全降级模式，正在清理过期历史快照...")
        await self._clean_expired_snapshots()
        return 0

    async def run_heartbeat(self):
        """
        后台心跳协程 (降级版)。
        定期清理过期的快照记录，防止数据库冗余。
        应通过 _fire_and_forget 在 on_program_start 中启动。
        """
        logger.info(f"[CronGuard] 💓 降级版快照守护已启动（清理间隔 {self.HEARTBEAT_INTERVAL}s）。")
        while self._is_running:
            try:
                await asyncio.sleep(self.HEARTBEAT_INTERVAL)
                await self._clean_expired_snapshots()
            except asyncio.CancelledError:
                logger.info("[CronGuard] 🛑 快照守护收到终止信号，安全退出。")
                raise
            except Exception as e:
                logger.error(f"[CronGuard] 快照清理检测异常（不影响系统）: {e}")

    def stop(self):
        """供 AstrMaiPlugin.terminate() 调用"""
        self._is_running = False

    # ────────────────────────────────────────────────────────
    # 内部实现
    # ────────────────────────────────────────────────────────

    async def _clean_expired_snapshots(self):
        """静默清理已过期的一次性任务快照"""
        try:
            snapshots = await self.db_service.get_all_active_cron_snapshots()
            if not snapshots:
                return

            now = time.time()
            cleaned = 0
            
            for snap in snapshots:
                # 仅清理明确已过期的一次性任务
                if snap.run_once and snap.run_at and snap.run_at < now:
                    await self.db_service.deactivate_cron_snapshot(snap.job_id)
                    cleaned += 1

            if cleaned > 0:
                logger.info(f"[CronGuard] 🧹 数据库保养：已自动清理 {cleaned} 个过期的定时任务快照。")
                
        except Exception as e:
            logger.debug(f"[CronGuard] 清理过期快照底层异常: {e}")