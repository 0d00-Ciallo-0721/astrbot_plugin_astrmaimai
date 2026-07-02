import asyncio
import time

from sqlmodel import select

from .orm_models import CronSnapshot


class CronPersistenceMixin:
    async def save_cron_snapshot(self, snapshot) -> None:
        def _sync():
            with self.get_session() as session:
                existing = session.get(CronSnapshot, snapshot.job_id)
                if existing:
                    existing.name = snapshot.name
                    existing.cron_expression = snapshot.cron_expression
                    existing.run_at = snapshot.run_at
                    existing.run_once = snapshot.run_once
                    existing.target_origin = snapshot.target_origin
                    existing.payload = snapshot.payload
                    existing.note = snapshot.note
                    existing.is_active = snapshot.is_active
                    existing.updated_at = time.time()
                    session.add(existing)
                else:
                    snapshot.updated_at = time.time()
                    session.add(snapshot)
                session.commit()

        async with self._db_lock:
            await asyncio.to_thread(_sync)

    async def get_all_active_cron_snapshots(self) -> list:
        def _sync():
            with self.get_session() as session:
                statement = select(CronSnapshot).where(CronSnapshot.is_active == True)
                results = session.exec(statement).all()
                return [CronSnapshot.model_validate(item.model_dump()) for item in results]

        return await asyncio.to_thread(_sync)

    async def deactivate_cron_snapshot(self, job_id: str) -> None:
        def _sync():
            with self.get_session() as session:
                snapshot = session.get(CronSnapshot, job_id)
                if snapshot:
                    snapshot.is_active = False
                    snapshot.updated_at = time.time()
                    session.add(snapshot)
                    session.commit()

        async with self._db_lock:
            await asyncio.to_thread(_sync)
