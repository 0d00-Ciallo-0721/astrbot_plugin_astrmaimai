from __future__ import annotations

import asyncio

from astrbot.api import logger
from ...shared.helpers.plugin_helpers import safe_create_task


class ExpressionGovernanceRunner:
    def __init__(
        self,
        *,
        state_engine,
        pattern_service=None,
        reflector=None,
        auto_check_task=None,
        jargon_auto_check_task=None,
        review_dispatcher=None,
        interval_seconds: int = 60,
        config=None,
    ):
        self.state_engine = state_engine
        self.pattern_service = pattern_service
        self.reflector = reflector
        self.auto_check_task = auto_check_task
        self.jargon_auto_check_task = jargon_auto_check_task
        self.review_dispatcher = review_dispatcher
        self.config = config
        self.interval_seconds = max(int(interval_seconds or 60), 15)
        self._is_running = False
        self._task = None

    def refresh_config(self, config) -> None:
        self.config = config
        self.interval_seconds = max(
            int(getattr(getattr(config, "evolution", None), "review_runner_interval_sec", 60) or 60),
            15,
        )
        for component in (self.reflector, self.auto_check_task, self.jargon_auto_check_task):
            refresh = getattr(component, "refresh_config", None)
            if callable(refresh):
                refresh(config)
            elif component is not None and hasattr(component, "config"):
                component.config = config

    async def start(self):
        if self._is_running:
            return
        self._is_running = True
        self._task = safe_create_task(self._loop())

    async def stop(self):
        self._is_running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None

    def _resolve_pattern_service(self):
        if self.pattern_service:
            return self.pattern_service
        for owner in (self.reflector, self.auto_check_task):
            db = getattr(owner, "db", None)
            service = getattr(getattr(db, "memory_engine", None), "expression_pattern_service", None)
            if service:
                return service
        return None

    async def _governance_groups(self) -> list[str]:
        groups: list[str] = []
        service = self._resolve_pattern_service()
        if service and hasattr(service, "list_governance_groups"):
            groups.extend(await service.list_governance_groups())
        if self.reflector and hasattr(self.reflector, "pending_scope_ids"):
            groups.extend(await self.reflector.pending_scope_ids())
        if self.jargon_auto_check_task and hasattr(self.jargon_auto_check_task, "list_governance_groups"):
            groups.extend(await self.jargon_auto_check_task.list_governance_groups())
        values = [str(item or "GLOBAL").strip() or "GLOBAL" for item in groups]
        return list(dict.fromkeys(values))

    async def run_once(self):
        if not self.reflector and not self.auto_check_task and not self.jargon_auto_check_task:
            return
        for chat_id in await self._governance_groups():
            if self.reflector:
                await self.reflector.reflect_batch(chat_id)
                await self.reflector.auto_audit(chat_id)
            if self.auto_check_task:
                await self.auto_check_task.run_once(chat_id)
            if self.jargon_auto_check_task:
                await self.jargon_auto_check_task.run_once(chat_id)
        if self.review_dispatcher:
            await self.review_dispatcher.dispatch_pending()

    async def _loop(self):
        while self._is_running:
            try:
                await asyncio.sleep(self.interval_seconds)
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"[ExpressionGovernanceRunner] degraded: {exc}")


__all__ = ["ExpressionGovernanceRunner"]
