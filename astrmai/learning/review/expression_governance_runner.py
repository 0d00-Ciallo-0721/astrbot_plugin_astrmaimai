from __future__ import annotations

import asyncio
import inspect
import uuid

from astrbot.api import logger
from ...shared.helpers.plugin_helpers import safe_create_task
from ...infrastructure.runtime.background_task_budget import (
    BackgroundTaskBudget,
    BackgroundTaskQueueFull,
    BackgroundTaskQueueTimeout,
)
from ...infrastructure.runtime.background_task_ledger import BackgroundTaskLedger


class ExpressionGovernanceRunner:
    RETRY_POLL_INTERVAL_SECONDS = 300.0

    def __init__(
        self,
        *,
        state_engine,
        pattern_service=None,
        reflector=None,
        auto_check_task=None,
        jargon_auto_check_task=None,
        review_dispatcher=None,
        interval_seconds: int = 21600,
        config=None,
        background_task_budget=None,
        owner_registry=None,
    ):
        self.state_engine = state_engine
        self.pattern_service = pattern_service
        self.reflector = reflector
        self.auto_check_task = auto_check_task
        self.jargon_auto_check_task = jargon_auto_check_task
        self.review_dispatcher = review_dispatcher
        self.config = config
        self.background_task_budget = background_task_budget or BackgroundTaskBudget()
        self.owner_registry = owner_registry
        self.interval_seconds = max(int(interval_seconds or 60), 15)
        self._is_running = False
        self._task = None
        db_path = getattr(getattr(state_engine, "persistence", None), "db_path", None) or getattr(state_engine, "db_path", None)
        self._task_ledger = BackgroundTaskLedger(db_path) if db_path else None

    def refresh_config(self, config) -> None:
        self.config = config
        self.interval_seconds = max(
            int(getattr(getattr(config, "evolution", None), "review_runner_interval_sec", 21600) or 21600),
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
        if self._task_ledger is not None:
            try:
                recovered = await self._task_ledger.recover_expired_leases()
                if recovered:
                    logger.info(
                        "[ExpressionGovernanceRunner] recovered expired leases count=%s",
                        recovered,
                    )
            except Exception as exc:
                logger.warning(
                    "[ExpressionGovernanceRunner] lease recovery degraded: %s",
                    exc,
                )
        self._is_running = True
        self._task = safe_create_task(self._loop(), name="governance:runner")
        if self.owner_registry is not None:
            self.owner_registry.register(
                self._task,
                task_family="governance.scheduler",
                scope_id="GLOBAL",
                run_id=f"governance-{uuid.uuid4().hex}",
                owner="ExpressionGovernanceRunner",
                generation=getattr(self.owner_registry, "generation", None),
            )

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
            await self.run_scope_once(chat_id)
        if self.review_dispatcher:
            await self._run_scoped(
                self.review_dispatcher.dispatch_pending,
                task_name="governance.review_dispatch",
                scope_id="GLOBAL",
            )

    async def run_scope_once(self, chat_id: str, *, force: bool = False):
        """Run one scope through the same lease path used by the scheduler."""
        scope_id = str(chat_id or "GLOBAL").strip() or "GLOBAL"
        if self.reflector:
            await self._run_scoped(
                lambda: self.reflector.reflect_batch(scope_id),
                task_name="governance.reflect",
                scope_id=scope_id,
                force=force,
            )
            await self._run_scoped(
                lambda: self._invoke_component(self.reflector, "auto_audit", scope_id, force),
                task_name="governance.audit",
                scope_id=scope_id,
                force=force,
            )
        if self.auto_check_task:
            await self._run_scoped(
                lambda: self._invoke_component(self.auto_check_task, "run_once", scope_id, force),
                task_name="governance.expression_check",
                scope_id=scope_id,
                force=force,
            )
        if self.jargon_auto_check_task:
            await self._run_scoped(
                lambda: self._invoke_component(self.jargon_auto_check_task, "run_once", scope_id, force),
                task_name="governance.jargon_check",
                scope_id=scope_id,
                force=force,
            )

    @staticmethod
    async def _invoke_component(component, method_name: str, scope_id: str, force: bool):
        method = getattr(component, method_name)
        try:
            supports_force = "force" in inspect.signature(method).parameters
        except (TypeError, ValueError):
            supports_force = False
        if supports_force:
            return await method(scope_id, force=force)
        return await method(scope_id)

    async def _run_scoped(self, awaitable_factory, *, task_name: str, scope_id: str, force: bool = False):
        lease = None
        if self._task_ledger is not None:
            lease = await self._task_ledger.claim(
                task_family=str(task_name),
                scope_id=str(scope_id),
                input_fingerprint="",
                lease_seconds=max(300.0, float(self.interval_seconds)),
                # ``force`` only bypasses candidate-count gates inside the
                # components. The per-scope six-hour governance cooldown is a
                # hard business limit shared by scheduler and admin calls.
                min_interval_seconds=float(self.interval_seconds),
            )
            if lease is None:
                return None
        budget = self.background_task_budget
        if budget is None:
            try:
                result = await awaitable_factory()
                if lease is not None:
                    await self._task_ledger.finish(lease, status="succeeded")
                return result
            except asyncio.CancelledError:
                if lease is not None:
                    await asyncio.shield(
                        self._task_ledger.finish(
                            lease,
                            status="retry_wait",
                            error="cancelled",
                            retry_after_seconds=0.0,
                        )
                    )
                raise
            except (BackgroundTaskQueueFull, BackgroundTaskQueueTimeout) as exc:
                if lease is not None:
                    await self._task_ledger.finish(
                        lease,
                        status="retry_wait",
                        error=str(exc),
                        retry_after_seconds=self.RETRY_POLL_INTERVAL_SECONDS,
                    )
                logger.debug(
                    f"[ExpressionGovernanceRunner] {task_name} skipped for {scope_id}: {exc}"
                )
            except Exception as exc:
                if lease is not None:
                    await self._task_ledger.finish(lease, status="failed", error=str(exc))
                logger.warning(
                    f"[ExpressionGovernanceRunner] {task_name} failed for {scope_id}: {exc}"
                )
            return None
        try:
            result = await budget.run(
                awaitable_factory,
                task_name=task_name,
                scope_id=scope_id,
                defer_release_on_timeout=True,
            )
            if lease is not None:
                await self._task_ledger.finish(lease, status="succeeded")
            return result
        except asyncio.CancelledError:
            if lease is not None:
                await asyncio.shield(
                    self._task_ledger.finish(
                        lease,
                        status="retry_wait",
                        error="cancelled",
                        retry_after_seconds=0.0,
                    )
                )
            raise
        except (BackgroundTaskQueueFull, BackgroundTaskQueueTimeout) as exc:
            if lease is not None:
                await self._task_ledger.finish(
                    lease,
                    status="retry_wait",
                    error=str(exc),
                    retry_after_seconds=self.RETRY_POLL_INTERVAL_SECONDS,
                )
            logger.debug(
                f"[ExpressionGovernanceRunner] {task_name} skipped for {scope_id}: {exc}"
            )
            return None
        except Exception as exc:
            if lease is not None:
                await self._task_ledger.finish(lease, status="failed", error=str(exc))
            logger.warning(
                f"[ExpressionGovernanceRunner] {task_name} failed for {scope_id}: {exc}"
            )
            return None

    def _poll_interval_seconds(self) -> float:
        return min(float(self.interval_seconds), self.RETRY_POLL_INTERVAL_SECONDS)

    async def _loop(self):
        while self._is_running:
            try:
                await asyncio.sleep(self._poll_interval_seconds())
                await self.run_once()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.warning(f"[ExpressionGovernanceRunner] degraded: {exc}")


__all__ = ["ExpressionGovernanceRunner"]
